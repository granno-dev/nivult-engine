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
EXPIRED_PATH = "/expired-ats"

# L'endpoint delle scadute ha un vocabolario di time_frame TUTTO SUO:
# 1h/1d/1m/6m, con "1d" dove la ricerca usa "24h" e "1m" che sulla
# ricerca non è valido. Terzo vocabolario per la stessa idea.
EXPIRED_FRAMES = ("1h", "1d", "1m", "6m")

MAX_LIMIT = 1000

# time_frame accetta scaglioni fissi, non una data. Si sceglie il più stretto
# che copre la finestra richiesta: chiedere più indietro del necessario costa
# crediti veri.
# active-ats accetta solo 1h/24h/7d/6m — NON "1m", che invece active-ats-count
# tollera. Verificato: la ricerca risponde 400 con l'elenco dei valori validi.
TIME_FRAMES = ((timedelta(hours=1), "1h"), (timedelta(days=1), "24h"),
               (timedelta(days=7), "7d"), (timedelta(days=186), "6m"))

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

    def count(self, *, query: str, country: str, since: datetime | None = None,
              taxonomy: str | None = None) -> int:
        """Quante offerte ci sarebbero. 1 Request, ZERO crediti Jobs.

        Da chiamare sempre prima di scaricare: è l'unico modo di sapere quanto
        costerebbe una fetch prima di averla pagata.
        """
        r = self.request("GET", BASE_URL + COUNT_PATH, headers=self._headers,
                         params=self.search_params(query=query, country=country,
                                                   since=since, taxonomy=taxonomy))
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

    def search_params(self, *, query: str, country: str, since: datetime | None,
                      taxonomy: str | None = None) -> dict:
        """Parametri di ricerca comuni a fetch e count.

        Se `taxonomy` c'è si filtra su quella e NON sul titolo: la tassonomia
        cattura la famiglia professionale in qualunque lingua, mentre il titolo
        costringe a rincorrere i sinonimi. Misurato in Germania su 7 giorni:
        title='Human Resources' dà 26 offerte, ai_taxonomies_a='Human Resources'
        ne dà 646.
        """
        p: dict[str, object] = {"location": COUNTRY_NAMES.get(country, country)}
        if taxonomy:
            p["ai_taxonomies_a"] = taxonomy
        else:
            p["title"] = query
        # time_frame è OBBLIGATORIO sulla ricerca, ma i suoi scaglioni sono
        # grossolani: 14 giorni ricadrebbero in "6m". date_posted_gte restringe
        # dentro lo scaglione — misurato: 6m dà 6775 offerte, 6m più la data
        # ne dà 2003 — e i crediti si pagano su quelle restituite, quindi la
        # data esatta è ciò che evita di pagare sei mesi per due settimane.
        p["time_frame"] = time_frame_for(since)
        if since:
            p["date_posted_gte"] = since.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S")
        return p

    def expired_ids(self, *, time_frame: str = "1d") -> list[str]:
        """Id delle offerte scadute nella finestra. ZERO crediti Jobs.

        Restituisce una lista di soli interi, non di record: è il segnale di
        rimozione più economico che abbiamo su qualunque fonte. Verificato: la
        quota Jobs non si muove.
        """
        if time_frame not in EXPIRED_FRAMES:
            raise ValueError(f"time_frame per le scadute: {EXPIRED_FRAMES}, "
                             f"ricevuto {time_frame!r}")
        r = self.request("GET", BASE_URL + EXPIRED_PATH, headers=self._headers,
                         params={"time_frame": time_frame})
        if r.status_code != 200:
            raise RuntimeError(f"scadute non recuperabili ({r.status_code}): {r.text[:200]}")
        return [str(i) for i in r.json()]

    def fetch(self, *, query: str, country: str, since: datetime | None = None,
              limit: int = 100, offset: int = 0, taxonomy: str | None = None,
              with_org_details: bool = True,
              extra_filters: dict | None = None) -> FetchResult:
        if country not in self.countries:
            raise ValueError(f"{self.source} non copre {country}")

        params = self.search_params(query=query, country=country, since=since,
                                    taxonomy=taxonomy)
        params.update({"limit": min(limit, MAX_LIMIT), "offset": offset})
        if extra_filters:
            params.update(extra_filters)
        if with_org_details:
            # L'arricchimento è OPT-IN: senza questo flag la risposta non porta
            # org_linkedin_size né gli altri campi di organizzazione.
            params["include_basic_organization_details"] = "true"

        r = self.request("GET", BASE_URL + SEARCH_PATH, headers=self._headers,
                         params=params)
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

        posted = _dt(r["date_posted"])
        if posted is None:
            raise ValueError("date_posted assente")

        return RawJob(
            source=self.source,
            source_job_id=str(r["id"]),
            url=url,
            canonical_url=canonical,
            link_kind=classify_link(canonical),
            title=r["title"],
            title_normalized=normalize_title(r["title"]),
            organization=r.get("organization") or None,
            date_posted=posted,
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
            date_valid_through=_dt(r.get("date_valid_through")),
            ai_education=r.get("ai_education"),
            organization_logo=r.get("organization_logo"),
            ai_work_arrangement_office_days=r.get("ai_work_arrangement_office_days"),
            org_size=r.get("org_linkedin_size"),
            org_headcount=_int_or_none(r.get("org_linkedin_headcount")),
            org_industry=r.get("org_linkedin_industry"),
            org_logo_permalink=r.get("org_logo_permalink"),
            employer_agency_declared=r.get("org_linkedin_recruitment_agency_derived"),
            raw=r,
        )


def _first(v):
    if isinstance(v, list):
        return v[0] if v else None
    return v


def _as_str(v):
    return None if v is None else str(v)


def _dt(value) -> datetime | None:
    """Timestamp della fonte, sempre con fuso.

    date_posted arriva senza marcatore ("2026-08-23T00:03:58"), quindi
    fromisoformat produce un datetime naive: confrontarlo con una finestra
    consapevole del fuso solleva, e scritto in una colonna timestamptz verrebbe
    interpretato secondo il fuso della sessione. Si assume UTC, che è ciò che
    la fonte usa negli altri campi datati.
    """
    if not value:
        return None
    d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _int_or_none(v):
    return int(v) if v and str(v).isdigit() else None
