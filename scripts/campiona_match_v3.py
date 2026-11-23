#!/usr/bin/env python3
"""Campione stratificato e decontaminato per il nuovo holdout match-v3."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

import duckdb

from nivult.ats.testo import pulito

ROOT = Path(__file__).resolve().parents[1]
QUERY = """
WITH base AS (
  SELECT id, title, ats AS platform_id, city, country,
         language AS lang, seniority, remote, technologies AS skills, company,
         CASE WHEN regexp_matches(lower(title), ?3) THEN 'positivo' ELSE 'difficile' END AS gruppo
  FROM offerte
  WHERE list_contains(?1, country) AND list_contains(?2, category)
    AND (regexp_matches(lower(title), ?4) OR regexp_matches(lower(title), ?5))
), classificate AS (
  SELECT *, row_number() OVER (
    PARTITION BY country, platform_id, gruppo ORDER BY md5(id)) AS rn
  FROM base
)
SELECT c.id, c.title, c.city, c.country, c.platform_id, c.lang, c.seniority,
       c.remote, c.skills, c.company, c.gruppo,
       json_extract_string(o.raw, 'url') AS url,
       json_extract_string(o.raw, 'description') AS raw_description
FROM classificate c JOIN offerte o ON o.id = c.id
WHERE rn <= 16
ORDER BY md5(c.id)
"""


def split_azienda(organizzazione: str | None, job_id: str) -> str:
    base = (organizzazione or job_id).strip().lower()
    valore = int(hashlib.md5(("match-v3|" + base).encode()).hexdigest()[:8], 16)
    return "test" if valore % 2 else "validation"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    ap.add_argument("--profili", default=str(ROOT / "dati/match-v3/profili.json"))
    ap.add_argument("--duckdb", default="/opt/nivult/api-clienti/api-clienti.duckdb")
    args = ap.parse_args()
    profili = json.loads(Path(args.profili).read_text())
    usati = set()
    for path in (ROOT / "dati/match-v2/candidati.jsonl",
                 ROOT / "dati/match-benchmark/candidati.jsonl"):
        if path.exists():
            usati.update(json.loads(x)["id"] for x in path.read_text().splitlines())

    selezionati = []
    conn = duckdb.connect(args.duckdb, read_only=True)
    conn.execute("SET memory_limit = '4GB'")
    temporaneo = Path(args.duckdb).parent / "duckdb-tmp"
    temporaneo.mkdir(parents=True, exist_ok=True)
    conn.execute("SET temp_directory = '" + str(temporaneo).replace("'", "''") + "'")
    conn.execute("SET threads = 4")
    cur = conn.cursor()
    for chiave, cfg in profili.items():
        print("inizio_profilo", chiave, flush=True)
        cur.execute(QUERY, [["IT", "DE", "FR", "SE"], cfg["families"],
                            cfg["positivo"], cfg["positivo"], cfg["difficile"]])
        nomi = [d[0] for d in cur.description]
        per_fonte = defaultdict(list)
        aziende_viste = set()
        for valori in cur.fetchall():
            r = dict(zip(nomi, valori))
            testo = pulito(r.pop("raw_description"), massimo=1600)
            if r["id"] in usati or len(testo) < 100:
                continue
            org = r.get("company")
            azienda = (org or r["id"]).lower()
            marcatore = (r["country"], r["gruppo"], azienda)
            if marcatore in aziende_viste:
                continue
            aziende_viste.add(marcatore)
            r["ai_requirements_summary"] = testo
            per_fonte[(r["country"], r["gruppo"], r["platform_id"])].append(r)
        for paese in ("IT", "DE", "FR", "SE"):
            for gruppo in ("positivo", "difficile"):
                candidati = {k[2]: v for k, v in per_fonte.items()
                             if k[:2] == (paese, gruppo)}
                scelte = []
                while any(candidati.values()) and len(scelte) < 8:
                    for fonte in sorted(candidati):
                        if candidati[fonte] and len(scelte) < 8:
                            r = candidati[fonte].pop(0)
                            org = r.get("company")
                            r.update({
                                "pair_id": f"{chiave}:{r['id']}", "profile_key": chiave,
                                "job_id": r["id"], "source_job_id": r["id"],
                                "organization": org,
                                "cities": [r.pop("city")] if r.get("city") else [],
                                "source": r.pop("platform_id"),
                                "ai_job_language": r.pop("lang"),
                                "ai_experience_level": r.pop("seniority"),
                                "ai_work_arrangement": r.pop("remote"),
                                "ai_key_skills": r.pop("skills") or [],
                                "split": split_azienda(org, r["id"]),
                            })
                            scelte.append(r)
                print(chiave, paese, gruppo, len(scelte), flush=True)
                if len(scelte) != 8:
                    raise RuntimeError(
                        f"campione insufficiente: {chiave}/{paese}/{gruppo} = {len(scelte)}")
                usati.update(r["id"] for r in scelte)
                selezionati.extend(scelte)
    conn.close()

    if len(selezionati) != 256:
        raise RuntimeError(f"attese 256 coppie, trovate {len(selezionati)}")
    aziende = defaultdict(set)
    for r in selezionati:
        aziende[(r.get("organization") or r["id"]).lower()].add(r["split"])
    if any(len(s) > 1 for s in aziende.values()):
        raise RuntimeError("azienda condivisa fra validation e test")
    out = Path(args.out)
    out.write_text("".join(json.dumps(c, ensure_ascii=False, default=str) + "\n"
                           for c in selezionati))
    print("per_profilo", dict(Counter(c["profile_key"] for c in selezionati)))
    print("split", dict(Counter(c["split"] for c in selezionati)))
    print("fonti", dict(Counter(c["source"] for c in selezionati)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
