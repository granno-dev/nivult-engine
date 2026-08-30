"""Cancellazione dell'utente su richiesta, a lotti.

Requisito GDPR, attivo dal primo utente.

Regole rispettate qui:
  - il contenuto del CV non viene MAI letto né registrato: si tocca solo
    storage_key, che è un percorso opaco;
  - ogni lotto è una transazione breve, così la cancellazione di uno storico
    lungo non tiene lock su tabelle calde;
  - le chiavi object storage vengono raccolte PRIMA di cancellare le righe,
    altrimenti i blob resterebbero orfani e la cancellazione sarebbe incompleta.

Chi esegue la cancellazione passa `rimuovi_blob`: una funzione che sa
togliere un file dallo storage. Senza, le chiavi restano depositate in
deletion_requests.pending_storage_keys e la richiesta NON viene chiusa —
meglio una richiesta che resta aperta di una cancellazione che si dichiara
completa mentre i file sono ancora lì.

Il parametro è opzionale e non per pigrizia: questo modulo non deve sapere
che esiste S3. Chi lo chiama sì, e gli passa `storage.elimina`.
"""

from __future__ import annotations

import json
import logging
import time

import psycopg
from collections.abc import Callable

log = logging.getLogger("nivult.gdpr")


class DeletionError(RuntimeError):
    pass


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


def request_deletion(conn: psycopg.Connection, user_id: str) -> str:
    """Registra la richiesta e marca l'utente come cancellato.

    Separata dall'esecuzione: l'utente smette immediatamente di ricevere digest,
    lo svuotamento può richiedere minuti.
    """
    _require_clean_connection(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE id = %s", (user_id,))
        if cur.fetchone() is None:
            raise DeletionError(f"utente inesistente: {user_id}")

        cur.execute(
            "UPDATE users SET status = 'deleted', deleted_at = now(), "
            "next_digest_at = NULL WHERE id = %s",
            (user_id,),
        )

        cur.execute("SELECT storage_key FROM user_cvs WHERE user_id = %s", (user_id,))
        keys = [row[0] for row in cur.fetchall()]

        cur.execute(
            "INSERT INTO deletion_requests (user_id, pending_storage_keys) "
            "VALUES (%s, %s) "
            "ON CONFLICT (user_id) WHERE status IN ('pending','running') DO NOTHING "
            "RETURNING id",
            (user_id, json.dumps(keys)),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "SELECT id FROM deletion_requests "
                "WHERE user_id = %s AND status IN ('pending','running')",
                (user_id,),
            )
            row = cur.fetchone()
            log.info("richiesta già aperta per %s", user_id)
        request_id = str(row[0])
    conn.commit()
    return request_id


def execute_deletion(
    conn: psycopg.Connection,
    request_id: str,
    batch_size: int = 5000,
    pause_seconds: float = 0.0,
    max_batches: int = 10_000,
    rimuovi_blob: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Esegue la cancellazione a lotti. Riprendibile: rilanciarla è sicuro."""
    _require_clean_connection(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, status, pending_storage_keys FROM deletion_requests "
            "WHERE id = %s FOR UPDATE",
            (request_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise DeletionError(f"richiesta inesistente: {request_id}")
        user_id, status, storage_keys = row
        if status == "completed":
            conn.rollback()
            log.info("richiesta %s già completata", request_id)
            return {}
        cur.execute(
            "UPDATE deletion_requests SET status = 'running', "
            "started_at = COALESCE(started_at, now()) WHERE id = %s",
            (request_id,),
        )
    conn.commit()

    totals: dict[str, int] = {}
    try:
        for _ in range(max_batches):
            # Una transazione per lotto, chiusa da un commit esplicito: il lock
            # dura quanto un lotto, non quanto l'intera cancellazione.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT step, rows_affected FROM delete_user_batch(%s, %s)",
                    (user_id, batch_size),
                )
                step, count = cur.fetchone()
            conn.commit()

            totals[step] = totals.get(step, 0) + int(count)
            log.info("richiesta %s: %s -> %d righe", request_id, step, count)

            if step == "users":
                break
            if pause_seconds:
                time.sleep(pause_seconds)
        else:
            raise DeletionError(
                f"superati {max_batches} lotti: qualcosa non converge, "
                f"cancellazione interrotta"
            )
    except Exception as exc:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE deletion_requests SET status = 'failed', "
                "error_message = %s, rows_deleted = %s WHERE id = %s",
                (str(exc), json.dumps(totals), request_id),
            )
        conn.commit()
        raise

    # I BLOB, per ultimi e uno alla volta.
    #
    # Dopo le righe, non prima: se il giro si interrompe a metà, un file
    # rimasto senza la sua riga è invisibile ma innocuo, mentre una riga
    # rimasta senza il suo file punterebbe nel vuoto — e il pannello
    # dell'utente proverebbe a scaricarlo.
    #
    # Una chiave che fallisce NON ferma le altre e resta nell'elenco: la
    # richiesta rimane aperta, e rilanciarla riprende da lì. Un errore su un
    # file non deve trasformarsi in nove file che nessuno rimuove più.
    remaining = list(storage_keys or [])
    if rimuovi_blob is not None and remaining:
        falliti: list[str] = []
        for chiave in remaining:
            try:
                rimuovi_blob(chiave)
            except Exception as exc:  # noqa: BLE001
                log.warning("richiesta %s: blob %s non rimosso: %s",
                            request_id, chiave, str(exc)[:120])
                falliti.append(chiave)
        remaining = falliti
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE deletion_requests SET pending_storage_keys = %s "
                "WHERE id = %s",
                (json.dumps(remaining), request_id),
            )
        conn.commit()

    final_status = "pending" if remaining else "completed"
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE deletion_requests SET status = %s, rows_deleted = %s, "
            "completed_at = CASE WHEN %s = 'completed' THEN now() ELSE NULL END "
            "WHERE id = %s",
            (final_status, json.dumps(totals), final_status, request_id),
        )
    conn.commit()

    if remaining:
        log.warning(
            "richiesta %s: righe cancellate, ma restano %d file su object storage "
            "da rimuovere (vedi deletion_requests.pending_storage_keys). "
            "La richiesta resta aperta.",
            request_id,
            len(remaining),
        )
    return totals
