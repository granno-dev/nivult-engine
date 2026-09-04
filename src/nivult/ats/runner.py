"""Il runner del sistema ATS autonomo: scarica, arricchisce, classifica.

    python -m nivult.ats.runner                    # tutti i job pendenti
    python -m nivult.ats.runner --piattaforma greenhouse   # solo una piattaforma
    python -m nivult.ats.runner --stats            # solo statistiche

Il database è nivult_ats, separato dal motore principale. Nessuna scrittura
su nivult: i due sistemi si parlano solo tramite il risultato finale.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from nivult.ats.adapters import ADAPTERS, RATE_PER_SECOND
from nivult.ats.enrichment import cerca_wikidata

log = logging.getLogger("nivult.ats.runner")

# ── Rate-limiter per-piattaforma, solo per gli ATS a ENDPOINT CONDIVISO
# (greenhouse, lever, smartrecruiters, ashby, recruitee: tutti i tenant
# passano dallo stesso host API). Gli altri ATS hanno un host per-tenant
# ({slug}.dominio) — server diversi — e non vanno limitati: i thread li
# parallelizzano liberi. Cosi' si puo' alzare la concorrenza senza rischiare
# il ban sui pochi condivisi. Il gate serializza al ritmo di RATE_PER_SECOND.
_rate_lock = threading.Lock()
_rate_next: dict[str, float] = {}


def _rate_gate(platform_id: str) -> None:
    rate = RATE_PER_SECOND.get(platform_id)
    if not rate:
        return
    intervallo = 1.0 / rate
    with _rate_lock:
        ora = time.monotonic()
        prossimo = _rate_next.get(platform_id, 0.0)
        attesa = prossimo - ora if prossimo > ora else 0.0
        _rate_next[platform_id] = (prossimo if prossimo > ora else ora) + intervallo
    if attesa > 0:
        time.sleep(attesa)

ATS_DSN = os.environ.get(
    "ATS_DATABASE_URL",
    "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")


def setup(dsn: str) -> None:
    """Applica lo schema (idempotente) e registra le piattaforme."""
    import pathlib
    schema = pathlib.Path(__file__).parent / "schema.sql"
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(schema.read_text())
            cur.execute("""
            INSERT INTO ats_platforms (id, name, api_type, notes) VALUES
              ('greenhouse',     'Greenhouse',     'json', 'API pubblica, link diretto'),
              ('smartrecruiters','SmartRecruiters','json', 'API pubblica, filtro paese'),
              ('lever',          'Lever',          'json', 'API pubblica, link diretto'),
              ('recruitee',      'Recruitee',      'json', 'API pubblica, diffuso EU'),
              ('ashby',          'Ashby',          'json', 'API pubblica, startup EU')
            ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, notes = EXCLUDED.notes
            """)
        conn.commit()


def semina_aziende(dsn: str, dsn_produzione: str) -> int:
    """Estrae le aziende dai domini degli URL nel database di produzione
    (SOLA LETTURA) e le registra nel database ATS.

    Gli URL delle offerte contengono lo slug dell'azienda sulla piattaforma:
    https://job-boards.greenhouse.io/{slug}/jobs/...
    https://jobs.smartrecruiters.com/{slug}/...
    https://jobs.lever.co/{slug}/...
    https://{slug}.recruitee.com/o/...
    https://jobs.ashbyhq.com/{slug}/...
    """
    PATTERN = {
        "greenhouse": r"job-boards\.(?:eu\.)?greenhouse\.io/([^/]+)/",
        "smartrecruiters": r"jobs\.smartrecruiters\.com/([^/]+)/",
        "lever": r"jobs\.lever\.co/([^/]+)/",
        "recruitee": r"https://([^/]+)\.recruitee\.com/",
        "ashby": r"jobs\.ashbyhq\.com/([^/.]+)",
    }

    estratti: list[tuple[str, str]] = []   # (platform_id, slug)
    with psycopg.connect(dsn_produzione) as prod:
        with prod.cursor() as cur:
            for platform_id, pattern in PATTERN.items():
                cur.execute(
                    f"SELECT DISTINCT substring(url from '{pattern}') "
                    f"FROM jobs WHERE url ~ '{pattern}' AND status = 'active'")
                for (slug,) in cur.fetchall():
                    if slug and "/" not in slug and len(slug) > 1:
                        estratti.append((platform_id, slug))

    with psycopg.connect(dsn) as ats:
        with ats.cursor() as cur:
            for platform_id, slug in estratti:
                cur.execute(
                    "INSERT INTO ats_companies (platform_id, slug, discovered_from) "
                    "VALUES (%s, %s, 'existing_db') "
                    "ON CONFLICT (platform_id, slug) DO NOTHING",
                    (platform_id, slug))
        ats.commit()
    return len(estratti)


def scrape(dsn: str, piattaforma: str | None = None,
           thread: int = 10, limite: int | None = None,
           solo_attivi: bool = False) -> dict[str, int]:
    """Scarica le offerte di tutte le aziende attive (o di una piattaforma).

    Il fetch delle aziende va in parallelo: quasi tutto il tempo di uno
    scrape e' attesa di rete, e mentre si aspetta una azienda se ne
    interrogano altre. Le scritture nel database restano serializzate
    nel thread principale (una sola connessione), che e' sicuro e
    comunque velocissimo rispetto alla rete.

    Con `limite` scarica solo le prime N aziende per priorita': serve al
    demone che gira in continuo a piccoli lotti invece di un giro unico
    da ore. La priorita' e' l'ordine qui sotto.
    """
    stats = {"aziende": 0, "offerte": 0, "nuove": 0, "aggiornate": 0,
             "fallite": 0}
    with psycopg.connect(dsn) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            sql = ("SELECT ac.slug, ac.platform_id, ac.company_name, "
                   "       ac.wd_server, ac.wd_instance, ac.pub_key "
                   "FROM ats_companies ac "
                   "JOIN ats_platforms ap ON ap.id = ac.platform_id "
                   "WHERE ac.is_active AND ap.is_active")
            params: list = []
            if piattaforma:
                sql += " AND ac.platform_id = %s"
                params.append(piattaforma)
            if solo_attivi:
                # corsia veloce: solo i tenant che HANNO offerte, rivisitati
                # di continuo dal piu' stantio — cosi' le posizioni appena
                # pubblicate compaiono in minuti, non in ore.
                sql += " AND ac.job_count > 0"
            # Priorita' di visita, in quattro scaglioni:
            #  1. le MAI viste (last_fetch NULL): il tesoro non ancora
            #     scavato — le aziende appena scoperte;
            #  2. lo SPAZZINO del codone: chiunque non sia visto da oltre
            #     SOGLIA_STANTIO, attivo o vuoto. Senza questo scaglione i
            #     118mila tenant vuoti restavano affamati dietro i 30mila
            #     attivi, e le posizioni aperte su un tenant prima vuoto si
            #     perdevano per giorni. Cosi' l'intero codone e' rivisitato
            #     entro la soglia: non si perde nulla.
            #  3. tra i visti di recente, prima i vivi (job_count>0) e piu'
            #     stantii: la capacita' avanzata li tiene extra-freschi;
            #  4. poi i vuoti recenti, che rendono meno.
            sql += (" ORDER BY (ac.last_fetch_at IS NULL) DESC, "
                    "(ac.last_fetch_at < now() - interval '12 hours') DESC, "
                    "(ac.job_count > 0) DESC, "
                    "ac.last_fetch_at ASC NULLS FIRST, "
                    "ac.platform_id, ac.slug")
            if limite:
                sql += " LIMIT %s"
                params.append(limite)
            cur.execute(sql, params)
            aziende = cur.fetchall()

        def _fetch(az):
            """Solo rete: nessun tocco al DB, cosi' gira in parallelo."""
            adapter_cls = ADAPTERS.get(az["platform_id"])
            if not adapter_cls:
                return az, None
            _rate_gate(az["platform_id"])   # gate solo per gli endpoint condivisi
            try:
                with adapter_cls() as adapter:
                    if az["platform_id"] == "workday":
                        return az, adapter.jobs(
                            az["slug"], az["wd_server"], az["wd_instance"])
                    if az["platform_id"] == "inrecruiting":
                        return az, adapter.jobs(az["slug"], az["pub_key"])
                    return az, adapter.jobs(az["slug"])
            except Exception as exc:  # noqa: BLE001
                log.warning("%s/%s: fetch fallita: %s",
                            az["platform_id"], az["slug"], exc)
                return az, None

        with ThreadPoolExecutor(max_workers=thread) as pool:
            futuri = [pool.submit(_fetch, az) for az in aziende]
            for fut in as_completed(futuri):
                az, jobs = fut.result()
                if jobs is None:
                    # Fetch fallita (rete, slug morto, piattaforma senza
                    # adapter). Va segnata comunque come tentata: senza
                    # questo, un'azienda che fallisce resta last_fetch NULL,
                    # torna in testa alla coda a ogni lotto e blocca per
                    # sempre le altre mai-viste dietro di lei — era la causa
                    # dei lotti «0 aziende» col tesoro fermo. Segnandola, va
                    # in fondo alla coda e si riprova solo molto piu' tardi.
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE ats_companies SET last_fetch_at = now() "
                            "WHERE platform_id = %s AND slug = %s",
                            (az["platform_id"], az["slug"]))
                    conn.commit()
                    stats["fallite"] = stats.get("fallite", 0) + 1
                    continue
                stats["aziende"] += 1

                for j in jobs:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO ats_jobs (platform_id, slug, external_id, title,
                              url, location, country, city, posted_at, department, raw)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (platform_id, external_id) DO UPDATE SET
                              title = EXCLUDED.title, url = EXCLUDED.url,
                              location = EXCLUDED.location,
                              country = COALESCE(EXCLUDED.country, ats_jobs.country),
                              city = EXCLUDED.city, posted_at = EXCLUDED.posted_at,
                              department = EXCLUDED.department, raw = EXCLUDED.raw,
                              expired_at = NULL,   -- rivista = di nuovo viva
                              fetched_at = now()
                            RETURNING (xmax = 0) AS is_new
                        """, (j.platform_id, j.slug, j.external_id, j.title, j.url,
                              j.location, j.country, j.city, j.posted_at,
                              j.department, psycopg.types.json.Json(j.raw)))
                        r = cur.fetchone()
                        if r and r[0]:
                            stats["nuove"] += 1
                        else:
                            stats["aggiornate"] += 1
                    stats["offerte"] += 1

                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ats_companies SET last_fetch_at = now(), "
                        "job_count = %s WHERE platform_id = %s AND slug = %s",
                        (len(jobs), az["platform_id"], az["slug"]))
                conn.commit()
                if len(jobs):
                    log.info("  %s/%s: %d offerte",
                             az["platform_id"], az["slug"], len(jobs))

    return stats


def arricchisci(dsn: str, limite: int = 50) -> int:
    """Arricchisce le aziende che non hanno ancora dati Wikidata.

    Estrae i nomi azienda distinti dalle offerte, li cerca su Wikidata,
    mette in cache in organizations.
    """
    arricchite = 0
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # Le aziende tracciate che non sono ancora in organizations.
            # Prima il nome leggibile, poi lo slug come ripiego.
            cur.execute("""
                SELECT COALESCE(ac.company_name, ac.slug) AS nome
                  FROM ats_companies ac
                 WHERE ac.is_active
                   AND COALESCE(ac.company_name, ac.slug) NOT IN (SELECT name FROM organizations)
                 ORDER BY ac.last_fetch_at DESC NULLS LAST
                 LIMIT %s
            """, (limite,))
            nomi = [r[0] for r in cur.fetchall() if r[0]]

        for nome in nomi:
            dati = cerca_wikidata(nome)
            if dati:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO organizations (name, wikidata_id, employees,
                          industry, logo_url, website, country)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (name) DO UPDATE SET
                          wikidata_id = EXCLUDED.wikidata_id,
                          employees = EXCLUDED.employees,
                          industry = EXCLUDED.industry,
                          logo_url = EXCLUDED.logo_url,
                          website = EXCLUDED.website,
                          country = EXCLUDED.country,
                          enriched_at = now()
                    """, (nome, dati.get("wikidata_id"), dati.get("employees"),
                          dati.get("industry"), dati.get("logo_url"),
                          dati.get("website"), dati.get("country")))
                conn.commit()
                arricchite += 1
                log.info("  %s → %s (%s dipendenti)",
                         nome, dati.get("wikidata_id"), dati.get("employees"))
            else:
                log.info("  %s → non trovata su Wikidata", nome)

    return arricchite


def stats(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT platform_id, count(*) FROM ats_jobs GROUP BY 1 ORDER BY 2 DESC")
            print("\nOfferte per piattaforma:")
            for platform, n in cur.fetchall():
                print(f"  {platform:<18} {n:>6}")
            cur.execute("SELECT count(*) FROM ats_companies")
            print(f"\nAziende tracciate: {cur.fetchone()[0]}")
            cur.execute("SELECT count(*) FROM organizations")
            print(f"Aziende arricchite: {cur.fetchone()[0]}")
            cur.execute("SELECT count(*) FROM job_classifications")
            print(f"Offerte classificate: {cur.fetchone()[0]}")
            cur.execute("""
                SELECT country, count(*) FROM ats_jobs
                WHERE country IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 10""")
            print("\nOfferte per paese:")
            for country, n in cur.fetchall():
                print(f"  {country:<6} {n:>6}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.ats.runner", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--piattaforma", help="solo questa piattaforma (greenhouse, …)")
    ap.add_argument("--stats", action="store_true", help="solo statistiche")
    ap.add_argument("--semina", action="store_true",
                    help="estrai aziende dal database di produzione (sola lettura)")
    ap.add_argument("--arricchisci", type=int, default=0, metavar="N",
                    help="arricchisci N aziende da Wikidata")
    ap.add_argument("--thread", type=int, default=10,
                    help="aziende interrogate in parallelo (default 10)")
    ap.add_argument("--solo-attivi", action="store_true",
                    help="corsia veloce: rivisita solo i tenant con offerte")
    ap.add_argument("--continuo", action="store_true",
                    help="cicla in continuo (per la corsia veloce)")
    ap.add_argument("--limite", type=int, default=None,
                    help="scarica solo le prime N aziende per priorita' "
                         "(per il demone a lotti; vuoto = tutte)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s",
                        stream=sys.stderr)
    dsn = ATS_DSN
    setup(dsn)
    print(f"database ATS: {dsn}")

    if args.semina:
        dsn_prod = os.environ.get("DATABASE_URL",
                                  "postgresql://giusepperanno@127.0.0.1:5432/nivult_dev")
        n = semina_aziende(dsn, dsn_prod)
        print(f"seminate {n} aziende dal database di produzione")

    if not args.stats:
        if args.continuo:
            import time as _t
            while True:
                s = scrape(dsn, args.piattaforma, thread=args.thread,
                           limite=args.limite, solo_attivi=args.solo_attivi)
                print("scrape:", s, flush=True)
                _t.sleep(5)
        s = scrape(dsn, args.piattaforma, thread=args.thread,
                   limite=args.limite, solo_attivi=args.solo_attivi)
        print(f"\nscrape: {s['aziende']} aziende, {s['offerte']} offerte "
              f"({s['nuove']} nuove, {s['aggiornate']} aggiornate, "
              f"{s.get('fallite', 0)} fallite)")

    if args.arricchisci:
        n = arricchisci(dsn, args.arricchisci)
        print(f"\narricchite {n} aziende da Wikidata")

    stats(dsn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
