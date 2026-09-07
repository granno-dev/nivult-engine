"""Le career page delle aziende SENZA ATS, lette come le legge Google.

Il detector trova la pagina carriere di un'azienda e, se non riconosce
nessun ATS, la lascia in `company_domains` con status `no_ats` — 38.000
domini al 07/09/2026, 1.100 italiani, fra cui Generali, Danieli,
Marcegaglia, Trenord. Sono le aziende che pubblicano da sole, sul
proprio sito, e per stare su Google for Jobs lo fanno nel modo
standard: **una sitemap delle offerte e un JobPosting JSON-LD su ogni
annuncio**. E' lo stesso canale delle agenzie per il lavoro
(`agenzie.py`), e la stessa logica: niente browser, niente login,
nessun termine violato — il sito lo espone apposta ai motori.

Due pezzi:

  scopri   — per ogni dominio `no_ats` cerca la SORGENTE: la sitemap
             delle offerte (da robots.txt / sitemap index, riconosciuta
             dal nome o dagli URL che contiene) oppure la pagina
             carriere con link ad annunci. Prova fino a 3 pagine: se
             almeno una porta un JobPosting, l'azienda entra in
             ats_companies come piattaforma `jsonld` con `sorgente_url`.
             I domini provati e vuoti si marcano (jsonld_checked_at) e
             non si riprovano prima di 30 giorni.
  adapter  — `JsonLd.jobs(slug, sorgente_url)` in adapters.py: legge la
             sorgente, scarica fino a 200 pagine, estrae i JobPosting.
             Il runner lo tratta come qualunque altra piattaforma.

    python -m nivult.ats.jsonld --scopri --limite 150 [--paese IT]
    python -m nivult.ats.jsonld --prova generali.com danieli.com   # senza scrivere
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import httpx
import psycopg

from .jobposting import _estrai_ld, _loc, _sitemap_urls

log = logging.getLogger("nivult.ats.jsonld")
UA = {"User-Agent": "Mozilla/5.0 (compatible; nivult-ats/1.0; +https://nivult.com)"}

# parole che nel nome di una sitemap o nel percorso di un URL dicono «offerte»
PAROLE_OFFERTE = re.compile(
    r"job|vacan|career|carrier|carriere|offerte|lavor|posizion|stellen|emploi|offres"
    r"|empleo|vacante|vaga|opportunit|recruit|opening|position|annunc|ricerca-personale",
    re.I)
# parole nel percorso della PAGINA di un singolo annuncio (non l'elenco)
PATH_ANNUNCIO = re.compile(r"/(job|jobs|vacancy|vacancies|offerta|offerte|position|positions|stelle|stellen|"
                           r"emploi|empleo|vaga|opportunit[a-z]*|annunci?o?|career[s]?|carriere|posizion[ei])[/-]", re.I)
MASSIMO_PAGINE = 200
PROVE_SCOPERTA = 4


def _dsn() -> str:
    d = os.environ.get("ATS_DATABASE_URL")
    if d:
        return d
    for f in ("/opt/nivult/.env", "/opt/nivult/engine/.env"):
        try:
            m = re.search(r"^POSTGRES_PASSWORD=(.*)$", open(f).read(), re.M)
            if m:
                return "postgresql://nivult:" + m.group(1).strip() + "@127.0.0.1:5432/nivult_ats"
        except OSError:
            pass
    raise SystemExit("ATS_DATABASE_URL assente")


def _stesso_sito(url: str, dominio: str) -> bool:
    h = urlparse(url).netloc.lower()
    d = dominio.lower().removeprefix("www.")
    return h == d or h.endswith("." + d)


def sitemap_offerte(client: httpx.Client, dominio: str) -> tuple[str | None, list[str]]:
    """(sitemap_scelta, url_annunci): la sitemap che parla di offerte, o
    quella i cui URL sembrano annunci. Guarda robots.txt e le sitemap
    solite; segue un livello di sitemapindex."""
    candidate: list[str] = []
    base = f"https://{dominio}"
    try:
        r = client.get(f"{base}/robots.txt")
        if r.status_code == 200:
            candidate += re.findall(r"(?im)^sitemap:\s*(\S+)", r.text)
    except httpx.HTTPError:
        pass
    for p in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/sitemaps.xml"):
        if f"{base}{p}" not in candidate:
            candidate.append(f"{base}{p}")
    viste: set[str] = set()
    figlie: list[str] = []
    for sm in candidate[:6]:
        if sm in viste:
            continue
        viste.add(sm)
        try:
            r = client.get(sm)
        except httpx.HTTPError:
            continue
        if r.status_code != 200 or "<" not in r.text[:200]:
            continue
        voci = _loc(r.text)
        if "<sitemapindex" in r.text:
            figlie += voci
        else:
            figlie.append(sm)
    # 1) una sitemap che gia' nel nome parla di offerte
    for f in figlie:
        if PAROLE_OFFERTE.search(f.rsplit("/", 1)[-1]):
            urls = [u for u in _sitemap_urls(client, [f], massimo=5000) if _stesso_sito(u, dominio)]
            if urls:
                return f, urls
    # 2) altrimenti la sitemap con piu' URL dal percorso «da annuncio»
    migliore, migliori = None, []
    for f in figlie[:12]:
        urls = [u for u in _sitemap_urls(client, [f], massimo=5000)
                if _stesso_sito(u, dominio) and PATH_ANNUNCIO.search(urlparse(u).path)]
        if len(urls) > len(migliori):
            migliore, migliori = f, urls
    return (migliore, migliori) if len(migliori) >= 3 else (None, [])


def link_annunci(client: httpx.Client, dominio: str) -> tuple[str | None, list[str]]:
    """Senza sitemap: la pagina carriere (dalla homepage) e i suoi link
    che sembrano annunci, sullo stesso sito."""
    from .detector import _link_carriere
    base = f"https://{dominio}"
    try:
        r = client.get(base)
    except httpx.HTTPError:
        return None, []
    if r.status_code >= 400:
        return None, []
    for url in _link_carriere(str(r.url), r.text)[:4]:
        if not _stesso_sito(url, dominio):
            continue
        try:
            rc = client.get(url)
        except httpx.HTTPError:
            continue
        if rc.status_code != 200:
            continue
        link = {urljoin(str(rc.url), g) for g in re.findall(r'href="([^"#]+)"', rc.text)}
        annunci = sorted(u for u in link if _stesso_sito(u, dominio) and PATH_ANNUNCIO.search(urlparse(u).path)
                         and u.rstrip("/") != str(rc.url).rstrip("/"))
        if len(annunci) >= 2:
            return str(rc.url), annunci
    return None, []


def trova_sorgente(client: httpx.Client, dominio: str) -> dict:
    """La sorgente degli annunci di un dominio, verificata: almeno un
    JobPosting vero su un campione di pagine. Ritorna un dict con
    sorgente, tipo ('sitemap'|'pagina'), candidati, jobposting_trovati."""
    esito = {"dominio": dominio, "sorgente": None, "tipo": None, "candidati": 0, "jobposting": 0, "esempio": None}
    sm, urls = sitemap_offerte(client, dominio)
    tipo = "sitemap"
    if not urls:
        sm, urls = link_annunci(client, dominio)
        tipo = "pagina"
    if not urls:
        return esito
    esito.update(candidati=len(urls))
    for u in urls[:PROVE_SCOPERTA]:
        try:
            r = client.get(u)
        except httpx.HTTPError:
            continue
        if r.status_code != 200:
            continue
        jp = _estrai_ld(r.text)
        if jp and (jp.get("title") or "").strip():
            esito["jobposting"] += 1
            esito["esempio"] = esito["esempio"] or (jp.get("title") or "")[:60]
    if esito["jobposting"]:
        esito.update(sorgente=sm, tipo=tipo)
    return esito


def scopri(dsn: str, limite: int = 150, thread: int = 8, paese: str | None = None) -> dict:
    stats = {"provati": 0, "trovati": 0, "annunci_stimati": 0}
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute("""INSERT INTO ats_platforms (id, name, is_active, api_type, notes)
                      VALUES ('jsonld', 'Career page con JobPosting JSON-LD', true, 'jsonld',
                              'aziende senza ATS: sitemap delle offerte + JSON-LD, come Google for Jobs')
                      ON CONFLICT (id) DO NOTHING""")
        domini = [r[0] for r in db.execute("""
            SELECT domain FROM company_domains
             WHERE status = 'no_ats' AND (jsonld_checked_at IS NULL OR jsonld_checked_at < now() - interval '30 days')
               AND (%s::text IS NULL OR country = %s)
             ORDER BY (country = 'IT') DESC, employees DESC NULLS LAST LIMIT %s""", (paese, paese, limite)).fetchall()]

        def _uno(dom):
            with httpx.Client(timeout=20, headers=UA, follow_redirects=True) as cl:
                try:
                    return trova_sorgente(cl, dom)
                except Exception as exc:  # noqa: BLE001
                    return {"dominio": dom, "sorgente": None, "errore": type(exc).__name__}

        with ThreadPoolExecutor(max_workers=thread) as pool:
            for e in pool.map(_uno, domini):
                stats["provati"] += 1
                db.execute("UPDATE company_domains SET jsonld_checked_at = now() WHERE domain = %s", (e["dominio"],))
                if not e.get("sorgente"):
                    continue
                stats["trovati"] += 1
                stats["annunci_stimati"] += e["candidati"]
                nome, ctry = db.execute("SELECT company_name, country FROM company_domains WHERE domain=%s",
                                        (e["dominio"],)).fetchone()
                db.execute("""INSERT INTO ats_companies (platform_id, slug, company_name, country, discovered_from, sorgente_url)
                              VALUES ('jsonld', %s, %s, %s, 'jsonld', %s)
                              ON CONFLICT (platform_id, slug) DO UPDATE SET sorgente_url = EXCLUDED.sorgente_url,
                                company_name = COALESCE(ats_companies.company_name, EXCLUDED.company_name)""",
                           (e["dominio"], nome if nome and not nome.startswith("Q") else None, ctry, e["sorgente"]))
                db.execute("UPDATE company_domains SET status='jsonld', careers_url=%s, careers_kind='jsonld' WHERE domain=%s",
                           (e["sorgente"], e["dominio"]))
                log.info("  %s: %s (%s), ~%d annunci, es. %r", e["dominio"], e["tipo"], e["sorgente"][:70],
                         e["candidati"], e.get("esempio"))
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.ats.jsonld", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scopri", action="store_true")
    ap.add_argument("--limite", type=int, default=150)
    ap.add_argument("--thread", type=int, default=8)
    ap.add_argument("--paese", default=None)
    ap.add_argument("--prova", nargs="*", help="domini da provare senza scrivere")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    if a.prova:
        with httpx.Client(timeout=20, headers=UA, follow_redirects=True) as cl:
            for dom in a.prova:
                t0 = time.time()
                e = trova_sorgente(cl, dom)
                print(f"{dom:28s} {e['tipo'] or '-':8s} candidati={e['candidati']:5d} jobposting={e['jobposting']}/{PROVE_SCOPERTA} "
                      f"{int(time.time()-t0):3d}s  {(e['sorgente'] or '')[:60]}  es={e['esempio']!r}")
        return 0
    if a.scopri:
        print("scoperta jsonld:", scopri(_dsn(), a.limite, a.thread, a.paese))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
