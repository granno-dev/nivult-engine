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
