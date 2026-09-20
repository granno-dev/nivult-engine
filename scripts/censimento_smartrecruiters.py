#!/usr/bin/env python3
"""I tenant SmartRecruiters dal «rubinetto» della bacheca del fornitore.

jobs.smartrecruiters.com/sr-jobs/search e' l'endpoint della vetrina
pubblica di SmartRecruiters: ignora filtri, offset e pagina (misurato il
07/09/2026: `location`, `country`, `offset=100000`, `page=2` tornano
tutti la stessa risposta) ma restituisce sempre le ~96 offerte PIU'
RECENTI del mondo, ciascuna con `company.identifier` — che e' esattamente
lo slug che il nostro adapter usa su api.smartrecruiters.com. Letto ogni
giro del volano, nel tempo enumera i tenant attivi: chi pubblica, prima
o poi passa di li'. Un tenant nuovo entra in ats_companies e il runner lo
legge come tutti gli altri.

    ATS_DATABASE_URL=... python scripts/censimento_smartrecruiters.py [--dry-run]
"""
from __future__ import annotations

import argparse
import logging
import os

import httpx
import psycopg

log = logging.getLogger("censimento_smartrecruiters")
URL = "https://jobs.smartrecruiters.com/sr-jobs/search?q=&limit=100"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    dsn = os.environ["ATS_DATABASE_URL"]
    try:
        r = httpx.get(URL, timeout=30, headers={"User-Agent": "Mozilla/5.0 (nivult-ats)"})
    except httpx.HTTPError as exc:
        log.warning("bacheca irraggiungibile: %s", exc)
        return 1
    if r.status_code != 200:
        log.warning("bacheca: HTTP %s", r.status_code)
        return 1
    offerte = r.json().get("content", [])
    tenant: dict[str, tuple[str | None, str | None]] = {}
    for o in offerte:
        az = o.get("company") or {}
        ident = (az.get("identifier") or "").strip()
        if ident and "/" not in ident and len(ident) < 80:
            paese = ((o.get("location") or {}).get("country") or "").upper() or None
            tenant.setdefault(ident, (az.get("name"), paese if paese and len(paese) == 2 else None))
    nuovi = 0
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        for ident, (nome, paese) in tenant.items():
            cur.execute("""
                INSERT INTO ats_companies (platform_id, slug, company_name, country, discovered_from)
                VALUES ('smartrecruiters', %s, %s, %s, 'bacheca')
                ON CONFLICT (platform_id, slug) DO NOTHING
                RETURNING 1""", (ident, nome, paese))
            nuovi += 1 if cur.fetchone() else 0
        if a.dry_run:
            conn.rollback()
        else:
            conn.commit()
    print(f"Rubinetto SmartRecruiters: {len(offerte)} offerte, {len(tenant)} tenant, {nuovi} nuovi"
          + (" (dry-run)" if a.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
