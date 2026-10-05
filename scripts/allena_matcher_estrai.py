#!/usr/bin/env python3
"""Estrae il dataset di distillazione del matcher del digest (05/10/2026).

GLM muore di bolletta: 1.328 match valutati da glm-5.2 nella tabella
`matches` sono le etichette per addestrare un modello nostro, piccolo e
CPU-friendly. Questo script li scarica nel formato ESATTO con cui GLM li
ha visti (profilo_come_testo / offerta_come_testo di matching/llm.py) —
addestrare su un formato diverso e' addestrare un modello diverso.

    DATABASE_URL=... python scripts/allena_matcher_estrai.py [out.jsonl]

Nota onesta: sono 3 profili utente e 683 offerte distinte. Lo split per
valutazione va fatto PER OFFERTA, non per riga — altrimenti l'esame
misura la memoria, non il modello.
"""
from __future__ import annotations

import json
import os
import sys

import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from nivult.matching.llm import offerta_come_testo, profilo_come_testo  # noqa: E402

SQL = """
SELECT m.score, m.threshold_used, m.reason, m.model, m.evaluated_at,
       c.families, c.seniority, c.skills, c.languages, c.years_experience,
       j.id::text, j.title, j.organization, j.cities,
       j.raw->>'ai_experience_level', j.raw->>'ai_work_arrangement',
       j.raw->>'ai_visa_sponsorship', j.raw->'ai_key_skills',
       j.raw->>'ai_requirements_summary'
FROM matches m
JOIN jobs j ON j.id = m.job_id
JOIN user_cvs c ON c.user_id = m.user_id AND c.status = 'active'
WHERE m.reason IS NOT NULL AND j.purged_at IS NULL
ORDER BY m.evaluated_at
"""


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "dati/matcher-v0.jsonl"
    dsn = os.environ["DATABASE_URL"]
    righe = 0
    with psycopg.connect(dsn) as conn, open(out, "w") as f:
        for r in conn.execute(SQL):
            (score, soglia, motivo, modello, quando,
             famiglie, seniority, skills, lingue, anni,
             jid, titolo, azienda, citta,
             livello, modalita, visto, key_skills, sintesi) = r
            profilo = profilo_come_testo({
                "ruolo": ", ".join(famiglie or []),
                "seniority": seniority or "—",
                "competenze": list(skills or []),
                "lingue": [l.get("code", "") if isinstance(l, dict) else str(l)
                           for l in (lingue or [])],
                "sedi": [],
                "note": f"{anni} anni di esperienza" if anni else None,
            })
            offerta = offerta_come_testo({
                "id": jid, "title": titolo, "organization": azienda,
                "cities": list(citta or []),
                "ai_experience_level": livello,
                "ai_work_arrangement": modalita,
                "ai_visa_sponsorship": visto in ("true", True),
                "ai_key_skills": key_skills or [],
                "ai_requirements_summary": sintesi,
            })
            f.write(json.dumps({
                "job_id": jid, "profilo": profilo, "offerta": offerta,
                "score": score, "soglia": soglia,
                "passato": score >= soglia,
                "motivo": motivo, "modello": modello,
                "quando": quando.isoformat(),
            }, ensure_ascii=False) + "\n")
            righe += 1
    print(f"{righe} righe in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
