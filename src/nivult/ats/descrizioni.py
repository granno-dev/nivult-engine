"""Descrizioni mancanti: il fetch di dettaglio, incrementale e gentile.

Meta' delle piattaforme mette la descrizione gia' nella lista; l'altra
meta' la tiene solo nella pagina di dettaglio, una chiamata per offerta.
Qui la andiamo a prendere per chi ne e' privo e la salviamo dentro
`raw.description` — cosi' digest, board e arricchimento AI hanno il testo.

- SmartRecruiters (80k senza): `/v1/companies/{slug}/postings/{id}`,
  API CONDIVISA fra tutti i tenant: un solo worker, ~1.4/s, mai di piu'.
- Workday (53k senza): `/wday/cxs/{slug}/{site}{externalPath}` con le
  coordinate (server, site) gia' risolte nel censimento. Ogni tenant e'
  un'istanza enterprise separata: qualche worker in parallelo va bene.

Incrementale: prima le offerte piu' recenti; la chiave si scrive anche
vuota, cosi' un dettaglio senza testo non viene ritentato per sempre.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import httpx
import psycopg

log = logging.getLogger("nivult.ats.descrizioni")

_UA = "Mozilla/5.0 (compatible; nivult-ats/1.0)"
_RITMO = 1.4          # richieste al secondo verso l'API condivisa


def _testo_jobad(d: dict) -> str | None:
    sezioni = (d.get("jobAd") or {}).get("sections") or {}
    pezzi = []
    for k in ("jobDescription", "qualifications", "additionalInformation",
              "companyDescription"):
        v = sezioni.get(k)
        if isinstance(v, dict) and v.get("text"):
            pezzi.append(str(v["text"]))
    testo = "\n\n".join(pezzi).strip()
    return testo[:30000] or None


def smartrecruiters(dsn: str, limite: int = 3000) -> dict:
    cli = httpx.Client(timeout=15, follow_redirects=True,
                       headers={"User-Agent": _UA,
                                "Accept": "application/json"})
    stats = {"esaminate": 0, "riempite": 0, "vuote": 0, "errori": 0}
    intervallo = 1.0 / _RITMO
    with psycopg.connect(dsn, autocommit=True) as c:
        righe = c.execute("""
            SELECT id, slug, external_id FROM ats_jobs
             WHERE platform_id = 'smartrecruiters' AND expired_at IS NULL
               AND NOT (raw ? 'description')
             ORDER BY posted_at DESC NULLS LAST
             LIMIT %s""", (limite,)).fetchall()
        for jid, slug, eid in righe:
            stats["esaminate"] += 1
            t0 = time.monotonic()
            testo = None
            try:
                r = cli.get("https://api.smartrecruiters.com/v1/companies/"
                            f"{slug}/postings/{eid}")
                if r.status_code == 200:
                    testo = _testo_jobad(r.json())
                elif r.status_code == 429:
                    log.warning("smartrecruiters 429: rallento")
                    time.sleep(20)
            except (httpx.HTTPError, ValueError):
                stats["errori"] += 1
            # si scrive SEMPRE la chiave (anche vuota): cosi' l'offerta non
            # viene ritentata a ogni giro se il dettaglio non ha testo.
            c.execute("""UPDATE ats_jobs
                            SET raw = jsonb_set(raw, '{description}',
                                                to_jsonb(%s::text), true)
                          WHERE id = %s""", (testo or "", jid))
            if testo:
                stats["riempite"] += 1
            else:
                stats["vuote"] += 1
            resto = intervallo - (time.monotonic() - t0)
            if resto > 0:
                time.sleep(resto)
    cli.close()
    log.info("descrizioni smartrecruiters: %s", stats)
    return stats


# Piattaforme la cui PAGINA pubblica porta il JSON-LD JobPosting completo
# (verificato a campione, una per una): un solo estrattore le copre tutte,
# e oltre alla descrizione raccoglie paese, citta' e data quando mancano.
_DA_PAGINA = ("jazzhr", "breezy", "teamtailor", "applicantstack",
              "freshteam", "vincere")

_LD = re.compile(r"<script[^>]*ld\+json[^>]*>(.*?)</script>", re.S | re.I)


def _jobposting(html: str) -> dict | None:
    for blocco in _LD.findall(html):
        try:
            d = json.loads(blocco.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(d, list):
            d = next((x for x in d if isinstance(x, dict)
                      and x.get("@type") == "JobPosting"), None)
        if isinstance(d, dict) and d.get("@type") == "JobPosting":
            return d
    return None


def da_pagina(dsn: str, limite: int = 3000, thread: int = 10) -> dict:
    """Apre la pagina pubblica delle offerte senza descrizione e legge il
    JSON-LD. Ogni tenant vive sul suo sottodominio: il carico si spalma
    da solo. La chiave si scrive anche vuota SOLO se la pagina ha
    risposto 200 senza JobPosting; un errore di rete non marca niente."""
    from concurrent.futures import ThreadPoolExecutor
    stats = {"esaminate": 0, "riempite": 0, "vuote": 0, "errori": 0,
             "paesi": 0}
    with psycopg.connect(dsn, autocommit=True) as c:
        righe = c.execute("""
            SELECT id, url, country FROM ats_jobs
             WHERE platform_id = ANY(%s) AND expired_at IS NULL
               AND NOT (raw ? 'description') AND url IS NOT NULL
             ORDER BY posted_at DESC NULLS LAST
             LIMIT %s""", (list(_DA_PAGINA), limite)).fetchall()

        def leggi(riga):
            jid, url, paese = riga
            try:
                with httpx.Client(timeout=12, follow_redirects=True,
                                  headers={"User-Agent": _UA}) as cli:
                    r = cli.get(url)
            except httpx.HTTPError:
                return jid, None, None
            if r.status_code != 200:
                return jid, None, None
            jp = _jobposting(r.text) or {}
            descr = str(jp.get("description") or "")[:30000]
            paese_nuovo = None
            if not paese:
                loc = jp.get("jobLocation") or {}
                if isinstance(loc, list):
                    loc = loc[0] if loc else {}
                cc = ((loc.get("address") or {}).get("addressCountry")
                      if isinstance(loc, dict) else None)
                if isinstance(cc, dict):
                    cc = cc.get("name")
                if isinstance(cc, str) and len(cc.strip()) == 2:
                    paese_nuovo = cc.strip().upper()
            return jid, descr, paese_nuovo

        with ThreadPoolExecutor(max_workers=thread) as pool:
            for jid, descr, paese_nuovo in pool.map(leggi, righe):
                stats["esaminate"] += 1
                if descr is None:
                    stats["errori"] += 1     # rete: si riprovera'
                    continue
                c.execute("""UPDATE ats_jobs
                                SET raw = jsonb_set(raw, '{description}',
                                                    to_jsonb(%s::text), true),
                                    country = COALESCE(country, %s)
                              WHERE id = %s""",
                          (descr, paese_nuovo, jid))
                if descr:
                    stats["riempite"] += 1
                else:
                    stats["vuote"] += 1
                if paese_nuovo:
                    stats["paesi"] += 1
                if stats["esaminate"] % 500 == 0:
                    log.info("  … da_pagina %s", stats)
    log.info("descrizioni da_pagina: %s", stats)
    return stats


# Piattaforme dove la pagina NON offre il JSON-LD ma il testo c'e'
# comunque: nell'HTML servito dal server (icims col trucco mobile=true,
# catsone) o in un JSON incorporato negli script (werecruit, zoho).
# Cornerstone resta fuori: guscio JS puro con API a token, cantiere a parte.
_TESTO_PIATTAFORME = ("icims", "catsone", "werecruit", "zohorecruit")

# "description":"..." dentro gli script: almeno 200 caratteri, con gli
# escape JSON gestiti dal decoder vero (niente unescape a mano)
_JSON_DESCR = re.compile(
    r'"(?:job_?[Dd]escription|description)"\s*:\s*'
    r'("(?:\\.|[^"\\]){200,}?")')


def _paragrafi(html: str) -> str | None:
    """I paragrafi densi della pagina: i nodi di testo lunghi sono la
    descrizione, quelli corti sono menu e briciole. Rozzezza voluta:
    regge il redesign delle piattaforme meglio di qualsiasi selettore."""
    import html as _h
    pulito = re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", " ", html)
    nodi = (_h.unescape(t).strip() for t in re.findall(r">([^<>]+)<", pulito))
    buoni = [n for n in nodi if len(n) >= 80]
    return "\n\n".join(buoni)[:30000] or None


def _estrai_testo(pid: str, html: str) -> str:
    jp = _jobposting(html)
    if jp and jp.get("description"):
        return str(jp["description"])[:30000]
    m = _JSON_DESCR.search(html)
    if m:
        try:
            return str(json.loads(m.group(1)))[:30000]
        except ValueError:
            pass
    return _paragrafi(html) or ""


def da_testo(dsn: str, limite: int = 3000, thread: int = 8) -> dict:
    """Descrizioni per le piattaforme senza JSON-LD: si legge la pagina
    e si prende il testo dove sta. Errori di rete non marcano; una
    pagina 200 senza testo si' (niente riesami eterni)."""
    from concurrent.futures import ThreadPoolExecutor
    stats = {"esaminate": 0, "riempite": 0, "vuote": 0, "errori": 0}
    with psycopg.connect(dsn, autocommit=True) as c:
        righe = c.execute("""
            SELECT id, platform_id, url FROM ats_jobs
             WHERE platform_id = ANY(%s) AND expired_at IS NULL
               AND NOT (raw ? 'description') AND url IS NOT NULL
             ORDER BY posted_at DESC NULLS LAST
             LIMIT %s""", (list(_TESTO_PIATTAFORME), limite)).fetchall()

        def leggi(riga):
            jid, pid, url = riga
            if pid == "icims":
                # la versione dentro-iframe e' quella renderizzata dal
                # server col testo completo (mobile=true spesso no)
                url = url + ("&" if "?" in url else "?") + "in_iframe=1"
            try:
                with httpx.Client(timeout=15, follow_redirects=True,
                                  headers={"User-Agent": _UA}) as cli:
                    r = cli.get(url)
            except httpx.HTTPError:
                return jid, None
            if r.status_code != 200:
                return jid, None
            return jid, _estrai_testo(pid, r.text)

        with ThreadPoolExecutor(max_workers=thread) as pool:
            for jid, testo in pool.map(leggi, righe):
                stats["esaminate"] += 1
                if testo is None:
                    stats["errori"] += 1
                    continue
                c.execute("""UPDATE ats_jobs
                                SET raw = jsonb_set(raw, '{description}',
                                                    to_jsonb(%s::text), true)
                              WHERE id = %s""", (testo, jid))
                if testo:
                    stats["riempite"] += 1
                else:
                    stats["vuote"] += 1
                if stats["esaminate"] % 500 == 0:
                    log.info("  … da_testo %s", stats)
    log.info("descrizioni da_testo: %s", stats)
    return stats


def workday(dsn: str, limite: int = 3000, thread: int = 8) -> dict:
    from concurrent.futures import ThreadPoolExecutor
    stats = {"esaminate": 0, "riempite": 0, "vuote": 0, "errori": 0}
    with psycopg.connect(dsn, autocommit=True) as c:
        righe = c.execute("""
            SELECT j.id, j.slug, ac.wd_server, ac.wd_instance,
                   j.raw->>'externalPath'
              FROM ats_jobs j
              JOIN ats_companies ac ON ac.platform_id = j.platform_id
                                   AND ac.slug = j.slug
             WHERE j.platform_id = 'workday' AND j.expired_at IS NULL
               AND NOT (j.raw ? 'description')
               AND ac.wd_server IS NOT NULL AND ac.wd_instance IS NOT NULL
               AND j.raw->>'externalPath' IS NOT NULL
             ORDER BY j.posted_at DESC NULLS LAST
             LIMIT %s""", (limite,)).fetchall()

        def leggi(riga):
            jid, slug, srv, site, percorso = riga
            url = (f"https://{slug}.{srv}.myworkdayjobs.com/wday/cxs/"
                   f"{slug}/{site}{percorso}")
            try:
                with httpx.Client(timeout=12, headers={
                        "User-Agent": _UA,
                        "Accept": "application/json"}) as cli:
                    r = cli.get(url)
                if r.status_code != 200:
                    return jid, None
                testo = (r.json().get("jobPostingInfo") or {}) \
                    .get("jobDescription") or ""
                return jid, str(testo)[:30000]
            except (httpx.HTTPError, ValueError):
                return jid, None

        with ThreadPoolExecutor(max_workers=thread) as pool:
            for jid, testo in pool.map(leggi, righe):
                stats["esaminate"] += 1
                if testo is None:
                    stats["errori"] += 1
                    testo = ""       # marcata comunque: niente retry eterni
                c.execute("""UPDATE ats_jobs
                                SET raw = jsonb_set(raw, '{description}',
                                                    to_jsonb(%s::text), true)
                              WHERE id = %s""", (testo, jid))
                if testo:
                    stats["riempite"] += 1
                else:
                    stats["vuote"] += 1
                if stats["esaminate"] % 500 == 0:
                    log.info("  … workday %s", stats)
    log.info("descrizioni workday: %s", stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.descrizioni")
    ap.add_argument("--smartrecruiters", action="store_true")
    ap.add_argument("--workday", action="store_true")
    ap.add_argument("--da-pagina", action="store_true")
    ap.add_argument("--da-testo", action="store_true")
    ap.add_argument("--limite", type=int, default=3000)
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    if args.workday:
        print(workday(dsn, args.limite))
    if args.da_pagina:
        print(da_pagina(dsn, args.limite))
    if args.da_testo:
        print(da_testo(dsn, args.limite))
    if args.smartrecruiters or not (args.workday or args.da_pagina
                                    or args.da_testo):
        print(smartrecruiters(dsn, args.limite))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
