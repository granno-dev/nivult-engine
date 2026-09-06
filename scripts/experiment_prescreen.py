#!/usr/bin/env python3
"""Il pre-screening perde offerte buone?

    python scripts/experiment_prescreen.py --cluster <uuid> --profile profilo.json
    python scripts/experiment_prescreen.py --cluster <uuid> --profile p.json --dry-run

Domanda a cui risponde: delle offerte che GLM metterebbe fra le migliori
vedendole TUTTE, quante sopravvivono al pre-screening di Mistral?

  ramo A (riferimento)  GLM valuta tutte le 200
  ramo B (valvola)      Mistral valuta tutte le 200 -> top 30 -> GLM valuta le 30

Il recall è la frazione delle top-K di A che compaiono anche in B. Se è alto la
valvola è sicura; se è basso stiamo scartando candidati buoni prima ancora di
guardarli, ed è il tipo di errore che l'utente non può vedere.

--dry-run stampa cosa verrebbe chiesto e quanto costerebbe, senza chiamare.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from nivult.config import load_dotenv, migrator_database_url  # noqa: E402
from nivult.matching import llm  # noqa: E402

# Prezzi in dollari per milione di token, per la stima.
PREZZI = {"glm-5.2": (0.60, 2.20), "mistral-small-latest": (0.10, 0.30)}


def carica_offerte(conn, cluster_id: str, n: int) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT j.id::text, j.title, j.organization, j.cities, j.ai_key_skills, "
            "       j.ai_experience_level, j.ai_work_arrangement, j.ai_requirements_summary "
            "FROM jobs j JOIN job_clusters jc ON jc.job_id = j.id "
            "WHERE jc.cluster_id = %s AND j.status = 'active' "
            "  AND j.duplicate_of_job_id IS NULL "
            "ORDER BY j.date_posted DESC LIMIT %s", (cluster_id, n))
        return cur.fetchall()


def costo(modello: str, ingresso: int, uscita: int) -> float:
    pin, pout = PREZZI.get(modello, (0, 0))
    return ingresso / 1e6 * pin + uscita / 1e6 * pout


def recall(rif: list[llm.Punteggio], valvola: list[llm.Punteggio], k: int) -> tuple[int, int]:
    top_rif = {p.job_id for p in sorted(rif, key=lambda p: -p.score)[:k]}
    top_val = {p.job_id for p in sorted(valvola, key=lambda p: -p.score)[:k]}
    return len(top_rif & top_val), len(top_rif)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cluster", required=True)
    ap.add_argument("--profile", required=True, help="JSON col profilo da usare")
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--keep", type=int, default=30, help="quante il pre-screening passa")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default="prescreen-results.json")
    args = ap.parse_args()

    load_dotenv()
    profilo = json.loads(Path(args.profile).read_text(encoding="utf-8"))

    with psycopg.connect(migrator_database_url()) as conn:
        jobs = carica_offerte(conn, args.cluster, args.sample)
    if not jobs:
        print("nessuna offerta per questo cluster.")
        return 1
    print(f"offerte nel campione: {len(jobs)}")

    if args.dry_run:
        esempio = llm.offerta_come_testo(jobs[0])
        token_offerta = len(esempio) // 4
        print(f"\nesempio di offerta come la vede il modello ({token_offerta} token circa):")
        print(f"  {esempio[:260]}…")
        ing_a = token_offerta * len(jobs) + 400
        print(f"\nstima ramo A (GLM su tutte): ~{ing_a} token in ingresso, "
              f"~{costo('glm-5.2', ing_a, 40*len(jobs)):.3f} $")
        ing_b = token_offerta * len(jobs) + token_offerta * args.keep + 800
        print(f"stima ramo B (Mistral su tutte + GLM su {args.keep}): "
              f"~{costo('mistral-small-latest', token_offerta*len(jobs), 20*len(jobs)) + costo('glm-5.2', token_offerta*args.keep, 200*args.keep):.3f} $")
        print("\n(--dry-run: nessuna chiamata effettuata)")
        return 0

    print("\n── ramo A: GLM su tutte (il riferimento) " + "─" * 26)
    t = time.time()
    with llm.GLM() as glm:
        rif = llm.valuta(glm, profilo, jobs, con_motivazione=False)
        costo_a = costo(glm.model, glm.input_tokens, glm.output_tokens)
        tempo_a = time.time() - t
    print(f"  {len(rif)} punteggi, {tempo_a:.0f}s, {costo_a:.4f} $")

    print("\n── ramo B: Mistral su tutte, poi GLM sulle migliori " + "─" * 15)
    t = time.time()
    with llm.MistralSmall() as mis:
        screening = llm.valuta(mis, profilo, jobs, con_motivazione=False)
        costo_screen = costo(mis.model, mis.input_tokens, mis.output_tokens)
    passate = {p.job_id for p in sorted(screening, key=lambda p: -p.score)[:args.keep]}
    sottoinsieme = [j for j in jobs if j["id"] in passate]
    with llm.GLM() as glm:
        valvola = llm.valuta(glm, profilo, sottoinsieme, con_motivazione=True)
        costo_glm_b = costo(glm.model, glm.input_tokens, glm.output_tokens)
    tempo_b = time.time() - t
    print(f"  Mistral: {len(screening)} punteggi, {costo_screen:.4f} $")
    print(f"  GLM sulle {len(sottoinsieme)} passate: {costo_glm_b:.4f} $")
    print(f"  totale ramo B: {tempo_b:.0f}s, {costo_screen + costo_glm_b:.4f} $")

    print("\n── recall: quante delle migliori secondo A sopravvivono a B " + "─" * 7)
    righe = []
    for k in (10, 15, 20, 30):
        if k > len(rif):
            continue
        # Il confronto giusto è contro chi il pre-screening ha lasciato passare:
        # se una top-K di A non è nemmeno arrivata a GLM, è persa.
        top_a = [p.job_id for p in sorted(rif, key=lambda p: -p.score)[:k]]
        sopravvissute = sum(1 for j in top_a if j in passate)
        righe.append((k, sopravvissute, len(top_a)))
        print(f"  top-{k:<3} di GLM: {sopravvissute}/{len(top_a)} passano il "
              f"pre-screening  ({100*sopravvissute/len(top_a):.0f}%)")

    risparmio = costo_a - (costo_screen + costo_glm_b)
    print(f"\n  risparmio della valvola su {len(jobs)} offerte: {risparmio:+.4f} $ "
          f"({100*risparmio/max(costo_a,1e-9):+.0f}%)")

    Path(args.out).write_text(json.dumps({
        "cluster": args.cluster, "campione": len(jobs), "keep": args.keep,
        "riferimento": [{"id": p.job_id, "score": p.score} for p in rif],
        "screening": [{"id": p.job_id, "score": p.score} for p in screening],
        "valvola": [{"id": p.job_id, "score": p.score, "reason": p.reason}
                    for p in valvola],
        "recall": [{"k": k, "sopravvissute": s, "totale": t} for k, s, t in righe],
        "costi": {"ramo_a": costo_a, "screening": costo_screen, "glm_b": costo_glm_b},
    }, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\n  risultati salvati in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
