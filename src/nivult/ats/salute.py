"""Il guardiano: controlla che il motore stia davvero girando, e avvisa.

Oggi il cron ha eseguito per giorni una copia vecchia dello script
notturno senza che nessuno se ne accorgesse — le agenzie non venivano
raccolte e tutto sembrava a posto. Questo modulo esiste perche' non
ricapiti: gira dopo il notturno (e ogni tot ore), controlla una lista
di condizioni, e se qualcosa e' rotto manda UNA email all'operatore.

Filosofia: silenzio = tutto bene. L'email arriva solo quando c'e' un
problema concreto, cosi' quando arriva la si legge davvero. Ogni
controllo e' una domanda a cui un «no» e' un guasto reale, non un
falso allarme rumoroso.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
from datetime import datetime, timezone

import psycopg

from .runner import ATS_DSN

log = logging.getLogger("nivult.ats.salute")

OPERATORE = os.environ.get("OPERATORE_EMAIL", "g.ranno@outlook.com")
MOTORE_DSN = os.environ.get(
    "DATABASE_URL", "postgresql://giusepperanno@127.0.0.1:5432/nivult")

# Soglie: sotto queste, e' un guasto. Larghe apposta — devono scattare
# su un crollo vero, non su una fluttuazione normale.
MIN_OFFERTE_ATTIVE = 50_000       # ne abbiamo 300k+: sotto 50k = disastro
MAX_QUOTA_SENZA_PAESE = 0.80      # se >80% e' senza paese, l'arricchimento e' morto
MAX_QUOTA_NON_CLASSIF = 0.85      # idem per il classificatore
ORE_TOLLERANZA = 30              # il notturno gira ogni 24h: 30h di margine


def _uno(dsn: str, sql: str):
    with psycopg.connect(dsn) as c:
        r = c.execute(sql).fetchone()
        return r[0] if r else None


def controlla() -> list[str]:
    """Ogni voce restituita e' un problema in chiaro. Lista vuota = sano."""
    problemi: list[str] = []

    # 1. Il bug di oggi: il cron esegue davvero il file aggiornato?
    link = "/opt/nivult/ats-nightly.sh"
    atteso = "/opt/nivult/engine/deploy/ats-nightly.sh"
    try:
        if os.path.realpath(link) != atteso:
            problemi.append(
                f"Il cron notturno punta a {os.path.realpath(link)}, non a "
                f"{atteso}: gli aggiornamenti al motore non vengono eseguiti.")
    except OSError as e:
        problemi.append(f"Non trovo lo script notturno {link}: {e}")

    # 2. I demoni systemd sono vivi?
    for servizio in ("nivult-scoperta", "nivult-api"):
        try:
            out = subprocess.run(
                ["systemctl", "is-active", servizio],
                capture_output=True, text=True, timeout=10).stdout.strip()
            if out != "active":
                problemi.append(f"Il servizio {servizio} non e' attivo "
                                f"(stato: {out or 'sconosciuto'}).")
        except (subprocess.SubprocessError, OSError) as e:
            problemi.append(f"Non riesco a interrogare {servizio}: {e}")

    # 3. Il notturno ha scrapato nelle ultime 30 ore?
    try:
        ultimo = _uno(ATS_DSN,
                      "SELECT max(fetched_at) FROM ats_jobs")
        if ultimo is None:
            problemi.append("Nessuna offerta ATS in banca: la raccolta non "
                            "ha mai girato.")
        else:
            eta = datetime.now(timezone.utc) - ultimo
            if eta.total_seconds() > ORE_TOLLERANZA * 3600:
                problemi.append(
                    f"L'ultima offerta ATS raccolta risale a {eta.days}g "
                    f"{eta.seconds // 3600}h fa: il notturno non gira.")
    except psycopg.Error as e:
        problemi.append(f"Database ATS irraggiungibile: {e}")

    # 4. Le offerte attive non sono crollate?
    try:
        attive = _uno(ATS_DSN,
                      "SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL")
        if attive is not None and attive < MIN_OFFERTE_ATTIVE:
            problemi.append(
                f"Solo {attive} offerte attive (soglia {MIN_OFFERTE_ATTIVE}): "
                "qualcosa ha svuotato il corpus.")
        # 5. La qualita' dei dati: paese e classificazione non esplose?
        if attive:
            senza_paese = _uno(ATS_DSN,
                "SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL "
                "AND country IS NULL")
            if senza_paese / attive > MAX_QUOTA_SENZA_PAESE:
                problemi.append(
                    f"{senza_paese} offerte su {attive} senza paese "
                    f"({senza_paese * 100 // attive}%): l'arricchimento "
                    "paese non tiene il passo.")
            non_cl = _uno(ATS_DSN,
                "SELECT count(*) FROM ats_jobs j LEFT JOIN job_classifications c "
                "ON c.job_id = j.id WHERE c.job_id IS NULL AND j.expired_at IS NULL")
            if non_cl / attive > MAX_QUOTA_NON_CLASSIF:
                problemi.append(
                    f"{non_cl} offerte su {attive} non classificate "
                    f"({non_cl * 100 // attive}%): il classificatore e' fermo.")
    except psycopg.Error as e:
        problemi.append(f"Non riesco a misurare la salute del corpus ATS: {e}")

    # 6. Il ponte ha portato offerte fresche nel motore?
    try:
        ponte_ultimo = _uno(MOTORE_DSN,
            "SELECT max(first_seen_at) FROM jobs WHERE source = 'ats'")
        if ponte_ultimo is not None:
            eta = datetime.now(timezone.utc) - ponte_ultimo
            if eta.total_seconds() > ORE_TOLLERANZA * 3600:
                problemi.append(
                    f"Il ponte non porta offerte nel motore da {eta.days}g "
                    f"{eta.seconds // 3600}h: i digest si stanno svuotando.")
    except psycopg.Error as e:
        problemi.append(f"Database del motore irraggiungibile: {e}")

    return problemi


def _avvisa(problemi: list[str]) -> None:
    righe = "\n".join(f"  - {p}" for p in problemi)
    testo = (f"Il guardiano di Nivult ha trovato {len(problemi)} "
             f"problema/i alle {datetime.now():%Y-%m-%d %H:%M}:\n\n{righe}\n\n"
             "Questa email arriva solo quando qualcosa e' rotto.")
    html = ("<h2>Nivult: controllo di salute</h2>"
            f"<p>{len(problemi)} problema/i alle "
            f"{datetime.now():%Y-%m-%d %H:%M}:</p><ul>"
            + "".join(f"<li>{p}</li>" for p in problemi)
            + "</ul><p style='color:#888'>Questa email arriva solo "
            "quando qualcosa e' rotto.</p>")
    try:
        from nivult.delivery.email import invia_generica
        invia_generica(OPERATORE, f"⚠ Nivult: {len(problemi)} problema/i "
                       "nel motore", testo, html)
        log.info("allarme inviato a %s", OPERATORE)
    except Exception as e:                       # noqa: BLE001
        # Se persino l'email non parte, almeno resti nel log del cron.
        log.error("ALLARME NON INVIABILE (%s). Problemi: %s", e, problemi)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    # Carica l'.env del motore: SMTP per l'allarme e i DSN. Senza,
    # l'email non partirebbe e l'allarme resterebbe muto.
    try:
        from nivult.config import load_dotenv
        load_dotenv()
    except Exception as e:  # noqa: BLE001
        log.warning("non ho caricato .env: %s", e)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sempre", action="store_true",
                    help="stampa l'esito anche quando e' tutto sano")
    ap.add_argument("--prova", action="store_true",
                    help="manda un'email di allarme di prova e esce")
    args = ap.parse_args()
    if args.prova:
        _avvisa(["Questo e' un test del guardiano: se leggi questa email, "
                 "il sistema di allarme funziona. Nessun problema reale."])
        return 0
    problemi = controlla()
    if problemi:
        for p in problemi:
            log.warning("PROBLEMA: %s", p)
        _avvisa(problemi)
        return 1
    if args.sempre:
        log.info("tutto sano: nessun problema rilevato")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
