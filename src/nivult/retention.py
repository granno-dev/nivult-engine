"""Retention delle offerte morte.

Offerte con status 'expired' o 'removed' da più di N giorni (60 di default)
spariscono, jsonb grezzo compreso. Le offerte 'active' non scadono mai.

Due livelli, perché un'offerta già valutata o già inviata non può semplicemente
sparire senza portarsi via l'anti-ripetizione e il registro di cosa un utente ha
ricevuto:

  - non referenziata -> cancellata;
  - referenziata     -> svuotata e marcata purged_at, resta una riga-lapide.

Prima di entrambe, i contatori aggregati per cluster e per mese vengono
aggiornati: le statistiche sopravvivono al corpus.
"""

from __future__ import annotations

import logging
import time

import psycopg

log = logging.getLogger("nivult.retention")

DEFAULT_RETENTION_DAYS = 60


def _require_clean_connection(conn: psycopg.Connection) -> None:
    """Rifiuta una connessione con una transazione già aperta.

    Questi cicli committano dopo ogni lotto, ed è tutto il punto: transazioni
    brevi invece di un lock lungo. Ma se il chiamante ha già una transazione
    aperta, conn.transaction() aprirebbe un semplice SAVEPOINT e i lotti non
    verrebbero committati affatto — il lavoro resterebbe in bilico fino al
    commit del chiamante, e un rollback lo butterebbe via in silenzio.
    Meglio un errore esplicito che una cancellazione che sembra riuscita.
    """
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError(
            "la connessione ha già una transazione aperta: questi lotti devono "
            "poter committare da soli. Usa una connessione dedicata."
        )


def purge(
    conn: psycopg.Connection,
    older_than_days: int = DEFAULT_RETENTION_DAYS,
    batch_size: int = 1000,
    max_batches: int = 100_000,
    pause_seconds: float = 0.0,
    dry_run: bool = False,
) -> dict[str, int]:
    """Cicla purge_dead_jobs finché non c'è più nulla da eliminare."""
    _require_clean_connection(conn)

    if dry_run:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FILTER (WHERE ref), count(*) FILTER (WHERE NOT ref) FROM ("
                "  SELECT EXISTS (SELECT 1 FROM matches m WHERE m.job_id = j.id)"
                "      OR EXISTS (SELECT 1 FROM digest_items d WHERE d.job_id = j.id) AS ref"
                "    FROM jobs j"
                "   WHERE j.status IN ('expired','removed') AND j.purged_at IS NULL"
                "     AND j.expired_at < now() - make_interval(days => %s)) t",
                (older_than_days,),
            )
            tomb, dele = cur.fetchone()
        conn.rollback()
        log.info("dry-run: %d da cancellare, %d da svuotare", dele, tomb)
        return {"da_cancellare": dele, "da_svuotare": tomb}

    totals = {"cancellate": 0, "svuotate": 0, "lotti": 0}
    for _ in range(max_batches):
        # Una transazione per lotto, chiusa da un commit esplicito: la retention
        # non deve tenere lock lunghi su jobs, che è la tabella più calda del
        # motore.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT jobs_deleted, jobs_tombstoned FROM purge_dead_jobs(%s, %s)",
                (older_than_days, batch_size),
            )
            deleted, tombstoned = cur.fetchone()
        conn.commit()

        if deleted == 0 and tombstoned == 0:
            break

        totals["cancellate"] += int(deleted)
        totals["svuotate"] += int(tombstoned)
        totals["lotti"] += 1
        log.info("lotto %d: %d cancellate, %d svuotate",
                 totals["lotti"], deleted, tombstoned)
        if pause_seconds:
            time.sleep(pause_seconds)
    else:
        raise RuntimeError(f"superati {max_batches} lotti: qualcosa non converge")

    log.info("retention completata: %(cancellate)d cancellate, "
             "%(svuotate)d svuotate in %(lotti)d lotti", totals)
    return totals
