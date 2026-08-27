"""Gli adapter ATS: parlano alle API JSON pubbliche delle piattaforme.

Ogni adapter scarica le offerte di UNA azienda su UNA piattaforma e le
restituisce in una forma comune. Nessuna dipendenza dal motore principale:
questo modulo vive nel database nivult_ats.

Le API sono tutte gratuite e senza chiave. Sono pagine pensate per essere
lette dal pubblico: il rate limiting va rispettato per cortesia, non per
contratto.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

log = logging.getLogger("nivult.ats")

# Rate per piattaforma: chiamate al secondo. Conservative per cortesia.
RATE_PER_SECOND = {
    "greenhouse": 2.0,
    "smartrecruiters": 1.5,
    "lever": 2.0,
    "recruitee": 2.0,
    "ashby": 1.0,
}


@dataclass(slots=True)
class AtsJob:
    """Un'offerta così come viene dall'ATS, prima di ogni arricchimento."""
    platform_id: str
    slug: str
    external_id: str
    title: str
    url: str
    location: str | None = None
    country: str | None = None
    city: str | None = None
    posted_at: datetime | None = None
    department: str | None = None
    raw: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.posted_at is not None:
            self.posted_at = _dt(self.posted_at)


def _dt(v) -> datetime | None:
    """I timestamp arrivano in formati diversi: ISO, epoch (sec o msec)."""
    if not v:
        return None
    if isinstance(v, (int, float)):
        # Epoch: se è in millisecondi (Lever), dividere per 1000
        ts = float(v)
        if ts > 1e12:
            ts /= 1000
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OverflowError):
            return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _iso(country: str | None) -> str | None:
    """Alcune API danno 'fr' o 'France': normalizza in 'FR'."""
    if not country:
        return None
    c = country.strip().upper()
    if len(c) == 2 and c.isalpha():
        return c
    NAMES = {"FRANCE": "FR", "GERMANY": "DE", "ITALY": "IT", "SPAIN": "ES",
             "SWEDEN": "SE", "NETHERLANDS": "NL", "BELGIUM": "BE",
             "AUSTRIA": "AT", "DENMARK": "DK", "FINLAND": "FI",
             "PORTUGAL": "PT", "IRELAND": "IE", "POLAND": "PL",
             "SWITZERLAND": "CH", "NORWAY": "NO", "UNITED KINGDOM": "GB"}
    return NAMES.get(c, c[:2] if len(c) > 2 else None)


class BaseAdapter:
    """Il contratto: scarica le offerte di un'azienda e le ritorna."""
    platform_id: str = "?"
    base_url: str = ""

    def __init__(self):
        self.client = httpx.Client(timeout=30, follow_redirects=True,
                                   headers={"User-Agent": "nivult-ats/0.1"})

    def close(self):
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def jobs(self, slug: str) -> list[AtsJob]:
        raise NotImplementedError


class Greenhouse(BaseAdapter):
    """boards-api.greenhouse.io — JSON, gratis, link diretto (absolute_url)."""
    platform_id = "greenhouse"

    def jobs(self, slug: str) -> list[AtsJob]:
        r = self.client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        if r.status_code == 404:
            return []          # board inesistente o azienda che ha lasciato
        r.raise_for_status()
        return [
            AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=str(j["id"]), title=j.get("title", ""),
                url=j.get("absolute_url", ""),
                location=(j.get("location") or {}).get("name"),
                posted_at=j.get("updated_at") or j.get("first_published"),
                raw=j)
            for j in r.json().get("jobs", [])]


class SmartRecruiters(BaseAdapter):
    """api.smartrecruiters.com — JSON, gratis, con filtro per paese."""
    platform_id = "smartrecruiters"

    def jobs(self, slug: str, country: str | None = None) -> list[AtsJob]:
        url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100"
        if country:
            url += f"&country={country.lower()}"
        r = self.client.get(url)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        out = []
        for j in r.json().get("content", []):
            loc = j.get("location") or {}
            # Il campo country dà l'ISO minuscolo ('fr'), city la città.
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=j["id"], title=j.get("name", ""),
                url=f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                location=loc.get("city", ""),
                country=_iso(loc.get("country")),
                city=loc.get("city"),
                posted_at=j.get("releasedDate"),
                department=(j.get("department") or {}).get("label"),
                raw=j))
        return out


class Lever(BaseAdapter):
    """api.lever.co — JSON, gratis, link diretto (hostedUrl)."""
    platform_id = "lever"

    def jobs(self, slug: str) -> list[AtsJob]:
        r = self.client.get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        if r.status_code == 404:
            return []
        r.raise_for_status()
        out = []
        for j in r.json():
            cat = j.get("categories") or {}
            country = None
            city = cat.get("location") or cat.get("workplaceType")
            # Lever non dà il paese in modo affidabile: si deduce dalla città
            # o si lascia al classificatore.
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=j["id"], title=j.get("text", ""),
                url=j.get("hostedUrl", ""),
                location=cat.get("location"),
                city=city,
                posted_at=j.get("createdAt"),
                department=cat.get("team"),
                raw=j))
        return out


class Recruitee(BaseAdapter):
    """{slug}.recruitee.com/api/offers — JSON, gratis, diffuso in Europa."""
    platform_id = "recruitee"

    def jobs(self, slug: str) -> list[AtsJob]:
        r = self.client.get(f"https://{slug}.recruitee.com/api/offers/")
        if r.status_code == 404:
            return []
        r.raise_for_status()
        out = []
        for j in r.json().get("offers", []):
            # Recruitee dà la location come stringa "City, Region, Country"
            loc = j.get("location") or ""
            pezzi = [p.strip() for p in loc.split(",")] if loc else []
            city = pezzi[0] if pezzi else None
            country = _iso(pezzi[-1]) if len(pezzi) >= 2 else None
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=str(j["id"]), title=j.get("title", ""),
                url=j.get("careers_url", ""),
                location=loc, city=city, country=country,
                posted_at=j.get("published_at"),
                department=j.get("department"),
                raw=j))
        return out


class Ashby(BaseAdapter):
    """api.ashbyhq.com — JSON, gratis, usato da startup europee."""
    platform_id = "ashby"

    def jobs(self, slug: str) -> list[AtsJob]:
        r = self.client.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
            f"?include=location,department")
        if r.status_code == 404:
            return []
        r.raise_for_status()
        out = []
        for j in r.json().get("jobs", []):
            # La location è spesso "City, Country" o "Remote - Country"
            loc = j.get("location") or ""
            pezzi = [p.strip() for p in loc.split(",")] if loc else []
            city = pezzi[0] if pezzi else None
            country = _iso(pezzi[-1]) if len(pezzi) >= 2 else None
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=j["id"], title=j.get("title", ""),
                url=j.get("jobUrl", ""),
                location=loc, city=city, country=country,
                posted_at=j.get("publishedAt") or j.get("updatedAt"),
                department=(j.get("department") or {}).get("name")
                if isinstance(j.get("department"), dict) else j.get("department"),
                raw=j))
        return out


# Il registro: l'runner cerca l'adapter per platform_id qui.
ADAPTERS: dict[str, type[BaseAdapter]] = {
    "greenhouse": Greenhouse,
    "smartrecruiters": SmartRecruiters,
    "lever": Lever,
    "recruitee": Recruitee,
    "ashby": Ashby,
}


class Workday(BaseAdapter):
    """Workday — POST JSON per tenant, il pattern varia per azienda.

    Ogni azienda ha tre pezzi che cambiano: il tenant (abb, cc, eiffage),
    il server (wd3, wd103, wd5) e l'istanza (Eiffage_Careers, Babilou).
    Tutti e tre stanno nelle colonne wd_server e wd_instance di ats_companies.
    """
    platform_id = "workday"

    # Workday rifiuta limit > 20: paginazione obbligatoria.
    LIMITE_PAGINA = 20
    MAX_PAGINE = 10    # 200 offerte per azienda: il taglio è voluto

    def jobs(self, slug: str, wd_server: str | None = None,
             wd_instance: str | None = None) -> list[AtsJob]:
        if not wd_server or not wd_instance:
            return []
        base = f"https://{slug}.{wd_server}.myworkdayjobs.com/{wd_instance}"
        url = (f"https://{slug}.{wd_server}.myworkdayjobs.com"
               f"/wday/cxs/{slug}/{wd_instance}/jobs")
        out = []
        for pagina in range(self.MAX_PAGINE):
            r = self.client.post(url, json={
                "appliedFacets": {}, "limit": self.LIMITE_PAGINA,
                "offset": pagina * self.LIMITE_PAGINA})
            if r.status_code != 200:
                break
            postings = r.json().get("jobPostings", [])
            if not postings:
                break
            for j in postings:
                path = j.get("externalPath", "")
                loc = j.get("locationsText") or ""
                pezzi = [p.strip() for p in loc.split(",")] if loc else []
                country = _iso(pezzi[-1]) if len(pezzi) >= 2 else None
                out.append(AtsJob(
                    platform_id=self.platform_id, slug=slug,
                    external_id=(j.get("bulletFields") or ["unknown"])[0],
                    title=j.get("title", ""),
                    url=f"{base}{path}",
                    location=loc,
                    country=country,
                    city=pezzi[0] if pezzi else None,
                    raw=j))
        return out


ADAPTERS["workday"] = Workday
