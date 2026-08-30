"""L'esplosione del censimento: da 45k a 200k+ domini aziendali europei.

    python -m nivult.ats.esplosione_censimento --gleif --paesi IT,FR,DE,GB
    python -m nivult.ats.esplosione_censimento --stats

DUE FONTI PARALLELE:

1. GLEIF (Legal Entity Identifier) — API gratuita, nessuna key:
   257k aziende italiane, 200k+ francesi, 200k+ tedesche.
   Dà i NOMI legali ma non i domini — il dominio si trova con
   una query DNS: nomeazienda.it esiste? → sì → censito.

2. DNS PROBING — per ogni nome azienda, prova i TLD del suo paese:
   "Acme SpA" → acme.it, acme.com, acme-spa.it
   Una query DNS per combinazione: ~1000/secondo con thread.

3. SCHEMA.ORG FALLBACK — per le aziende censite senza ATS riconosciuto:
   leggi la pagina /careers, cerca JSON-LD JobPosting, sitemap.xml.
   Non serve sapere quale ATS usano: il JSON-LD è standard.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import socket
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlparse

import httpx
import psycopg

log = logging.getLogger("nivult.ats.esplosione_censimento")

ATS_DSN = os.environ.get(
    "ATS_DATABASE_URL",
    "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")


# ── 1. GLEIF: scarica i nomi aziendali ───────────────────────────

GLEIF_PAESI = {
    "IT": "IT", "FR": "FR", "DE": "DE", "GB": "GB", "ES": "ES",
    "NL": "NL", "BE": "BE", "CH": "CH", "AT": "AT", "PL": "PL",
    "PT": "PT", "IE": "IE", "SE": "SE", "DK": "DK", "NO": "NO",
    "FI": "FI",
}


def scarica_gleif(dsn: str, paesi: str, per_paese: int = 2000) -> dict:
    """Scarica i nomi aziendali da GLEIF per i paesi richiesti."""
    stats = {"aziende": 0, "inserite": 0}
    paesi_list = paesi.split(",")

    with httpx.Client(timeout=30,
                      headers={"Accept": "application/vnd.api+json"}) as c:
        for iso in paesi_list:
            if iso not in GLEIF_PAESI:
                continue
            page = 1
            scaricate = 0
            # GLEIF: massimo 10.000 risultati per query (50 pagine × 200)
            MAX_PAGINE = min(per_paese // 200 + 1, 50)
            while scaricate < per_paese and page <= MAX_PAGINE:
                try:
                    r = c.get("https://api.gleif.org/api/v1/lei-records",
                              params={
                                  "filter[entity.legalAddress.country]": iso,
                                  "page[size]": "200",
                                  "page[number]": str(page),
                              })
                    if r.status_code != 200:
                        break
                    data = r.json().get("data", [])
                    if not data:
                        break
                except httpx.HTTPError:
                    break

                for item in data:
                    attr = item.get("attributes", {})
                    entity = attr.get("entity", {})
                    # legalName è un dict {"name": ..., "language": ...}
                    ln = entity.get("legalName") or {}
                    nome = ln.get("name", "") if isinstance(ln, dict) else str(ln or "")
                    if not nome or len(nome) < 3:
                        continue
                    stats["aziende"] += 1
                    scaricate += 1

                    # genera il dominio candidato dal nome
                    dominio = _nome_a_dominio(nome, iso)
                    if not dominio:
                        continue

                    # inserisci nel censimento (senza verificare —
                    # il detector lo farà nel giro notturno)
                    with psycopg.connect(dsn) as conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO company_domains
                                  (domain, company_name, country, source)
                                VALUES (%s, %s, %s, 'gleif')
                                ON CONFLICT (domain) DO NOTHING
                            """, (dominio, nome[:200], iso))
                            if cur.rowcount:
                                stats["inserite"] += 1
                        conn.commit()

                page += 1
                if page % 5 == 0:
                    log.info("  %s: %d scaricate (pagina %d)",
                             iso, scaricate, page)

    return stats


def _nome_a_dominio(nome: str, iso: str) -> str | None:
    """Genera il dominio candidato dal nome legale aziendale.

    "ACME INDUSTRIES SPA" → "acme-industries.it"
    Semplificato: rimuove Suffissi (SPA, SRL, GMBH, LTD, SAS, SA…),
    spazi → trattini, minuscolo.
    """
    nome = nome.lower().strip()
    # rimuovi suffissi legali comuni
    suffissi = [
        "s.p.a.", "s.p.a", "spa", "s.r.l.", "s.r.l", "srl",
        "gmbh", "ag", "ltd", "limited", "plc", "sas", "sa", "s.a.s.",
        "b.v.", "bv", "nv", "ab", "as", "oy", "aps", "s.a.", "s.a",
        "sociedad", "limitada", "s.l.", "sl", "pvt", "inc", "llc",
        "holdings", "holding", "group", "gruppo",
    ]
    for s in suffissi:
        if nome.endswith(" " + s):
            nome = nome[: -len(s)].strip()
            break

    # solo lettere, numeri e trattini
    nome = re.sub(r"[^a-z0-9\s-]", "", nome)
    nome = re.sub(r"\s+", "-", nome.strip())
    if len(nome) < 3 or len(nome) > 40:
        return None

    # TLD in base al paese
    TLD = {"IT": ".it", "FR": ".fr", "DE": ".de", "GB": ".co.uk",
           "ES": ".es", "NL": ".nl", "BE": ".be", "CH": ".ch",
           "AT": ".at", "PL": ".pl", "PT": ".pt", "IE": ".ie",
           "SE": ".se", "DK": ".dk", "NO": ".no", "FI": ".fi"}
    tld = TLD.get(iso, ".com")
    return nome + tld



def scarica_gleif_completo(dsn: str, paesi: str) -> dict:
    """Scarica TUTTE le aziende di un paese con query multiple per lettera.

    GLEIF limita a 10.000 risultati per query, ma il limite è PER QUERY
    non globale. Con 24 query (una per lettera) per paese, possiamo
    catturare tutte le aziende che contengono quella lettera nel nome.
    """
    stats = {"aziende": 0, "inserite": 0, "query": 0}
    
    with httpx.Client(timeout=30,
                      headers={"Accept": "application/vnd.api+json"}) as c:
        for iso in paesi.split(","):
            for lettera in "ABCDEFGHIJKLMNOPQRSTUVWZ":
                page = 1
                while page <= 50:  # max 10k per query
                    try:
                        r = c.get("https://api.gleif.org/api/v1/lei-records",
                                  params={
                                      "filter[entity.legalAddress.country]": iso,
                                      "filter[entity.legalName]": lettera,
                                      "page[size]": "200",
                                      "page[number]": str(page),
                                  })
                        if r.status_code != 200:
                            break
                        data = r.json().get("data", [])
                        if not data:
                            break
                    except httpx.HTTPError:
                        break

                    for item in data:
                        attr = item.get("attributes", {})
                        entity = attr.get("entity", {})
                        ln = entity.get("legalName") or {}
                        nome = ln.get("name", "") if isinstance(ln, dict) else str(ln or "")
                        if not nome or len(nome) < 3:
                            continue
                        stats["aziende"] += 1

                        dominio = _nome_a_dominio(nome, iso)
                        if not dominio:
                            continue

                        with psycopg.connect(dsn) as conn:
                            with conn.cursor() as cur:
                                cur.execute("""
                                    INSERT INTO company_domains
                                      (domain, company_name, country, source)
                                    VALUES (%s, %s, %s, 'gleif')
                                    ON CONFLICT (domain) DO NOTHING
                                """, (dominio, nome[:200], iso))
                                if cur.rowcount:
                                    stats["inserite"] += 1
                            conn.commit()

                    page += 1
                stats["query"] += 1
                log.info("  %s/%s: %d aziende (%d inserite, %d query)",
                         iso, lettera, stats["aziende"],
                         stats["inserite"], stats["query"])
    return stats

# ── 2. SCHEMA.ORG UNIVERSALE ──────────────────────────────────────

def leggi_schema_org(dsn: str, limite: int = 100) -> dict:
    """Per le aziende senza ATS: legge JSON-LD e sitemap.

    Il metodo di Google for Jobs: non serve sapere quale ATS
    usa l'azienda — basta leggere il JSON-LD JobPosting standard
    o il sitemap.xml che elenca le offerte.
    """
    stats = {"viste": 0, "jsonld": 0, "sitemap": 0, "offerte": 0}

    # prendi le aziende con ATS identificato ma senza offerte
    # (l'ATS è custom o il nostro adapter non funziona)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT cd.domain, cd.company_name, cd.country
                  FROM company_domains cd
             LEFT JOIN ats_companies ac ON ac.slug = cd.domain
             LEFT JOIN ats_jobs j ON j.slug = cd.domain
                 WHERE cd.status = 'ats'
                   AND j.id IS NULL
                 LIMIT %s
            """, (limite,))
            aziende = cur.fetchall()

    def analizza(azienda):
        dominio, nome, paese = azienda
        trovate = []
        with httpx.Client(timeout=12, follow_redirects=True,
                          headers={"User-Agent": "nivult-ats/0.1"}) as c:
            # 1. prova il sitemap
            try:
                r = c.get(f"https://{dominio}/sitemap.xml")
                if r.status_code == 200 and "<urlset" in r.text:
                    urls = re.findall(r'<loc>([^<]+)</loc>', r.text)
                    # filtra le URL che sembrano offerte
                    for u in urls:
                        if re.search(r'/job|/vacanc|/position|/offert|/stell', u, re.I):
                            trovate.append(("sitemap", u))
            except httpx.HTTPError:
                pass

            # 2. prova la pagina careers con JSON-LD
            for path in ["/careers", "/jobs", "/"]:
                try:
                    r = c.get(f"https://{dominio}{path}")
                    if r.status_code == 200:
                        # cerca JSON-LD JobPosting
                        for m in re.finditer(
                                r'<script type="application/ld\+json">(.*?)</script>',
                                r.text, re.S):
                            try:
                                d = json.loads(m.group(1))
                                if isinstance(d, dict) and d.get("@type") == "JobPosting":
                                    trovate.append(("jsonld", str(r.url)))
                                    break
                            except json.JSONDecodeError:
                                continue
                        break
                except httpx.HTTPError:
                    continue
        return dominio, trovate

    with ThreadPoolExecutor(max_workers=10) as pool:
        for dominio, trovate in pool.map(analizza, aziende):
            stats["viste"] += 1
            for tipo, url in trovate:
                if tipo == "jsonld":
                    stats["jsonld"] += 1
                else:
                    stats["sitemap"] += 1
                stats["offerte"] += 1

    return stats


# ── STATISTICHE ────────────────────────────────────────────────────

def stats(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM company_domains")
            totale = cur.fetchone()[0]
            cur.execute("""
                SELECT source, count(*) FROM company_domains
                GROUP BY 1 ORDER BY 2 DESC LIMIT 10
            """)
            per_fonte = cur.fetchall()
            cur.execute("SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL")
            offerte = cur.fetchone()[0]
    print(f"\ncensimento: {totale:,} domini")
    print(f"offerte vive: {offerte:,}")
    print("per fonte:")
    for fonte, n in per_fonte:
        print(f"  {fonte:20s} {n:>8,d}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-8s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.esplosione_censimento",
                                 description=__doc__)
    ap.add_argument("--gleif", action="store_true",
                    help="scarica aziende da GLEIF (gratuito)")
    ap.add_argument("--paesi", default="IT,FR,DE,GB,ES,NL,BE",
                    help="paesi ISO separati da virgola")
    ap.add_argument("--per-paese", type=int, default=2000)
    ap.add_argument("--gleif-completo", action="store_true",
                    help="scarica TUTTE le aziende con query per lettera")
    ap.add_argument("--schema-org", action="store_true",
                    help="analizza le aziende senza offerte con JSON-LD/sitemap")
    ap.add_argument("--limite", type=int, default=100)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)

    if args.gleif:
        s = scarica_gleif(ATS_DSN, args.paesi, args.per_paese)
        print(f"\nGLEIF: {s}")
    if args.gleif_completo:
        s = scarica_gleif_completo(ATS_DSN, args.paesi)
        print(f"\nGLEIF completo: {s}")
    if args.schema_org:
        s = leggi_schema_org(ATS_DSN, args.limite)
        print(f"\nSchema.org: {s}")
    if args.stats:
        stats(ATS_DSN)
    if not (args.gleif or args.gleif_completo
            or args.schema_org or args.stats):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
