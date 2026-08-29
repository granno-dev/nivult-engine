"""Il ciclo di ingestione: itera sui CLUSTER, non sugli utenti.

Iterando sugli utenti, diecimila utenti farebbero diecimila ricerche al giorno
sullo stesso identico insieme di offerte. Iterando sui cluster la parte costosa
si paga una volta per cluster. Vedi CLAUDE.md.

    python -m nivult.ingestion.runner --all
    python -m nivult.ingestion.runner --cluster <uuid> --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Json as PsycopgJson

from nivult.config import database_url, load_dotenv, safe_dsn
from nivult.ingestion import store
from nivult.ingestion.sources.arbetsformedlingen import ArbetsformedlingenClient
from nivult.ingestion.sources.fantastic import FantasticClient
from nivult.ingestion.sources.france_travail import FranceTravailClient

log = logging.getLogger("nivult.ingestion.runner")

CLIENTS = (FantasticClient, FranceTravailClient, ArbetsformedlingenClient)

# Fonti che sanno filtrare per tassonomia: non serve un termine di ricerca,
# si chiede direttamente la famiglia professionale.
TAXONOMY_SOURCES = frozenset({'fantastic'})

# Backfill: quanto storico prende un cluster mai scaricato.
#
# Due settimane, non un mese: un'offerta di tre settimane fa è spesso già
# chiusa, e un primo digest pieno di annunci morti è il peggior inizio
# possibile. Oltre al costo, è una scelta di qualità.
BACKFILL_WINDOW = timedelta(days=14)


@dataclass(slots=True)
class Cluster:
    id: str
    family: str
    country: str
    in_backfill: bool = False
    # Iscritti attivi e non in pausa: decide se le fonti A PAGAMENTO
    # vengono chiamate. Le gratuite girano comunque.
    iscritti: int = 0
    # Cursore e termine di ricerca per fonte: le fonti di un cluster procedono
    # ognuna per conto suo, e non devono nulla l'una all'altra.
    cursors: dict[str, datetime] = field(default_factory=dict)
    queries: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.family} × {self.country}"


def due_clusters(cur, cluster_id: str | None) -> list[Cluster]:
    # Il conteggio degli iscritti viaggia con il cluster: e' cio' che decide
    # se le fonti a pagamento si chiamano (vedi ingest_cluster).
    conteggio = ("(SELECT count(*) FROM user_clusters uc "
                 "  JOIN users u ON u.id = uc.user_id "
                 " WHERE uc.cluster_id = clusters.id AND NOT uc.is_paused "
                 "   AND u.status = 'active')")
    if cluster_id:
        cur.execute(
            "SELECT id, family, country, "
            f"      backfill_completed_at IS NULL, {conteggio} "
            "FROM clusters WHERE id = %s",
            (cluster_id,))
    else:
        # NULLS FIRST: chi non è mai stato scaricato ha la precedenza.
        cur.execute(
            "SELECT id, family, country, "
            f"      backfill_completed_at IS NULL, {conteggio} "
            "FROM clusters "
            "WHERE status = 'active' ORDER BY last_fetched_at NULLS FIRST")
    clusters = [Cluster(str(r[0]), r[1], r[2], r[3], iscritti=r[4])
                for r in cur.fetchall()]
    if clusters:
        cur.execute(
            "SELECT cluster_id, source, query FROM cluster_source_queries "
            "WHERE cluster_id = ANY(%s)", ([c.id for c in clusters],))
        per_id: dict[str, dict[str, str]] = {}
        for cid, src, q in cur.fetchall():
            per_id.setdefault(str(cid), {})[src] = q
        for c in clusters:
            c.queries = per_id.get(c.id, {})
        # Un cursore per coppia cluster-fonte, non per cluster: quando era uno
        # solo, la fonte che vedeva le offerte più recenti trascinava avanti
        # anche le altre, che da lì in poi chiedevano a una data mai raggiunta
        # — offerte perse in silenzio, con il run che risultava success.
        cur.execute(
            "SELECT cluster_id, source, last_seen_posted_at "
            "FROM cluster_source_cursors WHERE cluster_id = ANY(%s)",
            ([c.id for c in clusters],))
        cursors: dict[str, dict[str, datetime]] = {}
        for cid, src, ts in cur.fetchall():
            cursors.setdefault(str(cid), {})[src] = ts
        for c in clusters:
            c.cursors = cursors.get(c.id, {})
    return clusters


def riserva(cur, *, cluster_id: str, source: str, in_backfill: bool,
            credits: int) -> tuple[bool, str]:
    """Riserva su entrambi i budget. Ritorna (concesso, motivo del rifiuto).

    DUE budget, non uno: quello del cluster protegge dalla query impazzita,
    quello del fornitore protegge la fattura. Vanno chiesti entrambi, e in
    quest'ordine — se il fornitore rifiuta non ha senso aver già consumato la
    dotazione del cluster.
    """
    cur.execute("SELECT provider_try_consume(%s, %s, 1)", (source, credits))
    if not cur.fetchone()[0]:
        return False, "quota mensile del fornitore esaurita"

    fn = "cluster_try_consume_backfill" if in_backfill else "cluster_try_consume"
    cur.execute(f"SELECT {fn}(%s, %s)", (cluster_id, credits))
    if not cur.fetchone()[0]:
        # Restituisce al fornitore ciò che il cluster non userà.
        cur.execute("SELECT settle_credits(%s, NULL, %s)", (source, -credits))
        return False, ("dotazione di backfill esaurita" if in_backfill
                       else "tetto giornaliero del cluster raggiunto")
    return True, ""


def clients_for(country: str):
    return [c for c in CLIENTS if country in c.countries]


def ingest_cluster(conn: psycopg.Connection, cluster: Cluster, *, limit: int,
                   dry_run: bool, max_pages: int) -> dict[str, int]:
    totals = {"nuove": 0, "aggiornate": 0, "non_normalizzabili": 0,
              "rifiutate_dal_db": 0, "fuori_finestra": 0, "richieste": 0}
    clients = clients_for(cluster.country)
    if not clients:
        log.warning("%s: nessuna fonte copre %s", cluster.label, cluster.country)
        return totals

    with conn.cursor() as cur:
        cur.execute("SELECT provider FROM provider_quotas "
                    "WHERE monthly_credits_cap > 0")
        a_pagamento = {r[0] for r in cur.fetchall()}

    # Il backfill attinge a una dotazione dedicata: senza, il primo giro di ogni
    # cluster nuovo aprirebbe il breaker giornaliero e resterebbe a metà.
    # NON è un'esenzione dai soldi — provider_budget continua a valere.
    # In chiamata vanno SOLO paese e famiglia. I filtri personali restano nel
    # funnel: là costano zero e non lasciano buchi nel corpus quando cambiano
    # gli iscritti. Un archivio che dipende da chi era iscritto quel giorno vale
    # meno dei crediti che farebbe risparmiare.
    #
    # Il confine di quella regola: vale per i FILTRI di chi e' iscritto, non
    # per l'esistenza di iscritti. Zero iscritti non e' un filtro piu'
    # severo — e' l'assenza del motivo per cui il cluster spende. Da qui il
    # salto delle fonti a pagamento qui sotto, che con le gratuite attive
    # non buca il corpus: lo fa solo costare zero.

    # in_backfill resta valido per TUTTE le fonti del cluster: chiuderlo dopo la
    # prima faceva rifiutare le successive, che si trovavano a chiedere una
    # dotazione già chiusa. In produzione ha significato che France Travail e
    # Arbetsförmedlingen non hanno ingerito nulla al primo giro.
    in_backfill = cluster.in_backfill
    # L'esito per fonte, raccolto qui: la chiusura del backfill deve guardare
    # TUTTE le fonti. Prima decideva sull'ultima variabile rimasta in scope
    # dal ciclo — una fonte troncata dopo una completa chiudeva il backfill
    # come riuscito, e il cluster partiva con uno storico bucato senza
    # nemmeno il flag che lo dice.
    esiti_fonte: list[tuple[bool, bool]] = []  # (fetch_complete, esaurita)
    if cluster.in_backfill:
        log.info("%s: backfill, finestra di %d giorni, dotazione dedicata",
                 cluster.label, BACKFILL_WINDOW.days)

    for cls in clients:
        # Cosa si chiede a questa fonte: la tassonomia se la sa usare, altrimenti
        # il termine di ricerca specifico. Senza termine la fonte non può fare
        # nulla di sensato, e saltarla in silenzio è il modo di ritrovarsi un
        # cluster mezzo vuoto senza capire perché.
        # I soldi seguono i clienti. Un cluster senza iscritti attivi non
        # chiama le fonti a pagamento: il cluster si apre da solo quando un
        # utente si iscrive, ma niente lo chiudeva quando l'ultimo se ne
        # andava — e HR × FR ha continuato a consumare la fetta piu' grossa
        # del tetto Fantastic per nessuno. Le fonti GRATUITE e il ponte ATS
        # continuano: costano zero e tengono caldo il corpus, cosi' il primo
        # iscritto che arriva non parte dal vuoto.
        #
        # «A pagamento» lo dice provider_quotas (monthly_credits_cap > 0),
        # non una lista nel codice: se un giorno una fonte cambia prezzo,
        # cambia la riga in tabella, non questo file.
        #
        # NB: il campo scoperto non vede piu' fetch da questa fonte, quindi
        # per le sue offerte gia' scaricate la scadenza non si puo' piu'
        # dedurre («non piu' vista» presuppone una fetch che copriva). E'
        # accettato: nessun digest le sta leggendo, e expiry_blind_spots_v
        # le tiene in vista.
        if cluster.iscritti == 0 and cls.source in a_pagamento:
            log.info("%s: nessun iscritto attivo — fonte a pagamento %s "
                     "saltata (le gratuite continuano)",
                     cluster.label, cls.source)
            continue

        taxonomy = cluster.family if cls.source in TAXONOMY_SOURCES else None
        query = cluster.queries.get(cls.source, "")
        if not taxonomy and not query:
            log.warning("%s: nessun termine di ricerca per %s — fonte saltata. "
                        "Vedi cluster_coverage_v", cluster.label, cls.source)
            continue

        # Il cursore è di QUESTA coppia cluster-fonte. La finestra di backfill
        # resta il ripiego per una coppia che cursore non ha ancora: una fonte
        # che non ha mai consegnato nulla riparte da due settimane di storico.
        since = (cluster.cursors.get(cls.source)
                 or datetime.now(timezone.utc) - BACKFILL_WINDOW)

        # Riserva su entrambi i budget PRIMA della chiamata.
        with conn.cursor() as cur:
            allowed, motivo = riserva(cur, cluster_id=cluster.id, source=cls.source,
                                      in_backfill=in_backfill,
                                      credits=cls.credits_per_request)
        conn.commit()
        if not allowed:
            log.warning("%s: %s, salto %s", cluster.label, motivo, cls.source)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ingestion_runs (cluster_id, source, status, finished_at, "
                    "  error_message, request_params) VALUES (%s, %s, 'aborted_budget', now(), "
                    "  'tetto giornaliero raggiunto', %s::jsonb)",
                    (cluster.id, cls.source, '{}'))
            conn.commit()
            continue

        with conn.cursor() as cur:
            # Json vero e parametri VERI: registrava la famiglia anche per le
            # fonti che cercano per termine, e un apice nel valore avrebbe
            # prodotto jsonb malformato.
            cur.execute(
                "INSERT INTO ingestion_runs (cluster_id, source, request_params) "
                "VALUES (%s, %s, %s) RETURNING id",
                (cluster.id, cls.source,
                 PsycopgJson({"query": taxonomy or query, "taxonomy": bool(taxonomy),
                              "limit": limit, "since": since.isoformat()})))
            run_id = str(cur.fetchone()[0])
        conn.commit()

        # --- paginazione -----------------------------------------------
        #
        # La guida il runner e non il client, per una ragione precisa: il tetto
        # giornaliero sta nel database, e i client devono restare senza
        # database o il probe smette di funzionare senza credenziali.
        #
        # Il primo consumo di budget è già stato fatto sopra, per la prima
        # pagina: le successive lo chiedono una per una.
        offset = 0
        pages = 0
        # Contatore esplicito invece di dedurlo dall'offset: l'offset avanza
        # PRIMA di alcune uscite dal ciclo, quindi sommarci l'ultima pagina la
        # contava due volte.
        fetched = 0
        new = updated = 0
        newest = since
        last_page: object = None
        stop_reason = None

        try:
            with cls() as client:
                page_size = min(limit, client.page_size)
                while True:
                    kw: dict = {"taxonomy": taxonomy} if taxonomy else {}
                    result = client.fetch(query=query, country=cluster.country,
                                          since=since, limit=page_size, offset=offset,
                                          **kw)
                    last_page = result
                    pages += 1
                    fetched += result.received
                    totals["richieste"] += result.requests_made
                    totals["non_normalizzabili"] += result.skipped
                    attempt = client.attempts[-1] if client.attempts else None

                    # Le fonti con scaglioni fissi (Fantastic: 1h/24h/7d/1m/6m)
                    # non sanno restituire esattamente 14 giorni e danno il
                    # mese. Si scarta qui ciò che è più vecchio della finestra:
                    # un'offerta di tre settimane fa è spesso già chiusa, e un
                    # primo digest di annunci morti è il peggior inizio.
                    fuori_finestra = [j for j in result.jobs if j.date_posted < since]
                    if fuori_finestra:
                        result.jobs = [j for j in result.jobs if j.date_posted >= since]
                        totals["fuori_finestra"] += len(fuori_finestra)

                    if dry_run:
                        # La chiamata c'è stata e i crediti sono veri: la
                        # conciliazione e la riga di api_usage si scrivono
                        # anche qui, o i contatori mentono proprio nel giro
                        # che serve a controllare i costi.
                        with conn.cursor() as cur:
                            delta = result.credits_used - cls.credits_per_request
                            if delta:
                                cur.execute(
                                    "SELECT settle_credits(%s, %s, %s, %s)",
                                    (cls.source, cluster.id, delta, in_backfill))
                            store.record_usage(
                                cur, provider=cls.source, cluster_id=cluster.id,
                                run_id=run_id, requests=result.requests_made,
                                credits=result.credits_used,
                                http_status=attempt.status if attempt else None,
                                latency_ms=attempt.latency_ms if attempt else None)
                        conn.commit()
                    if not dry_run:
                        with conn.cursor() as cur:
                            for job in result.jobs:
                                try:
                                    job_id, is_new = store.upsert_job(cur, job)
                                    store.link_to_cluster(cur, job_id, cluster.id, run_id)
                                except psycopg.Error as exc:
                                    totals["rifiutate_dal_db"] += 1
                                    # WARNING e non DEBUG: un'offerta rifiutata dal
                                    # database è una perdita, e una perdita di cui
                                    # non si vede il motivo non si corregge mai.
                                    log.warning(
                                        "%s:%s rifiutata dal database [%s] %s",
                                        job.source, job.source_job_id, exc.sqlstate,
                                        str(exc).strip().splitlines()[0][:140])
                                    conn.rollback()
                                    continue
                                new += is_new
                                updated += not is_new
                                newest = max(newest, job.date_posted)
                            # Ogni pagina è una chiamata: va contata da sola,
                            # o il costo reale di un cluster resta invisibile.
                            # Il costo reale può differire da quanto riservato:
                            # su Fantastic si paga per offerta RESTITUITA, e la
                            # differenza va restituita a entrambi i budget.
                            delta = result.credits_used - cls.credits_per_request
                            if delta:
                                cur.execute(
                                    "SELECT settle_credits(%s, %s, %s, %s)",
                                    (cls.source, cluster.id, delta, in_backfill))
                            store.record_usage(
                                cur, provider=cls.source, cluster_id=cluster.id,
                                run_id=run_id, requests=result.requests_made,
                                credits=result.credits_used,
                                http_status=attempt.status if attempt else None,
                                latency_ms=attempt.latency_ms if attempt else None)
                        conn.commit()

                    log.info("  %s p%d: offset %d, %d normalizzate su %d, %s totali",
                             cls.source, pages, offset, len(result.jobs),
                             result.received, result.total_available)

                    if result.complete:
                        break
                    if result.received == 0:
                        stop_reason = "pagina vuota"
                        break

                    offset += result.received
                    if offset >= client.max_offset:
                        stop_reason = f"tetto di scorrimento della fonte ({client.max_offset})"
                        break
                    if pages >= max_pages:
                        stop_reason = f"limite di pagine ({max_pages})"
                        break

                    # Budget per la pagina SUCCESSIVA. Fermarsi qui significa
                    # fetch troncata, e le scadute non si deducono da una fetch
                    # troncata.
                    with conn.cursor() as cur:
                        ok, motivo = riserva(cur, cluster_id=cluster.id,
                                             source=cls.source,
                                             in_backfill=in_backfill,
                                             credits=cls.credits_per_request)
                    conn.commit()
                    if not ok:
                        stop_reason = motivo
                        break
        except Exception as exc:  # noqa: BLE001
            log.error("%s / %s: fetch fallita: %s", cluster.label, cls.source, exc)
            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ingestion_runs SET status = 'failed', finished_at = now(), "
                        "error_message = %s WHERE id = %s", (str(exc)[:500], run_id))
                conn.commit()
            continue

        # fetch_complete solo se l'ultima pagina ha esaurito i risultati E non
        # ci siamo fermati per nessun altro motivo.
        fetch_complete = bool(last_page and last_page.complete and stop_reason is None)
        if stop_reason:
            log.warning("%s / %s: fetch TRONCATA — %s", cluster.label, cls.source, stop_reason)

        if not dry_run:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE ingestion_runs SET status = 'success', finished_at = now(), "
                    "  fetch_complete = %s, jobs_fetched = %s, jobs_new = %s, "
                    "  jobs_updated = %s WHERE id = %s",
                    (fetch_complete, fetched, new, updated, run_id))
                cur.execute(
                    "UPDATE clusters SET last_fetched_at = now(), "
                    "  last_successful_fetch_at = now() WHERE id = %s",
                    (cluster.id,))
                # Il cursore avanza solo fino alla più recente offerta VISTA da
                # questa fonte, mai oltre: GREATEST lo protegge da un regresso
                # se una fetch torna meno di quanto era arrivata prima.
                cur.execute(
                    "INSERT INTO cluster_source_cursors (cluster_id, source, "
                    "  last_seen_posted_at) VALUES (%s, %s, %s) "
                    "ON CONFLICT (cluster_id, source) DO UPDATE SET "
                    "  last_seen_posted_at = GREATEST("
                    "    cluster_source_cursors.last_seen_posted_at, "
                    "    EXCLUDED.last_seen_posted_at)",
                    (cluster.id, cls.source, newest))
            conn.commit()
        else:
            conn.rollback()

        totals["nuove"] += new
        totals["aggiornate"] += updated

        # Il backfill si chiude DOPO tutte le fonti: qui si registra solo l'esito.
        if in_backfill:
            esiti_fonte.append(
                (bool(fetch_complete),
                 stop_reason == "dotazione di backfill esaurita"))
        log.info("%s / %s: %d pagine, %d nuove, %d aggiornate, completa=%s",
                 cluster.label, cls.source, pages, new, updated, fetch_complete)

    if in_backfill and esiti_fonte and not dry_run:
        complete_tutte = all(c for c, _ in esiti_fonte)
        esaurita_una = any(e for _, e in esiti_fonte)
        if complete_tutte or esaurita_una:
            with conn.cursor() as cur:
                cur.execute("SELECT cluster_finish_backfill(%s, %s)",
                            (cluster.id, not complete_tutte))
            conn.commit()
            log.info("%s: backfill chiuso%s", cluster.label,
                     "" if complete_tutte else " (troncato)")

    return totals


def resolve_duplicates(conn: psycopg.Connection, batch_size: int = 1000) -> tuple[int, int]:
    """Passo di deduplica morbida, dopo l'ingestione di tutti i cluster.

    Va DOPO e non durante: un'offerta arrivata da un cluster può essere il
    duplicato di una arrivata da un altro, e deciderlo a metà strada
    sceglierebbe l'originale in base a chi è passato per primo.
    """
    groups = marked = 0
    for _ in range(1000):
        with conn.cursor() as cur:
            cur.execute("SELECT groups_resolved, jobs_marked FROM resolve_duplicates(%s)",
                        (batch_size,))
            g, m = cur.fetchone()
        conn.commit()
        if not m:
            break
        groups += g
        marked += m
    else:
        raise RuntimeError("la deduplica non converge")
    return groups, marked


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.ingestion.runner", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="tutti i cluster attivi")
    g.add_argument("--cluster", help="un cluster preciso")
    ap.add_argument("--limit", type=int, default=150,
                    help="record per pagina (limitato dal massimo della fonte)")
    ap.add_argument("--max-pages", type=int, default=25,
                    help="rete di sicurezza contro un ciclo che non termina")
    ap.add_argument("--dry-run", action="store_true",
                    help="scarica e normalizza, poi annulla: non scrive nulla")
    ap.add_argument("--no-dedup", action="store_true",
                    help="salta il passo di deduplica morbida")
    args = ap.parse_args(argv)

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s",
                        stream=sys.stderr)

    # Il runner scrive offerte: gli basta DML, e nivult_app ha EXECUTE su
    # tutte le funzioni che usa (0010). Girava col ruolo migrator — con il
    # DDL in mano — contraddicendo la regola dei due ruoli che sta scritta
    # due righe sopra la sua stessa vecchia riga.
    dsn = database_url()
    print(f"database: {safe_dsn(dsn)}")

    grand = {"nuove": 0, "aggiornate": 0, "non_normalizzabili": 0,
             "rifiutate_dal_db": 0, "fuori_finestra": 0, "richieste": 0}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            clusters = due_clusters(cur, args.cluster)
        if not clusters:
            print("nessun cluster da elaborare.")
            return 0
        print(f"{len(clusters)} cluster"
              f"{' — DRY RUN, nulla verrà scritto' if args.dry_run else ''}\n")
        for c in clusters:
            t = ingest_cluster(conn, c, limit=args.limit, dry_run=args.dry_run,
                               max_pages=args.max_pages)
            for k in grand:
                grand[k] += t[k]

        if not args.dry_run and not args.no_dedup:
            groups, marked = resolve_duplicates(conn)
            grand["duplicate_marcate"] = marked
            log.info("deduplica: %d gruppi, %d offerte marcate duplicate", groups, marked)

    print("\nriepilogo:")
    for k, v in grand.items():
        print(f"  {k:<14} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
