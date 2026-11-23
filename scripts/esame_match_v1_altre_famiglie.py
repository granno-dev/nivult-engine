"""Esame del distillato FUORI dalla famiglia degli utenti (12/10/2026).

Il primo plico era tutto HR perche' gli utenti sono HR: uno studente che
regge solo la famiglia dei suoi utenti non e' un motore, e' un eco. Qui
profili sintetici di altre famiglie contro offerte VERE dal corpus ATS
grande (4M), in piu' lingue. Chi giudica legge alla cieca.

Per famiglia: 6 offerte della famiglia, 5 laterali, 6 fuori famiglia.
"""
from __future__ import annotations

import json

import psycopg

from nivult.ponte_ats import ats_database_url

PROFILI = [
    {"chiave": "data-engineer",
     "testo": "Ruolo cercato: Data Engineering\nSeniority: Senior\nCompetenze: Python, SQL, Spark, Airflow, data modeling, ETL, dbt, cloud data platforms\nLingue: English, Italian\nSedi accettate:\nNote: 9 anni di esperienza"},
    {"chiave": "infermiera",
     "testo": "Ruolo cercato: Nursing\nSeniority: Mid\nCompetenze: assistenza al paziente, triage, somministrazione farmaci, cartelle cliniche elettroniche, igiene ospedaliera\nLingue: Italian\nSedi accettate:\nNote: 6 anni di esperienza"},
    {"chiave": "logistica",
     "testo": "Ruolo cercato: Warehouse & Logistics\nSeniority: Entry\nCompetenze: picking, packing, muletto, inventario, spedizioni\nLingue: Italian\nSedi accettate:\nNote: 2 anni di esperienza"},
    {"chiave": "chef",
     "testo": "Ruolo cercato: Culinary\nSeniority: Senior\nCompetenze: cucina italiana, gestione brigata, menu engineering, HACCP, cost control\nLingue: Italian, English\nSedi accettate:\nNote: 12 anni di esperienza"},
]

# (famiglia, titolo ILIKE, piattaforma preferita per varieta')
ON_FAMILY = {
    "data-engineer": ["%data engineer%", "%data engineer%"],
    "infermiera": ["%nurse%", "%infermier%"],
    "logistica": ["%warehouse%", "%magazzinier%"],
    "chef": ["%chef%", "%cuoco%"],
}

SQL_ON = """
SELECT j.title, c.company_name, j.city, j.country, j.seniority, j.remote,
       j.skills, j.platform_id
FROM ats_jobs j JOIN ats_companies c ON c.platform_id = j.platform_id AND c.slug = j.slug
WHERE j.expired_at IS NULL AND j.title ILIKE %s AND c.company_name IS NOT NULL
  AND length(coalesce(array_to_string(j.skills, ' '), '')) > 20
ORDER BY random() LIMIT %s
"""

SQL_OFF = """
SELECT j.title, c.company_name, j.city, j.country, j.seniority, j.remote,
       j.skills, j.platform_id
FROM ats_jobs j JOIN ats_companies c ON c.platform_id = j.platform_id AND c.slug = j.slug
WHERE j.expired_at IS NULL AND NOT (j.title ILIKE %s) AND c.company_name IS NOT NULL
  AND length(coalesce(array_to_string(j.skills, ' '), '')) > 20
ORDER BY random() LIMIT %s
"""


def rendi(r) -> str:
    title, azienda, city, country, seniority, remote, skills, piattaforma = r
    pezzi = [f"id: {piattaforma}", f"titolo: {title}"]
    if azienda:
        pezzi.append(f"azienda: {azienda}")
    if seniority:
        pezzi.append(f"esperienza: {seniority}")
    if remote:
        pezzi.append(f"modalità: {remote}")
    if city:
        pezzi.append(f"sede: {city}, {country}")
    if skills:
        pezzi.append(f"competenze: {', '.join(skills[:12])}")
    return " | ".join(pezzi)


def main() -> int:
    out = open("/tmp/esame-famiglie.jsonl", "w", encoding="utf-8")
    n = 0
    with psycopg.connect(ats_database_url()) as conn:
        for p in PROFILI:
            pattern = ON_FAMILY[p["chiave"]][0]
            for r in conn.execute(SQL_ON, (pattern, 6)):
                out.write(json.dumps({"famiglia": p["chiave"], "tipo": "dentro",
                                      "profilo": p["testo"], "offerta": rendi(r)},
                                     ensure_ascii=False) + "\n")
                n += 1
            for r in conn.execute(SQL_OFF, (pattern, 6)):
                out.write(json.dumps({"famiglia": p["chiave"], "tipo": "fuori",
                                      "profilo": p["testo"], "offerta": rendi(r)},
                                     ensure_ascii=False) + "\n")
                n += 1
    out.close()
    print(f"coppie scritte: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
