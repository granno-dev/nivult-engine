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

# nivult-classifica non c'e' piu': il classificatore gira sull'operaio N5
# (dal 2026-09-06), e il suo battito lo dice il dato, non systemd
DEMONI = ["nivult-scrape", "nivult-scrape-veloce", "nivult-profonda",
          "nivult-scoperta", "nivult-arricchisci",
          "nivult-volano", "nivult-certificati", "nivult-api"]


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
        # a parametri, non a URL: una password dentro una f-string e'
        # indistinguibile da una in chiaro per chi scansiona i segreti
        with psycopg.connect(
                host="127.0.0.1", port=5432, user="nivult",
                password=_env().get("POSTGRES_PASSWORD", ""),
                dbname="nivult_ats", connect_timeout=10) as c:
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
            # completezza delle offerte NUOVE: il prodotto e' il match
            # sul testo — se le nuove arrivano spoglie, e' un guasto.
            # Si giudicano solo quelle con ALMENO 6 ORE di vita: la
            # catena di arricchimento ha il diritto di lavorare prima
            # che suoni l'allarme (il primo giro, senza maturita',
            # sparava sull'appena-nato non ancora arricchito).
            # created_at esiste dal 04/09/2026 sera: lo spartiacque
            # tiene fuori il backfill del default.
            # Si giudicano le offerte NUOVE per posted_at (la data vera
            # dell'annuncio), non per created_at: quest'ultimo e' stato
            # inquinato dal fix del backfill (righe senza posted_at
            # finite su fetched_at, che e' sempre recente) e faceva
            # sembrare "nuove" 227k offerte vecchie e spoglie.
            tot, con_d, con_p = c.execute("""SELECT count(*),
                count(*) FILTER (WHERE raw ?| array['description',
                    'descriptionHtml','descriptionPlain','jobDescription',
                    'job_description','content','externalDescription']),
                count(country)
                FROM ats_jobs
               WHERE posted_at > now() - interval '30 hours'
                 AND posted_at < now() - interval '6 hours'
                 -- solo le DAVVERO nuove: i timbri prima-vista
                 -- (posted_at = created_at) non sono pubblicazioni reali
                 -- e falsavano la completezza (67%% della finestra)
                 AND NOT coalesce(posted_at_estimated, false)""").fetchone()
            if tot and tot >= 2000:
                if con_d * 100 < tot * 40:
                    problemi.append(
                        f"nuove offerte quasi senza descrizione: "
                        f"{100*con_d//tot}% su {tot} nelle 24h")
                if con_p * 100 < tot * 70:
                    problemi.append(
                        f"nuove offerte quasi senza paese: "
                        f"{100*con_p//tot}% su {tot} nelle 24h")
    except Exception as exc:                          # noqa: BLE001
        problemi.append(f"database ATS irraggiungibile: {exc!r}"[:160])

    # 2-ter. scadenze di massa: il 2026-09-06 la regola «pubblicata da
    # piu' di 90 giorni» ha marcato scadute 163.710 offerte vive in
    # un'ora, e nessun controllo l'ha visto: Giuseppe ha notato il
    # numero delle attive scendere sul cruscotto. Piu' del 3% delle
    # attive scadute in due ore non e' fisiologia, e' un guasto.
    try:
        import psycopg
        with psycopg.connect(
                host="127.0.0.1", port=5432, user="nivult",
                password=_env().get("POSTGRES_PASSWORD", ""),
                dbname="nivult_ats", connect_timeout=10) as c:
            # conta le scadute che erano state VISTE negli ultimi 3 giorni:
            # un'offerta assente da tre giorni che scade e' fisiologia; una
            # vista ieri che scade e' un guasto (successo due volte il 06/09)
            attive, scadute_2h = c.execute(
                "SELECT count(*) FILTER (WHERE expired_at IS NULL), "
                "count(*) FILTER (WHERE expired_at > now() - interval '2 hours' "
                "                 AND fetched_at > now() - interval '3 days') "
                "FROM ats_jobs").fetchone()
            if attive and scadute_2h * 100 > attive * 1:
                # testo STABILE (niente numeri): il conteggio cambia a ogni corsa
                # e ogni testo nuovo e' una mail nuova — valanga del 06/09 sera
                problemi.append("scadenze anomale: oltre l'1% delle attive, viste di recente, "
                                "marcate scadute nelle ultime 2h (dettaglio sul cruscotto)")
    except Exception:                                 # noqa: BLE001
        pass

    # 2-quater. lo sprint GLM: se l'unita' e' giu' ma la coda non e' vuota e
    # il log non dice FINE/credito, e' morto per un errore: il pronto
    # soccorso lo rilancia (e' riprendibile). Nessun allarme a fine corsa.
    try:
        r = subprocess.run(["systemctl", "is-active", "nivult-sprint"], capture_output=True, text=True)
        if r.stdout.strip() != "active":
            coda = open("/opt/nivult/engine/logs/sprint-glm.log", errors="replace").read()[-1500:]
            fine = any(k in coda for k in ("FINE:", "FINITO", "1113", "credito"))
            import psycopg
            with psycopg.connect(host="127.0.0.1", port=5432, user="nivult",
                                 password=_env().get("POSTGRES_PASSWORD", ""),
                                 dbname="nivult_ats", connect_timeout=10) as c:
                if c.execute("SELECT to_regclass('sprint_coda')").fetchone()[0]:
                    n = c.execute("SELECT count(*) FROM sprint_coda").fetchone()[0]
                    if n > 1000 and not fine:
                        problemi.append(f"sprint fermo con {n} offerte in coda (non per fine o credito)")
    except Exception:                                 # noqa: BLE001
        pass

    # 2-bis. l'operaio a casa (N5): scrive un battito a ogni giro in
    # operaio_battiti. Se casa si spegne, l'arretrato aspetta senza che
    # nessuno se ne accorga: questo e' il "qualcuno".
    try:
        import psycopg
        with psycopg.connect(
                host="127.0.0.1", port=5432, user="nivult",
                password=_env().get("POSTGRES_PASSWORD", ""),
                dbname="nivult_ats", connect_timeout=10) as c:
            for nome, eta_s in c.execute(
                    "SELECT nome, extract(epoch FROM now()-battito) FROM operaio_battiti"):
                if eta_s > 2 * 3600:
                    problemi.append(f"operaio {nome} muto da {int(eta_s // 3600)}h "
                                    f"(N5 spento o Tailscale giu'?)")
    except Exception:                                 # noqa: BLE001
        pass                    # la tabella nasce col primo battito

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

    # 3-bis. la manutenzione notturna: un passo FALLITO merita una mail,
    # non solo una card gialla sul cruscotto. La data nel testo fa si'
    # che ogni corsa faccia allarme una volta sola, e il rientro scatti
    # alla prima corsa pulita.
    try:
        log_cron = open("/opt/nivult/engine/logs/ats-cron.log",
                        errors="replace").read()[-40000:]
        import re as _re
        avvii = _re.findall(r"=== ATS nightly (20\S+) ===", log_cron)
        blocco = log_cron.rsplit("=== ATS nightly 20", 1)[-1]
        if avvii and "completato" in blocco:
            passi = _re.findall(r"── ([^\n]+)\n\s+(ok|FALLITO)", blocco)
            falliti = [n_.split("(")[0].strip() for n_, esito in passi
                       if esito == "FALLITO"]
            if falliti:
                problemi.append(
                    f"manutenzione del {avvii[-1][:10]}: passi falliti "
                    f"({', '.join(falliti[:4])})")
    except OSError:
        pass

    # 3-ter. i passi diurni a cron: ciascuno scrive il suo log, e un
    # log fermo oltre il giro atteso vuol dire passo morto in silenzio
    # (successo: due passi "in coda" morti senza che nessuno lo vedesse).
    _PASSI = [("registri.log", 26), ("domini.log", 26),
              ("scheda-sito.log", 26), ("glm-extra.log", 26),
              ("lingue.log", 26), ("organico.log", 8 * 24)]
    for nome_log, ore in _PASSI:
        percorso = f"/opt/nivult/engine/logs/{nome_log}"
        try:
            eta_h = (time.time() - os.path.getmtime(percorso)) / 3600
            if eta_h > ore:
                problemi.append(f"passo {nome_log.split('.')[0]} fermo "
                                f"da {int(eta_h)}h (atteso ogni {ore}h)")
        except OSError:
            problemi.append(f"passo {nome_log.split('.')[0]}: mai partito"
                            " (log assente)")

    # 4. credito GLM: a zero si fermano digest e CV, in silenzio. La
    # sonda da 1 token costa nulla; a credito zero l'API risponde 429
    # con codice 1113 senza addebitare.
    chiave = _env().get("GLM_API_KEY")
    if chiave:
        try:
            import httpx
            r = httpx.post(
                "https://api.z.ai/api/paas/v4/chat/completions",
                headers={"Authorization": f"Bearer {chiave}"},
                json={"model": "glm-5.2", "max_tokens": 1,
                      "messages": [{"role": "user", "content": "ok"}]},
                timeout=20)
            if r.status_code == 429 and "1113" in r.text:
                problemi.append("credito GLM a ZERO: digest e CV fermi "
                                "- ricaricare su z.ai")
        except Exception:                             # noqa: BLE001
            pass                    # rete/timeout: non e' un allarme

    # 5. disco
    d = shutil.disk_usage("/")
    if d.free / d.total < 0.10:
        problemi.append(f"disco quasi pieno: {d.free // 2**30}GB liberi")

    # 6. il backup: lo stato lo scrive backup.sh (ok/failed + data). Il
    # 2026-09-06 il backup e' stato ucciso dal kernel alle 03:07 e l'ha
    # scoperto una persona leggendo il log alle 10: doveva dirlo lei.
    try:
        riga = open("/opt/nivult/backup-state").read().strip()
        esito, quando = riga.split("\t")[:2]
        eta_h = (time.time() - time.mktime(time.strptime(quando[:19], "%Y-%m-%dT%H:%M:%S"))) / 3600
        if esito != "ok":
            problemi.append(f"BACKUP FALLITO: {riga[:120]}")
        elif eta_h > 30:
            problemi.append(f"backup: ultimo ok {int(eta_h)}h fa (atteso ogni 24h)")
    except Exception as exc:                          # noqa: BLE001
        problemi.append(f"backup: stato illeggibile ({exc!r})"[:120])

    # 7. uccisioni per memoria nelle ultime 24h: il kernel non avvisa
    # nessuno, e un lotto morto a meta' notte sembra solo "non ha girato".
    try:
        out = subprocess.run(["journalctl", "-k", "--since", "-24h", "--no-pager", "-q"],
                             capture_output=True, text=True, timeout=20).stdout
        vittime = re.findall(r"Out of memory: Killed process \d+ \((\S+)\)", out)
        if vittime:
            # testo STABILE: il numero cambia a ogni corsa (la finestra di
            # 24h scorre) e ogni testo nuovo era una mail nuova — Giuseppe ne
            # ha ricevute tre per lo stesso guasto il 06/09. I nomi bastano.
            nomi = ", ".join(sorted(set(vittime))[:4])
            problemi.append(f"memoria esaurita nelle ultime 24h: il kernel ha ucciso {nomi}"
                            " (dettagli: journalctl -k)")
    except Exception:                                 # noqa: BLE001
        pass

    return problemi


def _chiave(problema: str) -> str:
    """Lo STESSO guasto con un numero diverso e' lo stesso guasto: «scadute
    59.608» e «scadute 53.122» non sono due mail. Il 06/09 Giuseppe ne ha
    ricevute una ogni 5 minuti perche' il conteggio cambiava a ogni corsa.
    La chiave dell'allarme e' il testo senza cifre."""
    return re.sub(r"\d+", "N", problema)


def main() -> int:
    problemi = _controlli()
    try:
        stato = json.load(open(STATO))
    except Exception:                                 # noqa: BLE001
        stato = {"attivi": {}, }
    # gli attivi sono indicizzati per chiave (testo senza cifre); gli stati
    # vecchi, indicizzati per testo, si convertono al volo
    attivi: dict = {_chiave(k): v for k, v in stato.get("attivi", {}).items()}
    adesso = time.time()

    # Un demone visto giu' UNA volta e' quasi sempre un riavvio (deploy,
    # systemctl restart): si tiene in sospeso e fa allarme solo se e'
    # giu' anche al giro dopo. Gli altri problemi non aspettano.
    sospetti_prima = {_chiave(s) for s in stato.get("sospetti", [])}
    sospetti_ora = [p for p in problemi
                    if p.startswith("demone ") and _chiave(p) not in sospetti_prima
                    and _chiave(p) not in attivi]
    problemi = [p for p in problemi if p not in sospetti_ora]

    nuovi = [p for p in problemi if _chiave(p) not in attivi]
    def _soglia_riallarme(problema: str) -> int:
        # la manutenzione puo' guarire SOLO alla corsa notturna
        # successiva: ricordarglielo ogni 6 ore e' spavento inutile
        # (successo: mail delle 14:40 per il fallito delle 04 gia' noto)
        if problema.startswith("manutenzione"):
            return 24 * 3600
        return RIALLARME_ORE * 3600

    persistenti = [p for p in problemi
                   if _chiave(p) in attivi
                   and adesso - attivi[_chiave(p)] > _soglia_riallarme(p)]
    chiavi_ora = {_chiave(p) for p in problemi}
    rientrati = [k for k in attivi if k not in chiavi_ora]

    if nuovi or persistenti:
        # il pronto soccorso cura i guasti NUOVI e noti, e racconta su Telegram
        from nivult.ats import pronto_soccorso
        fatte = pronto_soccorso.cura(nuovi)
        corpo = "Sentinella Nivult — problemi rilevati:\n\n" + \
            "\n".join(f"  • {p}" for p in problemi) + \
            ("\n\nPronto soccorso:\n" + "\n".join(f"  • {f}" for f in fatte) if fatte else "") + \
            "\n\nCruscotto: /cruscotto sul sito."
        pronto_soccorso.telegram("Sentinella Nivult", problemi, fatte)
        if _manda_mail(f"[Nivult] {len(problemi)} problema/i: "
                       f"{problemi[0][:60]}", corpo):
            for p in nuovi + persistenti:
                attivi[_chiave(p)] = adesso
    if rientrati and not problemi:
        from nivult.ats import pronto_soccorso
        pronto_soccorso.telegram("Sentinella Nivult: tutto ok", [], [], rientrati)
        _manda_mail("[Nivult] rientrato: tutto ok",
                    "Problemi rientrati:\n" +
                    "\n".join(f"  • {k}" for k in rientrati))
    for k in rientrati:
        attivi.pop(k, None)

    with open(STATO + ".tmp", "w") as f:
        json.dump({"attivi": attivi, "sospetti": sospetti_ora}, f)
    os.replace(STATO + ".tmp", STATO)
    if problemi:
        print("PROBLEMI:", "; ".join(problemi))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
