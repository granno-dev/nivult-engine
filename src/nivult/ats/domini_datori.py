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


def main() -> int:
    import argparse
    from .runner import ATS_DSN
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.domini_datori")
    ap.add_argument("--limite", type=int, default=2000)
    ap.add_argument("--solo-vanity", action="store_true")
    a = ap.parse_args()
    esito = da_vanity(ATS_DSN)
    if not a.solo_vanity:
        esito.update(da_brandfetch(ATS_DSN, a.limite))
    print(json.dumps(esito))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
