"""France Travail — API Offres d'emploi v2.

API pubblica ufficiale, con registrazione su francetravail.io.
Autenticazione OAuth2 client_credentials.

NON ANCORA VERIFICATA SUL CAMPO: mancano le credenziali. I dettagli qui sotto
vengono dalla documentazione, non da una risposta reale. Il primo `probe` con
credenziali valide è ciò che li conferma — o li smentisce.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from nivult.ingestion.base import HttpSource
from nivult.ingestion.models import FetchResult, RawJob
from nivult.ingestion.urls import canonicalize, classify_link, normalize_title, registrable_domain

log = logging.getLogger("nivult.ingestion.france_travail")

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
BASE_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2"
SCOPE = "api_offresdemploiv2 o2dsoffre"

PAGE_SIZE = 150          # massimo dichiarato per pagina
MAX_RESULTS = 3149       # tetto duro dell'API su una singola ricerca

# Mappature a regole, non LLM. Dove non c'è un equivalente si lascia None:
# meglio un campo vuoto che un valore inventato su cui poi filtriamo.
#
# experienceExige vale 'D' (débutant accepté), 'S' (souhaitée), 'E' (exigée) —
# NON '1'/'2'/'3' come avevo supposto. Ma il campo utile è un altro:
# experienceLibelle porta gli anni veri ("2 An(s)", "7 An(s)"), che si mappano
# sulla nostra scala molto meglio di un codice a tre valori.
EMPLOYMENT_MAP = {
    "CDI": "full-time",
    "CDD": "contract",
    "CCE": "contract",   # CDI de chantier ou d'opération
    "MIS": "contract",   # mission d'intérim
    "SAI": "contract",   # saisonnier
    "LIB": "contract",   # profession libérale
}
WORKING_HOURS_MAP = {"Temps plein": "full-time", "Temps partiel": "part-time"}

_MONTHS_PER_UNIT = {"an": 12, "ans": 12, "mois": 1}


def experience_level(offer: dict) -> str | None:
    """Livello di esperienza sulla nostra scala, o None.

    Si legge experienceLibelle, che porta la durata reale. Solo se manca si
    ripiega sul codice: 'D' significa débutant accepté, che è informazione
    sufficiente per il primo scaglione. 'S' ed 'E' dicono che serve esperienza
    ma non quanta, e da soli non bastano a scegliere uno scaglione: restano None
    invece di essere arrotondati a caso.
    """
    import re

    libelle = (offer.get("experienceLibelle") or "").strip().lower()
    m = re.match(r"(\d+)\s*(an\(s\)|ans?|mois)", libelle)
    if m:
        months = int(m.group(1)) * _MONTHS_PER_UNIT.get(
            m.group(2).replace("(s)", "s").rstrip("s") or "an", 12)
        years = months / 12
        if years < 2:
            return "0-2"
        if years < 5:
            return "2-5"
        if years < 10:
            return "5-10"
        return "10+"

    if offer.get("experienceExige") == "D" or "débutant" in libelle:
        return "0-2"
    return None


class FranceTravailClient(HttpSource):
    source = "france_travail"
    countries = frozenset({"FR"})
    credits_per_request = 0  # gratuita: il limite è il rate, non il costo

    def __init__(self, client_id: str | None = None, client_secret: str | None = None, **kw):
        super().__init__(rate_per_second=kw.pop("rate_per_second", 2.0), **kw)
        self.client_id = client_id or os.environ.get("FRANCE_TRAVAIL_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("FRANCE_TRAVAIL_CLIENT_SECRET", "")
        if not self.client_id or not self.client_secret:
            raise SystemExit(
                "Servono FRANCE_TRAVAIL_CLIENT_ID e FRANCE_TRAVAIL_CLIENT_SECRET.\n"
                "Registrazione su https://francetravail.io — vedi deploy/README.md"
            )
        self._token: str | None = None
        self._token_expires = 0.0

    def _access_token(self) -> str:
        import time
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        r = self.request(
            "POST", TOKEN_URL,
            params={"realm": "/partenaire"},
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": SCOPE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if r.status_code != 200:
            raise RuntimeError(f"token rifiutato ({r.status_code}): {r.text[:300]}")
        payload = r.json()
        self._token = payload["access_token"]
        self._token_expires = time.time() + int(payload.get("expires_in", 1200))
        return self._token

    def fetch(self, *, query: str, country: str = "FR", since: datetime | None = None,
              limit: int = PAGE_SIZE) -> FetchResult:
        if country not in self.countries:
            raise ValueError(f"{self.source} non copre {country}")

        params: dict[str, str] = {"motsCles": query, "range": f"0-{min(limit, PAGE_SIZE) - 1}"}
        if since:
            params["minCreationDate"] = since.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")

        r = self.request(
            "GET", f"{BASE_URL}/offres/search",
            params=params,
            headers={"Authorization": f"Bearer {self._access_token()}"},
        )
        # 204 = nessun risultato, 206 = risultato parziale (è la norma).
        if r.status_code == 204:
            return FetchResult(jobs=[], complete=True, requests_made=1, credits_used=0,
                               total_available=0)
        if r.status_code not in (200, 206):
            raise RuntimeError(f"ricerca fallita ({r.status_code}): {r.text[:300]}")

        payload = r.json()
        offers = payload.get("resultats", []) or []

        total = None
        content_range = r.headers.get("Content-Range", "")
        if "/" in content_range:
            tail = content_range.rsplit("/", 1)[-1]
            if tail.isdigit():
                total = int(tail)

        jobs, skipped = [], 0
        for offer in offers:
            try:
                jobs.append(self._to_raw_job(offer))
            except (ValueError, KeyError) as exc:
                skipped += 1
                log.debug("offerta scartata (%s): %s", offer.get("id"), exc)
        if skipped:
            log.info("%s: %d offerte scartate su %d", self.source, skipped, len(offers))

        # complete solo se abbiamo davvero visto tutto: serve allo sweep delle
        # scadute, che non deve mai basarsi su una fetch troncata.
        complete = total is not None and total <= len(offers)

        return FetchResult(jobs=jobs, complete=complete, requests_made=1,
                           credits_used=0, total_available=total)

    def _to_raw_job(self, o: dict) -> RawJob:
        origin = o.get("origineOffre") or {}
        url = origin.get("urlOrigine") or ""
        if not url:
            raise ValueError("urlOrigine assente")

        canonical = canonicalize(url)
        lieu = o.get("lieuTravail") or {}
        entreprise = o.get("entreprise") or {}

        return RawJob(
            source=self.source,
            source_job_id=str(o["id"]),
            url=url,
            canonical_url=canonical,
            link_kind=classify_link(canonical),
            title=o["intitule"],
            title_normalized=normalize_title(o["intitule"]),
            organization=entreprise.get("nom") or "Non comunicato",
            date_posted=datetime.fromisoformat(o["dateCreation"]),
            domain_derived=registrable_domain(canonical),
            cities=[lieu["libelle"]] if lieu.get("libelle") else [],
            countries=["FR"],
            locations=lieu or None,
            ai_job_language="fr",
            ai_experience_level=experience_level(o),
            ai_employment_type=(
                "internship" if o.get("alternance")
                else EMPLOYMENT_MAP.get(o.get("typeContrat", ""))),
            ai_working_hours=WORKING_HOURS_MAP.get(
                (o.get("dureeTravailLibelleConverti") or "").strip()),
            ai_requirements_summary=o.get("experienceLibelle"),
            ai_core_responsibilities=o.get("description"),
            ai_key_skills=[c["libelle"] for c in (o.get("competences") or [])
                           if c.get("libelle")]
                          + [q["libelle"] for q in (o.get("qualitesProfessionnelles") or [])
                             if q.get("libelle")],
            ai_keywords=[v for v in (o.get("romeLibelle"),
                                     o.get("appellationlibelle"),
                                     o.get("secteurActiviteLibelle")) if v],
            salary=o.get("salaire") or None,
            raw=o,
        )
