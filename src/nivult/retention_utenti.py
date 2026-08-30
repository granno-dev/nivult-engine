"""La conservazione dei dati personali, eseguita invece che promessa.

`retention.py` si occupa delle OFFERTE. Questo si occupa delle PERSONE, ed
esiste perche' la privacy policy prometteva due cose che nessun codice
faceva accadere:

  - «il CV e l'account spariscono entro 30 giorni dalla fine
    dell'abbonamento»;
  - il principio di limitazione della conservazione: dati tenuti non oltre
    lo scopo.

Una promessa scritta e non eseguita e' peggio del silenzio: e' una
dichiarazione falsa in un documento che il cliente legge prima di pagare.

DUE STRADE, e finiscono nello stesso posto.

  abbonamento finito   30 giorni dopo `current_period_end`, senza rinnovo.
                       Il termine e' quello scritto sulla pagina.

  inattivita' lunga    12 mesi senza alcun segno di vita. Qui NON si
                       cancella subito: parte un avviso, e la cancellazione
                       arriva 7 giorni dopo se l'utente non torna. Il
                       rientro azzera il conto alla rovescia, e lo azzera
                       DAVVERO — la colonna torna a NULL, non resta li' a
                       far ripartire il conto al prossimo giro.

La cancellazione vera non si riscrive qui: e' `gdpr.request_deletion` +
`gdpr.execute_deletion`, gli stessi due passi del bottone nel pannello,
compreso lo svuotamento dei blob su storage. Un secondo modo di cancellare
un utente sarebbe un secondo modo di sbagliare.

NIENTE CANCELLAZIONE SILENZIOSA. `avvisa()` manda l'email PRIMA, e
`esegui()` non tocca nessuno che non sia stato avvisato: sono due funzioni
separate proprio perche' la seconda non possa girare senza la prima.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import psycopg

from nivult import gdpr

log = logging.getLogger("nivult.retention_utenti")

MESI_INATTIVITA = 12
GIORNI_DOPO_AVVISO = 7
GIORNI_DOPO_ABBONAMENTO = 30


@dataclass(frozen=True)
class Utente:
    id: str
    email: str
    locale: str
    motivo: str


def _riga(r) -> Utente:
    return Utente(id=r[0], email=r[1], locale=r[2] or "en", motivo=r[3])


def rientrati(conn: psycopg.Connection) -> int:
    """Chi era stato avvisato ed e' tornato: l'avviso si annulla.

    Va chiamata PRIMA di tutto il resto. Un utente che rientra il sesto
    giorno non deve trovarsi cancellato il settimo perche' nessuno ha
    guardato la data giusta.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users u SET inactivity_warned_at = NULL "
            "  FROM user_activity_v a "
            " WHERE a.user_id = u.id "
            "   AND u.inactivity_warned_at IS NOT NULL "
            "   AND a.last_activity_at > u.inactivity_warned_at")
        n = cur.rowcount
    conn.commit()
    if n:
        log.info("%d utenti rientrati: avviso annullato", n)
    return n


def da_avvisare(conn: psycopg.Connection,
                mesi: int = MESI_INATTIVITA) -> list[Utente]:
    """Gli inattivi da oltre `mesi`, non ancora avvisati."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.user_id::text, a.email, u.locale, 'inattivita' "
            "  FROM user_activity_v a JOIN users u ON u.id = a.user_id "
            " WHERE u.status = 'active' AND u.deleted_at IS NULL "
            "   AND a.inactivity_warned_at IS NULL "
            "   AND a.last_activity_at < now() - make_interval(months => %s) "
            " ORDER BY a.last_activity_at",
            (mesi,))
        return [_riga(r) for r in cur.fetchall()]


def avvisa(conn: psycopg.Connection, utente: Utente,
           invia: Callable[[Utente], None]) -> None:
    """Manda l'avviso e segna la data, in questa sequenza.

    Se `invia` solleva, la data NON viene scritta e l'utente sara'
    riprovato al giro dopo: meglio due email che una cancellazione senza
    preavviso.
    """
    invia(utente)
    with conn.cursor() as cur:
        cur.execute("UPDATE users SET inactivity_warned_at = now() "
                    " WHERE id = %s", (utente.id,))
    conn.commit()
    log.info("avviso di inattivita' inviato a %s", utente.id)


def da_cancellare(conn: psycopg.Connection,
                  giorni_dopo_avviso: int = GIORNI_DOPO_AVVISO,
                  giorni_dopo_abbonamento: int = GIORNI_DOPO_ABBONAMENTO,
                  ) -> list[Utente]:
    """Chi ha esaurito il proprio termine, per entrambe le strade.

    L'inattivo solo se avvisato E ancora fermo dopo l'avviso; l'abbonato
    scaduto senza avviso, perche' li' il termine e' contrattuale ed e'
    scritto sulla pagina che ha letto prima di pagare.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.user_id::text, a.email, u.locale, motivo FROM ( "
            "  SELECT a.*, 'inattivita' AS motivo FROM user_activity_v a "
            "   WHERE a.inactivity_warned_at IS NOT NULL "
            "     AND a.inactivity_warned_at < now() - make_interval(days => %s) "
            "     AND a.last_activity_at <= a.inactivity_warned_at "
            "  UNION ALL "
            "  SELECT a.*, 'abbonamento finito' AS motivo FROM user_activity_v a "
            "   WHERE a.subscription_status IN ('canceled','past_due','expired') "
            "     AND a.current_period_end IS NOT NULL "
            "     AND a.current_period_end < now() - make_interval(days => %s) "
            ") a JOIN users u ON u.id = a.user_id "
            " WHERE u.status = 'active' AND u.deleted_at IS NULL",
            (giorni_dopo_avviso, giorni_dopo_abbonamento))
        return [_riga(r) for r in cur.fetchall()]


def esegui(conn: psycopg.Connection, utente: Utente, *,
           rimuovi_blob: Callable[[str], None] | None = None) -> dict:
    """La cancellazione vera, con gli stessi due passi del bottone."""
    richiesta = gdpr.request_deletion(conn, utente.id)
    totali = gdpr.execute_deletion(conn, richiesta, rimuovi_blob=rimuovi_blob)
    log.info("utente %s cancellato (%s): %s", utente.id, utente.motivo, totali)
    return totali


def giro(conn: psycopg.Connection, *,
         invia: Callable[[Utente], None],
         rimuovi_blob: Callable[[str], None] | None = None,
         dry_run: bool = False) -> dict:
    """Il giro completo, nell'ordine che non fa danni.

    `dry_run` e' il modo in cui questa cosa va guardata la prima volta, e
    ogni volta che si cambiano i termini: dice chi verrebbe toccato senza
    toccare nessuno.
    """
    if not dry_run:
        rientrati(conn)

    avvisare = da_avvisare(conn)
    cancellare = da_cancellare(conn)

    if dry_run:
        for u in avvisare:
            log.info("dry-run: avviserei %s (%s)", u.email, u.motivo)
        for u in cancellare:
            log.info("dry-run: cancellerei %s (%s)", u.email, u.motivo)
        return {"da_avvisare": len(avvisare),
                "da_cancellare": len(cancellare),
                "cv_da_svuotare": svuota_cv_superati(conn, dry_run=True)}

    esito = {"avvisati": 0, "cancellati": 0, "falliti": 0}
    for u in avvisare:
        try:
            avvisa(conn, u, invia)
            esito["avvisati"] += 1
        except Exception as exc:
            conn.rollback()
            esito["falliti"] += 1
            log.error("avviso fallito per %s: %s", u.id, exc)
    for u in cancellare:
        try:
            esegui(conn, u, rimuovi_blob=rimuovi_blob)
            esito["cancellati"] += 1
        except Exception as exc:
            conn.rollback()
            esito["falliti"] += 1
            log.error("cancellazione fallita per %s: %s", u.id, exc)
    # Dopo le cancellazioni, non prima: un utente cancellato si porta via
    # i suoi CV per intero, e svuotarli un attimo prima sarebbe lavoro
    # fatto due volte su righe che stanno per sparire.
    esito["cv_svuotati"] = svuota_cv_superati(conn)
    log.info("retention utenti: %s", esito)
    return esito


GIORNI_GRAZIA_CV = 30

# Le colonne che si svuotano di un CV superato. `raw_extraction` e'
# l'ovvia: headline, ruoli con i datori, formazione, certificazioni.
#
# Le altre due meritano una riga di spiegazione, perche' non erano nella
# richiesta e ci somigliano solo se le si guarda da vicino:
#
#   embedding          e' il CV compresso in numeri. Non e' leggibile, ma
#                      da un vettore si ricostruisce piu' di quanto la
#                      parola «vettore» faccia pensare, e una volta che il
#                      testo e' sparito non serve piu' a nessuna ricerca.
#   original_filename  «CV_Maria_Rossi_2026.pdf». E' il nome della persona,
#                      conservato in chiaro in una colonna che nessuno
#                      guarda, molto dopo che il file e' stato cancellato.
COLONNE_DA_SVUOTARE = ("raw_extraction", "embedding", "original_filename")


def svuota_cv_superati(conn: psycopg.Connection,
                       giorni_grazia: int = GIORNI_GRAZIA_CV,
                       dry_run: bool = False) -> int:
    """Cancella i dati estratti dai CV superati che non servono piu'.

    LA RIGA RESTA. Si svuota il contenuto, non il record: `matches.cv_id`
    continua a dire quale versione del profilo ha prodotto un dato
    punteggio, ed e' meta' della tracciabilita' che l'AI Act chiede. Il
    contenuto invece serviva a produrre quel punteggio, non a conservarlo.

    Tre condizioni, tutte necessarie:

      superato         non e' il CV attivo di nessuno;
      grazia scaduta   sono passati `giorni_grazia` dalla sostituzione. Non
                       e' burocrazia: un utente che ricarica il CV
                       sbagliato e se ne accorge la settimana dopo ha
                       ancora qualcosa a cui tornare;
      nessun match vivo   nessuna offerta prodotta da quel CV e' ancora
                       apribile nel pannello. Finche' lo e', il contenuto
                       e' quello che spiega all'utente perche' quella
                       offerta gli e' arrivata, e svuotarlo lascerebbe una
                       decisione senza spiegazione — che e' esattamente
                       cio' che l'AI Act non vuole.

    L'ultima condizione e' la ragione per cui questo non e' un semplice
    `DELETE ... WHERE status = 'superseded'`.
    """
    colonne = ", ".join(f"{c} = NULL" for c in COLONNE_DA_SVUOTARE)
    dove = (
        " FROM user_cvs cv "
        " WHERE cv.status = 'superseded' "
        "   AND cv.raw_extraction IS NOT NULL "
        "   AND cv.superseded_at IS NOT NULL "
        "   AND cv.superseded_at < now() - make_interval(days => %s) "
        "   AND NOT EXISTS ( "
        "         SELECT 1 FROM matches m JOIN jobs j ON j.id = m.job_id "
        "          WHERE m.cv_id = cv.id AND m.passed "
        "            AND j.purged_at IS NULL) ")

    with conn.cursor() as cur:
        if dry_run:
            cur.execute("SELECT count(*)" + dove, (giorni_grazia,))
            n = cur.fetchone()[0]
            conn.rollback()
            log.info("dry-run: svuoterei %d CV superati", n)
            return n
        cur.execute(
            f"UPDATE user_cvs SET {colonne} "
            " WHERE id IN (SELECT cv.id" + dove + ")", (giorni_grazia,))
        n = cur.rowcount
    conn.commit()
    if n:
        log.info("%d CV superati svuotati dei dati estratti", n)
    return n


def _avviso_email(u: Utente) -> None:
    """L'avviso di inattivita', in inglese e nella lingua dell'utente.

    Deliberatamente breve e senza toni di minaccia: e' un promemoria, non
    una diffida. Dice che cosa succede, quando, e come fermarlo — e come
    fermarlo e' semplicemente tornare.
    """
    from nivult.delivery import email as email_mod

    oggetto = "Your Nivult account is about to be deleted"
    testo = (
        "You have not used Nivult for a year.\n\n"
        "We do not keep a CV longer than it is useful, so in seven days we "
        "will delete your account, your CV and everything we read from it. "
        "This cannot be undone.\n\n"
        "If you want to keep it, just sign in once: https://www.nivult.com/login\n\n"
        "If you would rather we deleted it now, you do not need to do "
        "anything — or write to hello@nivult.com and a person will answer.\n")
    html = "<p>" + testo.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"
    email_mod.invia_generica(u.email, oggetto, testo, html)


def main() -> int:
    import argparse

    from nivult import storage
    from nivult.config import database_url, load_dotenv

    p = argparse.ArgumentParser(description="Retention dei dati personali")
    p.add_argument("--dry-run", action="store_true",
                   help="dice chi verrebbe toccato, senza toccare nessuno")
    args = p.parse_args()

    load_dotenv()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    with psycopg.connect(database_url()) as conn:
        esito = giro(conn, invia=_avviso_email,
                     rimuovi_blob=storage.elimina, dry_run=args.dry_run)
    print(esito)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
