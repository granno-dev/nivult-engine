#!/usr/bin/env python3
"""Il canarino: un controllo di salute al giorno, un messaggio solo se serve.

Controlla tutta la catena dei due prodotti (Nivult B2B, Mavult B2C) e scrive
su Telegram SOLO se trova qualcosa di rotto. Il lunedì manda comunque un
riepilogo settimanale, così il silenzio e' una notizia verificata e non un
canarino morto.

Controlli (ognuno e' indipendente: se uno esplode, diventa un allarme, mai un
canarino muto):
  - API Nivult e Mavult: radice, vocabolari, cluster e rotta digest presente
  - campioni B2B: gzip valido, JSONL leggibile e file delle ultime 30 ore
  - B2B Mavult chiuso: rotte rappresentative rispondono 404
  - servizi systemd chiave attivi
  - disco < 80%, RAM disponibile > 2 GB, load < core * 2
  - digest consegnati/falliti su entrambi i DB
  - backup di stanotte completato
  - pre-scorer, retrieval E5 v3 e scrittore sull'N5: salute e inferenza sintetica
  - errori gravi nei soli log delle ultime 24 ore (soglia, non singola riga)
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
import psycopg

API_NIVULT = "http://127.0.0.1:8000"
API_MAVULT = "http://127.0.0.1:8001"
PRESCORE_URL = "http://100.119.200.7:8092"
PRESCORE_V3_URL = "http://100.119.200.7:8095"
LOG_FILES = ("/var/log/nivult-digests.log", "/var/log/mavult-digests.log",
             "/var/log/nivult-nightly.log")
ALLARMI: list[str] = []
NOTE: list[str] = []


def allarme(testo: str) -> None:
    ALLARMI.append(testo)


def nota(testo: str) -> None:
    NOTE.append(testo)


def env_di(percorso: str) -> dict[str, str]:
    out = {}
    for riga in Path(percorso).read_text().splitlines():
        if "=" in riga and not riga.strip().startswith("#"):
            k, _, v = riga.partition("=")
            out[k.strip()] = v.strip().strip("'\"")
    return out


def prova(nome: str, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        allarme(f"{nome}: il controllo stesso e' fallito ({exc})")


def _json_get(url: str, timeout: float = 10):
    r = httpx.get(url, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    try:
        return r, r.json()
    except Exception:
        raise RuntimeError("JSON non valido") from None


def check_api() -> None:
    for nome, base, servizio in (("nivult", API_NIVULT, "nivult-api"),
                                 ("mavult", API_MAVULT, "mavult-api")):
        try:
            _, dati = _json_get(base + "/")
            if dati.get("stato") != "ok" or dati.get("servizio") != servizio:
                allarme(f"api {nome}: radice con contratto inatteso")
        except Exception as exc:
            allarme(f"api {nome} /: {exc}")

        for percorso in ("/vocabolari", "/cluster"):
            try:
                _, dati = _json_get(base + percorso)
                if percorso == "/vocabolari" and not isinstance(dati.get("famiglie"), list):
                    raise RuntimeError("vocabolari senza famiglie")
                if percorso == "/cluster" and not isinstance(dati, list):
                    raise RuntimeError("cluster non e' una lista")
            except Exception as exc:
                allarme(f"api {nome} {percorso}: {exc}")

        try:
            r = httpx.get(base + "/me/digest", timeout=10)
            if r.status_code != 401:
                allarme(f"api {nome} /me/digest: atteso 401 senza sessione, "
                        f"ricevuto {r.status_code}")
        except Exception as exc:
            allarme(f"api {nome} /me/digest: {exc}")


def check_mavult_b2b() -> None:
    percorsi = ("/v1/jobs", "/v1/demo/export-campione?tipo=offerte",
                "/portale/uso", "/me/chiavi", "/me/volumi",
                "/booking/conferma/canarino-rotta-b2b")
    for percorso in percorsi:
        try:
            r = httpx.get(API_MAVULT + percorso, timeout=10)
            if r.status_code != 404:
                allarme(f"B2B Mavult {percorso}: atteso 404, ricevuto {r.status_code}")
        except Exception as exc:
            allarme(f"B2B Mavult {percorso}: {exc}")


def check_campione(adesso: datetime | None = None) -> None:
    adesso = adesso or datetime.now(timezone.utc)
    for tipo in ("offerte", "aziende"):
        nome = f"campione {tipo}"
        try:
            r = httpx.get(API_NIVULT + "/v1/demo/export-campione",
                          params={"tipo": tipo}, timeout=30)
            if r.status_code != 200:
                allarme(f"{nome}: HTTP {r.status_code}")
                continue
            try:
                grezzo = gzip.decompress(r.content)
                # JSONL separa solo con LF; gli altri separatori Unicode sono contenuto.
                righe = [riga for riga in grezzo.decode("utf-8").split("\n") if riga.strip()]
                if not righe:
                    raise ValueError("vuoto")
                for riga in righe:
                    if not isinstance(json.loads(riga), dict):
                        raise ValueError("riga non oggetto")
            except Exception:
                allarme(f"{nome}: non e' un gzip JSONL valido e non vuoto")
                continue

            intestazione = r.headers.get("Last-Modified")
            if not intestazione:
                allarme(f"{nome}: Last-Modified assente, freschezza non verificabile")
                continue
            modificato = parsedate_to_datetime(intestazione)
            if modificato.tzinfo is None:
                modificato = modificato.replace(tzinfo=timezone.utc)
            eta = adesso - modificato.astimezone(timezone.utc)
            if eta > timedelta(hours=30) or eta < timedelta(minutes=-5):
                allarme(f"{nome}: file non fresco ({intestazione})")
            else:
                nota(f"{nome}: {len(righe)} righe JSONL, gzip fresco")
        except Exception as exc:
            allarme(f"{nome}: {exc}")


def check_servizi() -> None:
    for unita in ("nivult-api", "mavult-api", "nivult-scrape-cpx52", "cloudflared"):
        r = subprocess.run(["systemctl", "is-active", unita],
                           capture_output=True, text=True)
        if r.stdout.strip() != "active":
            allarme(f"servizio {unita}: {r.stdout.strip() or r.stderr.strip()}")


def check_risorse() -> None:
    disco = shutil.disk_usage("/")
    if disco.used / disco.total > 0.80:
        allarme(f"disco oltre l'80% ({disco.used / disco.total:.0%})")
    mem = {}
    for riga in Path("/proc/meminfo").read_text().splitlines():
        k, _, v = riga.partition(":")
        mem[k] = int(v.strip().split()[0])
    disponibili_mb = mem.get("MemAvailable", 0) / 1024
    if disponibili_mb < 2048:
        allarme(f"RAM disponibile sotto 2 GB ({disponibili_mb:.0f} MB)")
    load = float(Path("/proc/loadavg").read_text().split()[1])
    core = os.cpu_count() or 16
    if load > core * 2:
        allarme(f"load media 5 min {load:.1f} (sopra {core * 2})")


def check_digest(env_niv: dict, env_mav: dict) -> None:
    adesso = datetime.now(timezone.utc)
    for nome, url in (("nivult", env_niv["DATABASE_URL"]),
                      ("mavult", env_mav["DATABASE_URL"])):
        with psycopg.connect(url) as c, c.cursor() as cur:
            cur.execute("""SELECT count(*) FROM digests
                           WHERE sent_at > %s AND status = 'sent'""",
                        (adesso - timedelta(hours=30),))
            inviati = cur.fetchone()[0]
            cur.execute("""SELECT
                             count(*) FILTER (
                               WHERE coalesce(error_message, '') NOT LIKE %s),
                             count(*) FILTER (
                               WHERE coalesce(error_message, '') LIKE %s)
                           FROM digests
                           WHERE started_at > %s AND status = 'failed'""",
                        ("budget di valutazione esaurito%",
                         "budget di valutazione esaurito%",
                         adesso - timedelta(hours=24),))
            falliti, budget_esaurito = cur.fetchone()
            cur.execute("""SELECT count(*) FROM users u
                           WHERE u.status = 'active'
                             AND ((u.next_digest_at < %s AND EXISTS (
                               SELECT 1 FROM user_clusters uc_e
                               JOIN clusters c_e ON c_e.id = uc_e.cluster_id
                               WHERE uc_e.user_id = u.id
                                 AND uc_e.frequency IS NULL
                                 AND NOT uc_e.is_paused
                                 AND c_e.status = 'active'))
                               OR EXISTS (
                               SELECT 1 FROM user_clusters uc_o
                               JOIN clusters c_o ON c_o.id = uc_o.cluster_id
                               WHERE uc_o.user_id = u.id
                                 AND uc_o.frequency IS NOT NULL
                                 AND NOT uc_o.is_paused
                                 AND c_o.status = 'active'
                                 AND uc_o.next_digest_at < %s))""",
                        (adesso - timedelta(hours=6),
                         adesso - timedelta(hours=6)))
            dovuti = cur.fetchone()[0]
            if inviati == 0 and dovuti > 0 and not budget_esaurito:
                allarme(f"digest {nome}: {dovuti} utenti dovuti, 0 consegnati in 30 ore")
            else:
                nota(f"digest {nome}: {inviati} consegnati in 30 ore")
            if budget_esaurito:
                nota(f"digest {nome}: {budget_esaurito} non valutati per budget "
                     "mensile esaurito, non un guasto del motore")
            if falliti:
                allarme(f"digest {nome}: {falliti} falliti in 24 ore")


def check_backup() -> None:
    stato = Path("/opt/nivult/backup-state")
    if not stato.exists():
        allarme("backup-state mancante")
        return
    parti = stato.read_text().split("\t")
    quando = datetime.fromisoformat(parti[1].strip()) if len(parti) > 1 else None
    if parti[0].strip() != "ok" or not quando or \
            quando < datetime.now(timezone.utc) - timedelta(hours=30):
        allarme(f"backup non fresco o fallito ({parti})")


def check_prescore(env_mav: dict | None = None) -> None:
    base = ((env_mav or {}).get("PRESCORE_URL") or PRESCORE_URL).rstrip("/")
    try:
        _, salute = _json_get(base + "/salute", timeout=15)
        if not salute.get("ok"):
            allarme("pre-scorer N5: risponde ma non ok")
            return
        r = httpx.post(base + "/prescore", timeout=30, json={"pairs": [[
            "Synthetic candidate profile with project management experience.",
            "Synthetic project manager job posting requiring project management.",
        ]]})
        if r.status_code != 200:
            allarme(f"pre-scorer N5: inferenza HTTP {r.status_code}")
            return
        punteggi = r.json().get("scores")
        if (not isinstance(punteggi, list) or len(punteggi) != 1
                or isinstance(punteggi[0], bool) or not isinstance(punteggi[0], (int, float))
                or not math.isfinite(punteggi[0]) or not 0 <= punteggi[0] <= 100):
            allarme("pre-scorer N5: inferenza con scores invalido")
        else:
            nota("pre-scorer N5: salute e inferenza ok")
    except Exception:
        allarme("pre-scorer N5: salute o inferenza irraggiungibile/invalida")


def check_retrieval_v3() -> None:
    base = (env_di("/opt/nivult/engine/.env").get("PRESCORE_V3_URL")
            or PRESCORE_V3_URL).rstrip("/")
    try:
        _, salute = _json_get(base + "/salute", timeout=15)
        if salute.get("ok") is not True:
            allarme("retrieval E5 v3: risponde ma non ok")
            return
        r = httpx.post(base + "/rank", timeout=30, json={
            "profile": "[1] Ruolo cercato: HR Business Partner\n[2] Seniority: 5-10",
            "jobs": [{"title": "HR Business Partner"}, {"title": "Carpenter"}],
        })
        if r.status_code != 200:
            allarme(f"retrieval E5 v3: inferenza HTTP {r.status_code}")
            return
        punteggi = r.json().get("scores")
        if (not isinstance(punteggi, list) or len(punteggi) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or not math.isfinite(v) or not -1 <= v <= 1 for v in punteggi)):
            allarme("retrieval E5 v3: inferenza con scores invalido")
        elif punteggi[0] <= punteggi[1]:
            allarme("retrieval E5 v3: non distingue un ruolo pertinente da uno estraneo")
        else:
            nota("retrieval E5 v3: salute e inferenza ok")
    except Exception:
        allarme("retrieval E5 v3: salute o inferenza irraggiungibile/invalida")


def check_scrittore() -> None:
    base = env_di("/opt/nivult/engine/.env").get("SCRITTORE_URL", "").rstrip("/")
    if not base:
        nota("scrittore N5 non configurato")
        return
    try:
        _, salute = _json_get(base + "/health", timeout=15)
        if salute.get("ok") is not True and salute.get("status") != "ok":
            allarme("scrittore N5: risponde ma non e' sano")
            return
        r = httpx.post(base + "/chat/completions", timeout=60,
                       headers={"Authorization": "Bearer locale"},
                       json={
                           "model": "qwen3.6-35b-locale",
                           "messages": [
                               {"role": "system", "content": "Return only a JSON object, without markdown."},
                               {"role": "user", "content": "Return exactly this JSON: {\"ok\": true}"},
                           ],
                           "temperature": 0,
                           "max_tokens": 32,
                           "chat_template_kwargs": {"enable_thinking": False},
                       })
        if r.status_code != 200:
            allarme(f"scrittore N5: inferenza HTTP {r.status_code}")
            return
        scelte = r.json().get("choices")
        if not isinstance(scelte, list) or not scelte:
            raise ValueError("choices assente")
        contenuto = scelte[0].get("message", {}).get("content")
        if not isinstance(contenuto, str) or json.loads(contenuto).get("ok") is not True:
            raise ValueError("risposta non valida")
        nota("scrittore N5: salute e inferenza ok")
    except Exception:
        allarme("scrittore N5: salute o inferenza irraggiungibile/invalida")


def _data_log(riga: str) -> datetime | None:
    match = re.match(
        r"^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
        r"(?:Z|[+-]\d{2}:?\d{2})?)", riga)
    if not match:
        return None
    grezzo = match.group(1).replace(",", ".").replace("Z", "+00:00")
    try:
        quando = datetime.fromisoformat(grezzo)
    except ValueError:
        return None
    return quando.replace(tzinfo=timezone.utc) if quando.tzinfo is None else quando


def check_log_errori(adesso: datetime | None = None,
                     percorsi=LOG_FILES) -> None:
    ieri = (adesso or datetime.now(timezone.utc)) - timedelta(hours=24)
    for log in percorsi:
        p = Path(log)
        if not p.exists():
            continue
        conta = 0
        autenticazione = 0
        avvisi_rete = 0
        non_datati = 0
        quando_riga = None
        with p.open(encoding="utf-8", errors="replace") as f:
            for riga in f:
                quando = _data_log(riga)
                if quando is not None:
                    quando_riga = quando.astimezone(timezone.utc)
                bassa = riga.lower()
                avviso_rete = re.search(r"(?:^|\s)warning\s", bassa) and "errore di rete" in bassa
                livello = re.search(r"(?:^|\s)(?:error|critical)\b", bassa)
                http_401 = re.search(r'"http/\d(?:\.\d)? 401\b', bassa)
                errore_auth = (
                    http_401 or "invalid_client" in bassa
                    or "insufficient balance" in bassa
                    or (livello and re.search(r"\b401\b", bassa)))
                grave = livello or "traceback" in bassa or errore_auth
                if not grave and not avviso_rete:
                    continue
                if quando_riga is None:
                    if grave:
                        non_datati += 1
                elif quando_riga >= ieri:
                    if grave:
                        conta += 1
                    else:
                        avvisi_rete += 1
                    if errore_auth:
                        autenticazione += 1
        if autenticazione:
            allarme(f"{p.name}: {autenticazione} errori di autenticazione provider in 24 ore")
        if conta > 20:
            allarme(f"{p.name}: {conta} errori in 24 ore (soglia 20)")
        elif conta:
            nota(f"{p.name}: {conta} errori in 24 ore")
        if avvisi_rete:
            nota(f"{p.name}: {avvisi_rete} avvisi di rete in 24 ore")
        if non_datati:
            nota(f"{p.name}: {non_datati} errori senza data esclusi dalla finestra")


def telegram(token: str, chat_id: str, testo: str) -> None:
    r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                   json={"chat_id": chat_id, "text": testo,
                         "parse_mode": "HTML", "disable_web_page_preview": True},
                   timeout=15)
    r.raise_for_status()
    if r.json().get("ok") is not True:
        raise RuntimeError("Telegram non ha confermato l'invio")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-notify", action="store_true",
                        help="esegue i controlli senza inviare Telegram")
    args = parser.parse_args(argv)
    ALLARMI.clear()
    NOTE.clear()
    env_niv = env_di("/opt/nivult/.env")
    env_mav = env_di("/opt/mavult/.env")

    prova("API", check_api)
    prova("B2B Mavult", check_mavult_b2b)
    prova("campioni", check_campione)
    prova("servizi", check_servizi)
    prova("risorse", check_risorse)
    prova("digest", lambda: check_digest(env_niv, env_mav))
    prova("backup", check_backup)
    prova("pre-scorer", lambda: check_prescore(env_mav))
    prova("retrieval v3", check_retrieval_v3)
    prova("scrittore", check_scrittore)
    prova("log", check_log_errori)

    if args.no_notify:
        for riga in ALLARMI:
            print(f"ALLARME: {riga}", file=sys.stderr)
        for riga in NOTE:
            print(f"ok: {riga}")
        if not ALLARMI:
            print("Canarino: nessun allarme.")
        return 1 if ALLARMI else 0

    lunedi = datetime.now().weekday() == 0
    if not ALLARMI and not lunedi:
        return 0  # silenzio verificato: e' la risposta giusta

    if ALLARMI:
        testo = ("🚨 <b>Canarino Nivult/Mavult</b>\n\n" +
                 "\n".join(f"• {a}" for a in ALLARMI))
        if NOTE:
            testo += "\n\n<i>" + " · ".join(NOTE) + "</i>"
    else:
        testo = ("✅ <b>Canarino — riepilogo del lunedì</b>\n\nTutto in ordine: "
                 "API, B2B, campioni, servizi, digest, backup, pre-scorer, risorse.\n\n" +
                 "\n".join(f"• {n}" for n in NOTE))

    token = env_niv.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print(testo, file=sys.stderr)
        return 1
    # la chat di Giuseppe dal DB nivult (il suo account, anche se in pausa)
    with psycopg.connect(env_niv["DATABASE_URL"]) as c, c.cursor() as cur:
        cur.execute("""SELECT telegram_chat_id FROM users
                       WHERE email = 'g.ranno@outlook.com'""")
        riga = cur.fetchone()
    chat = str(riga[0]) if riga and riga[0] else ""
    if not chat:
        print(testo, file=sys.stderr)
        return 1
    try:
        telegram(token, chat, testo)
    except Exception:  # noqa: BLE001 — l'eccezione HTTP contiene l'URL col token
        print("Canarino: invio notifica Telegram fallito", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
