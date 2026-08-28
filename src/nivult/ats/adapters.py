"""Gli adapter ATS: parlano alle API JSON pubbliche delle piattaforme.

Ogni adapter scarica le offerte di UNA azienda su UNA piattaforma e le
restituisce in una forma comune. Nessuna dipendenza dal motore principale:
questo modulo vive nel database nivult_ats.

Le API sono tutte gratuite e senza chiave. Sono pagine pensate per essere
lette dal pubblico: il rate limiting va rispettato per cortesia, non per
contratto.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import unquote, urljoin
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
             "SWITZERLAND": "CH", "NORWAY": "NO", "UNITED KINGDOM": "GB",
             "UNITED STATES": "US", "USA": "US", "CANADA": "CA",
             "AUSTRALIA": "AU", "INDIA": "IN", "BRAZIL": "BR",
             "MEXICO": "MX", "JAPAN": "JP", "LUXEMBOURG": "LU",
             "CZECH REPUBLIC": "CZ", "CZECHIA": "CZ", "GREECE": "GR",
             "HUNGARY": "HU", "ROMANIA": "RO", "SLOVAKIA": "SK",
             "SLOVENIA": "SI", "ESTONIA": "EE", "LATVIA": "LV",
             "LITHUANIA": "LT", "CROATIA": "HR", "BULGARIA": "BG",
             "TURKEY": "TR", "ISRAEL": "IL", "SINGAPORE": "SG",
             "UNITED ARAB EMIRATES": "AE", "NEW ZEALAND": "NZ",
             "SOUTH AFRICA": "ZA", "ARGENTINA": "AR", "CHILE": "CL",
             "COLOMBIA": "CO", "CHINA": "CN", "SOUTH KOREA": "KR"}
    # niente first-2: 'UNITED STATES'→'UN' è un bug, meglio niente paese
    # che un paese sbagliato.
    return NAMES.get(c, None if len(c) > 2 else c)


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


class Teamtailor(BaseAdapter):
    """Teamtailor — JSON Feed standard, gratis.

    L'endpoint {slug}.teamtailor.com/jobs.json restituisce un JSON Feed 1.1
    con un campo _jobposting che è un JobPosting schema.org incorporato.
    È l'ATS più pulito che ci sia: dati strutturati, link diretto, data.
    """
    platform_id = "teamtailor"

    def jobs(self, slug: str) -> list[AtsJob]:
        r = self.client.get(f"https://{slug}.teamtailor.com/jobs.json")
        if r.status_code == 404:
            return []
        r.raise_for_status()
        out = []
        for item in r.json().get("items", []):
            jp = item.get("_jobposting", {})
            # Il jobLocation è una lista di indirizzi
            locations = jp.get("jobLocation", [])
            if isinstance(locations, dict):
                locations = [locations]
            city = country = None
            if locations:
                addr = locations[0].get("address", {})
                city = addr.get("addressLocality")
                country = _iso(addr.get("addressCountry"))
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=str(item.get("id", "")),
                title=item.get("title", ""),
                url=item.get("url", ""),
                location=city,
                country=country,
                city=city,
                posted_at=item.get("date_published") or jp.get("datePosted"),
                raw=item))
        return out


ADAPTERS["teamtailor"] = Teamtailor

class BreezyHR(BaseAdapter):
    """Breezy HR — {slug}.breezy.hr/json, formato standard con tutti i campi.

    L'endpoint restituisce una lista JSON con: id, name, url, published_date,
    location (con city e country), department, salary, company, locations.
    È dei JSON API il più completo dopo SmartRecruiters.
    """
    platform_id = "breezy"

    def jobs(self, slug: str) -> list[AtsJob]:
        r = self.client.get(f"https://{slug}.breezy.hr/json")
        if r.status_code == 404:
            return []
        r.raise_for_status()
        out = []
        for j in r.json():
            loc = j.get("location") or {}
            if isinstance(loc, str):
                loc = {"name": loc}
            locations = j.get("locations") or []
            city = loc.get("name") or (locations[0].get("name") if locations else None)
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=str(j.get("id", "")),
                title=j.get("name", ""),
                url=j.get("url", ""),
                location=city,
                city=city,
                posted_at=j.get("published_date"),
                department=(j.get("department") or {}).get("name")
                if isinstance(j.get("department"), dict) else j.get("department"),
                raw=j))
        return out


ADAPTERS["breezy"] = BreezyHR


class WeRecruit(BaseAdapter):
    """werecruit.io — dati offerta incorporati nella pagina elenco.

    La pagina elenco incorpora un oggetto JSON per ogni offerta (titolo,
    URL, città, data di pubblicazione): un'unica richiesta per azienda,
    senza aprire le singole pagine. Il JSON-LD JobPosting che stavamo
    leggendo sulle pagine offerta non c'è più (agosto 2026 resta solo
    Organization).
    """
    platform_id = "werecruit"

    @staticmethod
    def _inizio_oggetto(testo: str, idx: int) -> int:
        """L'indice della '{' che apre l'oggetto che contiene idx."""
        prof = 0
        for k in range(idx, -1, -1):
            ch = testo[k]
            if ch == '}':
                prof += 1
            elif ch == '{':
                if prof == 0:
                    return k
                prof -= 1
        return -1

    def jobs(self, slug: str, country: str = "fr") -> list[AtsJob]:
        r = self.client.get(
            f"https://careers.werecruit.io/{country}/{slug}",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
        if r.status_code != 200:
            return []

        testo = r.text
        dec = json.JSONDecoder()
        out: list[AtsJob] = []
        pos = 0
        visti: set[str] = set()
        while True:
            i = testo.find('"PublicationStartDate"', pos)
            if i == -1:
                break
            s = self._inizio_oggetto(testo, i - 1)
            if s == -1 or s < pos:
                # oggetto già consumato o non decodificabile: avanza
                pos = i + 10
                continue
            obj = None
            if s != -1:
                try:
                    decod, end = dec.raw_decode(testo[s:])
                    if isinstance(decod, dict) and "Url" in decod:
                        obj = decod
                        pos = s + end
                except json.JSONDecodeError:
                    pass
            if obj is None:
                pos = i + 10
                continue
            if obj["Url"] in visti:
                continue
            visti.add(obj["Url"])
            titolo = obj.get("TitleTranslated") or ""
            if isinstance(titolo, dict):
                titolo = next(iter(titolo.values()), "")
            data = obj.get("PublicationStartDate") or ""
            try:
                posted = datetime.fromisoformat(data) if data else None
            except ValueError:
                posted = None
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=str(obj.get("Id") or obj.get("Slug") or ""),
                title=titolo,
                url=obj["Url"],
                location=obj.get("Address_City"),
                country=_iso(obj.get("Address_State")) or country.upper(),
                city=obj.get("Address_City"),
                posted_at=posted,
                raw=obj))
        return out


ADAPTERS["werecruit"] = WeRecruit


class BambooHR(BaseAdapter):
    """bamboohr.com — la pagina carriere risponde in JSON con Accept json.

    Endpoint: {slug}.bamboohr.com/careers/list → {meta, result[]}.
    Il paese è scritto per esteso (Germany), la città in atsLocation.
    """
    platform_id = "bamboohr"

    def jobs(self, slug: str) -> list[AtsJob]:
        r = self.client.get(
            f"https://{slug}.bamboohr.com/careers/list",
            headers={"Accept": "application/json"})
        if r.status_code != 200:
            return []
        try:
            data = r.json()
        except ValueError:
            return []
        result = data.get("result") or []
        return [
            AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=str(j.get("id") or ""),
                title=j.get("jobOpeningName") or "",
                url=f"https://{slug}.bamboohr.com/careers/{j.get('id')}",
                location=(j.get("atsLocation") or {}).get("city"),
                country=_iso((j.get("atsLocation") or {}).get("country")),
                city=(j.get("atsLocation") or {}).get("city"),
                department=j.get("departmentLabel"),
                raw=j)
            for j in result if j.get("id") and j.get("jobOpeningName")
        ]


ADAPTERS["bamboohr"] = BambooHR


class Workable(BaseAdapter):
    """workable.com — widget pubblico con tutte le offerte in un colpo.

    Endpoint: apply.workable.com/api/v1/widget/accounts/{slug}?details=true
    Il campo jobs contiene tutto (titolo, shortcode, URL, città, data di
    pubblicazione); le aziende senza posizioni aperte rispondono jobs=[].
    """
    platform_id = "workable"

    def jobs(self, slug: str) -> list[AtsJob]:
        r = self.client.get(
            f"https://apply.workable.com/api/v1/widget/accounts/{slug}"
            "?details=true")
        if r.status_code != 200:
            return []
        try:
            data = r.json()
        except ValueError:
            return []
        out = []
        for j in data.get("jobs") or []:
            if not j.get("shortcode"):
                continue
            posted = j.get("published_on") or j.get("created_at")
            try:
                dt = datetime.fromisoformat(posted) if posted else None
            except ValueError:
                dt = None
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=j["shortcode"],
                title=j.get("title") or "",
                url=j.get("url") or
                f"https://apply.workable.com/j/{j['shortcode']}",
                location=j.get("city"),
                country=_iso(j.get("country")),
                city=j.get("city"),
                posted_at=dt,
                department=j.get("department"),
                raw=j))
        return out


ADAPTERS["workable"] = Workable


class Pinpoint(BaseAdapter):
    """pinpointhq.com — feed JSON pubblico per azienda.

    Endpoint: {slug}.pinpointhq.com/postings.json → {data: [...]}.
    Il paese non è dichiarato (solo città/provincia): resta NULL e lo
    recupera l'arricchimento.
    """
    platform_id = "pinpoint"

    def jobs(self, slug: str) -> list[AtsJob]:
        r = self.client.get(f"https://{slug}.pinpointhq.com/postings.json")
        if r.status_code != 200:
            return []
        try:
            data = r.json().get("data") or []
        except ValueError:
            return []
        out = []
        for j in data:
            if not j.get("id"):
                continue
            loc = j.get("location") or {}
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=str(j["id"]),
                title=j.get("title") or "",
                url=j.get("url") or
                f"https://{slug}.pinpointhq.com/en/postings/{j['id']}",
                location=loc.get("name") or loc.get("city"),
                city=loc.get("city") or loc.get("name"),
                posted_at=None,
                raw=j))
        return out


ADAPTERS["pinpoint"] = Pinpoint


class Join(BaseAdapter):
    """join.com — micrositio aziendale con __NEXT_DATA__.

    La lista offerte sta in initialState.jobs (5 per pagina, ?page=N).
    L'URL pubblica dell'offerta è join.com/companies/{slug}/{idParam}.
    """
    platform_id = "join"
    MAX_PAGINE = 20

    def jobs(self, slug: str) -> list[AtsJob]:
        out: list[AtsJob] = []
        visti: set[str] = set()
        for pagina in range(1, self.MAX_PAGINE + 1):
            r = self.client.get(
                f"https://{slug}.join.com/" + (f"?page={pagina}" if pagina > 1 else ""))
            if r.status_code != 200:
                break
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                          r.text, re.S)
            if not m:
                break
            try:
                stato = json.loads(m.group(1))["props"]["pageProps"]["initialState"]
                jobs = stato.get("jobs") or {}
                items = jobs.get("items") or []
            except (json.JSONDecodeError, KeyError):
                break
            if not items:
                break
            for j in items:
                id_param = j.get("idParam") or str(j.get("id") or "")
                if not id_param or id_param in visti:
                    continue
                visti.add(id_param)
                citta = (j.get("city") or {})
                creato = j.get("createdAt")
                try:
                    dt = datetime.fromisoformat(creato) if creato else None
                except ValueError:
                    dt = None
                out.append(AtsJob(
                    platform_id=self.platform_id, slug=slug,
                    external_id=id_param,
                    title=j.get("title") or "",
                    url=f"https://join.com/companies/{slug}/{id_param}",
                    location=citta.get("cityName"),
                    country=(j.get("country") or {}).get("iso3166"),
                    city=citta.get("cityName"),
                    posted_at=dt,
                    raw=j))
            pag = jobs.get("pagination") or {}
            if pagina >= (pag.get("pageCount") or 1):
                break
        return out


ADAPTERS["join"] = Join


class JazzHR(BaseAdapter):
    """applytojob.com (JazzHR) — offerte nell'HTML statico.

    Ogni offerta è un link /apply/{codice}/{Titolo-Con-Trattini}.
    Il titolo vero è il testo dell'ancora, non l'URL.
    """
    platform_id = "jazzhr"

    def jobs(self, slug: str) -> list[AtsJob]:
        r = self.client.get(f"https://{slug}.applytojob.com/")
        if r.status_code != 200:
            return []
        found = re.findall(
            r'<a[^>]*href="(https?://[^"]*\.applytojob\.com/apply/([A-Za-z0-9]+)/[^"]+)"[^>]*>(.*?)</a>',
            r.text, re.S)
        out: list[AtsJob] = []
        visti: set[str] = set()
        for url, code, txt in found:
            if code in visti:
                continue
            visti.add(code)
            titolo = re.sub(r'<[^>]+>', '', txt)
            titolo = re.sub(r'\s+', ' ', titolo).strip()
            if not titolo:
                continue
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=code,
                title=titolo,
                url=url,
                raw={"code": code}))
        return out


ADAPTERS["jazzhr"] = JazzHR


class Homerun(BaseAdapter):
    """homerun.co — offerte in un attributo Vue v-bind nell'HTML.

    La lista sta in <job-list v-bind="{content:{vacancies:[...]}}">
    con id, title e url già pronti. Le aziende chiuse rispondono 200
    con una pagina 404 (marcata da hrc404).
    """
    platform_id = "homerun"

    def jobs(self, slug: str) -> list[AtsJob]:
        r = self.client.get(f"https://{slug}.homerun.co/")
        if r.status_code != 200 or "hrc404" in r.text:
            return []
        m = re.search(r'<job-list[^>]*v-bind="([^"]*)"', r.text)
        if not m:
            return []
        import html as html_mod
        try:
            dati = json.loads(html_mod.unescape(m.group(1)))
            vacancies = (dati.get("content") or {}).get("vacancies") or []
        except json.JSONDecodeError:
            return []
        out = []
        for v in vacancies:
            if not v.get("id"):
                continue
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=str(v["id"]),
                title=v.get("title") or "",
                url=v.get("url") or
                f"https://{slug}.homerun.co/{v.get('id')}",
                raw=v))
        return out


ADAPTERS["homerun"] = Homerun


class Freshteam(BaseAdapter):
    """freshteam.com — listing server-rendered con data-attributes.

    Ogni card offerta è un <a href="/jobs/{token}/{titolo-slug}"> con
    data-portal-location per la città. Le pagine offerta sono JS-rendered,
    quindi il titolo si ricava dallo slug dell'URL.
    """
    platform_id = "freshteam"

    def jobs(self, slug: str) -> list[AtsJob]:
        r = self.client.get(f"https://{slug}.freshteam.com/jobs")
        if r.status_code != 200:
            return []
        # le card: href + data-portal-location nello stesso tag <a>
        cards = re.findall(
            r'<a href="(/jobs/([^/]+)/([^"]+))"[^>]*?'
            r'data-portal-title="[^"]*"'
            r'(?:[^>]*?data-portal-location="([^"]*)")?[^>]*>',
            r.text)
        out: list[AtsJob] = []
        visti: set[str] = set()
        for href, token, titolo_slug, citta in cards:
            if token in visti:
                continue
            visti.add(token)
            titolo = titolo_slug.replace("-", " ").replace("_", " ").strip()
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=token,
                title=titolo,
                url=f"https://{slug}.freshteam.com{href}",
                location=citta or None,
                city=citta or None,
                raw={"token": token, "location": citta or None}))
        return out


ADAPTERS["freshteam"] = Freshteam


class Radancy(BaseAdapter):
    """Radancy (ex-TMP) — siti carriere tipo jobs.azienda.com.

    Lo slug è il hostname del sito carriere (es. 'jobs.veolia.com').
    La search page è server-rendered con 15 offerte per pagina e
    paginazione ?p=N. Le offerte hanno URL
    /{lingua}/{sezione}/{luogo}/{titolo-slug}/{n1}/{n2}.
    I siti renderizzati client-side (ISS, Jerónimo Martins) non sono
    raggiungibili con httpx: restano al detector.
    """
    platform_id = "radancy"
    MAX_PAGINE = 40

    PERCORSI_RICERCA = [
        "/search/", "/search-jobs/",
        "/en/search/", "/en/search-jobs/",
        "/fr/search/", "/fr/search-jobs/",
    ]
    RX_OFFERTA = re.compile(
        r'href="(/(?:[a-z]{2}(?:[-_][A-Za-z]{2})?/)?'
        r'(?:job|jobs|emploi|emplois|empleo|vacancies|vacature|stellen'
        r'|offres|offerte|tarjoukset)/[^"]+/(\d+)/(\d+))"')

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        # trova il percorso di ricerca che risponde con offerte
        percorso = None
        for p in self.PERCORSI_RICERCA:
            try:
                r = self.client.get(f"{base}{p}")
                if r.status_code == 200 and self.RX_OFFERTA.search(r.text):
                    percorso = p
                    break
            except httpx.HTTPError:
                continue
        if not percorso:
            return []

        out: list[AtsJob] = []
        visti: set[str] = set()
        for pagina in range(1, self.MAX_PAGINE + 1):
            url = f"{base}{percorso}" + (f"?p={pagina}" if pagina > 1 else "")
            try:
                r = self.client.get(url)
            except httpx.HTTPError:
                break
            if r.status_code != 200:
                break
            trovate = 0
            for href, _, id_offerta in self.RX_OFFERTA.findall(r.text):
                if id_offerta in visti:
                    continue
                visti.add(id_offerta)
                trovate += 1
                segmenti = href.rstrip("/").split("/")
                # [-1]=id2 [-2]=id1 [-3]=titolo [-4]=luogo
                titolo = unquote(segmenti[-3]).replace("-", " ").strip()
                luogo = unquote(segmenti[-4]).replace("-", " ").strip() \
                    if len(segmenti) >= 5 else None
                out.append(AtsJob(
                    platform_id=self.platform_id, slug=slug,
                    external_id=id_offerta,
                    title=titolo,
                    url=urljoin(base, href),
                    location=luogo,
                    city=luogo,
                    raw={"href": href}))
            if trovate == 0:
                break
        return out


ADAPTERS["radancy"] = Radancy


class Phenom(BaseAdapter):
    """Phenom People — careers.azienda.com con l'API widgets protetta.

    L'API /widgets (POST con CSRF e token di sessione) è barricata:
    risponde 'tokenAvailable' a ogni replay fuori browser, Cloudflare
    incluso. La via libera è il sitemap: ogni sito Phenom espone
    sitemap.xml (o un indice di sitemap1.xml, sitemap2.xml, …) con
    tutte le offerte come /job/{jobId}/{titolo-slug}.
    Lo slug è il hostname del sito carriere (careers.roche.com).
    """
    platform_id = "phenom"
    MAX_SITEMAP = 12
    RX_OFFERTA = re.compile(r'/job/([A-Za-z0-9]+)/([^/]+)/?$')

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        r = self.client.get(f"{base}/sitemap.xml")
        if r.status_code != 200:
            return []
        if "sitemapindex" in r.text:
            file_sitemap = re.findall(r'<loc>([^<]+)</loc>', r.text)
            file_sitemap = file_sitemap[:self.MAX_SITEMAP]
        else:
            file_sitemap = [f"{base}/sitemap.xml"]

        out: list[AtsJob] = []
        visti: set[str] = set()
        for fs in file_sitemap:
            try:
                rs = self.client.get(fs)
            except httpx.HTTPError:
                continue
            if rs.status_code != 200:
                continue
            for loc in re.findall(r'<loc>([^<]+)</loc>', rs.text):
                m = self.RX_OFFERTA.search(loc)
                if not m or m.group(1) in visti:
                    continue
                visti.add(m.group(1))
                out.append(AtsJob(
                    platform_id=self.platform_id, slug=slug,
                    external_id=m.group(1),
                    title=unquote(m.group(2)).replace("-", " ").strip(),
                    url=loc,
                    raw={"sitemap": fs}))
        return out


ADAPTERS["phenom"] = Phenom


class SuccessFactors(BaseAdapter):
    """SAP SuccessFactors — portali /go/{nome}/{id} server-rendered.

    Lo slug è il hostname del sito carriere (careers.grunenthal.com).
    Dalla homepage si trovano i link /go/… (le bacheche), che elencano
    le offerte come /job/{luogo-titolo}/{id}/ paginando con l'offset
    come segmento di percorso: /go/{nome}/{id}/25/, /50/, …
    I feed RSS /services/rss/ esistono ma restituiscono 10 elementi.
    """
    platform_id = "successfactors"
    MAX_PAGINE = 30
    MAX_BACHECHE = 3

    def jobs(self, slug: str) -> list[AtsJob]:
        import html as html_mod
        base = f"https://{slug}"
        try:
            r = self.client.get(f"{base}/")
        except httpx.HTTPError:
            return []
        if r.status_code != 200:
            return []
        # le bacheche /go/ linkate dalla homepage (con entità HTML da
        # scodare: '/go/R&amp;D-Scientists/…'); finiscono in /{id}/ ma
        # NON sono offset: l'offset è un segmento in più
        bacheche = [html_mod.unescape(b)
                    for b in re.findall(r'href="(/go/[^"]+)"', r.text)]
        bacheche = [b for b in bacheche
                    if len(b.rstrip("/").split("/")) == 4]
        bacheche = list(dict.fromkeys(bacheche))[:self.MAX_BACHECHE]

        out: list[AtsJob] = []
        visti: set[str] = set()
        elenco = bacheche or ["/"]
        for bacheca in elenco:
            for pagina in range(self.MAX_PAGINE):
                url = f"{base}{bacheca}" + (f"{pagina * 25}/" if pagina else "")
                try:
                    rp = self.client.get(url)
                except httpx.HTTPError:
                    break
                if rp.status_code != 200:
                    break
                nuove = 0
                for href in re.findall(r'href="(/job/[^"]+/(\d+)/)"', rp.text):
                    path, id_offerta = href
                    if id_offerta in visti:
                        continue
                    visti.add(id_offerta)
                    nuove += 1
                    titolo_slug = path.rstrip("/").split("/")[-2]
                    out.append(AtsJob(
                        platform_id=self.platform_id, slug=slug,
                        external_id=id_offerta,
                        title=unquote(titolo_slug).replace("-", " ").strip(),
                        url=urljoin(base, path),
                        raw={"path": path}))
                if nuove == 0:
                    break
        return out


ADAPTERS["successfactors"] = SuccessFactors


class Jobvite(BaseAdapter):
    """Jobvite — il portale pubblico è jobs.jobvite.com/{token}/jobs/alljobs.

    Lo slug è il hostname del sito carriere dell'azienda: la homepage
    contiene il link a jobs.jobvite.com/{token}/ da cui si ricava il
    token. Il listing è server-rendered con nome e località in
    jv-job-list-name / jv-job-list-location.
    """
    platform_id = "jobvite"

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        try:
            r = self.client.get(f"{base}/")
        except httpx.HTTPError:
            return []
        m = re.search(r'jobs\.jobvite\.com/([a-z0-9-]+)/', r.text, re.I)
        if not m:
            return []
        token = m.group(1)
        try:
            rj = self.client.get(f"https://jobs.jobvite.com/{token}/jobs/alljobs")
        except httpx.HTTPError:
            return []
        if rj.status_code != 200:
            return []
        righe = re.findall(
            r'href="/%s/job/([A-Za-z0-9]+)"[^>]*>\s*'
            r'<div class="jv-job-list-name[^"]*">\s*([^<]+?)\s*</div>\s*'
            r'<div class="jv-job-list-location[^"]*">\s*([^<]*?)\s*</div>'
            % token, rj.text)
        return [
            AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=code,
                title=titolo,
                url=f"https://jobs.jobvite.com/{token}/job/{code}",
                location=luogo or None,
                city=luogo or None,
                raw={"token": token})
            for code, titolo, luogo in righe
        ]


ADAPTERS["jobvite"] = Jobvite


class OracleRecruiting(BaseAdapter):
    """Oracle Recruiting Cloud — CandidateExperience con REST pubblica.

    Ogni azienda ha host ({tenant}.fa.{region}.oraclecloud.com) e
    siteNumber (CX_1, CX_2…): entrambi compaiono nel link
    /hcmUI/CandidateExperience/…/sites/{siteNumber} che il sito
    carriere dell'azienda contiene. Lo slug è il hostname del sito
    carriere; l'API è aperta, senza token.
    """
    platform_id = "oracle"
    PER_PAGINA = 25
    MAX_PAGINE = 30

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        try:
            r = self.client.get(f"{base}/")
        except httpx.HTTPError:
            return []
        m = re.search(
            r'https://([a-z0-9.-]+\.oraclecloud\.com)/hcmUI/'
            r'CandidateExperience/[a-zA-Z-]+/sites/([A-Za-z0-9_]+)', r.text)
        if not m:
            return []
        host, sito = m.group(1), m.group(2)

        out: list[AtsJob] = []
        for pagina in range(self.MAX_PAGINE):
            offset = pagina * self.PER_PAGINA
            url = (f"https://{host}/hcmRestApi/resources/latest/"
                   f"recruitingCEJobRequisitions?onlyData=true"
                   f"&expand=requisitionList.workLocation,"
                   f"requisitionList.otherWorkLocations,"
                   f"requisitionList.secondaryLocations"
                   f"&finder=findReqs;siteNumber={sito},limit={self.PER_PAGINA},"
                   f"offset={offset},sortBy=POSTING_DATES_DESC")
            try:
                rr = self.client.get(url)
            except httpx.HTTPError:
                break
            if rr.status_code != 200:
                break
            try:
                items = rr.json().get("items") or []
            except ValueError:
                break
            rl = items[0].get("requisitionList") or [] if items else []
            if not rl:
                break
            for j in rl:
                if not j.get("Id"):
                    continue
                try:
                    dt = datetime.fromisoformat(j["PostedDate"]) \
                        if j.get("PostedDate") else None
                except ValueError:
                    dt = None
                out.append(AtsJob(
                    platform_id=self.platform_id, slug=slug,
                    external_id=str(j["Id"]),
                    title=j.get("Title") or "",
                    url=(f"https://{host}/hcmUI/CandidateExperience/en/sites/"
                         f"{sito}/job/{j['Id']}"),
                    location=j.get("PrimaryLocation"),
                    posted_at=dt,
                    department=j.get("Department"),
                    raw=j))
            if len(rl) < self.PER_PAGINA:
                break
        return out


ADAPTERS["oracle"] = OracleRecruiting


class Softgarden(BaseAdapter):
    """Softgarden — feed JSON schema.org pubblico per portale.

    Il feed vive su {azienda}.career.softgarden.de/jobs.feed.json con
    dataFeedElement[] di JobPosting (titolo, url, datePosted, id).
    I portali nuovi ({azienda}.softgarden.io) espongono lo stesso feed
    sul dominio classico: da guentner.softgarden.io si deriva
    guentner.career.softgarden.de. Lo slug è il hostname del sito
    carriere dell'azienda.
    """
    platform_id = "softgarden"

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        m = None
        # il link al portale può stare in homepage o nella pagina karriere
        for path in ("/", "/careers", "/karriere", "/jobs", "/en", "/de",
                     "/unternehmen/karriere", "/de-de/unternehmen/karriere"):
            try:
                r = self.client.get(f"{base}{path}")
            except httpx.HTTPError:
                continue
            if r.status_code != 200:
                continue
            m = re.search(
                r'https?://([a-z0-9-]+)\.(?:career\.)?softgarden\.(?:de|io)',
                r.text)
            if m:
                break
        if not m:
            return []
        feed = f"https://{m.group(1)}.career.softgarden.de/jobs.feed.json"
        try:
            rf = self.client.get(feed)
        except httpx.HTTPError:
            return []
        if rf.status_code != 200:
            return []
        try:
            dati = rf.json()
        except ValueError:
            return []
        out: list[AtsJob] = []
        for voce in dati.get("dataFeedElement") or []:
            item = voce.get("item") or {}
            ident = item.get("identifier") or {}
            if not item.get("url") or not ident.get("value"):
                continue
            try:
                dt = datetime.fromisoformat(item["datePosted"]) \
                    if item.get("datePosted") else None
            except ValueError:
                dt = None
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=str(ident["value"]),
                title=item.get("title") or "",
                url=item["url"],
                posted_at=dt,
                raw=item))
        return out


ADAPTERS["softgarden"] = Softgarden


class Eightfold(BaseAdapter):
    """Eightfold AI — API pcsx/search pubblica.

    Host tipo apply.azienda.com; il parametro domain= si legge nella
    pagina carriere (incorporato negli script della SPA). Pagina con
    start={offset}. Lo slug è il hostname del sito carriere.
    """
    platform_id = "eightfold"
    PER_PAGINA = 10
    MAX_PAGINE = 50

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        try:
            r = self.client.get(f"{base}/careers")
        except httpx.HTTPError:
            return []
        m = re.search(r'domain=([a-z0-9.-]+\.[a-z]{2,})', r.text)
        if not m:
            # ripiego: il dominio azienda dal referer
            m2 = re.search(r'"domain"\s*:\s*"([^"]+)"', r.text)
            if not m2:
                return []
            dominio = m2.group(1)
        else:
            dominio = m.group(1)

        out: list[AtsJob] = []
        visti: set[str] = set()
        for pagina in range(self.MAX_PAGINE):
            url = (f"{base}/api/pcsx/search?domain={dominio}&query=&location="
                   f"&start={pagina * self.PER_PAGINA}&")
            try:
                rr = self.client.get(url)
            except httpx.HTTPError:
                break
            if rr.status_code != 200:
                break
            try:
                dati = rr.json().get("data") or {}
                posizioni = dati.get("positions") or []
            except (ValueError, AttributeError):
                break
            if not posizioni:
                break
            for p in posizioni:
                pid = str(p.get("id") or "")
                if not pid or pid in visti:
                    continue
                visti.add(pid)
                luoghi = p.get("work_locations") or []
                out.append(AtsJob(
                    platform_id=self.platform_id, slug=slug,
                    external_id=pid,
                    title=p.get("name") or "",
                    url=f"{base}/job/{pid}",
                    location=luoghi[0] if luoghi else None,
                    raw=p))
            if len(posizioni) < self.PER_PAGINA:
                break
        return out


ADAPTERS["eightfold"] = Eightfold


class Cornerstone(BaseAdapter):
    """Cornerstone (csod.com) — API rec-job-search con token incorporato.

    Il sito carriere dell'azienda linka
    {tenant}.csod.com/ux/ats/careersite/{id}/home?c={tenant}; quella
    pagina incorpora nel JSON di configurazione il token Bearer e
    l'endpoint cloud (es. eu-cdg-hs.api.csod.com). Con entrambi la
    REST cerca le offerte, 25 per pagina. Lo slug è il hostname del
    sito carriere dell'azienda.
    """
    platform_id = "cornerstone"
    PER_PAGINA = 25
    MAX_PAGINE = 30

    PERCORSI = ("/", "/karriere", "/careers", "/jobs", "/en", "/de",
                "/unternehmen/karriere", "/en/careers")

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        portal = None
        for path in self.PERCORSI:
            try:
                r = self.client.get(f"{base}{path}")
            except httpx.HTTPError:
                continue
            if r.status_code != 200:
                continue
            m = re.search(
                r'https://([a-z0-9-]+)\.csod\.com/ux/ats/careersite/(\d+)/',
                r.text)
            if m:
                portal = m.group(1), int(m.group(2))
                break
        if not portal:
            return []
        tenant, site_id = portal

        # la pagina del portale contiene token e endpoint cloud
        try:
            rp = self.client.get(
                f"https://{tenant}.csod.com/ux/ats/careersite/{site_id}/"
                f"home?c={tenant}")
        except httpx.HTTPError:
            return []
        mt = re.search(r'"token"\s*:\s*"(eyJ[^"]+)"', rp.text)
        me = re.search(r'"cloud"\s*:\s*"(https://[^"]+)"', rp.text)
        if not mt or not me:
            return []
        token, cloud = mt.group(1), me.group(1).rstrip("/")

        out: list[AtsJob] = []
        for pagina in range(1, self.MAX_PAGINE + 1):
            body = {"careerSiteId": site_id, "careerSitePageId": site_id,
                    "pageNumber": pagina, "pageSize": self.PER_PAGINA,
                    "searchText": "", "states": [], "countryCodes": [],
                    "cities": [], "placeID": "", "radius": None,
                    "postingsWithinDays": None}
            try:
                rr = self.client.post(
                    f"{cloud}/rec-job-search/external/jobs",
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {token}",
                             "Origin": f"https://{tenant}.csod.com"},
                    json=body)
            except httpx.HTTPError:
                break
            if rr.status_code != 200:
                break
            try:
                dati = rr.json().get("data") or {}
                requisitions = dati.get("requisitions") or []
            except (ValueError, AttributeError):
                break
            if not requisitions:
                break
            for req in requisitions:
                rid = str(req.get("requisitionId") or "")
                if not rid:
                    continue
                luoghi = req.get("locations") or []
                citta = None
                if luoghi and isinstance(luoghi[0], dict):
                    citta = luoghi[0].get("city") or luoghi[0].get("name")
                out.append(AtsJob(
                    platform_id=self.platform_id, slug=slug,
                    external_id=rid,
                    title=req.get("displayJobTitle") or "",
                    url=(f"https://{tenant}.csod.com/ux/ats/careersite/"
                         f"{site_id}/job/{rid}?c={tenant}"),
                    location=citta,
                    posted_at=None,
                    raw=req))
            if len(requisitions) < self.PER_PAGINA:
                break
        return out


ADAPTERS["cornerstone"] = Cornerstone


class Avature(BaseAdapter):
    """Avature — feed RSS pubblico del portale carriere.

    Lo slug è il hostname del portale (jobs.totalenergies.com o
    {azienda}.avature.net). Il feed vive su
    /{locale}/careers/Home/feed/ e restituisce le ultime 20 offerte:
    la lista completa è sulla pagina SearchJobs (JS), il feed è il
    compromesso senza browser.
    """
    platform_id = "avature"
    LOCALI = ("en_US", "en_GB", "en", "de", "fr", "nl", "es", "it")

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        testo = None
        for loc in self.LOCALI:
            try:
                r = self.client.get(f"{base}/{loc}/careers/Home/feed/")
            except httpx.HTTPError:
                continue
            if r.status_code == 200 and "<item>" in r.text:
                testo = r.text
                break
        if not testo:
            return []
        out: list[AtsJob] = []
        visti: set[str] = set()
        for item in re.findall(r'<item>(.*?)</item>', testo, re.S):
            t = re.search(r'<title>(?:<!\[CDATA\[)?([^<\]]+)', item)
            l = re.search(r'<link>([^<]+)</link>', item)
            if not (t and l):
                continue
            m = re.search(r'/JobDetail/[^/]+/(\d+)', l.group(1))
            if not m or m.group(1) in visti:
                continue
            visti.add(m.group(1))
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=m.group(1),
                title=t.group(1).strip(),
                url=l.group(1).strip(),
                raw={}))
        return out


ADAPTERS["avature"] = Avature
