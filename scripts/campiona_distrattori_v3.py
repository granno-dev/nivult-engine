#!/usr/bin/env python3
"""Concorrenti reali per la prova di recall match-v3 su bacino grande."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import duckdb

from nivult.ats.testo import pulito

ROOT = Path(__file__).resolve().parents[1]
QUERY = """
WITH base AS (
  SELECT id, title, ats AS platform_id, city, country,
         language AS lang, seniority, remote, technologies AS skills, company
  FROM offerte
  WHERE list_contains(?1, country) AND list_contains(?2, category)
), classificate AS (
  SELECT *, row_number() OVER (
    PARTITION BY country, platform_id ORDER BY md5(id)) AS rn
  FROM base
)
SELECT c.id, c.title, c.city, c.country, c.platform_id, c.lang, c.seniority,
       c.remote, c.skills, c.company,
       json_extract_string(o.raw, 'url') AS url,
       json_extract_string(o.raw, 'description') AS raw_description
FROM classificate c JOIN offerte o ON o.id = c.id
WHERE rn <= 8
ORDER BY md5(c.id)
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-profilo", type=int, default=500)
    ap.add_argument("--profili", default=str(ROOT / "dati/match-v3/profili.json"))
    ap.add_argument("--duckdb", default="/opt/nivult/api-clienti/api-clienti.duckdb")
    args = ap.parse_args()
    if args.per_profilo <= 0:
        raise SystemExit("--per-profilo deve essere positivo")
    profili = json.loads(Path(args.profili).read_text())
    usati = set()
    for path in (ROOT / "dati/match-v3/candidati.jsonl",
                 ROOT / "dati/match-v2/candidati.jsonl"):
        usati.update(json.loads(x)["id"] for x in path.read_text().splitlines())

    conn = duckdb.connect(args.duckdb, read_only=True)
    conn.execute("SET memory_limit = '4GB'")
    temporaneo = Path(args.duckdb).parent / "duckdb-tmp"
    temporaneo.mkdir(parents=True, exist_ok=True)
    conn.execute("SET temp_directory = '" + str(temporaneo).replace("'", "''") + "'")
    conn.execute("SET threads = 4")
    cur = conn.cursor()
    out = []
    for chiave, cfg in profili.items():
        cur.execute(QUERY, [["IT", "DE", "FR", "SE"], cfg["families"]])
        nomi = [d[0] for d in cur.description]
        per_paese = defaultdict(list)
        for valori in cur.fetchall():
            r = dict(zip(nomi, valori))
            if r["id"] in usati:
                continue
            testo = pulito(r.pop("raw_description"), massimo=1600)
            if len(testo) < 100:
                continue
            r["ai_requirements_summary"] = testo
            per_paese[r["country"]].append(r)
        scelte = []

        def aggiungi(r, chiave=chiave, scelte=scelte):
            org = r.get("company")
            r.update({
                "pair_id": f"{chiave}:{r['id']}", "profile_key": chiave,
                "job_id": r["id"], "source_job_id": r["id"], "organization": org,
                "cities": [r.pop("city")] if r.get("city") else [],
                "source": r.pop("platform_id"), "ai_job_language": r.pop("lang"),
                "ai_experience_level": r.pop("seniority"),
                "ai_work_arrangement": r.pop("remote"),
                "ai_key_skills": r.pop("skills") or [], "distrattore": True,
            })
            scelte.append(r)

        quota = (args.per_profilo + 3) // 4
        for paese in ("IT", "DE", "FR", "SE"):
            for r in per_paese[paese][:quota]:
                aggiungi(r)
        if len(scelte) < args.per_profilo:
            gia = {r["id"] for r in scelte}
            for paese in ("IT", "DE", "FR", "SE"):
                for r in per_paese[paese]:
                    if r["id"] not in gia:
                        aggiungi(r)
                        gia.add(r["id"])
                        if len(scelte) == args.per_profilo:
                            break
                if len(scelte) == args.per_profilo:
                    break
        scelte = scelte[:args.per_profilo]
        if len(scelte) != args.per_profilo:
            raise RuntimeError(f"concorrenti insufficienti per {chiave}: {len(scelte)}")
        out.extend(scelte)
        print(chiave, len(scelte), flush=True)
    conn.close()
    Path(args.out).write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n"
                                      for r in out))
    print("totale", len(out), "paesi", dict(Counter(r["country"] for r in out)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
