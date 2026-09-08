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

CHIAVI = ("description", "content", "descriptionHtml", "descriptionPlain", "externalDescription",
          "jobDescription", "job_description", "Job_Description", "body", "description_html",
          "descriptionBody", "text")

# per le query SQL: la stessa lista, nello stesso ordine
SQL_TESTO = "COALESCE(" + ", ".join(f"raw->>'{k}'" for k in CHIAVI) + ", '')"
# vero se l'annuncio ha un testo utilizzabile
SQL_HA_TESTO = f"length({SQL_TESTO}) >= 80"


def descrizione(raw: dict | None, massimo: int = 30000) -> str:
    """Il primo campo di testo non vuoto, con i tag HTML tolti."""
    if not isinstance(raw, dict):
        return ""
    for k in CHIAVI:
        v = raw.get(k)
        if isinstance(v, str) and len(v) >= 80:
            return re.sub(r"<[^>]+>", " ", v)[:massimo]
    return ""
