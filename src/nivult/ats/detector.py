"""Il detector: trova le pagine carriere sui domini aziendali e dice che ATS usano.

    python -m nivult.ats.detector --wikidata          # carica domini da Wikidata
    python -m nivult.ats.detector --produzione        # carica dal DB del motore
    python -m nivult.ats.detector --rileva --limite 200
    python -m nivult.ats.detector --stats

Il giro è: homepage dell'azienda → link "careers/jobs/karriere/…" →
impronte dell'ATS nell'HTML. Le impronte sono stringhe precise
(niente 'tta': falsi positivi su 'attacks', verificato).

Quando il detector identifica una piattaforma:
- se l'URL carriere matcha un pattern del registro (es. myworkdayjobs.com)
  l'azienda entra in ats_companies e lo scrape parte al prossimo giro;
- se è un dominio personalizzato (careers.azienda.com) resta qui, con
  platform_id pronto per quando costruiremo l'adapter.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from urllib.parse import urljoin, urlparse

import httpx
import psycopg

from nivult.ats.registry import REGISTRY

log = logging.getLogger("nivult.ats.detector")

ATS_DSN = os.environ.get(
    "ATS_DATABASE_URL",
    "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")

# Le impronte: stringhe che compaiono nell'HTML della pagina carriere.
# SCELTE CON PRECISIONE: sottostringhe corte o generiche producono falsi
# positivi ('tta' matchava 'attacks' nelle news di AXA — verificato).
FINGERPRINT: dict[str, list[str]] = {
    "phenom": ["phenom.com", "phenompeople", "ph-robot"],
    "eightfold": ["eightfold.ai"],
    "successfactors": ["sapsf.com", "successfactors", "career17"],
    "workday": ["myworkdayjobs.com", "myworkdaysite"],
    "icims": ["icims.com"],
    "taleo": ["taleo.net"],
    "cornerstone": ["csod.com"],
    "greenhouse": ["greenhouse.io"],
    "lever": ["lever.co"],
    "smartrecruiters": ["smartrecruiters.com"],
    "avature": ["avature.net", "avature"],
    "teamtailor": ["teamtailor.com"],
    "personio": ["personio.com"],
    "bamboohr": ["bamboohr.com"],
    "recruitee": ["recruitee.com"],
    "workable": ["workable.com"],
    "jobvite": ["jobvite.com"],
    "pageup": ["pageuppeople"],
    "radancy": ["radancy"],
    "talktalent": ["talktalent"],
    "jobadder": ["jobadder"],
    "pinpoint": ["pinpointhq"],
    "freshteam": ["freshteam.com"],
    "jazzhr": ["applytojob.com"],
    "werecruit": ["werecruit"],
    "zohorecruit": ["zohorecruit"],
    # piattaforme del mercato europeo aggiunte dopo la miniera del censimento
    "paradox": ["paradox.ai"],
    "oracle": ["oraclecloud.com/hcm", "fa.ocs.oraclecloud"],
    "adp": ["workforcenow.adp.com", "adp.com/careers"],
    "ukg": ["ukg.net", "recruiting.ultipro.com", "ukg.com/careers"],
    "jobsoid": ["jobsoid.com"],
    "hiringthing": ["hiringthing.com"],
    "ceipal": ["ceipal.com"],
    "bullhorn": ["bullhorn.com"],
    "jobdiva": ["jobdiva.com"],
    "jibe": ["jibeapply.com"],
    "smashfly": ["smashfly.com"],
    "eploy": ["eploy.co.uk", "eploy.com"],
    "easyrecruitee": ["easyrecruitee"],
    "jobtoolz": ["jobtoolz"],
    "otys": ["otys.com"],
    "carerix": ["carerix"],
    "recruitcrm": ["recruitcrm.io"],
    "jobscore": ["jobscore.com"],
    "softgarden": ["softgarden"],
    "rippling": ["rippling-ats", "rippling.com/careers"],
    "apply2jobs": ["apply2jobs.com"],
    "paylocity": ["paylocity.com/careers", "recruiting.paylocity.com"],
    "paycom": ["paycom.com/careers", "paycomsoftware.net"],
    "firefish": ["firefishsoftware.com"],
    "applicantpro": ["applicantpro.com"],
    "talentsoft": ["talentsoft.com"],
    "jobteaser": ["jobteaser.com"],
    "manatal": ["manatal.com"],
    "comeet": ["comeet.co"],
    "beamery": ["beamery.com"],
    "vidcruiter": ["vidcruiter.com"],
    "hirevue": ["hirevue.com"],
    "welcometothejungle": ["welcometothejungle.com"],
}

# Le parole che identificano un link alla pagina carriere, nelle lingue
# dei paesi che ci interessano.
PAROLE_CARRIERE = re.compile(
    r"careers?|jobs?|karriere|recrutement|recruitment|stellenangebote"
    r"|stellenangebote|lavora|lavoro|carriere|opportunit|emploi|emplois"
    r"|offres|empleo|vacantes|trabaja|urząd|praca", re.I)

PAESI_EU = ["Q142", "Q183", "Q145", "Q38", "Q29", "Q55", "Q754", "Q34",
            "Q36", "Q39", "Q40", "Q31", "Q45", "Q35", "Q27", "Q20", "Q191"]

QUERY_WIKIDATA = """
SELECT ?cLabel ?sito ?paese ?dip WHERE {
  ?c wdt:P856 ?sito .
  ?c wdt:P17 ?pa .
  FILTER(?pa IN (%PAESI%))
  { ?c wdt:P31 wd:Q4830453 . } UNION { ?c wdt:P31 wd:Q6881511 . } UNION { ?c wdt:P31 wd:Q13360664 . }
  OPTIONAL { ?c wdt:P1128 ?dip . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

# Il giro esteso: 'company' (Q783794) e 'publicly traded company' (Q891723)
# valgono da sole decine di migliaia di aziende europee. La query è più
# lenta e va in timeout ogni tanto: si usa nel giro settimanale, con
# i retry per paese che già我们有.
QUERY_WIKIDATA_ESTESA = """
SELECT ?cLabel ?sito ?paese ?dip WHERE {
  ?c wdt:P856 ?sito .
  ?c wdt:P17 ?pa .
  FILTER(?pa IN (%PAESI%))
  { ?c wdt:P31 wd:Q4830453 . } UNION { ?c wdt:P31 wd:Q6881511 . }
  UNION { ?c wdt:P31 wd:Q13360664 . } UNION { ?c wdt:P31 wd:Q783794 . }
  UNION { ?c wdt:P31 wd:Q891723 . } UNION { ?c wdt:P31 wd:Q167667 . }
  OPTIONAL { ?c wdt:P1128 ?dip . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""


def _estrai_iso(paese_uri: str) -> str | None:
    """'http://www.wikidata.org/entity/Q142' → 'FR' (solo paesi UE noti)."""
    ISO = {"Q142": "FR", "Q183": "DE", "Q145": "GB", "Q38": "IT", "Q29": "ES",
           "Q55": "NL", "Q754": "SE", "Q34": "PL", "Q36": "CH", "Q39": "AT",
           "Q40": "BE", "Q45": "PT", "Q35": "DK", "Q27": "IE", "Q20": "NO",
           "Q191": "FI"}
    qid = paese_uri.rstrip("/").split("/")[-1]
    return ISO.get(qid)


def carica_wikidata(dsn: str, limite: int = 0, estesa: bool = False) -> int:
    """Scarica le aziende europee con sito ufficiale da Wikidata.

    Una query per paese: la risposta completa (17 paesi insieme) supera
    i 9MB e la connessione si tronca a metà JSON — verificato.
    Con estesa=True aggiunge 'company' e 'publicly traded company':
    decine di migliaia di aziende in più, per il giro settimanale.
    """
    template = QUERY_WIKIDATA_ESTESA if estesa else QUERY_WIKIDATA
    inseriti = 0
    for paese_qid in PAESI_EU:
        q = template.replace("%PAESI%", f"wd:{paese_qid}")
        for tentativo in range(3):
            try:
                r = httpx.get("https://query.wikidata.org/sparql",
                              params={"query": q, "format": "json"},
                              headers={"User-Agent": "nivult-ats/0.1 (research)"},
                              timeout=180)
                r.raise_for_status()
                righe = r.json()["results"]["bindings"]
                break
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("  wikidata %s tentativo %d: %s",
                            paese_qid, tentativo + 1, str(exc)[:60])
                righe = []
        if limite and len(righe) > limite:
            righe = righe[:limite]

        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                for b in righe:
                    dominio = urlparse(b["sito"]["value"]).netloc.lower()
                    dominio = dominio.removeprefix("www.")
                    if not dominio or "." not in dominio:
                        continue
                    dip = None
                    if "dip" in b:
                        try:
                            dip = int(float(b["dip"]["value"]))
                        except (ValueError, KeyError):
                            pass
                    cur.execute("""
                        INSERT INTO company_domains
                          (domain, company_name, country, employees, source)
                        VALUES (%s, %s, %s, %s, 'wikidata')
                        ON CONFLICT (domain) DO UPDATE SET
                          company_name = EXCLUDED.company_name,
                          country = EXCLUDED.country,
                          employees = EXCLUDED.employees
                    """, (dominio, b["cLabel"]["value"],
                          _estrai_iso(paese_qid), dip))
                    inseriti += 1
            conn.commit()
        log.info("  wikidata %s: %d righe", paese_qid, len(righe))
    log.info("Wikidata: %s domini caricati", inseriti)
    return inseriti


def carica_produzione(dsn: str, dsn_prod: str) -> int:
    """I domini azienda reali visti dal motore (jobs.domain_derived)."""
    inseriti = 0
    with psycopg.connect(dsn_prod) as prod:
        with prod.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT domain_derived, organization
                FROM jobs
                WHERE domain_derived IS NOT NULL AND domain_derived != ''
            """)
            righe = cur.fetchall()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for dominio, nome in righe:
                dominio = (dominio or "").lower().removeprefix("www.").strip()
                if not dominio or "." not in dominio:
                    continue
                cur.execute("""
                    INSERT INTO company_domains
                      (domain, company_name, source)
                    VALUES (%s, %s, 'production')
                    ON CONFLICT (domain) DO UPDATE SET
                      company_name = COALESCE(company_domains.company_name,
                                              EXCLUDED.company_name)
                """, (dominio, nome))
                inseriti += 1
        conn.commit()
    log.info("Produzione: %s domini caricati", inseriti)
    return inseriti


def _trova_impronte(html: str) -> list[str]:
    h = html.lower()
    return [p for p, marche in FINGERPRINT.items()
            if any(m in h for m in marche)]


def _link_carriere(home_url: str, html: str) -> list[str]:
    """I link alla pagina carriere che la homepage dichiara."""
    base = str(home_url)
    grezzi = re.findall(r'href="([^"]+)"', html)
    candidati = []
    for g in grezzi:
        if not PAROLE_CARRIERE.search(g):
            continue
        url = urljoin(base, g)
        if not urlparse(url).netloc:
            continue
        if url.endswith((".css", ".js", ".png", ".jpg", ".svg", ".ico", ".json")):
            continue
        candidati.append(url)
    # i link su dominio diverso (es. jobs.azienda.com) sono i più promettenti
    dominio_home = urlparse(base).netloc
    candidati.sort(key=lambda u: 0 if urlparse(u).netloc != dominio_home else 1)
    return list(dict.fromkeys(candidati))[:4]


def _pattern_registro(url: str) -> tuple[str, str] | None:
    """Se l'URL carriere matcha un pattern del registro, (piattaforma, slug)."""
    INFRA = {"www", "cdn", "static", "api", "assets", "app", "jobs",
             "careers", "career", "apply", "boards", "help", "support",
             "tt", "pp-cdn"}
    for p in REGISTRY:
        pattern = p.get("url_pattern", "")
        if not pattern:
            continue
        m = re.search(pattern, url)
        if m and m.lastindex and m.group(1):
            slug = m.group(1).lower()
            if ("." not in slug and len(slug) > 2 and slug not in INFRA
                    and "@" not in slug and "cdn" not in slug):
                return p["id"], slug
    return None


def _url_piattaforma_in_html(html: str) -> tuple[str, str, str] | None:
    """L'URL della piattaforma nascosto nella pagina carriere.

    Molte aziende tengono la vetrina su careers.azienda.com ma l'ATS
    traspare dai link agli annunci: es. myworkdayjobs.com dentro un
    pulsante 'Tutte le offerte'. Se lo troviamo, l'azienda diventa
    scrapeable subito. Ritorna (piattaforma, slug, url_completo).
    """
    # trova tutti gli URL assoluti nella pagina
    for url in re.findall(r'https?://[a-z0-9.-]+[^\s"\'<>)]*', html.lower()):
        hit = _pattern_registro(url)
        if hit:
            return hit[0], hit[1], url
    return None


def _registra_azienda(dsn: str, piattaforma: str, slug: str,
                      nome: str | None, paese: str | None,
                      url_piattaforma: str = "") -> None:
    """Un rilevamento con URL di piattaforma: entra in ats_companies."""
    wd_server, wd_instance = None, None
    if piattaforma == "workday":
        # es. https://acme.wd3.myworkdayjobs.com/it-IT/ACME
        m = re.search(r'\.(wd\d+)\.myworkdayjobs\.com/(?:[^/?]+/)?([^/?]+)',
                      url_piattaforma)
        if m:
            wd_server = m.group(1)
            wd_instance = m.group(2)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ats_companies
                  (platform_id, slug, company_name, country, discovered_from,
                   wd_server, wd_instance)
                VALUES (%s, %s, %s, %s, 'detector', %s, %s)
                ON CONFLICT (platform_id, slug) DO UPDATE SET
                  company_name = COALESCE(ats_companies.company_name,
                                          EXCLUDED.company_name)
            """, (piattaforma, slug, nome, paese, wd_server, wd_instance))
        conn.commit()


def _analizza_dominio(client: httpx.Client, dominio: str) -> tuple:
    """Il lavoro HTTP per un dominio. Puro, niente DB: parallelizzabile.

    Ritorna (esito, piattaforma, careers_url, kind, html_carriere).
    """
    esito, piattaforma, careers_url, kind = "no_careers", None, None, None
    html_carriere = ""
    try:
        r = client.get(f"https://{dominio}")
        if r.status_code >= 400:
            esito = "dead"
        else:
            # 1) l'ATS può già dichiararsi in homepage
            impronte = _trova_impronte(r.text)
            if impronte:
                piattaforma = impronte[0]
                careers_url = str(r.url)
                kind = "custom"
                esito = "ats"
                html_carriere = r.text
            else:
                # 2) segue il link carriere
                link = _link_carriere(str(r.url), r.text)
                for url in link:
                    try:
                        rc = client.get(url)
                        if rc.status_code != 200 or len(rc.text) < 300:
                            continue
                        impronte = _trova_impronte(rc.text)
                        if impronte:
                            piattaforma = impronte[0]
                            careers_url = str(rc.url)
                            kind = "custom"
                            esito = "ats"
                            html_carriere = rc.text
                            break
                    except httpx.HTTPError:
                        continue
                if esito != "ats" and not link:
                    esito = "no_careers"
                elif esito != "ats":
                    esito = "no_ats"
    except httpx.HTTPError:
        esito = "dead"
    except Exception:  # noqa: BLE001
        esito = "error"
    return esito, piattaforma, careers_url, kind, html_carriere


def _renderizza_e_analizza(dominio: str) -> tuple:
    """Variante Playwright: due livelli di navigazione con fingerprint.

    Per i siti JS delle grandi aziende: la homepage è marketing, la
    pagina carriere è una landing del gruppo, e l'ATS si vede solo
    sulla pagina di ricerca offerte, due clic più in profondità.
    Livello 1: homepage → pagina carriere.
    Livello 2: pagina carriere → pagina 'cerca offerte/vacancies'.
    Fingerprint a ogni passo.
    """
    esito, piattaforma, careers_url, kind = "no_careers", None, None, None
    html_carriere = ""
    # le parole dei link alla pagina RICERCA offerte (più strette di
    # quelle della pagina carriere: evitano di vagare per il sito)
    PAROLE_RICERCA = re.compile(
        r'search[-_]?jobs?|job[-_]?search|vacanc|stellenangebote'
        r'|offres?-d|job-listings|open[-_]?positions?|find-a-job'
        r'|job-openings|current[-_]?openings|job-angebote|vagas', re.I)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            # LIVELLO 0: i sottodomini carriere ipotizzati — molte big
            # (Siemens, Bosch) non linkano il sito carriere dalla
            # homepage: è un dominio a parte (jobs.azienda.com)
            url_home = None
            html = ""
            for candidato in (f"https://{dominio}",
                              f"https://jobs.{dominio}",
                              f"https://careers.{dominio}",
                              f"https://career.{dominio}"):
                try:
                    page.goto(candidato, wait_until="domcontentloaded",
                              timeout=20000)
                    page.wait_for_timeout(6000)
                    h = page.content()
                except Exception:
                    continue
                impronte = _trova_impronte(h)
                if impronte:
                    browser.close()
                    return ("ats", impronte[0], page.url, "custom", h)
                # il primo che risponde con contenuto vero è la base
                if url_home is None and len(h) > 5000:
                    url_home, html = page.url, h

            if url_home is None:
                browser.close()
                return ("dead", None, None, None, "")

            # LIVELLO 1: la pagina carriere
            link_carriere = _link_carriere(url_home, html)[:2]
            for url_c in link_carriere:
                try:
                    page.goto(url_c, wait_until="domcontentloaded",
                              timeout=25000)
                    page.wait_for_timeout(6000)
                    html_c = page.content()
                except Exception:
                    continue
                impronte = _trova_impronte(html_c)
                if impronte:
                    browser.close()
                    return ("ats", impronte[0], page.url, "custom", html_c)

                # LIVELLO 2: la pagina di ricerca offerte
                grezzi = re.findall(r'href="([^"]+)"', html_c)
                candidati = [urljoin(str(page.url), g) for g in grezzi
                             if PAROLE_RICERCA.search(g)]
                candidati = [c for c in dict.fromkeys(candidati)
                             if urlparse(c).netloc][:2]
                for url_r in candidati:
                    try:
                        page.goto(url_r, wait_until="domcontentloaded",
                                  timeout=25000)
                        page.wait_for_timeout(7000)
                        html_r = page.content()
                    except Exception:
                        continue
                    impronte = _trova_impronte(html_r)
                    if impronte:
                        browser.close()
                        return ("ats", impronte[0], page.url, "custom",
                                html_r)

            if link_carriere:
                esito = "no_ats"
            browser.close()
    except Exception:
        esito = "error"
    return esito, piattaforma, careers_url, kind, html_carriere


def rileva(dsn: str, limite: int = 200, solo_grandi: bool = False,
           thread: int = 16) -> dict:
    """Il giro di riconoscimento sui domini in attesa.

    I domini sono tutti siti diversi: connessioni simultanee verso
    host diversi non sovraccaricano nessuno, quindi si parallelizza
    con un pool di thread (l'HTTP rilascia il GIL).
    """
    stats = {"visitati": 0, "ats": 0, "no_ats": 0, "no_careers": 0,
             "dead": 0, "error": 0}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            sql = ("SELECT domain, company_name, country, employees "
                   "FROM company_domains WHERE status = 'pending'")
            if solo_grandi:
                sql += " AND employees >= 500"
            sql += (" ORDER BY employees DESC NULLS LAST, domain "
                    f"LIMIT {int(limite)}")
            cur.execute(sql)
            domini = cur.fetchall()

    log.info("Detector: %s domini da visitare (%s thread)",
             len(domini), thread)
    if not domini:
        return stats

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def lavoro(riga):
        dominio, nome, paese, dip = riga
        with httpx.Client(timeout=12, follow_redirects=True,
                          headers={"User-Agent": "nivult-ats/0.1"}) as client:
            return riga, _analizza_dominio(client, dominio)

    with ThreadPoolExecutor(max_workers=thread) as pool:
        futures = [pool.submit(lavoro, riga) for riga in domini]
        for fut in as_completed(futures):
            try:
                riga, risultato = fut.result()
            except Exception:  # noqa: BLE001
                stats["error"] += 1
                continue
            dominio, nome, paese, dip = riga
            esito, piattaforma, careers_url, kind = risultato[:4]
            html_carriere = risultato[4] if len(risultato) > 4 else ""
            stats["visitati"] += 1

            if esito == "ats" and piattaforma:
                stats["ats"] += 1
                # se l'URL carriere o uno dentro la pagina è di piattaforma
                # (pattern del registro), l'azienda è subito scrapeable
                hit = (_pattern_registro(careers_url or "")
                       or _url_piattaforma_in_html(html_carriere))
                if hit:
                    pid, slug = hit[0], hit[1]
                    url_p = hit[2] if len(hit) > 2 else careers_url
                    _registra_azienda(dsn, pid, slug, nome, paese, url_p)
                    kind = "platform"
                    log.info("  %s → %s (%s, scrapeable)", dominio, pid, slug)
                else:
                    log.info("  %s → %s (custom domain)", dominio,
                             piattaforma)
            else:
                stats[esito if esito in stats else "error"] += 1

            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE company_domains SET status = %s,
                          platform_id = %s, careers_url = %s,
                          careers_kind = %s, checked_at = now()
                        WHERE domain = %s
                    """, (esito, piattaforma, careers_url, kind, dominio))
                conn.commit()

            if stats["visitati"] % 500 == 0:
                log.info("  … %d visitati: %s", stats["visitati"], stats)

    return stats


def rileva_render(dsn: str, limite: int = 200, dip_minimi: int = 1000,
                  thread: int = 4) -> dict:
    """Riprova con Playwright i domini grossi finiti 'no_ats' o 'dead'.

    Molti siti carriere delle grandi aziende sono app JavaScript:
    l'HTML statico non contiene le impronte, ma dopo il rendering sì.
    Seccatura: un browser per thread, si va lenti di proposito.
    """
    stats = {"visitati": 0, "ats": 0, "no_ats": 0, "no_careers": 0,
             "dead": 0, "error": 0}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT domain, company_name, country, employees
                  FROM company_domains
                 WHERE status IN ('no_ats', 'dead', 'no_careers')
                   AND COALESCE(employees, 0) >= %s
                 ORDER BY employees DESC
                 LIMIT %s
            """, (dip_minimi, int(limite)))
            domini = cur.fetchall()

    log.info("Detector render: %s domini grandi da riprovare", len(domini))
    if not domini:
        return stats

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def lavoro(riga):
        dominio = riga[0]
        return riga, _renderizza_e_analizza(dominio)

    with ThreadPoolExecutor(max_workers=thread) as pool:
        futures = [pool.submit(lavoro, riga) for riga in domini]
        for fut in as_completed(futures):
            try:
                riga, risultato = fut.result()
            except Exception:  # noqa: BLE001
                stats["error"] += 1
                continue
            dominio, nome, paese, dip = riga
            esito, piattaforma, careers_url, kind = risultato[:4]
            html_carriere = risultato[4] if len(risultato) > 4 else ""
            stats["visitati"] += 1
            if esito == "ats" and piattaforma:
                stats["ats"] += 1
                hit = (_pattern_registro(careers_url or "")
                       or _url_piattaforma_in_html(html_carriere))
                if hit:
                    pid, slug = hit[0], hit[1]
                    url_p = hit[2] if len(hit) > 2 else careers_url
                    _registra_azienda(dsn, pid, slug, nome, paese, url_p)
                    kind = "platform"
                    log.info("  %s → %s (%s, scrapeable)", dominio, pid,
                             slug)
                else:
                    log.info("  %s → %s (custom)", dominio, piattaforma)
            else:
                stats[esito if esito in stats else "error"] += 1

            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE company_domains SET status = %s,
                          platform_id = %s, careers_url = %s,
                          careers_kind = %s, checked_at = now()
                        WHERE domain = %s
                    """, (esito, piattaforma, careers_url, kind, dominio))
                conn.commit()
            if stats["visitati"] % 50 == 0:
                log.info("  … %d renderizzati: %s", stats["visitati"], stats)
    return stats


def stats(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT status, count(*) FROM company_domains
                GROUP BY 1 ORDER BY 2 DESC
            """)
            print("\nStato del censimento domini:")
            for s, n in cur.fetchall():
                print(f"  {s:12s} {n:>7d}")
            cur.execute("""
                SELECT platform_id, count(*) FROM company_domains
                WHERE status = 'ats' GROUP BY 1 ORDER BY 2 DESC
            """)
            print("\nPiattaforme rilevate:")
            for p, n in cur.fetchall():
                print(f"  {p:20s} {n:>5d}")
            cur.execute("""
                SELECT count(*) FROM ats_companies
                WHERE discovered_from = 'detector'
            """)
            print(f"\nAziende rese scrapeable dal detector: {cur.fetchone()[0]}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-8s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.detector",
                                 description=__doc__)
    ap.add_argument("--wikidata", action="store_true",
                    help="carica i domini aziendali da Wikidata")
    ap.add_argument("--wikidata-estesa", action="store_true",
                    help="Wikidata con classi extra (company, quotata): giro settimanale")
    ap.add_argument("--produzione", action="store_true",
                    help="carica i domini dal DB del motore")
    ap.add_argument("--rileva", action="store_true",
                    help="gira il riconoscimento sui domini in attesa")
    ap.add_argument("--solo-grandi", action="store_true",
                    help="solo aziende con 500+ dipendenti (più alte probabilità)")
    ap.add_argument("--limite", type=int, default=200,
                    help="quanti domini visitare per giro (default 200)")
    ap.add_argument("--thread", type=int, default=16,
                    help="thread paralleli (default 16)")
    ap.add_argument("--render", action="store_true",
                    help="riprova con Playwright i grossi domini no_ats/dead")
    ap.add_argument("--dip-minimi", type=int, default=1000,
                    help="soglia dipendenti per il render (default 1000)")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)

    if args.wikidata:
        carica_wikidata(ATS_DSN)
    if args.wikidata_estesa:
        carica_wikidata(ATS_DSN, estesa=True)
    if args.produzione:
        dsn_prod = os.environ.get(
            "DATABASE_URL",
            "postgresql://giusepperanno@127.0.0.1:5432/nivult_dev")
        carica_produzione(ATS_DSN, dsn_prod)
    if args.rileva:
        s = rileva(ATS_DSN, args.limite, args.solo_grandi, args.thread)
        print(f"\nDetector: {s}")
    if args.render:
        s = rileva_render(ATS_DSN, args.limite, args.dip_minimi,
                          min(args.thread, 4))
        print(f"\nDetector render: {s}")
    if args.stats:
        stats(ATS_DSN)
    if not (args.wikidata or args.wikidata_estesa or args.produzione
            or args.rileva or args.render or args.stats):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
