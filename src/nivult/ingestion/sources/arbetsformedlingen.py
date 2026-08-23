"""Arbetsförmedlingen — JobTech Dev.

API aperta, documentata, senza autenticazione. Due endpoint, usati per due
scopi diversi:

  JobSearch  ricerca per parole chiave -> ingestione per cluster
  JobStream  delta dal timestamp indicato -> segnale nativo di rimozione

Il secondo è il motivo per cui questa fonte è utile a rodare la logica
incrementale: `removed` e `removed_date` ce li dà la fonte, mentre altrove
dobbiamo dedurre la scadenza dall'assenza.

Contratto verificato sul campo il 2026-08-23.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from nivult.ingestion.base import HttpSource
from nivult.ingestion.models import FetchResult, RawJob
from nivult.ingestion.urls import canonicalize, classify_link, normalize_title, registrable_domain

log = logging.getLogger("nivult.ingestion.arbetsformedlingen")

SEARCH_URL = "https://jobsearch.api.jobtechdev.se/search"
STREAM_URL = "https://jobstream.api.jobtechdev.se/stream"
AGENCY_URL = "https://arbetsformedlingen.se/platsbanken/annonser/{id}"

MAX_LIMIT = 100     # tetto per pagina della JobSearch
MAX_OFFSET = 2000   # tetto di scorrimento

# I timestamp arrivano senza fuso ("2026-08-10T10:55:20"). Sono ora locale
# svedese: interpretarli come UTC li sposterebbe di una o due ore a seconda
# dell'ora legale, e su un campo timestamptz l'errore non si vedrebbe mai.
SE = ZoneInfo("Europe/Stockholm")

# Mappature a regole. Dove non c'è un equivalente onesto si lascia None.
EMPLOYMENT_MAP = {
    "Vanlig anställning": "full-time",
    "Tidsbegränsad anställning": "contract",
    "Behovsanställning": "contract",
    "Sommarjobb / feriejobb": "contract",
}
WORKING_HOURS_MAP = {"Heltid": "full-time", "Deltid": "part-time"}


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=SE)


class ArbetsformedlingenClient(HttpSource):
    source = "arbetsformedlingen"
    countries = frozenset({"SE"})
    credits_per_request = 0

    def __init__(self, **kw):
        super().__init__(rate_per_second=kw.pop("rate_per_second", 3.0), **kw)

    def fetch(self, *, query: str, country: str = "SE", since: datetime | None = None,
              limit: int = MAX_LIMIT) -> FetchResult:
        if country not in self.countries:
            raise ValueError(f"{self.source} non copre {country}")

        params: dict[str, object] = {"q": query, "limit": min(limit, MAX_LIMIT), "offset": 0}
        if since:
            params["published-after"] = since.astimezone(SE).strftime("%Y-%m-%dT%H:%M:%S")

        r = self.request("GET", SEARCH_URL, params=params,
                         headers={"accept": "application/json"})
        if r.status_code != 200:
            raise RuntimeError(f"ricerca fallita ({r.status_code}): {r.text[:300]}")

        payload = r.json()
        hits = payload.get("hits") or []
        total = (payload.get("total") or {}).get("value")

        jobs, skipped = [], 0
        for hit in hits:
            try:
                jobs.append(self._to_raw_job(hit))
            except (ValueError, KeyError, TypeError) as exc:
                skipped += 1
                log.debug("offerta scartata (%s): %s", hit.get("id"), exc)
        if skipped:
            log.info("%s: %d offerte scartate su %d", self.source, skipped, len(hits))

        # complete solo se abbiamo davvero visto tutto il disponibile: lo sweep
        # delle scadute non deve mai basarsi su una fetch troncata.
        complete = total is not None and total <= len(hits)

        return FetchResult(jobs=jobs, complete=complete, requests_made=1,
                           credits_used=0, total_available=total)

    def fetch_removals(self, since: datetime) -> tuple[list[str], int]:
        """Id rimossi dallo stream, e quante variazioni sono state esaminate.

        La fonte ci dice esplicitamente cosa è sparito. È molto meglio che
        dedurlo dall'assenza, che è il metodo fragile che dobbiamo usare
        altrove — e che sbaglia ogni volta che una fetch è stata troncata.
        """
        if since < datetime.now(SE) - timedelta(days=30):
            raise ValueError("lo stream copre una finestra limitata: usa una data recente")

        r = self.request("GET", STREAM_URL,
                         params={"date": since.astimezone(SE).strftime("%Y-%m-%dT%H:%M:%S")},
                         headers={"accept": "application/json"})
        if r.status_code != 200:
            raise RuntimeError(f"stream fallito ({r.status_code}): {r.text[:300]}")

        entries = r.json()
        removed = [str(e["id"]) for e in entries if e.get("removed")]
        log.info("%s: %d rimozioni su %d variazioni", self.source, len(removed), len(entries))
        return removed, len(entries)

    def _to_raw_job(self, h: dict) -> RawJob:
        details = h.get("application_details") or {}
        employer = h.get("employer") or {}
        addr = h.get("workplace_address") or {}

        # Se la candidatura NON passa da Arbetsförmedlingen e c'è un URL, quello
        # è l'ATS aziendale: è un link diretto, e vale più della pagina
        # dell'agenzia. Altrimenti si ripiega sulla pagina di Platsbanken, che
        # viene etichettata come national_agency.
        url = details.get("url") if not details.get("via_af") else None
        if not url:
            url = AGENCY_URL.format(id=h["id"])

        canonical = canonicalize(url)
        occupation = (h.get("occupation") or {}).get("label")
        must_have = h.get("must_have") or {}

        return RawJob(
            source=self.source,
            source_job_id=str(h["id"]),
            url=url,
            canonical_url=canonical,
            link_kind=classify_link(canonical),
            title=h["headline"],
            title_normalized=normalize_title(h["headline"]),
            organization=employer.get("name") or "Okänd arbetsgivare",
            date_posted=_dt(h["publication_date"]),
            domain_derived=registrable_domain(canonical),
            cities=[addr["municipality"]] if addr.get("municipality") else [],
            countries=["SE"],
            locations=addr or None,
            ai_job_language=h.get("identified_language"),
            # experience_required è un booleano, non un livello: mapparlo sulla
            # nostra scala 0-2 / 2-5 / 5-10 / 10+ sarebbe inventare. Resta None.
            ai_experience_level=None,
            ai_employment_type=EMPLOYMENT_MAP.get((h.get("employment_type") or {}).get("label")),
            ai_working_hours=WORKING_HOURS_MAP.get(
                (h.get("working_hours_type") or {}).get("label")),
            ai_key_skills=[s["label"] for s in (must_have.get("skills") or []) if s.get("label")],
            ai_keywords=[occupation] if occupation else [],
            ai_core_responsibilities=(h.get("description") or {}).get("text"),
            date_valid_through=_dt(h.get("application_deadline")),
            organization_logo=h.get("logo_url"),
            raw=h,
        )
