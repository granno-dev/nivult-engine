"""La scheda GLEIF di chi ha un LEI: sede legale, sede operativa, forma
giuridica (codice ELF con l'etichetta ufficiale), stato (21/09/2026).

Il LEI arriva dai bilanci ESEF (`nivult.ats.bilanci`, `lei_source`): 240
aziende attive il 21/09. Per ognuna una chiamata a api.gleif.org (aperta,
senza chiave, 60 al minuto) e la scheda entra in `aziende_registro` con
fonte «gleif». L'etichetta della forma giuridica si prende una volta per
codice dall'endpoint entity-legal-forms e resta in `elf_codes`.

    python -m nivult.ats.gleif [--limite N]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time

import httpx
import psycopg

from nivult.ats.registri_imprese import DDL_REGISTRO, _tabella_ce, scrivi_scheda

log = logging.getLogger("nivult.ats.gleif")
_UA = "nivult-ats/1.0 (schede societarie da GLEIF; contact: ops@nivult.com)"
_LEI_RX = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")

DDL_ELF = """
CREATE TABLE IF NOT EXISTS elf_codes (
  codice text PRIMARY KEY,
  paese  text,
  nome   text,
  letto_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS gleif_checked_at timestamptz;
"""


def _indirizzo(a: dict | None) -> dict:
    a = a or {}
    return {"street": ", ".join(x for x in (a.get("addressLines") or []) if x) or None,
            "city": a.get("city"), "region": a.get("region"), "postal_code": a.get("postalCode"),
            "country": a.get("country")}


def etichetta_elf(cli: httpx.Client, c, codice: str | None) -> str | None:
    if not codice:
        return None
    r = c.execute("SELECT nome FROM elf_codes WHERE codice = %s", (codice,)).fetchone()
    if r:
        return r[0]
    try:
        resp = cli.get(f"https://api.gleif.org/api/v1/entity-legal-forms/{codice}")
        if resp.status_code != 200:
            return None
        a = resp.json()["data"]["attributes"]
        nomi = a.get("names") or []
        nome = next((n.get("transliteratedName") or n.get("localName") for n in nomi if n.get("language") == "en"), None) \
            or next((n.get("transliteratedName") or n.get("localName") for n in nomi), None)
        c.execute("INSERT INTO elf_codes (codice, paese, nome) VALUES (%s, %s, %s) ON CONFLICT (codice) DO NOTHING",
                  (codice, a.get("countryCode"), nome))
        return nome
    except Exception as exc:                       # noqa: BLE001
        log.warning("elf %s: %s", codice, type(exc).__name__)
        return None


def applica(dsn: str, limite: int = 500) -> dict:
    st = {"viste": 0, "schede": 0, "errori": 0}
    cli = httpx.Client(timeout=25, headers={"User-Agent": _UA, "Accept": "application/vnd.api+json"})
    with psycopg.connect(dsn, autocommit=True) as c:
        if not _tabella_ce(c, "aziende_registro"):
            c.execute(DDL_REGISTRO)
        if not _tabella_ce(c, "elf_codes"):
            c.execute("SET lock_timeout = '10s'")
            c.execute(DDL_ELF)
            c.execute("RESET lock_timeout")
        righe = c.execute("""
            SELECT id, lei FROM ats_companies
             WHERE lei IS NOT NULL AND job_count > 0
               AND (gleif_checked_at IS NULL OR gleif_checked_at < now() - interval '90 days')
             ORDER BY job_count DESC LIMIT %s""", (limite,)).fetchall()
        for cid, lei in righe:
            st["viste"] += 1
            lei = (lei or "").strip().upper()
            if not _LEI_RX.match(lei):
                c.execute("UPDATE ats_companies SET gleif_checked_at = now() WHERE id = %s", (cid,))
                continue
            try:
                r = cli.get(f"https://api.gleif.org/api/v1/lei-records/{lei}")
                time.sleep(1.1)
                if r.status_code == 404:
                    c.execute("UPDATE ats_companies SET gleif_checked_at = now() WHERE id = %s", (cid,))
                    continue
                r.raise_for_status()
                e = r.json()["data"]["attributes"]["entity"]
            except Exception as exc:                   # noqa: BLE001
                st["errori"] += 1
                log.warning("gleif %s: %s", lei, type(exc).__name__)
                time.sleep(5)
                continue          # errore di rete: non si marca, si riprova
            # la sede OPERATIVA e' quella che il mercato chiama headquarters;
            # la legale entra nella stessa riga come fallback e nel legal_name
            hq = _indirizzo(e.get("headquartersAddress") or e.get("legalAddress"))
            forma = (e.get("legalForm") or {}).get("id")
            data = e.get("creationDate")
            scheda = {**hq, "legal_name": (e.get("legalName") or {}).get("name"), "registro_id": lei,
                      "legal_form_code": forma, "legal_form": etichetta_elf(cli, c, forma),
                      "founded": data[:10] if isinstance(data, str) else None,
                      "status": (e.get("status") or "").lower() or None}
            if scrivi_scheda(c, cid, "gleif", scheda):
                st["schede"] += 1
            c.execute("UPDATE ats_companies SET gleif_checked_at = now() WHERE id = %s", (cid,))
    log.info("gleif: %s", st)
    return st


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=500)
    a = ap.parse_args(argv)
    dsn = os.environ.get("ATS_DATABASE_URL")
    if not dsn:
        for f in ("/opt/nivult/.env", "/opt/nivult/engine/.env"):
            try:
                m = re.search(r"^POSTGRES_PASSWORD=(.*)$", open(f).read(), re.M)
                if m:
                    dsn = "postgresql://nivult:" + m.group(1).strip() + "@127.0.0.1:5432/nivult_ats"
                    break
            except OSError:
                pass
    print(json.dumps(applica(dsn, a.limite)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
