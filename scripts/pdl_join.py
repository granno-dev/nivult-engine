#!/usr/bin/env python3
"""JOIN dei domini Nivult (/tmp/nostri_domini.txt) sul PDL Free Company Dataset.

Streama il CSV dallo zip (no decompressione su disco), matcha su dominio
normalizzato, scrive /tmp/pdl_match.jsonl e stampa statistiche di copertura.
"""
import csv
import json
import re
import subprocess
import sys
from collections import Counter

ZIP = "/tmp/pdl_free_company_dataset.csv.zip"
DOMAINS = "/tmp/nostri_domini.txt"
OUT = "/tmp/pdl_match.jsonl"
STATS = "/tmp/pdl_stats.json"

csv.field_size_limit(10 * 1024 * 1024)

FIELDS = ["name", "website", "industry", "size", "founded",
          "locality", "region", "country", "linkedin_url", "id"]

def norm_domain(s: str) -> str:
    s = (s or "").strip().lower()
    if not s:
        return ""
    s = re.sub(r"^https?://", "", s)
    s = s.split("/")[0].split("?")[0].split("#")[0]
    if s.startswith("www."):
        s = s[4:]
    return s.strip().strip(".")

ours = set()
with open(DOMAINS, encoding="utf-8") as f:
    for line in f:
        d = norm_domain(line)
        if d:
            ours.add(d)
print(f"Nostri domini unici (normalizzati): {len(ours)}", flush=True)

proc = subprocess.Popen(
    ["gzip", "-dc", ZIP],
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    bufsize=1024 * 1024,
)
assert proc.stdout is not None
reader = csv.reader((line.decode("utf-8", errors="replace")
                     for line in proc.stdout))
header = next(reader)
print("Header CSV:", header, flush=True)
idx = {h.strip().lower(): i for i, h in enumerate(header)}
missing = [c for c in FIELDS if c not in idx]
if missing:
    print("ATTENZIONE colonne mancanti:", missing, flush=True)

n_rows = 0
matched = {}          # dominio -> riga (teniamo la riga piu' completa)
dup_rows = 0

def completeness(row, idx):
    return sum(1 for c in ["industry", "size", "founded", "locality", "country"]
               if c in idx and idx[c] < len(row) and row[idx[c]].strip())

for row in reader:
    n_rows += 1
    if n_rows % 1_000_000 == 0:
        print(f"  ... {n_rows:,} righe lette, {len(matched):,} match", flush=True)
    wi = idx.get("website")
    if wi is None or wi >= len(row):
        continue
    d = norm_domain(row[wi])
    if d in ours:
        if d in matched:
            dup_rows += 1
            if completeness(row, idx) > completeness(matched[d], idx):
                matched[d] = row
        else:
            matched[d] = row

proc.stdout.close()
proc.wait()
print(f"Righe totali dataset: {n_rows:,}", flush=True)
print(f"Domini nostri matchati: {len(matched):,} ({100*len(matched)/len(ours):.1f}%)", flush=True)
print(f"Righe duplicate extra sullo stesso dominio: {dup_rows}", flush=True)

# scrittura jsonl + stats
field_cov = Counter()
size_dist = Counter()
industry_dist = Counter()
country_dist = Counter()

with open(OUT, "w", encoding="utf-8") as out:
    for d in sorted(matched):
        row = matched[d]
        rec = {"dominio": d}
        for c in FIELDS:
            i = idx.get(c)
            rec[c] = row[i].strip() if i is not None and i < len(row) else ""
        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        for c in ["industry", "size", "founded", "locality", "region",
                  "country", "linkedin_url", "name"]:
            if rec.get(c):
                field_cov[c] += 1
        size_dist[rec.get("size") or "(vuoto)"] += 1
        industry_dist[rec.get("industry") or "(vuoto)"] += 1
        country_dist[rec.get("country") or "(vuoto)"] += 1

stats = {
    "righe_dataset": n_rows,
    "domini_nostri": len(ours),
    "matchati": len(matched),
    "copertura_pct": round(100 * len(matched) / len(ours), 2),
    "righe_duplicate": dup_rows,
    "copertura_per_campo": {
        c: {"n": field_cov[c], "pct": round(100 * field_cov[c] / max(len(matched), 1), 2)}
        for c in ["name", "industry", "size", "founded", "locality",
                  "region", "country", "linkedin_url"]
    },
    "distribuzione_size": dict(size_dist.most_common()),
    "top_industry": dict(industry_dist.most_common(15)),
    "top_country": dict(country_dist.most_common(15)),
}
with open(STATS, "w", encoding="utf-8") as f:
    json.dump(stats, f, ensure_ascii=False, indent=2)

print(json.dumps(stats, ensure_ascii=False, indent=2))
