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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.types.json import Json as PsycopgJson

from nivult.config import database_url, load_dotenv, safe_dsn
from nivult.ingestion import store
from nivult.ingestion.sources.arbetsformedlingen import ArbetsformedlingenClient
from nivult.ingestion.sources.france_travail import FranceTravailClient
from nivult.ingestion.sources.indeed import IndeedClient
from nivult.ingestion.sources.linkedin import LinkedInClient

log = logging.getLogger("nivult.ingestion.runner")

CLIENTS = (FranceTravailClient, ArbetsformedlingenClient,  # Fantastic pensionato 23/10/2026
           LinkedInClient, IndeedClient)

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
    # Cursore e termini di ricerca per fonte: le fonti di un cluster procedono
    # ognuna per conto suo, e non devono nulla l'una all'altra.
    cursors: dict[str, datetime] = field(default_factory=dict)
    queries: dict[str, list[str]] = field(default_factory=dict)

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
            "WHERE cluster_id = ANY(%s) ORDER BY cluster_id, source, query",
            ([c.id for c in clusters],))
        per_id: dict[str, dict[str, list[str]]] = {}
        for cid, src, q in cur.fetchall():
            per_id.setdefault(str(cid), {}).setdefault(src, []).append(q)
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
        # Restituisce al fornitore ciò che il cluster non userà, richiesta inclusa.
        cur.execute("SELECT settle_credits(%s, NULL, %s)", (source, -credits))
        cur.execute("SELECT settle_requests(%s, NULL, -1)", (source,))
        return False, ("dotazione di backfill esaurita" if in_backfill
                       else "tetto giornaliero del cluster raggiunto")
    return True, ""


def clients_for(country: str):
    return [c for c in CLIENTS if country in c.countries]


def _concilia(cur, *, source: str, cluster_id: str, in_backfill: bool,
              credits_delta: int, requests_delta: int) -> None:
    if credits_delta:
        cur.execute("SELECT settle_credits(%s, %s, %s, %s)",
                    (source, cluster_id, credits_delta, in_backfill))
    if requests_delta:
        cur.execute("SELECT settle_requests(%s, %s, %s, %s)",
                    (source, cluster_id, requests_delta, in_backfill))


def _registra_salto(conn, *, cluster: Cluster, source: str, status: str,
                    motivo: str, params: dict, dry_run: bool) -> None:
    if dry_run:
        return
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ingestion_runs (cluster_id, source, status, finished_at, "
            "error_message, request_params) VALUES (%s, %s, %s, now(), %s, %s)",
            (cluster.id, source, status, motivo[:500], PsycopgJson(params)))
    conn.commit()


def ingest_cluster(conn: psycopg.Connection, cluster: Cluster, *, limit: int,
                   dry_run: bool, max_pages: int, allow_paid: bool = False) -> dict[str, int]:
    totals = {"nuove": 0, "aggiornate": 0, "non_normalizzabili": 0,
              "rifiutate_dal_db": 0, "fuori_finestra": 0, "richieste": 0,
              "crediti": 0}
    clients = clients_for(cluster.country)
    if not clients:
        log.warning("%s: nessuna fonte copre %s", cluster.label, cluster.country)
        return totals

    with conn.cursor() as cur:
        cur.execute("SELECT provider FROM provider_quotas WHERE monthly_credits_cap > 0")
        a_pagamento = {r[0] for r in cur.fetchall()}
    conn.commit()

    in_backfill = cluster.in_backfill
    esiti_fonte: list[tuple[bool, bool]] = []
    if in_backfill:
        log.info("%s: backfill, finestra di %d giorni, dotazione dedicata",
                 cluster.label, BACKFILL_WINDOW.days)

    for cls in clients:
        taxonomy = cluster.family if cls.source in TAXONOMY_SOURCES else None
        termini = ([cluster.family] if taxonomy else sorted({
            q.strip() for q in cluster.queries.get(cls.source, []) if q.strip()
        }))
        since = (cluster.cursors.get(cls.source)
                 or datetime.now(timezone.utc) - BACKFILL_WINDOW)
        params = {"queries": termini, "taxonomy": bool(taxonomy),
                  "limit": limit, "since": since.isoformat()}

        if not termini:
            motivo = f"nessun termine di ricerca per {cls.source}; vedi cluster_coverage_v"
            log.warning("%s: %s", cluster.label, motivo)
            _registra_salto(conn, cluster=cluster, source=cls.source, status="failed",
                            motivo=motivo, params=params, dry_run=dry_run)
            esiti_fonte.append((False, False))
            continue
        if cluster.iscritti == 0 and cls.source in a_pagamento:
            motivo = "nessun iscritto attivo: fonte a pagamento non chiamata"
            log.info("%s: %s (%s)", cluster.label, motivo, cls.source)
            _registra_salto(conn, cluster=cluster, source=cls.source, status="failed",
                            motivo=motivo, params=params, dry_run=dry_run)
            esiti_fonte.append((False, False))
            continue
        if dry_run and not allow_paid and cls.source in a_pagamento:
            log.info("%s: dry-run senza --allow-paid, %s non chiamata",
                     cluster.label, cls.source)
            esiti_fonte.append((False, False))
            continue

        # La configurazione del client si valida prima di riservare budget o
        # aprire un run: una chiave assente non deve lasciare addebiti o running.
        try:
            client_cm = cls()
        except (SystemExit, Exception) as exc:  # noqa: BLE001
            motivo = f"client non disponibile: {exc}"
            log.warning("%s / %s: %s", cluster.label, cls.source, motivo)
            _registra_salto(conn, cluster=cluster, source=cls.source, status="failed",
                            motivo=motivo, params=params, dry_run=dry_run)
            esiti_fonte.append((False, False))
            continue

        run_id = None
        try:
            with client_cm as client:
                if not dry_run:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO ingestion_runs (cluster_id, source, request_params) "
                            "VALUES (%s, %s, %s) RETURNING id",
                            (cluster.id, cls.source, PsycopgJson(params)))
                        run_id = str(cur.fetchone()[0])
                    conn.commit()

                page_size = min(limit, client.page_size)
                fetched = new = updated = pages_total = 0
                newest = since
                source_complete = True
                budget_stop = backfill_esaurita = False
                errori: list[str] = []
                arresti: list[str] = []

                for termine in termini:
                    offset = pages = 0
                    term_complete = False
                    stop_reason = None
                    while True:
                        if not dry_run:
                            with conn.cursor() as cur:
                                allowed, motivo = riserva(
                                    cur, cluster_id=cluster.id, source=cls.source,
                                    in_backfill=in_backfill,
                                    credits=cls.credits_per_request)
                            conn.commit()
                            if not allowed:
                                stop_reason = motivo
                                budget_stop = True
                                backfill_esaurita = motivo == "dotazione di backfill esaurita"
                                break

                        attempts_before = len(client.attempts)
                        try:
                            kw = {"taxonomy": taxonomy} if taxonomy else {}
                            result = client.fetch(
                                query="" if taxonomy else termine,
                                country=cluster.country, since=since,
                                limit=page_size, offset=offset, **kw)
                        except Exception as exc:  # noqa: BLE001
                            attempts = len(client.attempts) - attempts_before
                            totals["richieste"] += attempts
                            stop_reason = f"{termine}: {exc}"
                            errori.append(stop_reason)
                            if not dry_run:
                                with conn.cursor() as cur:
                                    _concilia(
                                        cur, source=cls.source, cluster_id=cluster.id,
                                        in_backfill=in_backfill,
                                        credits_delta=(-cls.credits_per_request
                                                       if attempts == 0 else 0),
                                        requests_delta=attempts - 1)
                                    attempt = client.attempts[-1] if client.attempts else None
                                    store.record_usage(
                                        cur, provider=cls.source, cluster_id=cluster.id,
                                        run_id=run_id, requests=attempts, credits=0,
                                        http_status=attempt.status if attempt else None,
                                        latency_ms=attempt.latency_ms if attempt else None)
                                conn.commit()
                            break

                        attempts = len(client.attempts) - attempts_before
                        result.requests_made = attempts
                        totals["richieste"] += attempts
                        totals["crediti"] += result.credits_used
                        totals["non_normalizzabili"] += result.skipped
                        totals["fuori_finestra"] += result.out_of_window
                        pages += 1
                        pages_total += 1
                        fetched += result.received

                        fuori = [j for j in result.jobs if j.date_posted < since]
                        if fuori:
                            result.jobs = [j for j in result.jobs if j.date_posted >= since]
                            result.out_of_window += len(fuori)
                            totals["fuori_finestra"] += len(fuori)

                        page_new = page_updated = 0
                        page_newest = newest
                        try:
                            if not dry_run:
                                with conn.transaction():
                                    with conn.cursor() as cur:
                                        for job in result.jobs:
                                            try:
                                                with conn.transaction():
                                                    job_id, is_new = store.upsert_job(cur, job)
                                                    store.link_to_cluster(
                                                        cur, job_id, cluster.id, run_id)
                                            except psycopg.Error as exc:
                                                totals["rifiutate_dal_db"] += 1
                                                log.warning(
                                                    "%s:%s rifiutata dal database [%s] %s",
                                                    job.source, job.source_job_id, exc.sqlstate,
                                                    str(exc).strip().splitlines()[0][:140])
                                                continue
                                            page_new += int(is_new)
                                            page_updated += int(not is_new)
                                            page_newest = max(page_newest, job.date_posted)
                                        attempt = client.attempts[-1] if client.attempts else None
                                        _concilia(
                                            cur, source=cls.source, cluster_id=cluster.id,
                                            in_backfill=in_backfill,
                                            credits_delta=(result.credits_used
                                                           - cls.credits_per_request),
                                            requests_delta=attempts - 1)
                                        store.record_usage(
                                            cur, provider=cls.source, cluster_id=cluster.id,
                                            run_id=run_id, requests=attempts,
                                            credits=result.credits_used,
                                            http_status=attempt.status if attempt else None,
                                            latency_ms=attempt.latency_ms if attempt else None)
                        except Exception as exc:  # noqa: BLE001
                            conn.rollback()
                            stop_reason = f"{termine}: scrittura pagina fallita: {exc}"
                            errori.append(stop_reason)
                            if not dry_run:
                                with conn.cursor() as cur:
                                    _concilia(
                                        cur, source=cls.source, cluster_id=cluster.id,
                                        in_backfill=in_backfill,
                                        credits_delta=(result.credits_used
                                                       - cls.credits_per_request),
                                        requests_delta=attempts - 1)
                                    attempt = client.attempts[-1] if client.attempts else None
                                    store.record_usage(
                                        cur, provider=cls.source, cluster_id=cluster.id,
                                        run_id=run_id, requests=attempts,
                                        credits=result.credits_used,
                                        http_status=attempt.status if attempt else None,
                                        latency_ms=attempt.latency_ms if attempt else None)
                                conn.commit()
                            break

                        new += page_new
                        updated += page_updated
                        newest = page_newest
                        totals["nuove"] += page_new
                        totals["aggiornate"] += page_updated
                        log.info("  %s p%d: offset %d, %d normalizzate su %d, %s totali",
                                 cls.source, pages, offset, len(result.jobs),
                                 result.received, result.total_available)

                        if result.complete:
                            term_complete = True
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

                    if not term_complete:
                        source_complete = False
                    if stop_reason:
                        arresti.append(stop_reason)
                    if budget_stop:
                        break

                fetch_complete = source_complete and not errori and not budget_stop
                if not dry_run:
                    status = ("aborted_budget" if budget_stop
                              else "failed" if errori else "success")
                    messaggio = "; ".join(errori + arresti)[:500] or None
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE ingestion_runs SET status = %s, finished_at = now(), "
                            "fetch_complete = %s, jobs_fetched = %s, jobs_new = %s, "
                            "jobs_updated = %s, error_message = %s WHERE id = %s",
                            (status, fetch_complete, fetched, new, updated,
                             messaggio, run_id))
                        cur.execute(
                            "UPDATE clusters SET last_fetched_at = now(), "
                            "last_successful_fetch_at = CASE WHEN %s THEN now() "
                            "ELSE last_successful_fetch_at END WHERE id = %s",
                            (fetch_complete, cluster.id))
                        if fetch_complete:
                            cur.execute(
                                "INSERT INTO cluster_source_cursors (cluster_id, source, "
                                "last_seen_posted_at) VALUES (%s, %s, %s) "
                                "ON CONFLICT (cluster_id, source) DO UPDATE SET "
                                "last_seen_posted_at = GREATEST("
                                "cluster_source_cursors.last_seen_posted_at, "
                                "EXCLUDED.last_seen_posted_at)",
                                (cluster.id, cls.source, newest))
                    conn.commit()
                esiti_fonte.append((fetch_complete, backfill_esaurita))
                log.info("%s / %s: %d pagine, %d nuove, %d aggiornate, completa=%s",
                         cluster.label, cls.source, pages_total, new, updated,
                         fetch_complete)
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            log.error("%s / %s: fonte fallita: %s", cluster.label, cls.source, exc)
            if run_id is None:
                _registra_salto(conn, cluster=cluster, source=cls.source,
                                status="failed", motivo=str(exc), params=params,
                                dry_run=dry_run)
            elif not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE ingestion_runs SET status = 'failed', finished_at = now(), "
                        "fetch_complete = false, error_message = %s WHERE id = %s",
                        (str(exc)[:500], run_id))
                conn.commit()
            esiti_fonte.append((False, False))

    if in_backfill and esiti_fonte and not dry_run:
        complete_tutte = len(esiti_fonte) == len(clients) and all(c for c, _ in esiti_fonte)
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
                    help="scarica e normalizza senza persistere run, budget, usage o cursori")
    ap.add_argument("--allow-paid", action="store_true",
                    help="nel dry-run chiama anche le fonti a pagamento; il costo è reale "
                         "e viene dichiarato nel riepilogo, non addebitato ai budget locali")
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
             "rifiutate_dal_db": 0, "fuori_finestra": 0, "richieste": 0,
             "crediti": 0}
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
                               max_pages=args.max_pages, allow_paid=args.allow_paid)
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
