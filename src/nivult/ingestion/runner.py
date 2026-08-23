"""Il ciclo di ingestione: itera sui CLUSTER, non sugli utenti.

Iterando sugli utenti, diecimila utenti farebbero diecimila ricerche al giorno
sullo stesso identico insieme di offerte. Iterando sui cluster la parte costosa
si paga una volta per cluster. Vedi CLAUDE.md.

    python -m nivult.ingestion.runner --all
    python -m nivult.ingestion.runner --cluster <uuid> --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg

from nivult.config import load_dotenv, migrator_database_url, safe_dsn
from nivult.ingestion import store
from nivult.ingestion.sources.arbetsformedlingen import ArbetsformedlingenClient
from nivult.ingestion.sources.france_travail import FranceTravailClient

log = logging.getLogger("nivult.ingestion.runner")

CLIENTS = (FranceTravailClient, ArbetsformedlingenClient)

# Prima finestra per un cluster mai scaricato. Senza, si chiederebbe l'intero
# storico della fonte al primo giro.
FIRST_WINDOW = timedelta(days=7)


@dataclass(slots=True)
class Cluster:
    id: str
    family: str
    country: str
    last_seen_posted_at: datetime | None

    @property
    def label(self) -> str:
        return f"{self.family} × {self.country}"


def due_clusters(cur, cluster_id: str | None) -> list[Cluster]:
    if cluster_id:
        cur.execute(
            "SELECT id, family, country, last_seen_posted_at FROM clusters WHERE id = %s",
            (cluster_id,))
    else:
        # NULLS FIRST: chi non è mai stato scaricato ha la precedenza.
        cur.execute(
            "SELECT id, family, country, last_seen_posted_at FROM clusters "
            "WHERE status = 'active' ORDER BY last_fetched_at NULLS FIRST")
    return [Cluster(str(r[0]), r[1], r[2], r[3]) for r in cur.fetchall()]


def clients_for(country: str):
    return [c for c in CLIENTS if country in c.countries]


def ingest_cluster(conn: psycopg.Connection, cluster: Cluster, *, limit: int,
                   dry_run: bool, max_pages: int) -> dict[str, int]:
    totals = {"nuove": 0, "aggiornate": 0, "non_normalizzabili": 0,
              "rifiutate_dal_db": 0, "richieste": 0}
    clients = clients_for(cluster.country)
    if not clients:
        log.warning("%s: nessuna fonte copre %s", cluster.label, cluster.country)
        return totals

    since = cluster.last_seen_posted_at or datetime.now(timezone.utc) - FIRST_WINDOW

    for cls in clients:
        # Il breaker PRIMA della chiamata: se il tetto è raggiunto non si parte.
        with conn.cursor() as cur:
            cur.execute("SELECT cluster_try_consume(%s, %s)",
                        (cluster.id, cls.credits_per_request))
            allowed = cur.fetchone()[0]
        if not allowed:
            log.warning("%s: breaker aperto, salto %s", cluster.label, cls.source)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ingestion_runs (cluster_id, source, status, finished_at, "
                    "  error_message, request_params) VALUES (%s, %s, 'aborted_budget', now(), "
                    "  'tetto giornaliero raggiunto', %s::jsonb)",
                    (cluster.id, cls.source, '{}'))
            conn.commit()
            continue

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ingestion_runs (cluster_id, source, request_params) "
                "VALUES (%s, %s, %s::jsonb) RETURNING id",
                (cluster.id, cls.source,
                 f'{{"query": "{cluster.family}", "limit": {limit}, "since": "{since.isoformat()}"}}'))
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
                    result = client.fetch(query=cluster.family, country=cluster.country,
                                          since=since, limit=page_size, offset=offset)
                    last_page = result
                    pages += 1
                    fetched += result.received
                    totals["richieste"] += result.requests_made
                    totals["non_normalizzabili"] += result.skipped
                    attempt = client.attempts[-1] if client.attempts else None

                    if not dry_run:
                        with conn.cursor() as cur:
                            for job in result.jobs:
                                try:
                                    job_id, is_new = store.upsert_job(cur, job)
                                    store.link_to_cluster(cur, job_id, cluster.id, run_id)
                                except psycopg.Error as exc:
                                    totals["rifiutate_dal_db"] += 1
                                    log.debug("scartata %s:%s — %s",
                                              job.source, job.source_job_id, exc)
                                    conn.rollback()
                                    continue
                                new += is_new
                                updated += not is_new
                                newest = max(newest, job.date_posted)
                            # Ogni pagina è una chiamata: va contata da sola,
                            # o il costo reale di un cluster resta invisibile.
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
                        cur.execute("SELECT cluster_try_consume(%s, %s)",
                                    (cluster.id, cls.credits_per_request))
                        if not cur.fetchone()[0]:
                            stop_reason = "tetto giornaliero raggiunto"
                            break
                    conn.commit()
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
                    "  last_successful_fetch_at = now(), "
                    "  last_seen_posted_at = GREATEST(COALESCE(last_seen_posted_at, %s), %s) "
                    "WHERE id = %s", (newest, newest, cluster.id))
            conn.commit()
        else:
            conn.rollback()

        totals["nuove"] += new
        totals["aggiornate"] += updated
        log.info("%s / %s: %d pagine, %d nuove, %d aggiornate, completa=%s",
                 cluster.label, cls.source, pages, new, updated, fetch_complete)

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

    # Il runner scrive offerte: gli basta DML, non DDL. Ma in produzione
    # DATABASE_URL è nivult_app, che è esattamente il ruolo giusto qui.
    dsn = migrator_database_url()
    print(f"database: {safe_dsn(dsn)}")

    grand = {"nuove": 0, "aggiornate": 0, "non_normalizzabili": 0,
             "rifiutate_dal_db": 0, "richieste": 0}
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
