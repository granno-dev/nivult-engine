"""La sentinella: se qualcosa muore in silenzio, arriva una mail.

Prima l'unico modo di accorgersi di un demone morto era aprire il
cruscotto. La sentinella gira da CRON ogni 5 minuti — fuori dai demoni,
apposta: un guardiano dentro il sistema muore col sistema — e controlla:

  1. gli otto demoni systemd siano `active`;
  2. i battiti nel dato: scrape (fetched_at fresco), classificatore,
     ponte (log recente e senza ERRORE);
  3. il disco non stia finendo;
  4. il codone non si stia affamando (oltre-24h sotto soglia).

Anti-rumore: lo stato degli allarmi vive in un file; un problema NUOVO
avvisa subito, uno gia' noto ri-avvisa ogni 6 ore, e al rientro parte il
«tutto ok». La mail usa l'SMTP gia' collaudato dai digest.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import smtplib
import subprocess
import time
from email.mime.text import MIMEText

STATO = "/opt/nivult/sentinella-stato.json"
ENV = "/opt/nivult/.env"
DESTINATARIO = "g.ranno@outlook.com"
RIALLARME_ORE = 6

DEMONI = ["nivult-scrape", "nivult-scrape-veloce", "nivult-profonda",
          "nivult-scoperta", "nivult-classifica", "nivult-arricchisci",
          "nivult-volano", "nivult-api"]


def _env() -> dict:
    out = {}
    try:
        for riga in open(ENV):
            m = re.match(r"^([A-Z_]+)=(.*)$", riga.strip())
            if m:
                out[m.group(1)] = m.group(2)
    except OSError:
        pass
    return out


def _manda_mail(oggetto: str, corpo: str) -> bool:
    e = _env()
    host, porta = e.get("SMTP_HOST"), int(e.get("SMTP_PORT") or 587)
    utente, password = e.get("SMTP_USER"), e.get("SMTP_PASSWORD")
    mittente = e.get("SMTP_FROM") or utente
    if not (host and utente and password):
        return False
    msg = MIMEText(corpo, "plain", "utf-8")
    msg["Subject"] = oggetto
    msg["From"] = mittente
    msg["To"] = DESTINATARIO
    try:
        if porta == 465:
            s = smtplib.SMTP_SSL(host, porta, timeout=20)
        else:
            s = smtplib.SMTP(host, porta, timeout=20)
            s.starttls()
        s.login(utente, password)
        s.sendmail(utente, [DESTINATARIO], msg.as_string())
        s.quit()
        return True
    except Exception:                                # noqa: BLE001
        return False


def _controlli() -> list[str]:
    problemi: list[str] = []

    # 1. demoni
    for d in DEMONI:
        r = subprocess.run(["systemctl", "is-active", d],
                           capture_output=True, text=True)
        if r.stdout.strip() != "active":
            problemi.append(f"demone {d}: {r.stdout.strip() or 'assente'}")

    # 2. battiti nel dato
    try:
        import psycopg
        pw = _env().get("POSTGRES_PASSWORD", "")
        with psycopg.connect(
                f"postgresql://nivult:{pw}@127.0.0.1:5432/nivult_ats",
                connect_timeout=10) as c:
            eta = c.execute("SELECT extract(epoch FROM now()-max(fetched_at))"
                            " FROM ats_jobs").fetchone()[0]
            if eta is None or eta > 1800:
                problemi.append(f"scrape fermo: ultimo fetch {int((eta or 0)//60)} min fa")
            eta = c.execute("SELECT extract(epoch FROM now()-max(classified_at))"
                            " FROM job_classifications").fetchone()[0]
            if eta is None or eta > 7200:
                problemi.append(f"classificatore fermo: {int((eta or 0)//60)} min fa")
            o24 = c.execute("""SELECT count(*) FROM ats_companies ac
                JOIN ats_platforms ap ON ap.id=ac.platform_id
                WHERE ac.is_active AND ap.is_active
                  AND ac.last_fetch_at < now()-interval '24 hours'""").fetchone()[0]
            if o24 > 20000:
                problemi.append(f"codone affamato: {o24} tenant oltre 24h")
    except Exception as exc:                          # noqa: BLE001
        problemi.append(f"database ATS irraggiungibile: {exc!r}"[:160])

    # 3. il ponte: log recente e senza errori nell'ultima corsa
    try:
        log = "/var/log/nivult-ponte-ats.log"
        eta = time.time() - os.path.getmtime(log)
        if eta > 5400:
            problemi.append(f"ponte fermo: log vecchio di {int(eta//60)} min")
        else:
            coda = open(log, errors="replace").read()[-4000:]
            ultima = coda.rsplit("=== fine ===", 1)[-1]
            if "ERRORE" in ultima:
                problemi.append("ponte in ERRORE nell'ultima corsa (vedi log)")
    except OSError:
        problemi.append("ponte: log non trovato")

    # 4. disco
    d = shutil.disk_usage("/")
    if d.free / d.total < 0.10:
        problemi.append(f"disco quasi pieno: {d.free // 2**30}GB liberi")

    return problemi


def main() -> int:
    problemi = _controlli()
    try:
        stato = json.load(open(STATO))
    except Exception:                                 # noqa: BLE001
        stato = {"attivi": {}, }
    attivi: dict = stato.get("attivi", {})
    adesso = time.time()

    nuovi = [p for p in problemi if p not in attivi]
    persistenti = [p for p in problemi
                   if p in attivi
                   and adesso - attivi[p] > RIALLARME_ORE * 3600]
    rientrati = [p for p in attivi if p not in problemi]

    if nuovi or persistenti:
        corpo = "Sentinella Nivult — problemi rilevati:\n\n" + \
            "\n".join(f"  • {p}" for p in problemi) + \
            "\n\nCruscotto: /cruscotto sul sito."
        if _manda_mail(f"[Nivult] {len(problemi)} problema/i: "
                       f"{problemi[0][:60]}", corpo):
            for p in nuovi + persistenti:
                attivi[p] = adesso
    if rientrati and not problemi:
        _manda_mail("[Nivult] rientrato: tutto ok",
                    "Problemi rientrati:\n" +
                    "\n".join(f"  • {p}" for p in rientrati))
    for p in rientrati:
        attivi.pop(p, None)

    json.dump({"attivi": attivi}, open(STATO, "w"))
    if problemi:
        print("PROBLEMI:", "; ".join(problemi))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
