"""Il contratto comune fra le fonti e il resto del motore.

Ogni client di fonte traduce il proprio payload nativo in RawJob. Da lì in poi
la pipeline non sa più da dove viene un'offerta: è la cucitura che fa sì che
aggiungere una fonte non tocchi nulla a valle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class RawJob:
    # --- identità ---
    source: str
    source_job_id: str
    url: str
    canonical_url: str
    link_kind: str  # career_site | national_agency | job_board

    # --- sempre presenti ---
    title: str
    title_normalized: str
    organization: str | None
    date_posted: datetime
    raw: dict

    # --- derivabili ---
    domain_derived: str | None = None
    org_linkedin_slug: str | None = None
    cities: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    locations: dict | None = None

    # --- campi AI: presenti su Fantastic, mappati a regole sulle nazionali,
    #     None dove la fonte non ha un equivalente. Mai inventati. ---
    ai_job_language: str | None = None
    ai_visa_sponsorship: bool | None = None
    ai_work_arrangement: str | None = None
    ai_experience_level: str | None = None
    ai_employment_type: str | None = None
    ai_working_hours: str | None = None
    ai_key_skills: list[str] = field(default_factory=list)
    ai_keywords: list[str] = field(default_factory=list)
    ai_taxonomies_a: list[str] = field(default_factory=list)
    ai_requirements_summary: str | None = None
    ai_core_responsibilities: str | None = None

    # --- parziali ---
    salary: dict | None = None
    date_valid_through: datetime | None = None
    ai_education: str | None = None
    organization_logo: str | None = None
    ai_work_arrangement_office_days: int | None = None

    def __post_init__(self) -> None:
        if not self.canonical_url:
            raise ValueError(f"canonical_url mancante per {self.source}:{self.source_job_id}")
        if self.link_kind not in ("career_site", "national_agency", "job_board"):
            raise ValueError(f"link_kind non valido: {self.link_kind!r}")


@dataclass(slots=True)
class FetchResult:
    """Esito di una fetch, non solo i risultati.

    `complete` dice se la fonte ha restituito tutto il disponibile. Serve allo
    sweep delle scadute: se la fetch è stata troncata, il fatto che un'offerta
    non sia comparsa non significa che sia scaduta — significa che non siamo
    arrivati a leggerla.
    """

    jobs: list[RawJob]
    complete: bool
    requests_made: int
    credits_used: int
    total_available: int | None = None
    # Record ricevuti ma non normalizzabili. Vanno riportati, non taciuti: una
    # perdita silenziosa in ingestione non si nota finché qualcuno non conta a
    # mano, e a quel punto è già andata avanti per settimane.
    skipped: int = 0
