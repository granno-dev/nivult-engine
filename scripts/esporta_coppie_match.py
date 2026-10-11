"""Esporta le coppie CV×offerta giudicate da GLM: il dataset della
distillazione del match (11/10/2026).

Ogni riga e' una coppia come GLM l'ha vista davvero: i testi sono
prodotti dagli stessi renderer della produzione (profilo_come_testo,
offerta_come_testo), non ricostruiti a mano. Il profilo e' quello
STRUTTURATO (famiglie, skills, lingue, seniority) — il CV grezzo non
serve e non si tocca: niente decriptazione, niente dati in chiaro.

Privacy: escono solo id tecnici (user_id serve allo split train/val PER
UTENTE — lo stesso CV non deve mai stare su entrambi i lati). Niente
email, nomi, testi CV. Il file non si committa: sta sui volumi di
lavoro, non nel repo.

Uso: DATABASE_URL=... python scripts/esporta_coppie_match.py /tmp/coppie.jsonl.gz
"""
from __future__ import annotations

import argparse
import gzip
import json

import psycopg

from nivult.config import database_url, load_dotenv
from nivult.matching.llm import offerta_come_testo, profilo_come_testo
from nivult.matching.worker import _profilo

SQL = """
SELECT m.id::text, m.user_id::text, m.score, m.threshold_used, m.passed,
       m.evaluated_at,
       cv.families, cv.seniority, cv.skills, cv.languages, cv.years_experience,
       e.label AS seniority_label,
       j.id::text AS job_id, j.title, j.organization, j.ai_experience_level,
       j.ai_work_arrangement, j.cities, j.ai_visa_sponsorship, j.ai_key_skills,
       j.ai_requirements_summary
FROM matches m
LEFT JOIN user_cvs cv ON cv.id = m.cv_id
LEFT JOIN experience_levels e ON e.code = cv.seniority
JOIN jobs j ON j.id = m.job_id
ORDER BY m.evaluated_at
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("destinazione")
    args = ap.parse_args()
    load_dotenv()

    n = 0
    with psycopg.connect(database_url()) as conn, \
            gzip.open(args.destinazione, "wt", encoding="utf-8") as out:
        for r in conn.execute(SQL):
            (match_id, user_id, score, soglia, passed, creato,
             families, seniority, skills, languages, years, seniority_label,
             job_id, title, org, exp, arrangiamento, cities, visa,
             key_skills, requisiti) = r
            cv = {"families": families, "seniority": seniority,
                  "seniority_label": seniority_label, "skills": skills,
                  "languages": languages, "years_experience": years}
            offerta = {"id": job_id, "title": title, "organization": org,
                       "ai_experience_level": exp,
                       "ai_work_arrangement": arrangiamento,
                       "cities": cities, "ai_visa_sponsorship": visa,
                       "ai_key_skills": key_skills,
                       "ai_requirements_summary": requisiti}
            # il desiderio (parole dell'utente) non e' nella riga del match:
            # e' del cluster. v1 del dataset senza; la rubrica lo pesa come
            # preferenza, il distillato lo imparera' in una v2.
            out.write(json.dumps({
                "match_id": match_id, "user_id": user_id,
                "profilo": profilo_come_testo(_profilo(cv)),
                "offerta": offerta_come_testo(offerta),
                # il dizionario grezzo: bench_giudice_locale ricostruisce la
                # prompt di produzione dal dict, non dal testo gia' reso
                "job": offerta,
                "score": score, "threshold": soglia, "passed": passed,
                "at": creato.isoformat(),
            }, ensure_ascii=False) + "\n")
            n += 1
    print(f"coppie esportate: {n} -> {args.destinazione}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
