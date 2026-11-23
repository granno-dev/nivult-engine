"""Il contratto comune fra le fonti e il resto del motore.

Ogni client di fonte traduce il proprio payload nativo in RawJob. Da lì in poi
la pipeline non sa più da dove viene un'offerta: è la cucitura che fa sì che
aggiungere una fonte non tocchi nulla a valle.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


EMPLOYMENT_TYPES = frozenset({
    "FULL_TIME", "PART_TIME", "CONTRACTOR", "TEMPORARY", "INTERN", "OTHER",
})
_EMPLOYMENT_ALIASES = {
    "FULL_TIME": "FULL_TIME", "FULLTIME": "FULL_TIME",
    "PART_TIME": "PART_TIME", "PARTTIME": "PART_TIME",
    "CONTRACT": "CONTRACTOR", "CONTRACTOR": "CONTRACTOR",
    "FREELANCE": "CONTRACTOR",
    "TEMP": "TEMPORARY", "TEMPORARY": "TEMPORARY",
    "FIXED_TERM": "TEMPORARY", "SEASONAL": "TEMPORARY",
    "INTERN": "INTERN", "INTERNSHIP": "INTERN",
    "OTHER": "OTHER",
}
_PERIODI = {"hour": "HOUR", "day": "DAY", "week": "WEEK",
            "month": "MONTH", "year": "YEAR"}
_PERIODI_INVERSI = {v: k for k, v in _PERIODI.items()}


def normalize_employment(value) -> str | None:
    if not isinstance(value, str):
        return None
    key = re.sub(r"[\s-]+", "_", value.strip().upper())
    return _EMPLOYMENT_ALIASES.get(key)


def _importo(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    if not math.isfinite(float(value)) or value <= 0:
        return None
    return value


def _periodo(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if value in _PERIODI else None


def _valuta(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().upper()
    return value if len(value) == 3 and value.isalpha() else None


def monetary_amount(minimum, maximum, currency, period) -> dict | None:
    """Il MonetaryAmount unico esposto dal contratto, solo con dati presenti."""
    minimum, maximum = _importo(minimum), _importo(maximum)
    if minimum is None and maximum is None:
        return None
    value = {}
    if minimum is not None:
        value["minValue"] = minimum
    if maximum is not None:
        value["maxValue"] = maximum
    unit = _PERIODI.get(period) if period else None
    if unit:
        value["unitText"] = unit
    amount = {"@type": "MonetaryAmount", "value": value}
    if currency:
        amount["currency"] = currency
    return amount


def _da_monetary_amount(salary) -> tuple:
    if not isinstance(salary, dict):
        return None, None, None, None
    value = salary.get("value")
    dentro = value if isinstance(value, dict) else salary
    minimum, maximum = dentro.get("minValue"), dentro.get("maxValue")
    if not isinstance(value, (dict, list)) and value is not None:
        minimum = value
    if isinstance(value, list) and value:
        minimum = value[0]
        maximum = value[1] if len(value) > 1 else None
    period = _PERIODI_INVERSI.get(str(dentro.get("unitText") or "").upper())
    return minimum, maximum, salary.get("currency"), period


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
    date_posted_declared: bool | None = None

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
    # Costruito da __post_init__ come MonetaryAmount canonico quando ci sono
    # importi; un payload senza evidenza non si spaccia per tale.
    salary: dict | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    salary_da: str | None = None
    date_valid_through: datetime | None = None
    ai_education: str | None = None
    organization_logo: str | None = None
    ai_work_arrangement_office_days: int | None = None

    # Arricchimento organizzazione: presente solo sulle fonti che lo espongono.
    org_size: str | None = None
    org_headcount: int | None = None
    org_industry: str | None = None
    org_logo_permalink: str | None = None
    employer_agency_declared: bool | None = None

    def __post_init__(self) -> None:
        if not self.canonical_url:
            raise ValueError(f"canonical_url mancante per {self.source}:{self.source_job_id}")
        if self.link_kind not in ("career_site", "national_agency", "job_board"):
            raise ValueError(f"link_kind non valido: {self.link_kind!r}")
        self.ai_employment_type = normalize_employment(self.ai_employment_type)

        minimum, maximum = _importo(self.salary_min), _importo(self.salary_max)
        currency, period = _valuta(self.salary_currency), _periodo(self.salary_period)
        if minimum is None and maximum is None:
            smin, smax, scurrency, speriod = _da_monetary_amount(self.salary)
            minimum, maximum = _importo(smin), _importo(smax)
            currency = currency or _valuta(scurrency)
            period = period or _periodo(speriod)
        if minimum is not None and maximum is not None and minimum > maximum:
            minimum, maximum = maximum, minimum

        self.salary_min, self.salary_max = minimum, maximum
        self.salary_currency, self.salary_period = currency, period
        self.salary = monetary_amount(minimum, maximum, currency, period)
        if self.salary is None:
            self.salary_currency = None
            self.salary_period = None
            self.salary_da = None


@dataclass(slots=True)
class FetchResult:
    """Esito di UNA PAGINA, non dell'intera ingestione.

    `complete` dice se questa pagina ha esaurito i risultati disponibili. Se il
    ciclo si ferma prima — per il tetto giornaliero o per il limite di
    scorrimento della fonte — la fetch complessiva è troncata anche quando
    l'ultima pagina diceva il contrario. Quella somma la fa il runner, ed è ciò
    che finisce in `ingestion_runs.fetch_complete`.
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
    # Scartati dal filtro data locale: non sono errori di normalizzazione, ma
    # contano fra i record ricevuti per non rileggere sempre la stessa pagina.
    out_of_window: int = 0

    @property
    def received(self) -> int:
        """Record arrivati dalla fonte, normalizzabili o no.

        È questo che fa avanzare l'offset, non len(jobs): contando solo quelli
        normalizzati, ogni scarto sposterebbe la finestra e salterebbe
        silenziosamente un'offerta buona a ogni pagina.
        """
        return len(self.jobs) + self.skipped + self.out_of_window
