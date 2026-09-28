#!/usr/bin/env python3
"""Resuscita le offerte jsonld uccise dalla sitemap-finestra (28/09/2026).

Dal 24/09 la scadenza per presenza ha scaduto migliaia di offerte jsonld
«non piu' nella sitemap» — ma per i portali la sitemap e' una finestra
rotante, non l'inventario (mindpal.co: 446 campionate, tutte vive).
Ogni scaduta viene sondato sulla sua pagina: JobPosting presente → torna
attiva (expired_at NULL, fetched_at ora, cosi' la ripesca del ponte la
riattiva anche nel funnel). Morte vere (404/410, pagina senza
JobPosting) e illeggibili restano come sono. Idempotente.

    ATS_DATABASE_URL=... python scripts/resuscita_jsonld.py [--dal 2026-09-24] [--limite N]
"""
from __future__ import annotations

import argparse
import os
import time

import psycopg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dal", default="2026-09-24")
    ap.add_argument("--limite", type=int, default=10**9)
    a = ap.parse_args()

    import httpx
    from concurrent.futures import ThreadPoolExecutor
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from nivult.ats.jobposting import _estrai_ld

    dsn = os.environ["ATS_DATABASE_URL"]
    t0 = time.time()
    vivi, morti, illeggibili = 0, 0, 0
    with psycopg.connect(dsn, autocommit=True) as conn, \
            httpx.Client(timeout=15, follow_redirects=True,
                         headers={"User-Agent": "nivult-ats/0.1"},
                         limits=httpx.Limits(max_connections=16)) as cl:
        cand = conn.execute("""
            SELECT id, url FROM ats_jobs
             WHERE platform_id='jsonld' AND expired_at IS NOT NULL
               AND expired_at > %s::timestamptz
             ORDER BY expired_at DESC""", (a.dal,)).fetchall()
        cand = cand[:a.limite]
        print(f"candidati: {len(cand):,} (scadute dal {a.dal})", flush=True)

        def sonda(r):
            jid, url = r
            try:
                p = cl.get(url)
            except httpx.HTTPError:
                return jid, None
            if p.status_code in (404, 410):
                return jid, False
            if p.status_code == 200:
                try:
                    return jid, bool(_estrai_ld(p.text))
                except Exception:                 # noqa: BLE001
                    return jid, None
            return jid, None

        for i in range(0, len(cand), 2000):
            blocco = cand[i:i + 2000]
            rivivi = []
            with ThreadPoolExecutor(max_workers=8) as pool:
                for jid, vivo in pool.map(sonda, blocco):
                    if vivo is True:
                        rivivi.append(jid)
                    elif vivo is False:
                        morti += 1
                    else:
                        illeggibili += 1
            if rivivi:
                for j in range(0, len(rivivi), 5000):
                    conn.execute("UPDATE ats_jobs SET expired_at=NULL, fetched_at=now() "
                                 "WHERE id = ANY(%s::uuid[])", (rivivi[j:j + 5000],))
                vivi += len(rivivi)
            print(f"  vivi={vivi:,} morti={morti:,} illeggibili={illeggibili:,} "
                  f"({time.time()-t0:.0f}s)", flush=True)
    print(f"FINE: resuscitate {vivi:,}, morte vere {morti:,}, illeggibili {illeggibili:,} "
          f"in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
