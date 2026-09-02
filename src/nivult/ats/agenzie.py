"""Le agenzie per il lavoro italiane, lette alla fonte.

Le agenzie valgono la fetta piu' grossa del mercato italiano degli
annunci — e pubblicano tutto in chiaro, perche' vogliono stare su
Google for Jobs: sitemap dedicata alle offerte + JSON-LD JobPosting
su ogni pagina di dettaglio. Misurato il 2026-09-03:

    randstad.it      5.000 URL   JSON-LD presente
    gigroup.it       3.361 URL   JSON-LD presente
    umana.it         1.092 URL   JSON-LD presente
    adecco.com/it    6.320 URL   JSON-LD presente (sitemap per paese)
    manpower.it      8.000 URL   JSON-LD presente
    openjobmetis.it  1.916 URL   JSON-LD ASSENTE (parse HTML, dopo)

Il giro e' semplice e onesto: si legge la sitemap delle offerte, si
scaricano solo le pagine MAI viste (le altre si rinfrescano solo nel
timestamp), si estrae il JobPosting. Niente browser, niente login,
nessun termine violato: e' il canale che le agenzie stesse offrono
ai motori di ricerca.
"""

from __future__ import annotations

import argparse
import logging
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import httpx
import psycopg

from .runner import ATS_DSN

log = logging.getLogger("nivult.ats.agenzie")

UA = {"User-Agent": "Mozilla/5.0 (compatible; nivult-ats/1.0; "
                    "+https://nivult.com)"}

# Solo agenzie con JSON-LD verificato a mano entrano qui; le altre
# aspettano il loro lettore. La sitemap e' quella DELLE OFFERTE, non
# quella generale del sito.
AGENZIE = {
    "randstad": {
        "nome": "Randstad Italia",
        "sitemaps": [
            "https://www.randstad.it/sitemaps/jobs/it/sitemap-jobdetails.xml",
            "https://www.randstad.it/sitemaps/jobs/it/sitemap-jobdetails-2.xml",
        ],
    },
    "gigroup": {
        "nome": "Gi Group",
        "sitemaps": ["https://www.gigroup.it/offerte-sitemap.xml"],
    },
    "umana": {
        "nome": "Umana",
        "sitemaps": ["https://www.umana.it/jobs-sitemap.xml"],
    },
    # Adecco e' migrata sulla piattaforma globale adecco.com: una
    # sitemap-offerte per paese (60 paesi!). Qui l'Italia; Francia,
    # Germania e Spagna sono a un rigo di distanza quando serviranno.
    "adecco": {
        "nome": "Adecco Italia",
        "sitemaps": ["https://www.adecco.com/sitemap-jobs-italy-it.xml"],
    },
    "manpower": {
        "nome": "Manpower Italia",
        "sitemaps": ["https://www.manpower.it/sitemap/italy/"
                     "it-manpower/sitemap_job-offer.xml"],
    },
    # Helplavoro e' un portale dove le agenzie pubblicano direttamente:
    # 34.482 URL in quattro sitemap, JSON-LD su ogni annuncio. Portera'
    # doppioni delle agenzie che leggiamo gia' alla fonte — e' il ponte
    # e il dedup del motore a doverli riconoscere, non questo lettore.
    "helplavoro": {
        "nome": "Helplavoro (portale agenzie)",
        "sitemaps": [f"https://www.helplavoro.it/xmlofferte{i}/sitemap.xml"
                     for i in (1, 2, 3, 4)],
    },
    # Eurointerim non pubblica JSON-LD: si legge l'HTML, che per fortuna
    # e' statico e ordinato (h1 = titolo, «Luogo di lavoro: Citta' (PR)»).
    "eurointerim": {
        "nome": "Eurointerim",
        "sitemaps": ["https://www.eurointerim.it/job-sitemap.xml",
                     "https://www.eurointerim.it/job-sitemap2.xml"],
        "html": {
            "titolo": r"<h1[^>]*>(.*?)</h1>",
            "luogo": r"Luogo di lavoro[^:]*:\s*(?:</?\w+>|\s)*([^<]{2,60})",
        },
    },
}


def _loc(xml: str) -> list[str]:
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)


def _sitemap_urls(client: httpx.Client, sitemaps: list[str]) -> set[str]:
    """Gli URL offerta, seguendo un livello di sitemapindex se serve."""
    urls: set[str] = set()
    for sm in sitemaps:
        try:
            corpo = client.get(sm).text
        except httpx.HTTPError as e:
            log.warning("sitemap %s: %s", sm, e)
            continue
        voci = _loc(corpo)
        if "<sitemapindex" in corpo:
            for figlio in voci:
                try:
                    urls.update(_loc(client.get(figlio).text))
                except httpx.HTTPError as e:
                    log.warning("sitemap figlia %s: %s", figlio, e)
        else:
            urls.update(voci)
    return urls


def _jobposting(doc):
    """Trova il JobPosting in un documento ld+json (lista/@graph/dict)."""
    if isinstance(doc, list):
        for d in doc:
            jp = _jobposting(d)
            if jp:
                return jp
        return None
    if not isinstance(doc, dict):
        return None
    if doc.get("@type") in ("JobPosting", ["JobPosting"]):
        return doc
    return _jobposting(doc.get("@graph"))


def _estrai_ld(html: str):
    for blocco in re.findall(
            r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
            html, re.S | re.I):
        try:
            jp = _jobposting(json.loads(blocco.strip()))
        except ValueError:
            # il JSON-LD delle agenzie e' spesso scritto a mano: Umana
            # ci lascia dentro commenti «//» e virgole pendenti. Google
            # li perdona, quindi li perdoniamo anche noi — ma solo in
            # forma conservativa: righe-commento intere e virgole prima
            # di } o ], mai dentro le stringhe (gli https:// ringraziano)
            pulito = "\n".join(r for r in blocco.strip().splitlines()
                               if not r.lstrip().startswith("//"))
            pulito = re.sub(r",\s*([}\]])", r"\1", pulito)
            try:
                jp = _jobposting(json.loads(pulito))
            except ValueError:
                continue
        if jp:
            return jp
    return None


def _estrai_html(html: str, regole: dict):
    """Un JobPosting minimo cavato dall'HTML, per i siti senza JSON-LD."""
    m = re.search(regole["titolo"], html, re.S | re.I)
    if not m:
        return None
    titolo = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    if not titolo:
        return None
    jp = {"title": titolo}
    ml = re.search(regole["luogo"], html, re.S | re.I)
    if ml:
        luogo = ml.group(1).strip().strip(":").strip()
        jp["jobLocation"] = {"address": {"addressLocality": luogo,
                                         "addressCountry": "IT"}}
    return jp


def _data(jp) -> datetime | None:
    for chiave in ("datePosted", "datePublished"):
        v = jp.get(chiave)
        if not v:
            continue
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _luogo(jp) -> tuple[str | None, str | None]:
    posti = jp.get("jobLocation")
    if isinstance(posti, dict):
        posti = [posti]
    for p in posti or []:
        ind = (p or {}).get("address") or {}
        citta = (ind.get("addressLocality") or "").strip() or None
        paese = (ind.get("addressCountry") or "").strip() or None
        if isinstance(paese, dict):
            paese = (paese.get("name") or "").strip() or None
        if citta or paese:
            return citta, (paese[:2].upper() if paese else None)
    return None, None


def raccogli(dsn: str, quali: list[str] | None, limite: int,
             thread: int) -> dict:
    esito: dict = {}
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ats_platforms (id, name, is_active, api_type, notes)
            VALUES ('agenzie', 'Agenzie per il lavoro (sitemap+JSON-LD)',
                    true, 'jsonld',
                    'il canale che le agenzie offrono a Google for Jobs')
            ON CONFLICT (id) DO NOTHING""")
        conn.commit()

    for slug, cfg in AGENZIE.items():
        if quali and slug not in quali:
            continue
        stats = {"in_sitemap": 0, "nuove": 0, "senza_ld": 0, "errori": 0}
        with httpx.Client(timeout=25, headers=UA,
                          follow_redirects=True) as client:
            urls = _sitemap_urls(client, cfg["sitemaps"])
            stats["in_sitemap"] = len(urls)
            with psycopg.connect(dsn) as conn:
                gia = {r[0] for r in conn.execute(
                    "SELECT external_id FROM ats_jobs "
                    "WHERE platform_id='agenzie' AND slug=%s", (slug,))}
                # le offerte gia' viste e ancora in sitemap: vive, si
                # rinfresca il timestamp cosi' la manutenzione non le spegne
                ancora = list(urls & gia)
                for i in range(0, len(ancora), 500):
                    conn.execute(
                        "UPDATE ats_jobs SET fetched_at = now() "
                        "WHERE platform_id='agenzie' AND slug=%s "
                        "AND external_id = ANY(%s)",
                        (slug, ancora[i:i + 500]))
                conn.commit()
            nuove = sorted(urls - gia)[:limite]

            def _scarica(u: str):
                try:
                    r = client.get(u)
                    if r.status_code != 200:
                        return u, None, f"http {r.status_code}"
                    jp = _estrai_ld(r.text)
                    if not jp and cfg.get("html"):
                        jp = _estrai_html(r.text, cfg["html"])
                    return u, jp, None
                except httpx.HTTPError as e:
                    return u, None, str(e)

            with psycopg.connect(dsn) as conn, \
                    ThreadPoolExecutor(max_workers=thread) as pool:
                futs = [pool.submit(_scarica, u) for u in nuove]
                for f in as_completed(futs):
                    u, jp, err = f.result()
                    if err:
                        stats["errori"] += 1
                        continue
                    if not jp:
                        stats["senza_ld"] += 1
                        continue
                    titolo = (jp.get("title") or "").strip()
                    if not titolo:
                        stats["senza_ld"] += 1
                        continue
                    citta, paese = _luogo(jp)
                    org = jp.get("hiringOrganization") or {}
                    if isinstance(org, dict) and org.get("name"):
                        jp.setdefault("company", {"name": org["name"]})
                    conn.execute("""
                        INSERT INTO ats_jobs
                          (platform_id, slug, external_id, title, url,
                           location, country, city, posted_at,
                           employment_type, raw)
                        VALUES ('agenzie', %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (platform_id, external_id) DO UPDATE SET
                          title = EXCLUDED.title,
                          city = EXCLUDED.city, raw = EXCLUDED.raw,
                          fetched_at = now()
                    """, (slug, u, titolo[:300], u, citta,
                          paese or "IT", citta, _data(jp),
                          (str(jp.get("employmentType") or "")[:40] or None),
                          psycopg.types.json.Json(jp)))
                    stats["nuove"] += 1
                    if stats["nuove"] % 200 == 0:
                        conn.commit()
                        time.sleep(1)          # respiro per il sito ospite
                conn.commit()
        esito[slug] = stats
        log.info("%s: %s", slug, stats)
    return esito


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agenzie", default="",
                    help="lista separata da virgole; vuoto = tutte")
    ap.add_argument("--limite", type=int, default=6000,
                    help="pagine nuove per agenzia per corsa")
    ap.add_argument("--thread", type=int, default=6)
    args = ap.parse_args()
    quali = [a.strip() for a in args.agenzie.split(",") if a.strip()]
    print(raccogli(ATS_DSN, quali or None, args.limite, args.thread))


if __name__ == "__main__":
    main()
