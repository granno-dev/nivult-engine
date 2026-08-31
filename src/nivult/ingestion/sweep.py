"""Sweep delle offerte scadute.

    python -m nivult.ingestion.sweep [--dry-run] [--grace-hours 6]

Tre segnali, in ordine di affidabilità:

  1. la fonte lo dichiara       -> 'removed'
  2. date_valid_through passata -> 'expired'
  3. non più vista              -> 'expired', ma solo dopo una fetch COMPLETA

Il terzo è l'unico che può sbagliare, ed è per questo che è vincolato a
`ingestion_runs.fetch_complete`: su una fetch troncata l'assenza di un'offerta
non significa che sia sparita, significa che non siamo arrivati a leggerla.
"""

from __future__ import annotations

import argparse
import logging
import sys

import psycopg

from nivult.config import database_url, load_dotenv, safe_dsn

log = logging.getLogger("nivult.ingestion.sweep")


def _require_clean_connection(conn: psycopg.Connection) -> None:
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        raise RuntimeError(
            "la connessione ha già una transazione aperta: usa una connessione dedicata.")


def declared_removals(conn: psycopg.Connection, *, dry_run: bool) -> dict[str, int]:
    """Segnali nativi delle fonti. Entrambi gratuiti."""
    out: dict[str, int] = {}

    from nivult.ingestion.sources.fantastic import FantasticClient
    try:
        with FantasticClient() as c:
            ids = c.expired_ids(time_frame="1d")
        log.info("fantastic: %d id dichiarati scaduti nelle ultime 24 ore", len(ids))
        if not dry_run and ids:
            with conn.cursor() as cur:
                cur.execute("SELECT mark_jobs_removed('fantastic', %s)", (ids,))
                out["fantastic"] = cur.fetchone()[0]
            conn.commit()
        else:
            out["fantastic"] = 0
    except SystemExit:
        log.warning("fantastic: chiave assente, segnale di rimozione saltato")
    except Exception as exc:  # noqa: BLE001
        log.error("fantastic: segnale di rimozione non recuperato: %s", exc)

    from datetime import datetime, timedelta, timezone
    from nivult.ingestion.sources.arbetsformedlingen import ArbetsformedlingenClient
    try:
        with ArbetsformedlingenClient() as c:
            ids, esaminate = c.fetch_removals(
                datetime.now(timezone.utc) - timedelta(days=1))
        log.info("arbetsformedlingen: %d rimozioni su %d variazioni", len(ids), esaminate)
        if not dry_run and ids:
            with conn.cursor() as cur:
                cur.execute("SELECT mark_jobs_removed('arbetsformedlingen', %s)", (ids,))
                out["arbetsformedlingen"] = cur.fetchone()[0]
            conn.commit()
        else:
            out["arbetsformedlingen"] = 0
    except Exception as exc:  # noqa: BLE001
        log.error("arbetsformedlingen: segnale di rimozione non recuperato: %s", exc)

    return out


def deduce_expired(conn: psycopg.Connection, *, grace_hours: int,
                   batch_size: int = 5000, max_batches: int = 1000) -> dict[str, int]:
    totali: dict[str, int] = {}
    for _ in range(max_batches):
        with conn.cursor() as cur:
            cur.execute("SELECT motivo, righe FROM expire_stale_jobs(%s, %s)",
                        (grace_hours, batch_size))
            righe = cur.fetchall()
        conn.commit()
        mosse = 0
        for motivo, n in righe:
            totali[motivo] = totali.get(motivo, 0) + int(n)
            mosse += int(n)
        if not mosse:
            break
    else:
        raise RuntimeError("lo sweep non converge")
    return totali


# I segnali nel titolo, per la correzione qui sotto. Volutamente
# CONSERVATIVI e senza confini di parola sul lato junior: «international»
# contiene «intern» e viene risparmiata dalla correzione. Il costo delle
# due direzioni non e' simmetrico — una correzione mancata lascia
# un'offerta filtrata com'era gia', una correzione sbagliata metterebbe
# uno stage vero nel giro di valutazione di un senior (dove comunque il
# punteggio lo boccerebbe: il danno vero e' solo il costo del giudizio).
_TITOLO_SENIOR = (r"responsabile|direttore|director|manager|specialist"
                  r"|head of|lead|senior|business partner"
                  r"|coordinator|coordinatore")
_TITOLO_JUNIOR = r"junior|stage|intern|trainee|graduate|assistente|apprend|tirocin"


def distrust_seniority_tags(conn: psycopg.Connection, *,
                            dry_run: bool = False) -> int:
    """L'etichetta «0-2» smentita dal titolo diventa sconosciuta.

    Fantastic marca a livello «0-2» anche offerte con titoli da quadro —
    misurato il 2026-08-31 sul cluster Risorse umane x Italia: 13 su 167,
    fra cui un «Responsabile Risorse Umane». Un utente con la fascia
    mid/senior le perde IN SILENZIO, per un'etichetta che non ha scelto:
    e' lo stesso vizio del false sul visto e della stringa vuota sul
    settore — un valore sbagliato che filtra come se fosse vero.

    La cura e' la stessa di allora: il valore inattendibile diventa
    ASSENZA (NULL), e l'assenza non filtra mai — l'offerta arriva al
    giudizio, che la pesa contro il CV vero. Non si corregge al valore
    «giusto»: indovinarlo sarebbe un'altra etichetta inventata.

    Gira ogni notte perche' la re-ingestione riscrive il campo dalla
    fonte: la correzione e' un contrappeso permanente, non una tantum.
    """
    sql = ("UPDATE jobs SET ai_experience_level = NULL "
           " WHERE status = 'active' AND ai_experience_level = '0-2' "
           "   AND title ~* %s AND title !~* %s")
    with conn.cursor() as cur:
        if dry_run:
            cur.execute("SELECT count(*) FROM jobs "
                        " WHERE status = 'active' AND ai_experience_level = '0-2' "
                        "   AND title ~* %s AND title !~* %s",
                        (_TITOLO_SENIOR, _TITOLO_JUNIOR))
            n = cur.fetchone()[0]
            conn.rollback()
            return n
        cur.execute(sql, (_TITOLO_SENIOR, _TITOLO_JUNIOR))
        n = cur.rowcount
    conn.commit()
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.ingestion.sweep", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--grace-hours", type=int, default=6,
                    help="quanto aspettare prima di dedurre da un'assenza")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-declared", action="store_true",
                    help="salta i segnali nativi delle fonti")
    args = ap.parse_args(argv)

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s",
                        stream=sys.stderr)
    dsn = database_url()
    print(f"database: {safe_dsn(dsn)}")

    with psycopg.connect(dsn) as conn:
        _require_clean_connection(conn)

        if not args.no_declared:
            dichiarate = declared_removals(conn, dry_run=args.dry_run)
        else:
            dichiarate = {}

        dedotte = deduce_expired(conn, grace_hours=args.grace_hours) \
            if not args.dry_run else {}

        sfiduciate = distrust_seniority_tags(conn, dry_run=args.dry_run)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FILTER (WHERE status='active'), "
                "       count(*) FILTER (WHERE status='expired'), "
                "       count(*) FILTER (WHERE status='removed') FROM jobs")
            attive, scadute, rimosse = cur.fetchone()
            cur.execute("SELECT coalesce(sum(offerte_non_giudicabili),0) "
                        "FROM expiry_blind_spots_v")
            cieche = cur.fetchone()[0]

    print("\nrimozioni dichiarate dalla fonte:")
    for k, v in (dichiarate or {"—": 0}).items():
        print(f"  {k:<22} {v}")
    print("\nscadenze dedotte:")
    for k, v in (dedotte or {"—": 0}).items():
        print(f"  {k:<38} {v}")
    if sfiduciate:
        print(f"\netichette seniority smentite dal titolo -> sconosciute: {sfiduciate}")
    print(f"\nstato: {attive} attive, {scadute} scadute, {rimosse} rimosse")
    if cieche:
        print(f"  {cieche} offerte in cluster con fetch troncate: lì la scadenza "
              f"non si può dedurre (vedi expiry_blind_spots_v)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
