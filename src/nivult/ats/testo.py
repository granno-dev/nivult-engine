"""Il testo dell'annuncio, DOVUNQUE l'adapter l'abbia messo.

Ogni ATS chiama la descrizione a modo suo e gli adapter l'hanno conservata
con il nome della fonte: Greenhouse `content`, Ashby `descriptionHtml` /
`descriptionPlain`, Zoho `Job_Description`, Workday `jobDescription`,
Teamtailor `body`. Il 08/09/2026 il demone di v1 e l'estrattore del
dataset leggevano solo `description`: 162.000 offerte Greenhouse e
64.000 Ashby risultavano «senza testo» e restavano fuori da tutto, mentre
il testo c'era. Da qui in poi il testo si legge SOLO passando da qui.
"""
from __future__ import annotations

import re

CHIAVI = ('description', 'content', 'descriptionHtml', 'descriptionPlain', 'externalDescription', 'jobDescription', 'job_description', 'Job_Description', 'body', 'content_html', 'description_html', 'descriptionBody', 'text')

# per le query SQL: il PRIMO campo con almeno 80 caratteri. Non COALESCE:
# COALESCE si ferma su una stringa vuota (Zoho ha description='' e il testo
# in Job_Description), e 11.774 offerte con il testo risultavano senza.
SQL_TESTO = "(SELECT v FROM unnest(ARRAY[raw->>'description', raw->>'content', raw->>'descriptionHtml', raw->>'descriptionPlain', raw->>'externalDescription', raw->>'jobDescription', raw->>'job_description', raw->>'Job_Description', raw->>'body', raw->>'content_html', raw->>'description_html', raw->>'descriptionBody', raw->>'text', raw->'_jobposting'->>'description', raw->>'ShortDescriptionStr']) v WHERE length(v) >= 80 LIMIT 1)"
SQL_HA_TESTO = f"({SQL_TESTO}) IS NOT NULL"


def descrizione(raw: dict | None, massimo: int = 30000) -> str:
    """Il primo campo di testo non vuoto, con i tag HTML tolti."""
    if not isinstance(raw, dict):
        return ""
    candidati = [raw.get(k) for k in CHIAVI] + [(raw.get("_jobposting") or {}).get("description") if isinstance(raw.get("_jobposting"), dict) else None,
                                               raw.get("ShortDescriptionStr")]
    for v in candidati:
        if isinstance(v, str) and len(v) >= 80:
            return re.sub(r"<[^>]+>", " ", v)[:massimo]
    return ""
