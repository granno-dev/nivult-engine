"""Adzuna come mappa di scoperta: dai nomi azienda alle offerte alla fonte.

Adzuna elenca ~300.000 offerte italiane con il NOME dell'azienda, ma con
link di rimbalzo (adzuna.it/land/...) inutili per noi — provato: la
landing non reindirizza al datore ne' espone l'URL originale. Il valore
non e' l'offerta, e' il nome: da quello si indovina il dominio, si
cerca il career site, si riconosce l'ATS e si aggiunge l'azienda al
censimento, cosi' il demone scrape la prende ALLA FONTE con link
diretto.

Non e' un jackpot e lo diciamo: su un campione, ~11% delle aziende le
avevamo gia', ~19% sono agenzie gia' coperte, e delle sconosciute solo
~un quarto ha un ATS scopribile. Ma quelle sono aziende con offerte
VERE e attive — scoperta migliore degli archivi, che danno domini
indovinati a vuoto. E costa zero: l'API Adzuna e' gratuita.

⚠ BOCCIATO DAL TEST (2026-09-03). Provato su 15 aziende dirette con
offerte Adzuna: 0 con un ATS scopribile via HTTP. Il motivo: le aziende
moderne caricano l'ATS via JavaScript/iframe, e l'URL (jobs.lever.co/…)
non e' nell'HTML statico che questo probe legge. La stima iniziale del
20-30%% era un falso positivo — cercava la PAROLA «lever» (presente negli
script), non l'URL reale. Il modulo resta come memoria: per farlo
rendere servirebbe un browser headless per ogni azienda (costoso), e
comunque molte non hanno un ATS pubblico. NON in produzione.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import psycopg

from .runner import ATS_DSN

log = logging.getLogger("nivult.ats.adzuna")

ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs"

# Riconosce l'ATS dall'URL trovato sul career site ed estrae lo slug.
# Gli stessi domini che il resto del sistema gia' scrapa.
ATS_URL = {
    "greenhouse": r"(?:job-boards|boards)\.(?:eu\.)?greenhouse\.io/([^/?#\"']+)",
    "lever": r"jobs\.lever\.co/([^/?#\"']+)",
    "ashby": r"jobs\.ashbyhq\.com/([^/?#.\"']+)",
    "smartrecruiters": r"jobs\.smartrecruiters\.com/([^/?#\"']+)",
    "recruitee": r"([a-z0-9-]+)\.recruitee\.com",
    "workable": r"apply\.workable\.com/([^/?#\"']+)",
    "personio": r"([a-z0-9-]+)\.jobs\.personio\.(?:com|de)",
    "teamtailor": r"([a-z0-9-]+)\.teamtailor\.com",
    "bamboohr": r"([a-z0-9-]+)\.bamboohr\.com",
}

# I percorsi dove un'azienda mette la pagina «lavora con noi».
CAREER_PATHS = ["/careers", "/jobs", "/lavora-con-noi", "/carriere",
                "/en/careers", "/join-us", ""]

# Parole che segnalano un'agenzia per il lavoro: il datore vero e'
# nascosto, e le grandi le prendiamo gia' alla fonte — si salta.
AGENZIA = re.compile(
    r"lavoro|interim|staff|risorse|agenzia|adecco|randstad|manpower|"
    r"gigroup|umana|synergie|adhr|openjob|recruit", re.I)


def _domini(nome: str) -> list[str]:
    base = re.sub(r"[^a-z0-9]", "", nome.lower())
    if len(base) < 3:
        return []
    return [f"https://www.{base}.it", f"https://www.{base}.com"]


def _scopri_ats(client: httpx.Client, nome: str):
    """(platform_id, slug) se l'azienda ha un ATS riconoscibile, else None."""
    for base in _domini(nome):
        vivo = False
        for path in CAREER_PATHS:
            try:
                r = client.get(base + path)
            except httpx.HTTPError:
                continue
            if r.status_code != 200:
                continue
            vivo = True
            for pid, pat in ATS_URL.items():
                m = re.search(pat, r.text)
                if m:
                    slug = m.group(1).strip().lower()
                    if slug and slug not in ("careers", "jobs", "www"):
                        return pid, slug
        if vivo:
            # il dominio risponde ma niente ATS: non provare l'altro TLD,
            # e' quasi certo la stessa azienda
            break
    return None


def _aziende_da_adzuna(paese: str, pagine: int, app_id: str,
                       app_key: str) -> set[str]:
    aziende: set[str] = set()
    with httpx.Client(timeout=30,
                      headers={"User-Agent": "nivult-ats/1.0"}) as c:
        for pagina in range(1, pagine + 1):
            url = f"{ADZUNA_BASE}/{paese}/search/{pagina}"
            try:
                r = c.get(url, params={
                    "app_id": app_id, "app_key": app_key,
                    "results_per_page": 50,
                    "content-type": "application/json"})
                if r.status_code != 200:
                    log.warning("Adzuna %s pagina %d: http %d",
                                paese, pagina, r.status_code)
                    break
                for o in r.json().get("results", []):
                    nome = (o.get("company") or {}).get("display_name")
                    if nome and nome.strip():
                        aziende.add(nome.strip())
            except (httpx.HTTPError, ValueError) as e:
                log.warning("Adzuna %s pagina %d: %s", paese, pagina, e)
                break
    return aziende


def scopri(dsn: str, paesi: str, pagine: int, thread: int) -> dict:
    app_id = os.environ.get("ADZUNA_APP_ID", "")
    app_key = os.environ.get("ADZUNA_APP_KEY", "")
    if not app_id or not app_key:
        raise SystemExit("ADZUNA_APP_ID / ADZUNA_APP_KEY mancanti nell'ambiente")

    stats = {"aziende": 0, "gia_note": 0, "agenzie": 0, "con_ats": 0,
             "senza_ats": 0}

    # 1. i nomi azienda da Adzuna, per i paesi chiesti
    aziende: set[str] = set()
    for paese in [p.strip().lower() for p in paesi.split(",") if p.strip()]:
        n = _aziende_da_adzuna(paese, pagine, app_id, app_key)
        log.info("Adzuna %s: %d aziende", paese, len(n))
        aziende |= n
    stats["aziende"] = len(aziende)

    # 2. via quelle gia' censite e le agenzie
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    with psycopg.connect(dsn) as conn:
        note = {r[0] for r in conn.execute(
            "SELECT DISTINCT lower(regexp_replace(coalesce(company_name, slug),"
            " '[^a-zA-Z0-9]', '', 'g')) FROM ats_companies")}

    candidate = []
    for nome in aziende:
        if AGENZIA.search(nome):
            stats["agenzie"] += 1
        elif _norm(nome) in note:
            stats["gia_note"] += 1
        else:
            candidate.append(nome)

    # 3. scopri l'ATS delle candidate, in parallelo
    def _una(nome):
        with httpx.Client(timeout=12, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 "
                                   "(compatible; nivult-ats/1.0)"}) as c:
            try:
                return nome, _scopri_ats(c, nome)
            except Exception as e:               # noqa: BLE001
                log.debug("scoperta %s: %s", nome, e)
                return nome, None

    with psycopg.connect(dsn) as conn, \
            ThreadPoolExecutor(max_workers=thread) as pool:
        for fut in as_completed([pool.submit(_una, n) for n in candidate]):
            nome, trovato = fut.result()
            if not trovato:
                stats["senza_ats"] += 1
                continue
            pid, slug = trovato
            r = conn.execute(
                "INSERT INTO ats_companies (platform_id, slug, company_name, "
                " discovered_from) VALUES (%s, %s, %s, 'adzuna') "
                "ON CONFLICT (platform_id, slug) DO NOTHING",
                (pid, slug, nome[:200]))
            if r.rowcount:
                stats["con_ats"] += 1
                log.info("  nuova: %s -> %s/%s", nome, pid, slug)
            else:
                stats["gia_note"] += 1
            conn.commit()

    log.info("Adzuna scoperta: %s", stats)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--paesi", default="it",
                    help="ISO2 separati da virgola (default it)")
    ap.add_argument("--pagine", type=int, default=20,
                    help="pagine Adzuna per paese, 50 offerte l'una")
    ap.add_argument("--thread", type=int, default=10)
    args = ap.parse_args()
    print(scopri(ATS_DSN, args.paesi, args.pagine, args.thread))


if __name__ == "__main__":
    main()
