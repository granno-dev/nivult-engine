"""Il pool da etichettare per la distillazione del match (12/10/2026).

Per ogni utente che ha gia' match giudicati da GLM: il suo profilo
strutturato + i candidati dell'imbuto NON ancora valutati. Sono le coppie
che il maestro locale (bench_giudice_locale.py, Stadio A misurato:
rho 0.73 / AUC 0.94 contro GLM) etichettera' stanotte.

Il mix per utente: le piu' recenti piu' un campione a caso dal resto —
il digest pesa la freschezza, il distillato deve conoscere anche il
codone. Le coppie gia' giudicate non entrano: sono le ancore GLM, gia'
esportate da esporta_coppie_match.py.

Uso: DATABASE_URL=... python scripts/distilla_dataset.py /tmp/pool.jsonl.gz
"""
from __future__ import annotations

import argparse
import gzip
import json
import random

import psycopg
from psycopg.rows import dict_row

from nivult.config import database_url, load_dotenv
from nivult.matching.funnel import candidati
from nivult.matching.llm import offerta_come_testo, profilo_come_testo
from nivult.matching.worker import _profilo


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("destinazione")
    ap.add_argument("--per-utente", type=int, default=100,
                    help="coppie per utente: 60%% recenti, il resto a caso")
    ap.add_argument("--seme", type=int, default=42)
    args = ap.parse_args()
    load_dotenv()
    random.seed(args.seme)

    with psycopg.connect(database_url()) as conn, \
            gzip.open(args.destinazione, "wt", encoding="utf-8") as out:
        utenti = [r[0] for r in conn.execute(
            "SELECT DISTINCT user_id::text FROM matches")]
        tot = 0
        for user_id in utenti:
            with conn.cursor(row_factory=dict_row) as cur:
                cv = cur.execute(
                    "SELECT cv.families, cv.seniority, cv.skills, cv.languages, "
                    "       cv.years_experience, e.label AS seniority_label "
                    "FROM user_cvs cv "
                    "LEFT JOIN experience_levels e ON e.code = cv.seniority "
                    "WHERE cv.user_id = %s AND cv.status = 'active'",
                    (user_id,)).fetchone()
            if not cv:
                continue
            profilo = profilo_come_testo(_profilo(cv))
            cand = candidati(conn, user_id)
            random.shuffle(cand)
            recenti = sorted(cand, key=lambda j: str(j.get("date_posted")),
                             reverse=True)
            n_recenti = min(len(recenti), args.per_utente * 3 // 5)
            pool = recenti[:n_recenti] + cand[:args.per_utente - n_recenti]
            viste = set()
            for j in pool:
                chiave = j["id"]
                if chiave in viste:
                    continue
                viste.add(chiave)
                out.write(json.dumps({
                    "user_id": user_id, "profilo": profilo,
                    "offerta": offerta_come_testo(j), "job": j,
                }, ensure_ascii=False, default=str) + "\n")
                tot += 1
            print(f"{user_id[:8]}…: {len(viste)} coppie nel pool")
    print(f"totale pool da etichettare: {tot} -> {args.destinazione}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
