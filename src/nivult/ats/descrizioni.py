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

import logging
import os
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
    ap.add_argument("--limite", type=int, default=3000)
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    if args.workday:
        print(workday(dsn, args.limite))
    if args.smartrecruiters or not args.workday:
        print(smartrecruiters(dsn, args.limite))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
