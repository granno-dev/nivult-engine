"""Il mantenimento del database ATS: expira, normalizza, deduplica.

    python -m nivult.ats.mantenimento --expira
    python -m nivult.ats.mantenimento --normalizza --limite 5000
    python -m nivult.ats.mantenimento --dedup
    python -m nivult.ats.mantenimento --stats

Tre lavori che chiudono i gap di qualità verso Fantastic Jobs:

1. EXPIRA — un'offerta che lo scraper non rivede per GIORNI_SCADENZA
   giorni è scaduta: l'azienda l'ha ritirata. La si marca, non si
   cancella (lo storico serve).

2. NORMALIZZA — il JSON grezzo (raw) che conserviamo da ogni
   piattaforma contiene stipendio e contratto in forme diverse:
   qui si estraggono nei campi comuni. Per piattaforma, il suo
   formato.

3. DEDUP — la stessa posizione esiste spesso su due ATS (vetrina
   Radancy che rimanda a Workday). Chiave: titolo+azienda+luogo
   normalizzati in minuscolo; la prima che resta viva vince, le
   altre restano ma marcate come duplicati.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import unicodedata

import psycopg

log = logging.getLogger("nivult.ats.mantenimento")

ATS_DSN = os.environ.get(
    "ATS_DATABASE_URL",
    "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")

# Quanti giorni senza essere rivista prima di considerarla scaduta.
# Gli scraper girano ogni notte: 21 giorni = tre settimane di
# assenze consecutive.
# Con lo sweep a 12h (ATS) e i servizi pubblici a 24h, un'offerta ancora
# in bacheca ha sempre fetched_at fresco: 3 giorni bastano con margine
# 3-6x. Era 21: un'offerta rimossa restava «attiva» tre settimane.
GIORNI_SCADENZA = 3


# ── 1. EXPIRA ─────────────────────────────────────────────────────

def expira(dsn: str, giorni: int = GIORNI_SCADENZA) -> int:
    """Marca scadute le offerte non più viste dallo scraper.

    Due regole:
    1. fetched_at vecchio — lo scraper non la rivede: ritirata.
    2. posted_at molto vecchio — l'ATS continua a elencarla ma è uno
       zombie (listing del 2013 ancora sulla pagina): la freschezza
       la decide la data di pubblicazione, non la presenza.
    """
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE ats_jobs
                   SET expired_at = now()
                 WHERE expired_at IS NULL
                   AND (
                        fetched_at < now() - make_interval(days => %s)
                     OR posted_at < now() - make_interval(days => 90)
                   )
                RETURNING id
            """, (giorni,))
            n = cur.rowcount
        conn.commit()
    log.info("expira: %d offerte scadute (non viste o pubblicate da troppo)", n)
    return n


# ── 2. NORMALIZZA ─────────────────────────────────────────────────

def _estrai_stipendio(raw: dict, piattaforma: str) -> tuple:
    """(min, max, valuta, contratto) dal raw, per piattaforma."""
    if not isinstance(raw, dict):
        return None, None, None, None

    def num(x):
        try:
            return float(str(x).replace(",", ".").replace(" ", ""))
        except (ValueError, TypeError):
            return None

    if piattaforma == "workday":
        # workday: raw ha postedOn, locationPath, titleText...
        # lo stipendio non c'è quasi mai nell'elenco
        return None, None, None, raw.get("timeType") or raw.get("postedOn")
    if piattaforma == "smartrecruiters":
        # smartrecruiters: compensation, typeOfEmployment
        comp = (raw or {}).get("customField") or []
        comp_d = {}
        for c in comp if isinstance(comp, list) else []:
            if isinstance(c, dict) and c.get("fieldId") in (
                    "compensation", "typeOfEmployment", "department"):
                comp_d[c["fieldId"]] = c.get("valueLabel")
        return None, None, None, comp_d.get("typeOfEmployment")
    if piattaforma == "workable":
        # workable: salary_min/salary_max/salary_currency/employment_type
        return (num(raw.get("salary_min")), num(raw.get("salary_max")),
                raw.get("salary_currency"), raw.get("employment_type"))
    if piattaforma == "bamboohr":
        return None, None, None, raw.get("employmentStatusLabel")
    if piattaforma == "softgarden":
        return None, None, None, None
    if piattaforma == "varbi":
        return None, None, None, None
    if piattaforma == "oracle":
        return None, None, None, raw.get("ContractType") or raw.get("JobSchedule")
    if piattaforma == "phenom":
        return None, None, None, None
    if piattaforma == "join":
        # join: salaryAmountFrom/To con salaryFrequency
        return (num(raw.get("salaryAmountFrom")), num(raw.get("salaryAmountTo")),
                "EUR", raw.get("employmentType", {}).get("name")
                if isinstance(raw.get("employmentType"), dict) else None)
    if piattaforma == "radancy":
        return None, None, None, None
    if piattaforma == "adp":
        return None, None, None, (raw.get("workLevelCode") or {}).get("shortName")
    # ripiego generico: campi con nomi comuni
    return (num(raw.get("salary_min") or raw.get("salaryMin")),
            num(raw.get("salary_max") or raw.get("salaryMax")),
            raw.get("salary_currency") or raw.get("salaryCurrency"),
            raw.get("employment_type") or raw.get("employmentType"))


def normalizza(dsn: str, limite: int = 5000) -> int:
    """Estrae stipendio e contratto dal raw nelle colonne comuni."""
    fatti = 0
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, platform_id, raw FROM ats_jobs
                 WHERE normalized_at IS NULL AND raw IS NOT NULL
                   AND raw::text != '{}'
                 ORDER BY platform_id
                 LIMIT %s
            """, (limite,))
            righe = cur.fetchall()
        for jid, pid, raw in righe:
            smin, smax, valuta, contratto = _estrai_stipendio(raw, pid)
            # I raw di 62 piattaforme portano forme arbitrarie: quando un
            # campo che dovrebbe essere piatto arriva come dict, meglio
            # NULL a database che il giro morto. Successo il 2026-08-31:
            # un contratto arrivato dict ha ucciso la normalizza con
            # «cannot adapt type dict», e ogni notte moriva sulla stessa
            # riga avvelenata senza committare niente.
            smin = smin if isinstance(smin, (int, float)) else None
            smax = smax if isinstance(smax, (int, float)) else None
            valuta = (valuta.strip()[:8] or None) if isinstance(valuta, str) else None
            contratto = (contratto.strip()[:80] or None) if isinstance(contratto, str) else None
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE ats_jobs SET salary_min = %s, salary_max = %s,
                      salary_currency = %s, employment_type = %s,
                      normalized_at = now()
                    WHERE id = %s
                """, (smin, smax, valuta, contratto, jid))
            fatti += 1
        conn.commit()
    log.info("normalizza: %d offerte elaborate", fatti)
    return fatti



# ── 2b. ARRICCHISCI (backfill) ────────────────────────────────────

def _paese_dal_raw(raw: dict, piattaforma: str):
    """Il paese sepolto nel raw, per piattaforma."""
    if not isinstance(raw, dict):
        return None
    if piattaforma == "lever":
        c = raw.get("country")
        return c.strip().upper() if isinstance(c, str) and len(c) == 2 else None
    if piattaforma == "softgarden":
        # jobLocation.address.addressCountry = "Deutschland"
        jl = (raw.get("item") or raw).get("jobLocation") or raw.get("jobLocation")
        if isinstance(jl, dict):
            addr = jl.get("address") or {}
            paese = addr.get("addressCountry")
            if paese:
                return _iso_da_nome(paese)
    if piattaforma == "oracle":
        p = raw.get("PrimaryLocation")
        if isinstance(p, str) and "," not in p:
            return _iso_da_nome(p)
    if piattaforma == "cornerstone":
        locs = raw.get("locations") or []
        if locs and isinstance(locs[0], dict):
            c = locs[0].get("country")
            if c and len(c) == 2:
                return c.upper()
            if c:
                return _iso_da_nome(c)
    if piattaforma == "greenhouse":
        loc = raw.get("location") or {}
        nome = loc.get("name") or ""
        # "Austin, TX; Toronto, Canada" → prova ogni segmento
        for pezzo in reversed(nome.replace(";", ",").split(",")):
            pezzo = pezzo.strip()
            if pezzo:
                iso = _iso_da_nome(pezzo)
                if iso:
                    return iso
    return None


_NOMI_PAESI = {
    "ITALY": "IT", "FRANCE": "FR", "GERMANY": "DE", "DEUTSCHLAND": "DE",
    "SPAIN": "ES", "ESPANA": "ES", "UNITED KINGDOM": "GB", "NETHERLANDS": "NL",
    "BELGIUM": "BE", "SWEDEN": "SE", "SWITZERLAND": "CH", "AUSTRIA": "AT",
    "POLAND": "PL", "PORTUGAL": "PT", "DENMARK": "DK", "IRELAND": "IE",
    "NORWAY": "NO", "FINLAND": "FI", "UNITED STATES": "US", "USA": "US",
    "CANADA": "CA", "CHINA": "CN", "INDIA": "IN", "AUSTRALIA": "AU",
    "JAPAN": "JP", "BRAZIL": "BR", "MEXICO": "MX", "SINGAPORE": "SG",
}


def _iso_da_nome(nome: str) -> str | None:
    n = nome.strip().upper()
    if len(n) == 2 and n.isalpha():
        return n
    return _NOMI_PAESI.get(n)


def _data_dal_raw(raw: dict, piattaforma: str):
    """La data di pubblicazione sepolta nel raw."""
    if not isinstance(raw, dict):
        return None
    from datetime import datetime, timezone
    if piattaforma == "cornerstone":
        d = raw.get("postingEffectiveDate")
        if d and d != "-":
            for fmt in ("%m/%d/%Y", "%d.%m.%Y"):
                try:
                    return datetime.strptime(d, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
    if piattaforma == "jibe":
        icims = ((raw.get("meta_data") or {}).get("icims") or {})
        d = ((icims.get("primary_posted_site_object") or {}).get("datePosted"))
        if d:
            try:
                return datetime.fromisoformat(d.replace("Z", "+00:00"))
            except ValueError:
                pass
    return None


def arricchisci(dsn: str, limite: int = 100000) -> dict:
    """Backfill: paese e data dal raw, per le piattaforme che li hanno."""
    stats = {"paesi": 0, "date": 0, "visti": 0}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, platform_id, raw FROM ats_jobs
                 WHERE (country IS NULL OR posted_at IS NULL)
                   AND raw IS NOT NULL AND raw::text != '{}'
                   AND platform_id IN ('lever', 'softgarden', 'oracle',
                        'cornerstone', 'greenhouse', 'jibe')
                 LIMIT %s
            """, (limite,))
            righe = cur.fetchall()
        for jid, pid, raw in righe:
            if not isinstance(raw, dict):
                continue
            stats["visti"] += 1
            paese = _paese_dal_raw(raw, pid)
            data = _data_dal_raw(raw, pid)
            if paese or data:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE ats_jobs
                           SET country = COALESCE(country, %s),
                               posted_at = COALESCE(posted_at, %s)
                         WHERE id = %s
                    """, (paese, data, jid))
                stats["paesi"] += 1 if paese else 0
                stats["date"] += 1 if data else 0
        conn.commit()
    log.info("arricchisci: %s", stats)
    return stats


# ── 3. DEDUP ──────────────────────────────────────────────────────

def _chiave(titolo: str, slug: str, citta: str | None) -> str | None:
    """La chiave di dedup: titolo+azienda+luogo, appiattita."""
    def pulisci(s):
        s = unicodedata.normalize("NFKD", s or "")
        s = s.lower()
        s = re.sub(r"[^a-z0-9 ]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s
    t = pulisci(titolo)
    a = pulisci(slug)
    if not t or not a:
        return None
    l = pulisci(citta)[:24] if citta else ""
    return f"{t[:60]}|{a}|{l}"


def dedup(dsn: str, limite: int = 50000) -> int:
    """Calcola la duplicate_key e marca i duplicati."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, slug, city FROM ats_jobs
                 WHERE duplicate_key IS NULL
                 ORDER BY id
                 LIMIT %s
            """, (limite,))
            righe = cur.fetchall()

        def _scrivi(lotto: list) -> None:
            # in ordine di id e a lotti corti: i demoni toccano le stesse
            # righe, e 50k UPDATE sparsi in un'unica transazione erano un
            # calamita-deadlock (FALLITO in manutenzione). Un tentativo
            # di riserva per gli incroci residui.
            for _ in range(2):
                try:
                    with conn.cursor() as cur:
                        cur.executemany(
                            "UPDATE ats_jobs SET duplicate_key = %s "
                            "WHERE id = %s", lotto)
                    return
                except psycopg.errors.DeadlockDetected:
                    pass
            raise RuntimeError("dedup: lotto bloccato due volte")

        lotto: list = []
        for jid, titolo, slug, citta in righe:
            lotto.append((_chiave(titolo, slug, citta), jid))
            if len(lotto) >= 300:
                _scrivi(lotto)
                lotto = []
        if lotto:
            _scrivi(lotto)
    # statistiche: quante chiavi condivise
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*) FROM (
                    SELECT duplicate_key FROM ats_jobs
                     WHERE expired_at IS NULL AND duplicate_key IS NOT NULL
                     GROUP BY duplicate_key HAVING count(*) > 1) s
            """)
            n = cur.fetchone()[0]
    log.info("dedup: %d chiavi con più offerte vive", n)
    return n


# ── statistiche ────────────────────────────────────────────────────

def stats(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ats_jobs")
            tot = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM ats_jobs WHERE expired_at IS NOT NULL")
            scad = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM ats_jobs WHERE salary_min IS NOT NULL")
            stip = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM ats_jobs WHERE employment_type IS NOT NULL")
            contr = cur.fetchone()[0]
            cur.execute("""
                SELECT count(*) FROM (
                    SELECT duplicate_key FROM ats_jobs
                     WHERE expired_at IS NULL AND duplicate_key IS NOT NULL
                     GROUP BY duplicate_key HAVING count(*) > 1) s
            """)
            dup = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL")
            vive = cur.fetchone()[0]
    print(f"\nofferte totali:       {tot}")
    print(f"vive (non scadute):   {vive}")
    print(f"scadute:              {scad}")
    print(f"con stipendio:        {stip}")
    print(f"con tipo contratto:   {contr}")
    print(f"chiavi duplicate:     {dup}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-8s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.mantenimento",
                                 description=__doc__)
    ap.add_argument("--expira", action="store_true")
    ap.add_argument("--normalizza", action="store_true")
    ap.add_argument("--dedup", action="store_true")
    ap.add_argument("--arricchisci", action="store_true",
                    help="backfill paese/data dal raw (lever, softgarden, oracle…)")
    ap.add_argument("--limite", type=int, default=5000)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)

    if args.expira:
        expira(ATS_DSN)
    if args.normalizza:
        normalizza(ATS_DSN, args.limite)
    if args.dedup:
        dedup(ATS_DSN, args.limite * 10)
    if args.arricchisci:
        arricchisci(ATS_DSN, args.limite)
    if args.stats:
        stats(ATS_DSN)
    if not (args.expira or args.normalizza or args.dedup
            or args.arricchisci or args.stats):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
