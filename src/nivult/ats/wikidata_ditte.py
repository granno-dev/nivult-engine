"""Settore e dipendenti delle aziende, da Wikidata: firmographics aperte.

TheirStack vende industria e dimensione; la fonte aperta che le sa per
le aziende non-microscopiche e' Wikidata. Si cerca PER NOME con la
guardia anti-omonimi (il nocciolo del nome deve combaciare col label o
un alias: «Sirio SpA» non deve prendersi il settore di «Sirius XM»), si
leggono i claim P452 (settore) e P1128 (dipendenti), e le etichette dei
settori si risolvono in inglese a fine giro, in un'unica chiamata.

A fette educate (1.5 richieste/s, marcatore industry_checked_at contro i
riesami): le grandi si trovano, le microscopiche no — e va bene cosi',
il campo resta NULL invece di riempirsi a occhio.
"""
from __future__ import annotations

import logging
import os
import re
import time

import httpx
import psycopg

log = logging.getLogger("nivult.ats.wikidata_ditte")


def _colonna_manca(c, tabella: str, colonna: str) -> bool:
    """Un ALTER TABLE, anche IF NOT EXISTS, chiede il lock esclusivo: in
    coda dietro una transazione lunga CONGELA tutto cio' che arriva dopo
    (successo: mezz'ora di sistema fermo, dashboard compresa). Il DDL
    nei cicli caldi si esegue SOLO se serve davvero: prima si guarda il
    catalogo, che non chiede lock a nessuno."""
    return c.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (tabella, colonna)).fetchone() is None


def _tabella_manca(c, tabella: str) -> bool:
    return c.execute("SELECT to_regclass(%s)",
                     (tabella,)).fetchone()[0] is None

API = "https://www.wikidata.org/w/api.php"
_UA = "nivult-ats/1.0 (firmographics; contact: ops@nivult.com)"


def _norm(s: str) -> str:
    s = re.sub(r"\b(srl|spa|s\.p\.a\.|gmbh|ag|bv|b\.v\.|inc|llc|ltd|sa|"
               r"s\.a\.|oy|ab|as|plc|co|corp|group|holding)\b\.?", " ",
               s.lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _combacia(nome: str, voce: dict) -> bool:
    core = _norm(nome)
    if len(core) < 4:
        return False
    candidati = [voce.get("label") or ""] + list(voce.get("aliases") or [])
    for c in candidati:
        cn = _norm(c)
        if cn and (cn == core or core in cn or cn in core):
            return True
    return False


def arricchisci(dsn: str, limite: int = 1500) -> dict:
    stats = {"esaminate": 0, "settore": 0, "dipendenti": 0, "scartate": 0}
    cli = httpx.Client(timeout=20, headers={"User-Agent": _UA})
    settori_qid: dict[str, list[str]] = {}     # (pid,slug) -> [QID..]
    with psycopg.connect(dsn, autocommit=True) as c:
        if _colonna_manca(c, "ats_companies", "industry_checked_at"):
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT "
                      "EXISTS industry text")
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT "
                      "EXISTS employees_wd int")
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT "
                      "EXISTS industry_checked_at timestamptz")
        righe = c.execute("""
            SELECT platform_id, slug, company_name FROM ats_companies
             WHERE is_active AND job_count > 0
               AND company_name IS NOT NULL
               AND industry_checked_at IS NULL
             ORDER BY job_count DESC
             LIMIT %s""", (limite,)).fetchall()
        for pid, slug, nome in righe:
            stats["esaminate"] += 1
            qid = None
            try:
                r = cli.get(API, params={
                    "action": "wbsearchentities", "search": nome[:80],
                    "language": "en", "type": "item", "limit": 3,
                    "format": "json"})
                for voce in (r.json().get("search") or []):
                    if _combacia(nome, voce):
                        qid = voce["id"]
                        break
            except (httpx.HTTPError, ValueError):
                pass
            settore, dip = None, None
            if qid:
                try:
                    r = cli.get(API, params={
                        "action": "wbgetentities", "ids": qid,
                        "props": "claims", "format": "json"})
                    claims = ((r.json().get("entities") or {})
                              .get(qid, {}).get("claims", {}))
                    p452 = claims.get("P452") or []
                    qids = [x["mainsnak"]["datavalue"]["value"]["id"]
                            for x in p452[:3]
                            if x.get("mainsnak", {}).get("datavalue")]
                    if qids:
                        settori_qid[(pid, slug)] = qids
                    p1128 = claims.get("P1128") or []
                    if p1128 and p1128[0].get("mainsnak", {}) \
                            .get("datavalue"):
                        dip = int(float(p1128[0]["mainsnak"]["datavalue"]
                                        ["value"]["amount"]))
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    pass
            else:
                stats["scartate"] += 1
            c.execute("""UPDATE ats_companies
                            SET employees_wd = COALESCE(%s, employees_wd),
                                industry_checked_at = now()
                          WHERE platform_id = %s AND slug = %s""",
                      (dip, pid, slug))
            if dip:
                stats["dipendenti"] += 1
            time.sleep(0.7)

        # le etichette dei settori, risolte in blocco (50 QID a chiamata)
        tutti = sorted({q for v in settori_qid.values() for q in v})
        nomi_settore: dict[str, str] = {}
        for i in range(0, len(tutti), 50):
            try:
                r = cli.get(API, params={
                    "action": "wbgetentities",
                    "ids": "|".join(tutti[i:i + 50]),
                    "props": "labels", "languages": "en",
                    "format": "json"})
                for q, ent in (r.json().get("entities") or {}).items():
                    lab = (ent.get("labels", {}).get("en") or {}) \
                        .get("value")
                    if lab:
                        nomi_settore[q] = lab
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.7)
        for (pid, slug), qids in settori_qid.items():
            etichette = [nomi_settore[q] for q in qids
                         if q in nomi_settore][:2]
            if etichette:
                c.execute("""UPDATE ats_companies SET industry = %s
                              WHERE platform_id = %s AND slug = %s""",
                          (", ".join(etichette), pid, slug))
                stats["settore"] += 1
    log.info("wikidata ditte: %s", stats)
    return stats


def main() -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.wikidata_ditte")
    ap.add_argument("--limite", type=int, default=1500)
    args = ap.parse_args()
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    print(arricchisci(dsn, args.limite))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
