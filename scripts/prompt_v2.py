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


