"""La sentinella, seconda versione: GESTIONE DI INCIDENTI, non lista di
sintomi. Riprogettata il 2026-09-06 sera su richiesta di Giuseppe
(«ricevo notifiche, poi mi dici che va tutto bene e devo aspettare che
sparisca da sola; se intervieni, l'allarme deve sparire»).

Principi:
  1. Ogni controllo misura IL PRESENTE: «e' rotto adesso?». Niente finestre
     di 24 ore che restano accese a guasto risolto.
  2. Ogni incidente ha una scheda nel database (`incidenti`): chiave stabile,
     gravita', stato (sospetto / aperto / in_cura / risolto), chi l'ha
     risolto, quando. Il cruscotto la legge; a risolto, sparisce dal
     riquadro rosso e resta nello storico.
  3. Un incidente = un messaggio Telegram, che viene MODIFICATO quando si
     risolve. Niente «rientrato» separati.
  4. Tre gravita': critica (Telegram + mail subito), avviso (Telegram),
     info (solo cruscotto + riepilogo delle 07:00).
  5. Dopo una cura (pronto soccorso, medico, o Giuseppe che rilancia la
     sentinella) i controlli si rifanno nello stesso giro: se la
     condizione e' sparita, l'incidente si chiude subito.
  6. Un demone visto giu' una volta e' un sospetto; al secondo giro
     consecutivo diventa incidente (i riavvii da deploy non fanno rumore).

Gira da cron ogni 5 minuti come root. Log: /var/log/nivult-sentinella.log.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import smtplib
import subprocess
import time
from dataclasses import dataclass
from email.mime.text import MIMEText

ENV = "/opt/nivult/.env"
DESTINATARIO = "g.ranno@outlook.com"
ESCALAZIONE_ORE = 6          # un critico ancora aperto dopo 6h: un solo richiamo

DEMONI = ["nivult-scrape", "nivult-scrape-veloce", "nivult-profonda",
          "nivult-scoperta", "nivult-arricchisci",
          "nivult-volano", "nivult-certificati", "nivult-api"]
BOLLINO = {"critica": "🔴", "avviso": "🟠", "info": "⚪"}


@dataclass
class Condizione:
    chiave: str          # stabile: senza numeri
    gravita: str         # critica | avviso | info
    titolo: str
    dettaglio: str = ""
    grazia: bool = False # vero = serve la conferma al giro dopo (demoni)


def _env() -> dict:
    out = {}
    for f in (ENV, "/opt/nivult/engine/.env"):
        try:
            for riga in open(f):
                m = re.match(r"^([A-Z_]+)=(.*)$", riga.strip())
                if m and m.group(1) not in out:
                    out[m.group(1)] = m.group(2)
        except OSError:
            pass
    return out


def _db():
    import psycopg
    return psycopg.connect(host="127.0.0.1", port=5432, user="nivult",
                           password=_env().get("POSTGRES_PASSWORD", ""),
                           dbname="nivult_ats", connect_timeout=10, autocommit=True)


def _manda_mail(oggetto: str, corpo: str) -> bool:
    e = _env()
    host, porta = e.get("SMTP_HOST"), int(e.get("SMTP_PORT") or 587)
    utente, password = e.get("SMTP_USER"), e.get("SMTP_PASSWORD")
    if not (host and utente and password):
        return False
    msg = MIMEText(corpo, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = oggetto, e.get("SMTP_FROM") or utente, DESTINATARIO
    try:
        s = smtplib.SMTP_SSL(host, porta, timeout=20) if porta == 465 else smtplib.SMTP(host, porta, timeout=20)
        if porta != 465:
            s.starttls()
        s.login(utente, password); s.sendmail(utente, [DESTINATARIO], msg.as_string()); s.quit()
        return True
    except Exception:                                # noqa: BLE001
        return False


# ── i controlli: ognuno risponde «e' rotto ADESSO?» ────────────────────

def _controlli() -> list[Condizione]:
    c: list[Condizione] = []
    sub = lambda cmd, t=10: subprocess.run(cmd, capture_output=True, text=True, timeout=t).stdout.strip()

    # demoni (con grazia: un riavvio da deploy non e' un incidente)
    for d in DEMONI:
        st = sub(["systemctl", "is-active", d])
        if st != "active":
            c.append(Condizione(f"demone {d}", "critica" if d == "nivult-api" else "avviso",
                                f"demone {d} giu'", f"stato systemd: {st or 'assente'}", grazia=True))

    # il dato
    try:
        with _db() as db:
            eta = db.execute("SELECT extract(epoch FROM now()-max(fetched_at)) FROM ats_jobs").fetchone()[0]
            if eta is None or eta > 1800:
                c.append(Condizione("scrape fermo", "critica", "raccolta ferma",
                                    f"ultima offerta raccolta {int((eta or 0)//60)} min fa"))
            eta = db.execute("SELECT extract(epoch FROM now()-max(classified_at)) FROM job_classifications").fetchone()[0]
            if eta is None or eta > 7200:
                c.append(Condizione("classificatore fermo", "avviso", "classificazione ferma",
                                    f"ultima famiglia scritta {int((eta or 0)//60)} min fa (N5 o sprint)"))
            o24 = db.execute("""SELECT count(*) FROM ats_companies ac JOIN ats_platforms ap ON ap.id=ac.platform_id
                WHERE ac.is_active AND ap.is_active AND ac.last_fetch_at < now()-interval '24 hours'""").fetchone()[0]
            if o24 > 20000:
                c.append(Condizione("codone affamato", "info", "aziende non riviste da 24h",
                                    f"{o24} tenant oltre 24h"))
            # scadenze anomale: viste di recente E scadute negli ultimi 15 minuti
            attive, anom = db.execute("""SELECT count(*) FILTER (WHERE expired_at IS NULL),
                count(*) FILTER (WHERE expired_at > now()-interval '15 minutes' AND fetched_at > now()-interval '3 days')
                FROM ats_jobs""").fetchone()
            if attive and anom * 1000 > attive * 3:        # oltre lo 0,3% in un quarto d'ora
                c.append(Condizione("scadenze anomale", "critica", "scadenze anomale in corso",
                                    f"{anom} offerte viste di recente scadute negli ultimi 15 min ({100*anom/attive:.1f}% delle attive)"))
            # completezza delle nuove (info: si giudica al riepilogo, non di notte)
            tot, con_d, con_p = db.execute("""SELECT count(*),
                count(*) FILTER (WHERE raw ?| array['description','descriptionHtml','descriptionPlain','jobDescription','job_description','content','externalDescription']),
                count(country) FROM ats_jobs
                WHERE posted_at > now()-interval '30 hours' AND posted_at < now()-interval '6 hours'
                  AND NOT coalesce(posted_at_estimated,false)""").fetchone()
            if tot and tot >= 2000 and con_d * 100 < tot * 40:
                c.append(Condizione("nuove senza descrizione", "info", "nuove offerte quasi senza descrizione",
                                    f"{100*con_d//tot}% su {tot} nelle 24h"))
            if tot and tot >= 2000 and con_p * 100 < tot * 70:
                c.append(Condizione("nuove senza paese", "info", "nuove offerte quasi senza paese",
                                    f"{100*con_p//tot}% su {tot} nelle 24h"))
            # l'operaio a casa
            for nome, eta_s in db.execute("SELECT nome, extract(epoch FROM now()-battito) FROM operaio_battiti"):
                if eta_s > 2 * 3600:
                    c.append(Condizione(f"operaio {nome} muto", "avviso", f"operaio {nome} muto",
                                        f"ultimo battito {int(eta_s//3600)}h fa: N5 spento, Tailscale giu' o ciclo bloccato"))
            # lo sprint: unita' giu' con coda piena e non per fine/credito
            if sub(["systemctl", "is-active", "nivult-sprint"]) != "active" \
                    and db.execute("SELECT to_regclass('sprint_coda')").fetchone()[0]:
                n = db.execute("SELECT count(*) FROM sprint_coda").fetchone()[0]
                coda = open("/opt/nivult/engine/logs/sprint-glm.log", errors="replace").read()[-1500:]
                fine = any(k in coda for k in ("FINE:", "FINITO", '"1113"', "Insufficient balance"))
                if n > 1000 and not fine:
                    c.append(Condizione("sprint fermo", "avviso", "sprint GLM morto con coda piena",
                                        f"{n} offerte in coda; ultima riga: {coda.strip().splitlines()[-1][:100] if coda.strip() else '-'}"))
    except Exception as exc:                          # noqa: BLE001
        c.append(Condizione("database ATS", "critica", "database delle offerte irraggiungibile", repr(exc)[:160]))

    # il ponte
    try:
        log = "/var/log/nivult-ponte-ats.log"
        eta = time.time() - os.path.getmtime(log)
        if eta > 5400:
            c.append(Condizione("ponte fermo", "avviso", "ponte verso il motore fermo", f"log vecchio di {int(eta//60)} min"))
        else:
            ultima = open(log, errors="replace").read()[-4000:].rsplit("=== fine ===", 1)[-1]
            if "ERRORE" in ultima:
                c.append(Condizione("ponte errore", "avviso", "ponte in ERRORE nell'ultima corsa", ""))
    except OSError:
        pass

    # manutenzione notturna: passi falliti (info: si sistema di giorno)
    try:
        lc = open("/opt/nivult/engine/logs/ats-cron.log", errors="replace").read()[-40000:]
        avvii = re.findall(r"=== ATS nightly (20\S+) ===", lc)
        blocco = lc.rsplit("=== ATS nightly 20", 1)[-1]
        if avvii and "completato" in blocco:
            falliti = [n.split("(")[0].strip() for n, e in re.findall(r"── ([^\n]+)\n\s+(ok|FALLITO)", blocco) if e == "FALLITO"]
            if falliti and avvii[-1][:10] == time.strftime("%Y-%m-%d"):
                c.append(Condizione("manutenzione", "info", "passi falliti nella manutenzione notturna", ", ".join(falliti[:5])))
    except OSError:
        pass

    # passi diurni fermi (info)
    for nome_log, ore in (("registri", 26), ("domini", 26), ("scheda-sito", 26), ("lingue", 26), ("organico", 8 * 24)):
        p = f"/opt/nivult/engine/logs/{nome_log}.log"
        try:
            eta_h = (time.time() - os.path.getmtime(p)) / 3600
            if eta_h > ore:
                c.append(Condizione(f"passo {nome_log}", "info", f"passo diurno {nome_log} fermo", f"da {int(eta_h)}h (atteso ogni {ore}h)"))
        except OSError:
            c.append(Condizione(f"passo {nome_log}", "info", f"passo diurno {nome_log} mai partito", "log assente"))

    # credito GLM
    chiave = _env().get("GLM_API_KEY")
    if chiave:
        try:
            import httpx
            r = httpx.post("https://api.z.ai/api/paas/v4/chat/completions",
                           headers={"Authorization": f"Bearer {chiave}"},
                           json={"model": "glm-5.2", "max_tokens": 1, "messages": [{"role": "user", "content": "ok"}]}, timeout=20)
            if r.status_code == 429 and '"1113"' in r.text:
                c.append(Condizione("credito GLM", "critica", "credito GLM a zero", "digest e CV fermi: ricaricare su z.ai"))
        except Exception:                             # noqa: BLE001
            pass

    # l'API risponde DAVVERO? (non «il servizio e' acceso»)
    try:
        import httpx
        t0 = time.time()
        r = httpx.get("https://api.nivult.com/cruscotto", timeout=15, follow_redirects=True)
        ms = int((time.time() - t0) * 1000)
        if r.status_code >= 500:
            c.append(Condizione("api errore", "critica", "l'API risponde con errore", f"HTTP {r.status_code} in {ms} ms"))
        elif ms > 5000:
            c.append(Condizione("api lenta", "avviso", "l'API risponde lentamente", f"{ms} ms per /cruscotto"))
    except Exception as exc:                          # noqa: BLE001
        c.append(Condizione("api giu", "critica", "l'API non risponde", repr(exc)[:120]))
    # certificato HTTPS
    try:
        out = subprocess.run("echo | openssl s_client -servername api.nivult.com -connect api.nivult.com:443 2>/dev/null | openssl x509 -noout -enddate",
                             shell=True, capture_output=True, text=True, timeout=20).stdout.strip()
        m = re.search(r"notAfter=(.+)", out)
        if m:
            import email.utils
            scad = time.mktime(time.strptime(m.group(1).strip(), "%b %d %H:%M:%S %Y %Z"))
            giorni = (scad - time.time()) / 86400
            if giorni < 10:
                c.append(Condizione("certificato", "critica" if giorni < 3 else "avviso",
                                    "certificato HTTPS in scadenza", f"api.nivult.com scade fra {int(giorni)} giorni"))
    except Exception:                                 # noqa: BLE001
        pass
    # i digest del motore (il prodotto): falliti nelle ultime 24h
    try:
        import psycopg
        url = _env().get("DATABASE_URL")
        if url:
            with psycopg.connect(url, connect_timeout=10) as m_:
                falliti = m_.execute("SELECT count(*) FROM digests WHERE started_at > now()-interval '24 hours' AND status='failed'").fetchone()[0]
                if falliti:
                    c.append(Condizione("digest falliti", "critica", "digest falliti", f"{falliti} nelle ultime 24h (utenti senza consegna)"))
    except Exception:                                 # noqa: BLE001
        pass
    # temperatura del N5, dal suo battito
    try:
        with _db() as db:
            nota = db.execute("SELECT note FROM operaio_battiti WHERE nome='n5' AND battito > now()-interval '30 minutes'").fetchone()
            if nota and nota[0]:
                t_ = json.loads(nota[0]).get("temp_c")
                if t_ and t_ >= 92:
                    c.append(Condizione("n5 caldo", "avviso", "N5 troppo caldo", f"{t_} °C: abbassare i core (operaio-n5.sh --cpus)"))
    except Exception:                                 # noqa: BLE001
        pass
    # scadenze di massa per VOLUME, a prescindere da quando furono viste:
    # il controllo «viste di recente» qui sopra non puo' vedere un lotto
    # scaricato 3 giorni fa e mai rivisto (JazzHR, 33.344 in un'ora, 06/09)
    try:
        with _db() as db:
            att_, m15 = db.execute("""SELECT count(*) FILTER (WHERE expired_at IS NULL),
                count(*) FILTER (WHERE expired_at > now()-interval '15 minutes') FROM ats_jobs""").fetchone()
            if att_ and m15 > max(2000, att_ * 0.005):
                c.append(Condizione("scadenze di massa", "avviso", "scadenze di massa negli ultimi 15 minuti",
                                    f"{m15:,} scadute ({100.0*m15/att_:.1f}% delle attive) — le pagine sono vive?".replace(",", ".")))
    except Exception:                                 # noqa: BLE001
        pass
    # ADAPTER ROTTO: i canarini (3 tenant di riferimento per piattaforma,
    # ogni ora) danno tutti zero. Il file lo scrive nivult.ats.canarini.
    try:
        cf = "/opt/nivult/canarini.json"
        if time.time() - os.path.getmtime(cf) < 2 * 3600:
            for r_ in json.load(open(cf)).get("rotte", []):
                pid = r_["piattaforma"]
                att = ", ".join(f"{k['slug']} (attese {k['attese']})" for k in r_["canarini"][:3])
                c.append(Condizione(f"adapter rotto {pid}", "avviso", f"adapter rotto: {pid}",
                                    f"tutti i canarini a zero: {att}"))
    except (OSError, ValueError, KeyError):
        pass
    # ADAPTER MUTO: nell'ultima ora l'adapter ha detto «zero» su tenant le
    # cui offerte stanno ANCORA nella pagina (letture_sospette, scritte
    # dal runner col ripiego). Dal 5 in su e almeno meta' delle visite.
    try:
        with _db() as db:
            for pid, sosp, vis in db.execute("""
                WITH s AS (SELECT platform_id, count(DISTINCT slug) n FROM letture_sospette
                            WHERE at > now()-interval '60 minutes' GROUP BY 1),
                     v AS (SELECT platform_id, count(*) n FROM ats_companies
                            WHERE last_fetch_at > now()-interval '60 minutes' AND job_count > 0 GROUP BY 1)
                SELECT s.platform_id, s.n, coalesce(v.n, 0) FROM s LEFT JOIN v USING (platform_id)
                 WHERE s.n >= 5 AND s.n >= coalesce(v.n, 0) * 0.5""").fetchall():
                c.append(Condizione(f"adapter muto {pid}", "avviso", f"adapter muto: {pid}",
                                    f"{sosp} tenant a zero nell'ultima ora con offerte ancora in pagina ({vis} letti bene)"))
    except Exception:                                 # noqa: BLE001
        pass
    # scadenze di massa RIFIUTATE da expira (adapter muto su una piattaforma):
    # il file lo scrive mantenimento.expira e lo toglie quando il rifiuto cessa
    try:
        rf = "/opt/nivult/expira-rifiutata.json"
        if time.time() - os.path.getmtime(rf) < 1800:
            d_ = json.load(open(rf))
            pz = ", ".join(f"{p['piattaforma']} ({p['scadrebbero']:,} di {p['attive']:,})".replace(",", ".")
                           for p in d_.get("piattaforme", []))
            c.append(Condizione("scadenze rifiutate", "avviso", "scadenze di massa rifiutate: adapter muto?",
                                f"{pz} — le pagine sono vive? l'adapter legge ancora il template?"))
    except (OSError, ValueError, KeyError):
        pass

    # errori 5xx dell'API negli ultimi 15 minuti
    try:
        out = subprocess.run(["journalctl", "-u", "nivult-api", "--since", "-15min", "--no-pager", "-q"],
                             capture_output=True, text=True, timeout=20).stdout
        n5xx = len(re.findall(r'" 5\d\d ', out))
        if n5xx >= 5:
            c.append(Condizione("api 5xx", "avviso", "errori 5xx dell'API", f"{n5xx} negli ultimi 15 minuti"))
    except Exception:                                 # noqa: BLE001
        pass

    # disco, memoria adesso, uccisioni recenti
    d = shutil.disk_usage("/")
    if d.free / d.total < 0.10:
        c.append(Condizione("disco", "avviso", "disco quasi pieno", f"{d.free // 2**30} GB liberi"))
    try:
        m = {}
        for riga in open("/proc/meminfo"):
            k, v = riga.split(":"); m[k] = int(v.split()[0])
        if m["MemAvailable"] < 700 * 1024:
            c.append(Condizione("memoria bassa", "avviso", "memoria quasi esaurita adesso", f"{m['MemAvailable']//1024} MB disponibili"))
    except Exception:                                 # noqa: BLE001
        pass
    try:
        out = subprocess.run(["journalctl", "-k", "--since", "-60min", "--no-pager", "-q"], capture_output=True, text=True, timeout=20).stdout
        vittime = re.findall(r"Killed process \d+ \((\S+)\)", out)
        if vittime:
            gravi = [v for v in vittime if v in ("postgres", "uvicorn", "python3")]
            c.append(Condizione("oom", "critica" if gravi else "avviso",
                                "processi uccisi per memoria nell'ultima ora", ", ".join(sorted(set(vittime))[:5])))
    except Exception:                                 # noqa: BLE001
        pass

    # backup
    try:
        riga = open("/opt/nivult/backup-state").read().strip()
        esito, quando = riga.split("\t")[:2]
        eta_h = (time.time() - time.mktime(time.strptime(quando[:19], "%Y-%m-%dT%H:%M:%S"))) / 3600
        if esito != "ok":
            c.append(Condizione("backup fallito", "critica", "backup fallito", riga[:140]))
        elif eta_h > 30:
            c.append(Condizione("backup vecchio", "avviso", "backup non fatto", f"ultimo ok {int(eta_h)}h fa"))
    except Exception as exc:                          # noqa: BLE001
        c.append(Condizione("backup stato", "avviso", "stato del backup illeggibile", repr(exc)[:100]))
    return c


# ── la scheda degli incidenti ──────────────────────────────────────────

def _prepara(db) -> None:
    """`IF NOT EXISTS` non protegge da due creazioni SIMULTANEE: Postgres
    alza una violazione di unicita' su `pg_type`, e la sentinella moriva
    lì — proprio nel giro in cui c'erano due esecuzioni sovrapposte, cioe'
    quando si sta lavorando sul server. Il guardiano non deve poter
    morire per una corsa fra due copie di se stesso."""
    import psycopg
    try:
        _crea(db)
    except psycopg.errors.UniqueViolation:
        pass


def _crea(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS incidenti (
        id bigserial PRIMARY KEY, chiave text NOT NULL, gravita text NOT NULL, titolo text NOT NULL,
        dettaglio text, stato text NOT NULL DEFAULT 'aperto',
        aperto_at timestamptz NOT NULL DEFAULT now(), aggiornato_at timestamptz NOT NULL DEFAULT now(),
        risolto_at timestamptz, risolto_da text, cura text, telegram_id text, escalato boolean NOT NULL DEFAULT false)""")
    db.execute("CREATE INDEX IF NOT EXISTS incidenti_aperti_idx ON incidenti (chiave) WHERE stato <> 'risolto'")
    db.execute("GRANT SELECT ON incidenti TO nivult_app")


def _telegram(titolo: str, righe: list[str]) -> str | None:
    from nivult.ats import pronto_soccorso as ps
    return ps.telegram_id(titolo, righe)


def _telegram_modifica(mid: str, titolo: str, righe: list[str]) -> None:
    from nivult.ats import pronto_soccorso as ps
    ps.telegram_modifica(mid, titolo, righe)


def _testo(inc: dict, condizione: Condizione | None = None) -> tuple[str, list[str]]:
    b = BOLLINO.get(inc["gravita"], "⚪")
    titolo = f"{b} {inc['titolo']}"
    righe = [inc["dettaglio"] or ""]
    if inc.get("cura"):
        righe.append(f"Fatto: {inc['cura']}")
    if inc["stato"] == "risolto":
        titolo = f"🟢 risolto · {inc['titolo']}"
        righe.append(f"risolto alle {inc['risolto_at'].strftime('%H:%M')} UTC da {inc['risolto_da']}")
    elif inc["stato"] == "in_cura":
        righe.append("in cura: il medico sta guardando")
    return titolo, [r for r in righe if r]


SILENZIO = "/opt/nivult/silenzio-fino"


def _in_silenzio() -> bool:
    """`/silenzio 30m` dal bot (o `runbook.sh silenzio 30`): finche' dura,
    gli avvisi e le info non aprono incidenti. Le critiche passano sempre:
    un silenzio che nasconde un database giu' non e' manutenzione."""
    try:
        return time.time() < float(open(SILENZIO).read().strip())
    except (OSError, ValueError):
        return False


def _battito(db) -> None:
    """La sentinella lascia il proprio battito: il guardiano sul N5 lo
    legge per accorgersi se e' LEI a essere morta (cron fermo, server giu')."""
    db.execute("INSERT INTO operaio_battiti (nome, battito, note) VALUES ('sentinella', now(), NULL) "
               "ON CONFLICT (nome) DO UPDATE SET battito = now()")


def main() -> int:
    from nivult.ats import pronto_soccorso as ps
    cond = {c.chiave: c for c in _controlli()}
    if _in_silenzio():
        cond = {k: c for k, c in cond.items() if c.gravita == "critica"}
    with _db() as db:
        _prepara(db)
        _battito(db)
        aperti = {r[0]: dict(zip(("chiave", "id", "gravita", "titolo", "dettaglio", "stato", "aperto_at", "telegram_id", "escalato", "cura"), r))
                  for r in db.execute("SELECT chiave, id, gravita, titolo, dettaglio, stato, aperto_at, telegram_id, escalato, cura "
                                      "FROM incidenti WHERE stato <> 'risolto'").fetchall()}
        nuovi_da_curare: list[tuple[Condizione, dict]] = []
        # 1. condizioni attive: aggiorna o apri
        for chiave, c in cond.items():
            inc = aperti.get(chiave)
            if inc:
                db.execute("UPDATE incidenti SET dettaglio=%s, aggiornato_at=now() WHERE id=%s", (c.dettaglio, inc["id"]))
                if inc["stato"] == "sospetto":
                    # seconda vista consecutiva: diventa un incidente vero
                    db.execute("UPDATE incidenti SET stato='aperto', aperto_at=now() WHERE id=%s", (inc["id"],))
                    inc.update(stato="aperto", dettaglio=c.dettaglio)
                    nuovi_da_curare.append((c, inc))
                elif inc["gravita"] == "critica" and not inc["escalato"] \
                        and (time.time() - inc["aperto_at"].timestamp()) > ESCALAZIONE_ORE * 3600:
                    db.execute("UPDATE incidenti SET escalato=true WHERE id=%s", (inc["id"],))
                    _telegram(f"⏰ ancora aperto da {ESCALAZIONE_ORE}h: {inc['titolo']}", [c.dettaglio, "serve una mano umana"])
                continue
            stato = "sospetto" if c.grazia else "aperto"
            rid = db.execute("INSERT INTO incidenti (chiave, gravita, titolo, dettaglio, stato) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                             (chiave, c.gravita, c.titolo, c.dettaglio, stato)).fetchone()[0]
            inc = {"id": rid, "chiave": chiave, "gravita": c.gravita, "titolo": c.titolo, "dettaglio": c.dettaglio,
                   "stato": stato, "telegram_id": None, "cura": None}
            aperti[chiave] = inc
            if stato == "aperto":
                nuovi_da_curare.append((c, inc))
        # 2. i nuovi: notifica, cura, medico
        for c, inc in nuovi_da_curare:
            if c.gravita in ("critica", "avviso"):
                mid = _telegram(*_testo(inc))
                if mid:
                    db.execute("UPDATE incidenti SET telegram_id=%s WHERE id=%s", (mid, inc["id"]))
                    inc["telegram_id"] = mid
            if c.gravita == "critica":
                _manda_mail(f"[Nivult] {c.titolo}", f"{c.titolo}\n\n{c.dettaglio}\n\nCruscotto: /cruscotto")
            testo_vecchio = c.chiave  # per il pronto soccorso, che ragiona sui prefissi
            fatte = ps.cura([_prefisso_cura(c)]) if ps.curabile(_prefisso_cura(c)) else []
            if fatte:
                db.execute("UPDATE incidenti SET cura=%s, stato='in_cura' WHERE id=%s", ("; ".join(fatte), inc["id"]))
                inc.update(cura="; ".join(fatte), stato="in_cura")
            elif c.gravita != "info" and ps.chiama_medico([f"{c.titolo}: {c.dettaglio}"]):
                db.execute("UPDATE incidenti SET cura=%s, stato='in_cura' WHERE id=%s", ("chiamato il medico", inc["id"]))
                inc.update(cura="chiamato il medico", stato="in_cura")
        # 3. dopo le cure: si ricontrolla SUBITO
        if any(inc["cura"] and not inc["cura"].startswith("chiamato") for _, inc in nuovi_da_curare):
            time.sleep(3)
            cond = {c.chiave: c for c in _controlli()}
            if _in_silenzio():
                cond = {k: c for k, c in cond.items() if c.gravita == "critica"}
        # 4. incidenti aperti la cui condizione e' sparita: risolti
        for chiave, inc in list(aperti.items()):
            if chiave in cond:
                continue
            if inc["stato"] == "sospetto":
                db.execute("DELETE FROM incidenti WHERE id=%s", (inc["id"],)); continue
            da = "pronto soccorso" if (inc.get("cura") and not inc["cura"].startswith("chiamato")) \
                else "rientro dopo la visita del medico" if inc.get("cura") else "rientro spontaneo"
            db.execute("UPDATE incidenti SET stato='risolto', risolto_at=now(), risolto_da=%s WHERE id=%s", (da, inc["id"]))
            r = db.execute("SELECT risolto_at FROM incidenti WHERE id=%s", (inc["id"],)).fetchone()
            inc.update(stato="risolto", risolto_at=r[0], risolto_da=da)
            if inc.get("telegram_id"):
                _telegram_modifica(inc["telegram_id"], *_testo(inc))
        # 5. il riepilogo delle 07:00 (una volta: il giro fra 07:00 e 07:05 UTC)
        t = time.gmtime()
        if t.tm_hour == 7 and t.tm_min < 5:
            ap = db.execute("SELECT gravita, titolo, dettaglio FROM incidenti WHERE stato <> 'risolto' ORDER BY gravita").fetchall()
            ri = db.execute("SELECT titolo, risolto_da FROM incidenti WHERE risolto_at > now()-interval '24 hours' ORDER BY risolto_at DESC LIMIT 12").fetchall()
            att = db.execute("SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL").fetchone()[0]
            righe = [f"offerte attive: {att:,}".replace(",", "."),
                     f"aperti: {len(ap)}" + ("".join(f"\n  {BOLLINO[g]} {ti} — {de}" for g, ti, de in ap) if ap else " — nessuno"),
                     f"risolti nelle 24h: {len(ri)}" + ("".join(f"\n  🟢 {ti} ({da})" for ti, da in ri) if ri else "")]
            _telegram("☀️ Riepilogo delle 7", righe)
        aperti_ora = [(i["gravita"], i["titolo"]) for i in aperti.values() if i["stato"] != "risolto"]
    print("INCIDENTI APERTI:", "; ".join(f"{BOLLINO[g]} {t}" for g, t in aperti_ora) if aperti_ora else "nessuno")
    return 0


def _prefisso_cura(c: Condizione) -> str:
    """Il pronto soccorso riconosce i guasti dal prefisso del vecchio testo:
    qui si traduce la chiave nuova in quel prefisso."""
    return {"scrape fermo": "scrape fermo", "sprint fermo": "sprint fermo",
            "backup fallito": "BACKUP FALLITO", "backup vecchio": "backup: vecchio",
            "ponte fermo": "ponte fermo", "ponte errore": "ponte in ERRORE"}.get(
        c.chiave, c.chiave + ":" if c.chiave.startswith("demone ") else c.chiave)


# le chiavi «adapter rotto <pid>» / «adapter muto <pid>» passano intere:
# il pronto soccorso ne estrae la piattaforma con un'espressione regolare


if __name__ == "__main__":
    raise SystemExit(main())
