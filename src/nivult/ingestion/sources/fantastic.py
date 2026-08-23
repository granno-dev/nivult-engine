"""Fantastic.jobs — API a pagamento.

CONTRATTO NON ANCORA VERIFICATO SUL CAMPO: mancano le chiavi. Endpoint,
autenticazione e parametri vengono da una prova manuale riferita, non da una
risposta che abbiamo visto. Il primo `probe` è ciò che li conferma.

Contabilità dei crediti, che qui è diversa da tutte le altre fonti:

    ogni chiamata scala 1 da Requests
    e scala da Jobs UN CREDITO PER OFFERTA RESTITUITA

Quindi il costo non è noto prima di fare la chiamata. Il ciclo è
riserva-poi-concilia: si riserva il costo peggiore (la dimensione di pagina),
si chiama, e si restituisce la differenza. Vedi `cluster_settle_credits`.

`active-ats-count` restituisce solo il conteggio: 1 Request, zero crediti Jobs.
Serve a dimensionare prima di scaricare — su un piano a consumo, sapere quanto
costerebbe prima di spendere vale più di una pagina di risultati.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from nivult.ingestion.base import HttpSource
from nivult.ingestion.models import FetchResult, RawJob
from nivult.ingestion.urls import canonicalize, classify_link, normalize_title, registrable_domain

log = logging.getLogger("nivult.ingestion.fantastic")

BASE_URL = "https://data.fantastic.jobs/v1"
SEARCH_PATH = "/active-ats"
COUNT_PATH = "/active-ats-count"

MAX_LIMIT = 1000

# time_frame accetta scaglioni fissi, non una data. Si sceglie il più stretto
# che copre la finestra richiesta: chiedere più indietro del necessario costa
# crediti veri.
TIME_FRAMES = ((timedelta(hours=1), "1h"), (timedelta(days=1), "24h"),
               (timedelta(days=7), "7d"), (timedelta(days=31), "1m"),
               (timedelta(days=186), "6m"))

# La fonte restituisce i paesi per NOME ("Germany"), mentre France Travail dà
# "FR" e Arbetsförmedlingen "SE". Mescolarli in jobs.countries romperebbe ogni
# filtro per paese in modo silenzioso: la riga c'è, ma nessun WHERE la trova.
COUNTRY_NAMES = {
    "FR": "France", "DE": "Germany", "IT": "Italy", "ES": "Spain",
    "SE": "Sweden", "NO": "Norway", "FI": "Finland", "NL": "Netherlands",
    "BE": "Belgium", "AT": "Austria", "PT": "Portugal", "IE": "Ireland",
    "DK": "Denmark", "PL": "Poland", "CH": "Switzerland",
}


# Il chiamante calcola `since` come now - X, e qui si ricalcola `now`: fra le
# due letture passano microsecondi, e senza tolleranza una finestra di
# esattamente 24 ore ricadeva nello scaglione 7d. Su una fonte che scala un
# credito per offerta restituita, quello scarto silenzioso costa sette volte
# tanto.
_SLACK = timedelta(minutes=5)


COUNTRY_CODES = {name.lower(): code for code, name in COUNTRY_NAMES.items()}


def to_iso_country(value: str) -> str | None:
    """"Germany" -> "DE". None se non riconosciuto: meglio un vuoto che un
    valore che nessun filtro troverà mai."""
    if not value:
        return None
    v = value.strip()
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return COUNTRY_CODES.get(v.lower())


def time_frame_for(since: datetime | None) -> str:
    if since is None:
        return "7d"
    age = datetime.now(timezone.utc) - since.astimezone(timezone.utc)
    for span, label in TIME_FRAMES:
        if age <= span + _SLACK:
            return label
    return "6m"


class FantasticClient(HttpSource):
    source = "fantastic"
    countries = frozenset(COUNTRY_NAMES)
    # Parte fissa: 1 Request per chiamata.
    credits_per_request = 1
    # Parte variabile: 1 credito Jobs per offerta restituita. È questa che rende
    # il costo ignoto prima della chiamata.
    credits_per_job = 1
    page_size = MAX_LIMIT
    max_offset = 100_000

    def __init__(self, api_key: str | None = None, **kw):
        super().__init__(rate_per_second=kw.pop("rate_per_second", 1.0), **kw)
        # Quota residua dichiarata dalla fonte, per riconciliare provider_budget
        # con la verità invece che con la nostra stima.
        self.jobs_remaining: int | None = None
        self.requests_remaining: int | None = None
        self.api_key = api_key or os.environ.get("FANTASTIC_API_KEY", "")
        if not self.api_key:
            raise SystemExit(
                "Serve FANTASTIC_API_KEY.\n"
                "Piano Starter-20k: 20.000 offerte e 10.000 richieste al mese."
            )

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "accept": "application/json"}

    def count(self, *, query: str, country: str, since: datetime | None = None) -> int:
        """Quante offerte ci sarebbero. 1 Request, ZERO crediti Jobs.

        Da chiamare sempre prima di scaricare: è l'unico modo di sapere quanto
        costerebbe una fetch prima di averla pagata.
        """
        r = self.request("GET", BASE_URL + COUNT_PATH, headers=self._headers,
                         params={"title": query,
                                 "location": COUNTRY_NAMES.get(country, country),
                                 "time_frame": time_frame_for(since)})
        if r.status_code != 200:
            raise RuntimeError(f"conteggio fallito ({r.status_code}): {r.text[:300]}")
        payload = r.json()
        # La forma esatta va confermata dal probe: potrebbe essere un intero
        # nudo o un oggetto con una chiave.
        if isinstance(payload, int):
            return payload
        for key in ("count", "total", "results", "value"):
            if isinstance(payload, dict) and key in payload:
                return int(payload[key])
        raise RuntimeError(f"forma del conteggio non riconosciuta: {str(payload)[:200]}")

    def fetch(self, *, query: str, country: str, since: datetime | None = None,
              limit: int = 100, offset: int = 0) -> FetchResult:
        if country not in self.countries:
            raise ValueError(f"{self.source} non copre {country}")

        r = self.request("GET", BASE_URL + SEARCH_PATH, headers=self._headers,
                         params={"title": query,
                                 "location": COUNTRY_NAMES.get(country, country),
                                 "time_frame": time_frame_for(since),
                                 "limit": min(limit, MAX_LIMIT),
                                 "offset": offset})
        if r.status_code != 200:
            raise RuntimeError(f"ricerca fallita ({r.status_code}): {r.text[:300]}")

        payload = r.json()
        records = payload if isinstance(payload, list) else payload.get("data", [])

        # Il costo REALE, dichiarato dalla fonte. Meglio di contare i record:
        # se un giorno la tariffa cambia, questo lo dice e il conteggio no.
        billed = r.headers.get("x-api-jobs-this-request")
        credits = int(billed) if billed and billed.isdigit() else len(records)
        self.jobs_remaining = _int_or_none(r.headers.get("x-api-jobs-remaining"))
        self.requests_remaining = _int_or_none(r.headers.get("x-api-requests-remaining"))

        jobs, skipped = [], 0
        for rec in records:
            try:
                jobs.append(self._to_raw_job(rec))
            except (ValueError, KeyError, TypeError) as exc:
                skipped += 1
                log.debug("offerta scartata (%s): %s", rec.get("id"), exc)
        if skipped:
            log.warning("%s: %d record non normalizzabili su %d",
                        self.source, skipped, len(records))

        # Meno record del limite richiesto significa fine dei risultati.
        complete = len(records) < min(limit, MAX_LIMIT)

        return FetchResult(
            jobs=jobs, complete=complete, requests_made=1,
            credits_used=credits,
            total_available=None, skipped=skipped)

    def _to_raw_job(self, r: dict) -> RawJob:
        url = r.get("url") or ""
        if not url:
            raise ValueError("url assente")
        canonical = canonicalize(url)

        posted = r["date_posted"]
        valid = r.get("date_valid_through")

        return RawJob(
            source=self.source,
            source_job_id=str(r["id"]),
            url=url,
            canonical_url=canonical,
            link_kind=classify_link(canonical),
            title=r["title"],
            title_normalized=normalize_title(r["title"]),
            organization=r.get("organization") or None,
            date_posted=datetime.fromisoformat(str(posted).replace("Z", "+00:00")),
            domain_derived=r.get("domain_derived") or registrable_domain(canonical),
            org_linkedin_slug=r.get("org_linkedin_slug"),
            cities=[c for c in (r.get("cities_derived") or []) if c],
            countries=[iso for iso in
                       (to_iso_country(c) for c in (r.get("countries_derived") or []))
                       if iso],
            locations=r.get("locations_raw") or r.get("locations") or None,
            ai_job_language=r.get("ai_job_language"),
            ai_visa_sponsorship=r.get("ai_visa_sponsorship"),
            ai_work_arrangement=r.get("ai_work_arrangement"),
            ai_experience_level=r.get("ai_experience_level"),
            ai_employment_type=_first(r.get("ai_employment_type")),
            ai_working_hours=_as_str(r.get("ai_working_hours")),
            ai_key_skills=list(r.get("ai_key_skills") or []),
            ai_keywords=list(r.get("ai_keywords") or []),
            ai_taxonomies_a=list(r.get("ai_taxonomies_a") or []),
            ai_requirements_summary=r.get("ai_requirements_summary"),
            ai_core_responsibilities=r.get("ai_core_responsibilities"),
            salary=r.get("salary") or None,
            date_valid_through=(datetime.fromisoformat(str(valid).replace("Z", "+00:00"))
                                if valid else None),
            ai_education=r.get("ai_education"),
            organization_logo=r.get("organization_logo"),
            ai_work_arrangement_office_days=r.get("ai_work_arrangement_office_days"),
            raw=r,
        )


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _as_str(v):
    return None if v is None else str(v)


def _int_or_none(v):
    return int(v) if v and str(v).isdigit() else None
