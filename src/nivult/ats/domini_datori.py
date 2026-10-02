"""Il dominio del datore, a strati dal certo al verificato.

Misurato il 2026-09-05: il 97% delle aziende attive non ha un dominio
noto, e senza dominio la scheda-dal-sito non tocca niente. La ricerca
esterna (5k/mese gratis) impiegherebbe otto mesi: non scala. Scala
questo:

  1. VANITY — l'host dell'offerta quando non e' un ATS noto
     (careers.dhl.com -> dhl.com): e' il sito carriere dell'azienda
     stessa, certezza piena. ~1.500 aziende subito, gratis.
  2. BRANDFETCH — la Search gia' pagata per i loghi fa nome->dominio.
     Guardia anti-omonimi obbligatoria: «Rossi Impianti» tornava
     rossiresidencial.com.br. Il dominio entra come CANDIDATO
     (site_domain_source='brandfetch'): la conferma vera la da'
     scheda_sito trovando il nome dell'azienda nella pagina.

site_domain non sovrascrive mai logo_domain: e' il ripiego, e porta
la fonte accanto.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from urllib.parse import urlparse

import httpx
import psycopg

from .riscoperta import _radice
from .registri_imprese import _combacia

log = logging.getLogger("nivult.ats.domini")


def _colonna_manca(c, tabella: str, colonna: str) -> bool:
    return c.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (tabella, colonna)).fetchone() is None


def prepara(c) -> None:
    if _colonna_manca(c, "ats_companies", "site_domain"):
        c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS "
                  "site_domain text")
        c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS "
                  "site_domain_source text")


def da_vanity(dsn: str) -> dict:
    """Strato 1: l'host dell'offerta, quando non e' un ATS noto."""
    n = 0
    with psycopg.connect(dsn, autocommit=True) as c:
        prepara(c)
        righe = c.execute("""
            SELECT DISTINCT ON (j.platform_id, j.slug)
                   j.platform_id, j.slug, j.url
              FROM ats_jobs j JOIN ats_companies ac
                ON ac.platform_id = j.platform_id AND ac.slug = j.slug
             WHERE j.expired_at IS NULL AND j.url IS NOT NULL
               AND ac.logo_domain IS NULL AND ac.site_domain IS NULL
             ORDER BY j.platform_id, j.slug""").fetchall()
        for pid, slug, url in righe:
            host = urlparse(url).hostname or ""
            radice = _radice(host)          # applica la denylist ATS
            if not radice:
                continue
            c.execute("""UPDATE ats_companies
                            SET site_domain = %s,
                                site_domain_source = 'vanity'
                          WHERE platform_id = %s AND slug = %s""",
                      (radice, pid, slug))
            n += 1
    log.info("vanity: %d domini", n)
    return {"vanity": n}


def da_brandfetch(dsn: str, limite: int = 2000) -> dict:
    """Strato 2: nome -> dominio con la Search dei loghi. Candidati,
    con guardia anti-omonimi; la conferma la fara' scheda_sito."""
    cid = os.environ.get("BRANDFETCH_CLIENT_ID")
    if not cid:
        log.warning("BRANDFETCH_CLIENT_ID assente: strato saltato")
        return {"brandfetch": 0}
    stats = {"esaminate": 0, "brandfetch": 0}
    cli = httpx.Client(timeout=15)
    with psycopg.connect(dsn, autocommit=True) as c:
        prepara(c)
        righe = c.execute("""
            SELECT platform_id, slug, company_name FROM ats_companies
             WHERE is_active AND job_count >= 3
               AND company_name IS NOT NULL
               AND logo_domain IS NULL AND site_domain IS NULL
             ORDER BY job_count DESC LIMIT %s""", (limite,)).fetchall()
        for pid, slug, nome in righe:
            stats["esaminate"] += 1
            try:
                r = cli.get("https://api.brandfetch.io/v2/search/"
                            + httpx.QueryParams({"q": nome})["q"],
                            params={"c": cid})
                voci = r.json() if r.status_code == 200 else []
            except Exception:                        # noqa: BLE001
                time.sleep(3)
                continue
            time.sleep(0.5)
            scelto = None
            for v in voci[:3]:
                if isinstance(v, dict) and v.get("domain") \
                        and _combacia(nome, [v.get("name")]):
                    scelto = v["domain"]
                    break
            if scelto:
                c.execute("""UPDATE ats_companies
                                SET site_domain = %s,
                                    site_domain_source = 'brandfetch'
                              WHERE platform_id = %s AND slug = %s""",
                          (scelto, pid, slug))
                stats["brandfetch"] += 1
    log.info("brandfetch: %s", stats)
    return stats


# ── strato 3: LinkedIn, la pagina aziendale PUBBLICA (01/10/2026) ────────
# Via searxng (google/yahoo trattano bene site:linkedin.com/company) e poi
# la pagina pubblica, senza login e senza proxy: a ritmo gentile l'authwall
# non parte (misurato). Esce: dominio vero (dal redir del sito), dipendenti
# esatti e freschi, settore, sede. Dato aziendale, non personale; restano
# comunque pagine pubbliche chieste piano, e i numeri si marcano 'linkedin'
# come fonte — sono auto-dichiarati.
_SEARX = os.environ.get("SEARX_URL", "http://100.119.200.7:8899/search")
_UA_LI = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _pagina_linkedin(nome: str, cli: httpx.Client) -> dict | None:
    """La pagina aziendale LinkedIn per nome, o None."""
    r = cli.get(_SEARX, params={"q": f'site:linkedin.com/company "{nome}"',
                                "format": "json", "engines": "google,yahoo"},
                headers={"User-Agent": "nivult/1.0"})
    r.raise_for_status()
    li = next((x for x in r.json().get("results") or []
               if "linkedin.com/company/" in (x.get("url") or "")), None)
    if not li:
        return None
    time.sleep(5)
    p = cli.get(li["url"], headers={"User-Agent": _UA_LI},
                follow_redirects=True)
    if p.status_code != 200:
        return None
    h = p.text
    out: dict = {"pagina": li["url"]}
    m = re.search(r'"numberOfEmployees":\{"value":(\d+)', h)
    if m:
        out["dipendenti"] = int(m.group(1))
    m = re.search(r'redir/redirect\?url=([^"&]+)', h)
    if m:
        from urllib.parse import unquote
        host = (urlparse(unquote(m.group(1))).hostname or "").lower()
        if host and "linkedin" not in host:
            out["dominio"] = _radice(host)
    for tid, chiave in (("about-us__industry", "settore"),
                        ("about-us__headquarters", "sede")):
        m = re.search(tid + r'[^<]*<dd[^>]*>\s*([^<]+?)\s*</dd>', h, re.S)
        if m:
            out[chiave] = m.group(1).strip()[:80]
    return out


def da_linkedin(dsn: str, limite: int = 500) -> dict:
    """Strato 3: nome -> pagina LinkedIn -> dominio + dipendenti + settore.

    Le colonne li_* tengono la fonte separata dal dato certo: i numeri di
    LinkedIn sono auto-dichiarati e vanno letti come stime."""
    stats = {"esaminate": 0, "pagina": 0, "dominio": 0, "dipendenti": 0,
             "settore": 0}
    cli = httpx.Client(timeout=30)
    with psycopg.connect(dsn, autocommit=True) as c:
        prepara(c)
        c.execute("ALTER TABLE ats_companies "
                  "ADD COLUMN IF NOT EXISTS li_employees integer")
        c.execute("ALTER TABLE ats_companies "
                  "ADD COLUMN IF NOT EXISTS li_industry text")
        c.execute("ALTER TABLE ats_companies "
                  "ADD COLUMN IF NOT EXISTS li_checked_at timestamptz")
        righe = c.execute("""
            SELECT platform_id, slug, company_name FROM ats_companies
             WHERE is_active AND job_count > 1
               AND company_name IS NOT NULL AND length(company_name) > 4
               AND logo_domain IS NULL AND site_domain IS NULL
               AND li_checked_at IS NULL
             ORDER BY job_count DESC LIMIT %s""", (limite,)).fetchall()
        for pid, slug, nome in righe:
            stats["esaminate"] += 1
            try:
                esito = _pagina_linkedin(nome, cli)
            except Exception:                        # noqa: BLE001
                time.sleep(4)
                continue
            if not esito:
                c.execute("UPDATE ats_companies SET li_checked_at = now() "
                          "WHERE platform_id = %s AND slug = %s", (pid, slug))
                continue
            stats["pagina"] += 1
            dom = esito.get("dominio")
            c.execute("""UPDATE ats_companies SET
                           site_domain = coalesce(site_domain, %s),
                           site_domain_source = CASE
                             WHEN site_domain IS NULL THEN 'linkedin'
                             ELSE site_domain_source END,
                           li_employees = %s, li_industry = %s,
                           li_checked_at = now()
                         WHERE platform_id = %s AND slug = %s""",
                      (dom, esito.get("dipendenti"), esito.get("settore"),
                       pid, slug))
            stats["dominio"] += 1 if dom else 0
            stats["dipendenti"] += 1 if esito.get("dipendenti") else 0
            stats["settore"] += 1 if esito.get("settore") else 0
            time.sleep(1.5)
    log.info("linkedin: %s", stats)
    return stats


def main() -> int:
    import argparse
    from .runner import ATS_DSN
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.domini_datori")
    ap.add_argument("--limite", type=int, default=2000)
    ap.add_argument("--solo-vanity", action="store_true")
    ap.add_argument("--solo-linkedin", action="store_true")
    ap.add_argument("--limite-linkedin", type=int, default=500)
    a = ap.parse_args()
    esito = da_vanity(ATS_DSN)
    if not a.solo_vanity:
        esito.update(da_brandfetch(ATS_DSN, a.limite))
    if a.solo_linkedin:
        esito = da_linkedin(ATS_DSN, a.limite_linkedin)
    print(json.dumps(esito))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
