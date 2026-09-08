"""Il prompt di nivult-v2, IDENTICO in addestramento, esame e produzione.
Cambiarlo in un posto solo = un modello diverso: sta qui e basta."""
from __future__ import annotations

FAMIGLIE = ['Administrative', 'Agriculture', 'Art & Design', 'Construction', 'Consulting', 'Creative & Media',
            'Customer Service & Support', 'Data & Analytics', 'Education', 'Energy', 'Engineering',
            'Environmental & Sustainability', 'Finance & Accounting', 'Food & Beverage', 'Government & Public Sector',
            'Healthcare', 'Hospitality', 'Human Resources', 'Legal', 'Logistics', 'Management & Leadership',
            'Manufacturing', 'Marketing', 'Retail', 'Sales', 'Science & Research', 'Security & Safety',
            'Social Services', 'Software', 'Sports & Recreation', 'Technology', 'Trades', 'Transportation', 'none']
SENIORITY = ["intern", "junior", "mid", "senior", "lead", "head"]
CONTRATTO = ["full_time", "part_time", "contract", "temporary", "internship", "apprenticeship"]
REMOTO = ["remote", "hybrid", "onsite"]

SISTEMA = (
    "Sei nivult, il classificatore di annunci di lavoro di Nivult. Leggi l'annuncio e rispondi SOLO con un JSON con i campi richiesti.\n"
    "family: la famiglia del RUOLO (non del settore dell'azienda), una di: " + ", ".join(FAMIGLIE) + ". "
    "\"none\" se non e' un annuncio di lavoro (candidatura spontanea, talent pool, pagina di prova).\n"
    "seniority: " + ", ".join(SENIORITY) + ". employment_type: " + ", ".join(CONTRATTO) + ". remote: " + ", ".join(REMOTO) + ".\n"
    "languages_required: codici ISO-639-1 delle lingue richieste esplicitamente (lista vuota se nessuna).\n"
    "Per seniority, employment_type e remote aggiungi \"<campo>_stimato\": true quando il testo non lo dichiara e lo stai deducendo dal ruolo, "
    "dal titolo, dall'azienda o dalla sede; false quando il testo lo dice."
)


def utente(r: dict, campi: list[str], testo: str) -> str:
    return (f"Campi richiesti: {', '.join(campi)}\n\n"
            f"Titolo: {r.get('title') or ''}\nSede: {r.get('location') or ''} ({r.get('country') or '-'})\n"
            f"Azienda: {(r.get('azienda') or '').split('/')[-1]}\n\n{testo}")


import re  # noqa: E402

# la menzione nel testo: se c'e', e' estrazione; se manca, e' stima
MENZIONE = {
    "employment_type": {
        "full_time": r"\b(full[- ]?time|tempo pieno|temps plein|vollzeit|cdi|indeterminato|permanent|unbefristet|tillsvidare|heltid|fast stilling)\b",
        "part_time": r"\b(part[- ]?time|tempo parziale|temps partiel|teilzeit|deltid|\d{1,2}\s?h(?:/sem|eures)?)\b",
        "temporary": r"\b(temporary|cdd|determinato|fixed[- ]term|befristet|intérim|interim|saisonnier|stagionale|vikariat|tidsbegränsad|contract to hire)\b",
        "contract": r"\b(freelance|contractor|contract|p\.?iva|libéral|selbstständig)\b",
        "internship": r"\b(intern(ship)?|stage|stagiaire|tirocin|praktik)\w*",
        "apprenticeship": r"\b(apprenti|apprendist|ausbildung|alternance|azubi|lehrling)\w*",
    },
    "seniority": {
        "intern": r"\b(intern|stage|stagiaire|tirocin|praktik|student)\w*",
        "junior": r"\b(junior|entry[- ]level|débutant|debutant|einsteiger|neolaureat|graduate|0-2 (years|anni|ans))\w*",
        "mid": r"\b(mid[- ]level|intermediate|confirmé|[2-4] (years|anni|ans|jahre))\b",
        "senior": r"\b(senior|experienced|expérimenté|erfahren|[5-9]\+? (years|anni|ans|jahre)|\d{2}\+? (years|anni|ans|jahre))\b",
        "lead": r"\b(lead|team ?lead|coordinator|coordinatore|responsable d'équipe|teamleiter|supervisor)\b",
        "head": r"\b(head of|director|direttore|directeur|direktor|chief|vp|vice president|c[a-z]o)\b",
    },
    "remote": {
        "remote": r"\b(remote|da remoto|télétravail|teletravail|homeoffice|home office|distans|fully remote)\b",
        "hybrid": r"\b(hybrid|ibrido|hybride|smart working|smartworking)\b",
        "onsite": r"\b(on[- ]?site|in sede|sur site|vor ort|på plats|in presenza)\b",
    },
}
_RX_MENZIONE = {c: {v: re.compile(rx, re.I) for v, rx in d.items()} for c, d in MENZIONE.items()}


def menziona(campo: str, valore: str, testo: str) -> bool:
    rx = _RX_MENZIONE.get(campo, {}).get(valore)
    return bool(rx and rx.search(testo))


