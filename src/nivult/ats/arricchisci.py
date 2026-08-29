"""Arricchisce le offerte Phenom (e altre) leggendo le pagine di dettaglio.

    python -m nivult.ats.arricchisci --phenom --limite 5000

Phenom è la piattaforma con più offerte senza paese (40.000+): il sitemap
dà solo titolo e URL, ma la pagina di ogni offerta porta un JSON-LD
JobPosting completo — città, paese, data di pubblicazione. Una richiesta
per offerta, sparse su centinaia di domini aziendali: nessun server
sotto pressione.

Le offerte SuccessFactors e Radancy senza paese prendono come ripiego
il paese dell'azienda dal censimento (ats_companies.country) — meglio
di niente per il ponte, che comunque filtra per soglia.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import httpx
import psycopg

log = logging.getLogger("nivult.ats.arricchisci")

ATS_DSN = os.environ.get(
    "ATS_DATABASE_URL",
    "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")

_NOMI_PAESI = {
    "ITALY": "IT", "FRANCE": "FR", "GERMANY": "DE", "DEUTSCHLAND": "DE",
    "SPAIN": "ES", "ESPANA": "ES", "UNITED KINGDOM": "GB", "NETHERLANDS": "NL",
    "BELGIUM": "BE", "SWEDEN": "SE", "SWITZERLAND": "CH", "AUSTRIA": "AT",
    "POLAND": "PL", "PORTUGAL": "PT", "DENMARK": "DK", "IRELAND": "IE",
    "NORWAY": "NO", "FINLAND": "FI", "UNITED STATES": "US", "USA": "US",
    "CANADA": "CA", "CHINA": "CN", "INDIA": "IN", "AUSTRALIA": "AU",
    "JAPAN": "JP", "BRAZIL": "BR", "MEXICO": "MX", "SINGAPORE": "SG",
    "LUXEMBOURG": "LU", "CZECH REPUBLIC": "CZ", "GREECE": "GR",
    "HUNGARY": "HU", "ROMANIA": "RO", "TURKEY": "TR", "ISRAEL": "IL",
}


def _iso(nome: str | None) -> str | None:
    if not nome:
        return None
    n = nome.strip().upper()
    if len(n) == 2 and n.isalpha():
        return n
    return _NOMI_PAESI.get(n)


def _estrai_jsonld(html: str) -> dict:
    """Il JSON-LD JobPosting dalla pagina, se c'è."""
    for m in re.finditer(
            r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            d = json.loads(m.group(1))
            if isinstance(d, dict) and d.get("@type") == "JobPosting":
                loc = d.get("jobLocation") or {}
                if isinstance(loc, list):
                    loc = loc[0] if loc else {}
                addr = loc.get("address") or {}
                paese = _iso(addr.get("addressCountry"))
                citta = addr.get("addressLocality")
                data = d.get("datePosted")
                try:
                    dt = datetime.fromisoformat(data) if data else None
                except ValueError:
                    dt = None
                if paese or citta or dt:
                    return {"country": paese, "city": citta, "posted_at": dt}
        except (json.JSONDecodeError, KeyError):
            continue
    return {}


def arricchisci_phenom(dsn: str, limite: int = 5000, thread: int = 10) -> dict:
    """Legge le pagine di dettaglio delle offerte Phenom senza paese."""
    stats = {"viste": 0, "paesi": 0, "citta": 0, "date": 0, "errori": 0}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, url FROM ats_jobs
                 WHERE platform_id = 'phenom'
                   AND country IS NULL AND expired_at IS NULL
                 ORDER BY id
                 LIMIT %s
            """, (limite,))
            righe = cur.fetchall()

    log.info("phenom: %d pagine da leggere (%d thread)", len(righe), thread)
    if not righe:
        return stats

    def leggi(riga):
        jid, url = riga
        try:
            with httpx.Client(timeout=15, follow_redirects=True,
                              headers={"User-Agent": "nivult-ats/0.1"}) as c:
                r = c.get(url)
                if r.status_code == 200:
                    return jid, _estrai_jsonld(r.text)
        except httpx.HTTPError:
            pass
        return jid, {}

    risultati = []
    with ThreadPoolExecutor(max_workers=thread) as pool:
        futures = [pool.submit(leggi, r) for r in righe]
        for i, fut in enumerate(as_completed(futures)):
            try:
                jid, dati = fut.result()
                stats["viste"] += 1
                if dati:
                    stats["paesi"] += 1 if dati.get("country") else 0
                    stats["citta"] += 1 if dati.get("city") else 0
                    stats["date"] += 1 if dati.get("posted_at") else 0
                    risultati.append((jid, dati))
            except Exception:
                stats["errori"] += 1
            if (i + 1) % 500 == 0:
                log.info("  … %d lette: %s", i + 1, stats)

    with psycopg.connect(dsn) as conn:
        for jid, dati in risultati:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE ats_jobs
                       SET country = COALESCE(country, %s),
                           city = COALESCE(city, %s),
                           location = COALESCE(location, %s),
                           posted_at = COALESCE(posted_at, %s)
                     WHERE id = %s
                """, (dati.get("country"), dati.get("city"),
                      dati.get("city"), dati.get("posted_at"), jid))
        conn.commit()
    return stats


def arricchisci_da_azienda(dsn: str) -> int:
    """Ripiego: il paese dell'azienda per le offerte senza paese.

    Per SF e Radancy, dove la pagina di dettaglio non espone il paese:
    il paese del portale carriere (dal censimento) è il miglior dato
    disponibile. Multinazionali con offerte fuori sede prenderanno il
    paese della sede — impreciso ma meglio di niente per il ponte.
    """
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE ats_jobs j
                   SET country = ac.country
                  FROM ats_companies ac
                 WHERE j.platform_id = ac.platform_id
                   AND j.slug = ac.slug
                   AND j.country IS NULL
                   AND j.expired_at IS NULL
                   AND ac.country IS NOT NULL
                RETURNING j.id
            """)
            n = cur.rowcount
        conn.commit()
    log.info("da_azienda: %d offerte con il paese del portale", n)
    return n


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-8s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.arricchisci",
                                 description=__doc__)
    ap.add_argument("--phenom", action="store_true",
                    help="legge le pagine di dettaglio Phenom (JSON-LD)")
    ap.add_argument("--da-azienda", action="store_true",
                    help="paese del portale carriere come ripiego")
    ap.add_argument("--limite", type=int, default=5000)
    ap.add_argument("--thread", type=int, default=10)
    args = ap.parse_args(argv)

    if args.phenom:
        s = arricchisci_phenom(ATS_DSN, args.limite, args.thread)
        print(f"\nPhenom: {s}")
    if args.da_azienda:
        n = arricchisci_da_azienda(ATS_DSN)
        print(f"\nDa azienda: {n}")
    if not (args.phenom or args.da_azienda):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
