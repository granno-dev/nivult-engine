"""LinkedIn — endpoint guest pubblici, senza login.

COMPLIANCE, scritta qui perche' e' parte del contratto della fonte:

  - solo le pagine PUBBLICHE degli annunci (jobs-guest): mai login, mai
    profili di persone, mai niente che stia dietro autenticazione;
  - query chirurgiche: una per ciascun termine configurato sul cluster,
    massimo 8 passi da 10 risultati per termine, non scarichi massivi;
  - ritmo gentile: 2-5 secondi con jitter fra una richiesta e l'altra,
    429 onorato con Retry-After e stop pulito (lo fa HttpSource);
  - user-agent onesto: quello standard del motore, niente travestimenti.
    Verificato il 22/10/2026: l'endpoint guest risponde 200 anche cosi'.

Due endpoint, entrambi pubblici:

  - ricerca: /jobs-guest/jobs/api/seeMoreJobPostings/search
    (frammenti HTML di card, ~10 per pagina, paginazione start=0,10,20…)
  - dettaglio: /jobs-guest/jobs/api/jobPosting/{jobId}
    (descrizione completa + criteri: seniority, tipo di contratto)

Parsing con html.parser della libreria standard: bs4/lxml non sono fra le
dipendenze del progetto e il frammento delle card e' regolare. Se un giorno
LinkedIn cambia il markup il parser semplicemente non trovera' piu' nulla —
e il conteggio `skipped` lo dira', invece di fallire in silenzio.
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser

from nivult.ingestion.base import HttpSource
from nivult.ingestion.models import FetchResult, RawJob
from nivult.ingestion.urls import canonicalize, normalize_title, registrable_domain

log = logging.getLogger("nivult.ingestion.linkedin")

BASE = "https://www.linkedin.com/jobs-guest/jobs/api"
SEARCH_URL = BASE + "/seeMoreJobPostings/search"
DETAIL_URL = BASE + "/jobPosting/"

PAGE_SIZE = 10           # passo di paginazione prudente (le pagine guest
                         # variano 10-30 card: la dedup assorbe le sovrapposizioni)
MAX_OFFSET = 80          # 8 passi per query: i giorni pieni di un mercato
                         # grande non si tagliano piu' a 20 (23/10/2026)

# La ricerca guest filtra per freschezza con f_TPR (secondi): chiedere solo
# la finestra che serve e' la meta' della gentilezza — l'altra meta' e' il
# ritmo. Scaglioni della fonte: giorno, settimana, mese.
def _f_tpr(since: datetime | None) -> str | None:
    if since is None:
        return None
    eta = (datetime.now(timezone.utc) - since.astimezone(timezone.utc)).days
    if eta <= 1:
        return "r86400"
    if eta <= 7:
        return "r604800"
    return "r2592000"


# LinkedIn vuole il paese per NOME e in inglese funziona meglio — la stessa
# mappa di fantastic.py, che copre i mercati aperti dal motore.
COUNTRY_NAMES = {
    "FR": "France", "DE": "Germany", "IT": "Italy", "ES": "Spain",
    "SE": "Sweden", "NO": "Norway", "FI": "Finland", "NL": "Netherlands",
    "BE": "Belgium", "AT": "Austria", "PT": "Portugal", "IE": "Ireland",
    "DK": "Denmark", "PL": "Poland", "CH": "Switzerland",
}

# I criteri del dettaglio, mappati a regole sul nostro vocabolario. "Not
# Applicable" e' un'assenza travestita: diventa None, non uno scaglione.
SENIORITY_MAP = {
    "internship": "0-2", "entry level": "0-2", "associate": "2-5",
    "mid-senior level": "5-10", "director": "10+", "executive": "10+",
}
EMPLOYMENT_MAP = {
    "full-time": "FULL_TIME", "part-time": "PART_TIME",
    "contract": "CONTRACTOR", "temporary": "TEMPORARY",
    "internship": "INTERN", "other": "OTHER",
}


class _CardParser(HTMLParser):
    """Le card della ricerca guest, senza dipendenze.

    Ogni card e' un <div class="... job-search-card" data-entity-urn=
    "urn:li:jobPosting:{id}"> con dentro: il link pubblico
    (a.base-card__full-link), il titolo (h3.base-search-card__title),
    l'azienda (h4.base-search-card__subtitle), la sede
    (span.job-search-card__location) e la data (<time datetime="YYYY-MM-DD">,
    che risparmia di interpretare «1 week ago»).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[dict] = []
        self._card: dict | None = None
        self._cattura: str | None = None     # quale campo sta arrivando
        self._in_company_link = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        # Attributi senza valore (<div itemscope>) arrivano come None.
        cls = a.get("class") or ""
        if tag == "div" and "job-search-card" in cls and a.get("data-entity-urn"):
            m = re.search(r"jobPosting:(\d+)", a["data-entity-urn"])
            if m:
                self._card = {"id": m.group(1)}
            return
        if self._card is None:
            return
        if tag == "a" and "base-card__full-link" in cls:
            # L'href porta refId/trackingId: resta nel raw; l'URL pubblico
            # canonico lo costruiamo noi dal jobId.
            self._card["href"] = a.get("href", "")
        elif tag == "h3" and "base-search-card__title" in cls:
            self._cattura = "title"
        elif tag == "h4" and "base-search-card__subtitle" in cls:
            self._cattura = "company"
        elif tag == "a" and self._cattura == "company" \
                and "linkedin.com/company/" in a.get("href", ""):
            m = re.search(r"/company/([^/?]+)", a["href"])
            if m:
                self._card["org_slug"] = m.group(1)
        elif tag == "span" and "job-search-card__location" in cls:
            self._cattura = "location"
        elif tag == "time" and a.get("datetime"):
            self._card["posted"] = a["datetime"]

    def handle_endtag(self, tag):
        if tag in ("h3", "h4", "span"):
            self._cattura = None
        elif tag == "li" and self._card is not None:
            self.cards.append(self._card)
            self._card = None
            self._cattura = None

    def handle_data(self, data):
        if self._card is not None and self._cattura:
            testo = " ".join(data.split())
            if testo:
                self._card[self._cattura] = (
                    self._card.get(self._cattura, "") + " " + testo).strip()


class _DettaglioParser(HTMLParser):
    """Il dettaglio guest: descrizione in show-more-less-html__markup e i
    criteri (Seniority level, Employment type, …) come coppie
    sottotitolo/valore."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.descrizione: list[str] = []
        self.criteri: dict[str, str] = {}
        self._in_descrizione = 0
        self._voce: str | None = None        # 'chiave' | 'valore'
        self._chiave = ""

    def handle_starttag(self, tag, attrs):
        # Attributi senza valore (<div itemscope>) arrivano come None.
        cls = dict(attrs).get("class") or ""
        if "show-more-less-html__markup" in cls:
            self._in_descrizione = 1
        elif self._in_descrizione and tag == "div":
            # I div annidati nella descrizione contano: senza, il primo
            # </div> interno chiuderebbe la cattura a meta' testo.
            self._in_descrizione += 1
        elif "description__job-criteria-subheader" in cls:
            self._voce = "chiave"
            self._chiave = ""
        elif "description__job-criteria-text" in cls:
            self._voce = "valore"
        elif self._in_descrizione and tag in ("p", "br", "li"):
            self.descrizione.append("\n")

    def handle_endtag(self, tag):
        if self._in_descrizione and tag == "div":
            self._in_descrizione -= 1
        elif tag in ("h3", "span") and self._voce:
            self._voce = None

    def handle_data(self, data):
        if self._in_descrizione:
            self.descrizione.append(data)
        elif self._voce == "chiave":
            self._chiave += data.strip()
        elif self._voce == "valore" and self._chiave:
            self.criteri[self._chiave] = " ".join(data.split())

    def testo(self) -> str:
        """La descrizione come testo puro, con gli a capo dove stavano."""
        grezzo = "".join(self.descrizione)
        righe = [" ".join(r.split()) for r in grezzo.split("\n")]
        return "\n".join(r for r in righe if r).strip()


class LinkedInClient(HttpSource):
    source = "linkedin"
    countries = frozenset(COUNTRY_NAMES)
    credits_per_request = 0
    page_size = PAGE_SIZE
    max_offset = MAX_OFFSET

    def __init__(self, **kw):
        # Ritmo gentile di default: il bucket impone il passo minimo, il
        # jitter in _piano() rende le richieste irregolari come farebbe una
        # persona, e su 429 la base onora Retry-After e poi si ferma.
        self.con_dettaglio = kw.pop("con_dettaglio", True)
        super().__init__(rate_per_second=kw.pop("rate_per_second", 0.25),
                         burst=1, max_retries=kw.pop("max_retries", 2), **kw)

    @staticmethod
    def _piano() -> None:
        """2-5 secondi fra una richiesta e l'altra, con jitter."""
        time.sleep(random.uniform(2.0, 5.0))

    def fetch(self, *, query: str, country: str, since: datetime | None = None,
              limit: int = PAGE_SIZE, offset: int = 0, **_) -> FetchResult:
        if country not in self.countries:
            raise ValueError(f"{self.source} non copre {country}")
        first_attempt = len(self.attempts)

        params: dict[str, object] = {
            "keywords": query,
            "location": COUNTRY_NAMES[country],
            "start": offset,
        }
        tpr = _f_tpr(since)
        if tpr:
            params["f_TPR"] = tpr

        self._piano()
        r = self.request("GET", SEARCH_URL, params=params)
        if r.status_code in (403, 999):
            # Blocco della fonte: stop pulito e rumoroso, mai martellare.
            raise RuntimeError(f"{self.source}: bloccato ({r.status_code})")
        if r.status_code != 200:
            raise RuntimeError(f"ricerca fallita ({r.status_code})")

        parser = _CardParser()
        parser.feed(r.text)
        cards = parser.cards

        jobs, skipped = [], 0
        for card in cards:
            try:
                jobs.append(self._to_raw_job(card, country))
            except (ValueError, KeyError, TypeError) as exc:
                skipped += 1
                log.debug("card scartata (%s): %s", card.get("id"), exc)
        if skipped:
            log.warning("%s: %d card non normalizzabili su %d",
                        self.source, skipped, len(cards))

        # Pagina corta o vuota = fine dei risultati.
        complete = len(cards) < PAGE_SIZE
        return FetchResult(
            jobs=jobs, complete=complete,
            requests_made=len(self.attempts) - first_attempt,
            credits_used=0, total_available=None, skipped=skipped)

    def _dettaglio(self, job_id: str) -> _DettaglioParser | None:
        """Descrizione e criteri dell'annuncio. Un dettaglio fallito non
        uccide la card: resta l'offerta senza descrizione."""
        self._piano()
        try:
            r = self.request("GET", DETAIL_URL + job_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: dettaglio %s non letto: %s", self.source, job_id, exc)
            return None
        if r.status_code != 200:
            log.warning("%s: dettaglio %s risposto %d", self.source, job_id,
                        r.status_code)
            return None
        p = _DettaglioParser()
        p.feed(r.text)
        return p

    def _to_raw_job(self, card: dict, country: str) -> RawJob:
        job_id = card["id"]
        title = (card.get("title") or "").strip()
        if not title:
            raise ValueError("titolo assente")
        posted_raw = card.get("posted")
        if not posted_raw:
            raise ValueError("data assente")
        posted = datetime.fromisoformat(posted_raw).replace(tzinfo=timezone.utc)

        # L'URL pubblico canonico: solo il jobId, senza i parametri di
        # tracciamento che il link della card porta sempre.
        url = f"https://www.linkedin.com/jobs/view/{job_id}"

        sede = (card.get("location") or "").strip()
        citta = sede.split(",")[0].strip() if sede else ""

        dettaglio = self._dettaglio(job_id) if self.con_dettaglio else None
        descrizione = dettaglio.testo() if dettaglio else ""
        criteri = dettaglio.criteri if dettaglio else {}

        seniorita = SENIORITY_MAP.get(
            (criteri.get("Seniority level") or "").lower())
        contratto = EMPLOYMENT_MAP.get(
            (criteri.get("Employment type") or "").lower())

        return RawJob(
            source=self.source,
            source_job_id=job_id,
            url=url,
            canonical_url=canonicalize(url),
            link_kind="job_board",   # LinkedIn e' in JOB_BOARD_DOMAINS
            title=title,
            title_normalized=normalize_title(title),
            organization=(card.get("company") or "").strip() or None,
            date_posted=posted,
            date_posted_declared=True,
            domain_derived=registrable_domain(url),
            org_linkedin_slug=card.get("org_slug"),
            cities=[citta] if citta else [],
            countries=[country],
            ai_experience_level=seniorita,
            ai_employment_type=contratto,
            # La descrizione integrale sta nel raw; il riassunto per il
            # modello prende il suo tratto iniziale, dove stanno i requisiti.
            ai_requirements_summary=descrizione[:2000] or None,
            # Lingua, visto, remote: LinkedIn guest non li espone in forma
            # affidabile — NULL, che per regola del funnel non esclude mai.
            raw={
                "href_card": card.get("href", ""),
                "sede": sede,
                "criteri": criteri,
                "descrizione": descrizione,
            },
        )
