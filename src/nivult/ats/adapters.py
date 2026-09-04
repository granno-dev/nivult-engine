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


def senza_nulli(obj):
    """Via i byte NULL (\\x00): Postgres li rifiuta sia in text che in
    jsonb, e ogni tanto un'offerta ne porta uno nel testo (copia-incolla
    sporchi, encoding rotti). Senza questa pulizia un solo carattere
    guasto fa fallire l'inserimento e blocca lo scrape dell'azienda.
    Ricorsiva su dict e liste; lascia intatto tutto il resto."""
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if isinstance(obj, dict):
        return {k: senza_nulli(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [senza_nulli(v) for v in obj]
    return obj


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
        # Sanifica i byte NULL prima che l'offerta arrivi al database.
        self.title = senza_nulli(self.title)
        self.url = senza_nulli(self.url)
        self.location = senza_nulli(self.location)
        self.city = senza_nulli(self.city)
        self.department = senza_nulli(self.department)
        self.external_id = senza_nulli(self.external_id)
        self.raw = senza_nulli(self.raw)


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
                # alcuni posting hanno solo bulletFields (l'ID) e niente
                # titolo: righe vuote, si scartano — verificate sul giro reale
                if not j.get("title"):
                    continue
                path = j.get("externalPath", "")
                loc = j.get("locationsText") or ""
                pezzi = [p.strip() for p in loc.split(",")] if loc else []
                country = _iso(pezzi[-1]) if len(pezzi) >= 2 else None
                out.append(AtsJob(
                    platform_id=self.platform_id, slug=slug,
                    external_id=(j.get("bulletFields") or ["unknown"])[0],
                    title=j["title"],
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


class Personio(BaseAdapter):
    """{slug}.jobs.personio.com/xml — feed XML pubblico, gratis.

    Personio e' fra gli ATS piu' diffusi in area tedesca: ogni azienda
    espone un feed XML con tutte le posizioni aperte (id, ufficio,
    reparto, titolo, data). L'ufficio e' una citta' («Hamburg»,
    «Berlin»): il paese lo scioglie poi il geocoder. Il feed vive su
    .com o .de a seconda dell'azienda; si prova .com e si ripiega .de.
    """
    platform_id = "personio"

    def jobs(self, slug: str) -> list[AtsJob]:
        import xml.etree.ElementTree as ET
        contenuto = None
        for tld in ("com", "de"):
            try:
                r = self.client.get(f"https://{slug}.jobs.personio.{tld}/xml")
            except httpx.HTTPError:
                continue
            if r.status_code == 200 and b"<position>" in r.content:
                contenuto = r.content
                break
        if contenuto is None:
            return []
        try:
            root = ET.fromstring(contenuto)
        except ET.ParseError:
            return []
        out = []
        for pos in root.iter("position"):
            pid = (pos.findtext("id") or "").strip()
            if not pid:
                continue
            uffici = [o.text.strip() for o in pos.iter("office")
                      if o.text and o.text.strip()]
            ufficio = uffici[0] if uffici else None
            dept = (pos.findtext("department") or "").strip() or None
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=pid,
                title=(pos.findtext("name") or "").strip(),
                url=f"https://{slug}.jobs.personio.com/job/{pid}",
                location=ufficio, city=ufficio,
                posted_at=pos.findtext("createdAt"),
                department=dept,
                raw={"id": pid, "office": ufficio, "offices": uffici,
                     "department": dept, "employmentType":
                     pos.findtext("employmentType"),
                     "recruitingCategory": pos.findtext("recruitingCategory"),
                     "createdAt": pos.findtext("createdAt")}))
        return out


ADAPTERS["personio"] = Personio


class Recruiterbox(BaseAdapter):
    """{slug}.hire.trakstar.com/jobfeeds/{slug} — RSS di sindacazione.

    Recruiterbox e' diventato Trakstar Hire: la pagina carriere e' una
    SPA che carica le offerte via Firebase (irreplicabile con una
    semplice richiesta), ma il feed RSS pensato per i job board resta
    pubblico e stabile — e porta citta', stato e paese gia' separati nel
    namespace `job:`. Gli account migrati altrove rispondono con un feed
    senza `<item>`: si ritorna vuoto.
    """
    platform_id = "recruiterbox"
    _NS = {"job": "https://recruiterbox.com/rss/job/"}

    def jobs(self, slug: str) -> list[AtsJob]:
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime
        try:
            r = self.client.get(
                f"https://{slug}.hire.trakstar.com/jobfeeds/{slug}")
        except httpx.HTTPError:
            return []
        if r.status_code != 200 or b"<item>" not in r.content:
            return []
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            return []
        out = []
        for it in root.iter("item"):
            link = (it.findtext("link") or "").strip()
            guid = (it.findtext("guid") or link).strip()
            ext = guid.rstrip("/").rsplit("/", 1)[-1] or guid
            if not ext:
                continue
            city = (it.findtext("job:locationCity", namespaces=self._NS)
                    or "").strip() or None
            state = (it.findtext("job:locationState", namespaces=self._NS)
                     or "").strip() or None
            paese = (it.findtext("job:locationCountry", namespaces=self._NS)
                     or "").strip() or None
            loc = ", ".join(p for p in (city, state, paese) if p) or None
            posted = it.findtext("pubDate")
            try:
                dt = parsedate_to_datetime(posted) if posted else None
            except (TypeError, ValueError):
                dt = None
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=ext,
                title=(it.findtext("title") or "").strip(),
                url=link, location=loc, city=city, country=_iso(paese),
                posted_at=dt,
                department=(it.findtext("job:team", namespaces=self._NS)
                            or "").strip() or None,
                raw={"positionType": it.findtext(
                    "job:positionType", namespaces=self._NS),
                    "state": state, "country": paese}))
        return out


ADAPTERS["recruiterbox"] = Recruiterbox


class Icims(BaseAdapter):
    """{slug}.icims.com — la vista MOBILE della ricerca e' server-rendered.

    Il portale desktop iCIMS carica le offerte via iframe/JS annidati,
    illeggibile con una richiesta sola. La stessa ricerca con
    `mobile=true` invece stampa le offerte nell'HTML: una lista di
    `iCIMS_JobCardItem` con titolo, URL (che contiene l'id) e, quando il
    portale li mostra, i campi di intestazione (requisition, sede). Si
    pagina con `pr` finche' non arrivano piu' card nuove.
    """
    platform_id = "icims"
    _CARD = re.compile(r'<li class="iCIMS_JobCardItem">(.*?)</li>', re.S)
    _LINK = re.compile(
        r'href="(https://[^"]+/jobs/(\d+)/[^"]+/job)"[^>]*?title="([^"]*)"',
        re.S)
    _H3 = re.compile(r"<h3[^>]*>(.*?)</h3>", re.S)
    _FIELD = re.compile(
        r'iCIMS_JobHeaderField"[^>]*>(?:<[^>]+>)?\s*([^<]+?)\s*</dt>\s*'
        r'<dd[^>]*>(?:<[^>]*>)*\s*([^<]+)', re.S)
    # i portali che server-rendono mostrano la sede nella card, come
    # «field-label">Job Locations</span><span>US-KS-Wichita</span>»
    _LOC = re.compile(
        r'field-label">Job Locations?</span>\s*<span[^>]*>\s*'
        r'([^<]+?)\s*</span>', re.S)
    _UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
           "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148")

    def jobs(self, slug: str) -> list[AtsJob]:
        out: list[AtsJob] = []
        visti: set[str] = set()
        for page in range(0, 40):
            url = (f"https://{slug}.icims.com/jobs/search?pr={page}"
                   "&mobile=true&needsRedirect=false")
            try:
                r = self.client.get(url, headers={"User-Agent": self._UA})
            except httpx.HTTPError:
                break
            if r.status_code != 200:
                break
            cards = self._CARD.findall(r.text)
            if not cards:
                break
            nuovi = 0
            for card in cards:
                lm = self._LINK.search(card)
                if not lm:
                    continue
                jurl, jid, titleattr = lm.group(1), lm.group(2), lm.group(3)
                if jid in visti:
                    continue
                visti.add(jid)
                nuovi += 1
                import html as _html
                hm = self._H3.search(card)
                titolo = (re.sub(r"<[^>]+>", " ", hm.group(1)).strip()
                          if hm else "")
                if not titolo:
                    # il title dell'anchor e' «REQ - Titolo»: tieni il dopo
                    titolo = titleattr.split(" - ", 1)[-1].strip()
                titolo = _html.unescape(re.sub(r"\s+", " ", titolo)).strip()
                campi = {f.strip(): d.strip()
                         for f, d in self._FIELD.findall(card)}
                lm2 = self._LOC.search(card)
                loc = (lm2.group(1).strip() if lm2 else
                       next((v for k, v in campi.items()
                             if "location" in k.lower()), None))
                # la sede e' spesso «CC-Stato-Citta» (US-KS-Wichita,
                # IN-Remote): il primo pezzo e' il codice paese, l'ultimo
                # la citta'
                citta = paese = None
                if loc:
                    m3 = re.match(r"^([A-Z]{2})-(.+)$", loc)
                    if m3:
                        paese = _iso(m3.group(1))
                        citta = m3.group(2).split("-")[-1].strip() or None
                    else:
                        citta = loc
                out.append(AtsJob(
                    platform_id=self.platform_id, slug=slug,
                    external_id=jid, title=titolo, url=jurl,
                    location=loc, city=citta, country=paese, raw=campi))
            if nuovi == 0:
                break
        return out


ADAPTERS["icims"] = Icims


class ZohoRecruit(BaseAdapter):
    """{slug}.zohorecruit.{dc}/jobs/Careers — le offerte sono nel blob.

    La pagina carriere di Zoho e' renderizzata dal framework Lyte, ma le
    offerte NON arrivano da una chiamata separata: stanno gia' dentro
    l'HTML, in un attributo `value` (con le virgolette HTML-escaped)
    dell'elemento `id="jobs"` — un array JSON con titolo, citta', paese,
    tipo. L'account vive in un data center preciso (eu/com/in): si prova
    finche' la pagina non contiene l'elemento `id="jobs"`.
    """
    platform_id = "zohorecruit"
    _DC = ("eu", "com", "in")
    _BLOB = re.compile(r'value="([^"]*Posting_Title[^"]*)"', re.S)

    def jobs(self, slug: str) -> list[AtsJob]:
        import html as _html
        pagina = dc = None
        for tld in self._DC:
            try:
                r = self.client.get(
                    f"https://{slug}.zohorecruit.{tld}/jobs/Careers")
            except httpx.HTTPError:
                continue
            if r.status_code == 200 and 'id="jobs"' in r.text:
                pagina, dc = r.text, tld
                break
        if pagina is None:
            return []
        m = self._BLOB.search(pagina)
        if not m:
            return []          # data center giusto ma nessuna offerta
        try:
            data = json.loads(_html.unescape(m.group(1)))
        except ValueError:
            return []
        out = []
        for j in data:
            if j.get("Publish") is False:
                continue
            jid = str(j.get("id") or "").strip()
            if not jid:
                continue
            citta = (j.get("City") or "").strip() or None
            paese = (j.get("Country") or "").strip() or None
            loc = ", ".join(p for p in (citta, paese) if p) or None
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=jid,
                title=(j.get("Posting_Title")
                       or j.get("Job_Opening_Name") or "").strip(),
                url=f"https://{slug}.zohorecruit.{dc}/jobs/Careers/{jid}",
                location=loc, city=citta, country=_iso(paese),
                department=(j.get("Department_Name") or "").strip() or None,
                raw=j))
        return out


ADAPTERS["zohorecruit"] = ZohoRecruit


class HireHive(BaseAdapter):
    """{slug}.hirehive.com/api/v1/jobs — JSON pubblico, link diretto.

    Feed aperto: ogni offerta porta titolo, localita', paese (oggetto
    con `code` ISO2), data e `hostedUrl` (il link diretto all'annuncio).
    """
    platform_id = "hirehive"

    def jobs(self, slug: str) -> list[AtsJob]:
        try:
            r = self.client.get(f"https://{slug}.hirehive.com/api/v1/jobs")
        except httpx.HTTPError:
            return []
        if r.status_code != 200:
            return []
        try:
            d = r.json()
        except ValueError:
            return []
        jobs = (d.get("jobs") if isinstance(d, dict)
                else d if isinstance(d, list) else []) or []
        out = []
        for j in jobs:
            jid = str(j.get("id") or "").strip()
            if not jid:
                continue
            paese = j.get("country") or {}
            cc = paese.get("code") if isinstance(paese, dict) else None
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=jid,
                title=j.get("title") or "",
                url=j.get("hostedUrl") or f"https://{slug}.hirehive.com/",
                location=j.get("location"),
                city=j.get("location"),
                country=_iso(cc),
                posted_at=j.get("publishedDate") or j.get("createdDate"),
                department=j.get("category"),
                raw={k: j.get(k) for k in
                     ("id", "location", "stateCode", "type", "category",
                      "experience", "language")}))
        return out


ADAPTERS["hirehive"] = HireHive


class JobScore(BaseAdapter):
    """careers.jobscore.com/careers/{slug}/feed — JSON pubblico, link diretto.

    Feed ricco: titolo, citta'/stato/paese separati, `detail_url` diretto
    all'annuncio, data, tipo, salario. Lo slug e' il codice azienda.
    """
    platform_id = "jobscore"

    def jobs(self, slug: str) -> list[AtsJob]:
        try:
            r = self.client.get(
                f"https://careers.jobscore.com/careers/{slug}/feed")
        except httpx.HTTPError:
            return []
        if r.status_code != 200:
            return []
        try:
            d = r.json()
        except ValueError:
            return []
        jobs = d if isinstance(d, list) else (d.get("jobs") or [])
        out = []
        for j in jobs:
            jid = str(j.get("id") or "").strip()
            if not jid:
                continue
            citta = (j.get("city") or "").strip() or None
            paese = (j.get("country") or "").strip() or None
            loc = ((j.get("location") or "").strip()
                   or ", ".join(p for p in (citta, j.get("state"), paese)
                                if p) or None)
            url = (j.get("detail_url") or j.get("share_url")
                   or j.get("apply_url") or "")
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=jid,
                title=j.get("title") or "",
                url=url.split("?")[0] if url else "",
                location=loc, city=citta, country=_iso(paese),
                posted_at=j.get("opened_date") or j.get("created_on"),
                department=j.get("department"),
                raw={k: j.get(k) for k in
                     ("id", "location", "city", "state", "country",
                      "job_type", "remote", "experience_level")}))
        return out


ADAPTERS["jobscore"] = JobScore


class CatsOne(BaseAdapter):
    """{slug}.catsone.com/careers/ — pagina server-rendered, link diretto.

    La bacheca e' una tabella: ogni riga e' un `<a class="table-row">` con
    l'URL (`/careers/{cid}/jobs/{jid}-{titolo}`), il titolo, la categoria
    e la sede in celle `data-label`. Tutto nell'HTML, nessuna API.
    """
    platform_id = "catsone"
    _UA = "Mozilla/5.0 (compatible; nivult-ats/1.0)"
    _ROW = re.compile(
        r'<a class="table-row" href="(/careers/\d+/jobs/(\d+)-[^"]+)".*?</a>',
        re.S)
    _TITLE = re.compile(r'title-cell">([^<]*)</div>')
    _LOC = re.compile(r'data-label="Location">([^<]*)</div>')
    _CAT = re.compile(r'data-label="Category">([^<]*)</div>')

    def jobs(self, slug: str) -> list[AtsJob]:
        import html as _html
        try:
            r = self.client.get(f"https://{slug}.catsone.com/careers/",
                                 headers={"User-Agent": self._UA})
        except httpx.HTTPError:
            return []
        if r.status_code != 200:
            return []
        out: list[AtsJob] = []
        visti: set[str] = set()
        for m in self._ROW.finditer(r.text):
            blocco, path, jid = m.group(0), m.group(1), m.group(2)
            if jid in visti:
                continue
            visti.add(jid)
            tm, lm, cm = (self._TITLE.search(blocco), self._LOC.search(blocco),
                          self._CAT.search(blocco))
            loc = (lm.group(1).strip() if lm else "") or None
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug, external_id=jid,
                title=_html.unescape(tm.group(1).strip() if tm else ""),
                url=f"https://{slug}.catsone.com{path}",
                location=loc, city=loc,
                department=(cm.group(1).strip() if cm else None) or None,
                raw={"path": path}))
        return out


ADAPTERS["catsone"] = CatsOne


class Crelate(BaseAdapter):
    """jobs.crelate.com/portal/{slug} — SPA, ma l'API pubblica e' aperta.

    Il portale (staffing) e' una SPA che chiama
    `/api/candidateportal/GetAllJobs?requestEnvelope={OrganizationId:...}`.
    L'OrganizationId (un GUID) sta nel guscio HTML del portale: lo si
    estrae e si chiama l'API. Risposta ricca: City/State/Country, Url,
    data.
    """
    platform_id = "crelate"
    _UA = "Mozilla/5.0 (compatible; nivult-ats/1.0)"
    _GUID = re.compile(
        r'OrganizationId["\s:]+([0-9a-f-]{36})|'
        r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
        re.I)

    def jobs(self, slug: str) -> list[AtsJob]:
        import json as _json
        from urllib.parse import quote
        try:
            shell = self.client.get(f"https://jobs.crelate.com/portal/{slug}",
                                    headers={"User-Agent": self._UA})
        except httpx.HTTPError:
            return []
        m = self._GUID.search(shell.text)
        if not m:
            return []
        guid = m.group(1) or m.group(2)
        env = quote(_json.dumps({"Locations": None, "OrganizationId": guid,
                                 "SearchText": None, "Tags": None}))
        try:
            r = self.client.get(
                "https://jobs.crelate.com/api/candidateportal/GetAllJobs"
                f"?requestEnvelope={env}", headers={"User-Agent": self._UA})
            d = r.json()
        except (httpx.HTTPError, ValueError):
            return []
        out = []
        for j in d.get("Jobs") or []:
            jid = str(j.get("Id") or "").strip()
            if not jid:
                continue
            city = (j.get("City") or "").strip() or None
            state = (j.get("State") or "").strip() or None
            paese = (j.get("Country") or "").strip() or None
            loc = ", ".join(p for p in (city, state, paese) if p) or None
            url = j.get("Url") or ""
            if url and not url.startswith("http"):
                url = f"https://jobs.crelate.com/portal/{slug}/job/{jid}"
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug, external_id=jid,
                title=j.get("Title") or "",
                url=url or f"https://jobs.crelate.com/portal/{slug}/job/{jid}",
                location=loc, city=city, country=_iso(paese),
                posted_at=j.get("LastPostedOnDate") or j.get("LastPostedOn"),
                raw={k: j.get(k) for k in
                     ("Id", "JobCode", "City", "State", "Country",
                      "PostalCode")}))
        return out


ADAPTERS["crelate"] = Crelate


class HiringThing(BaseAdapter):
    """{slug}.hiringthing.com — pagina server-rendered, link diretto.

    Ogni offerta: `<a href="/job/{id}/{titolo}"><h2>Titolo</h2></a>` con la
    sede in `<div class="job-location">`. Tutto nell'HTML.
    """
    platform_id = "hiringthing"
    _UA = "Mozilla/5.0 (compatible; nivult-ats/1.0)"
    _JOB = re.compile(
        r'href="(/job/(\d+)/[^"]+)"[^>]*>\s*<h2>(.*?)</h2>'
        r'(?:(?!href="/job/).)*?job-location">\s*(.*?)\s*</div>', re.S)

    def jobs(self, slug: str) -> list[AtsJob]:
        import html as _html
        try:
            r = self.client.get(f"https://{slug}.hiringthing.com/",
                                 headers={"User-Agent": self._UA})
        except httpx.HTTPError:
            return []
        if r.status_code != 200:
            return []
        out, visti = [], set()
        for path, jid, titolo, loc in self._JOB.findall(r.text):
            if jid in visti:
                continue
            visti.add(jid)
            titolo = _html.unescape(re.sub(r"<[^>]+>", " ", titolo)).strip()
            sede = _html.unescape(re.sub(r"<[^>]+>", " ", loc)).strip() or None
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug, external_id=jid,
                title=titolo, url=f"https://{slug}.hiringthing.com{path}",
                location=sede, city=sede, raw={"path": path}))
        return out


ADAPTERS["hiringthing"] = HiringThing


class ApplicantStack(BaseAdapter):
    """{slug}.applicantstack.com/x/openings — tabella server-rendered.

    Righe: `<a href="/x/detail/{id}">Titolo</a></td><td>Sede</td>
    <td>Reparto</td><td>Salario</td>`. Link diretto.
    """
    platform_id = "applicantstack"
    _UA = "Mozilla/5.0 (compatible; nivult-ats/1.0)"

    @staticmethod
    def _testo(x: str) -> str:
        import html as _html
        return _html.unescape(re.sub(r"<[^>]+>", " ", x)).strip()

    def jobs(self, slug: str) -> list[AtsJob]:
        try:
            r = self.client.get(
                f"https://{slug}.applicantstack.com/x/openings",
                headers={"User-Agent": self._UA})
        except httpx.HTTPError:
            return []
        if r.status_code != 200:
            return []
        # le colonne cambiano per portale: si trova l'indice di «Location»
        # (e «Department») dall'intestazione della tabella
        thead = re.search(r'<tr[^>]*>(?:\s*<th.*?</th>\s*)+</tr>', r.text, re.S)
        etich = ([self._testo(t) for t in
                  re.findall(r'<th[^>]*>(.*?)</th>', thead.group(0), re.S)]
                 if thead else [])
        i_loc = next((i for i, e in enumerate(etich)
                      if "location" in e.lower()), None)
        i_dep = next((i for i, e in enumerate(etich)
                      if "department" in e.lower()), None)
        out, visti = [], set()
        for row in re.findall(r"<tr[^>]*>.*?</tr>", r.text, re.S):
            m = re.search(r'/x/detail/([a-z0-9]+)"[^>]*>(.*?)</a>', row, re.S)
            if not m:
                continue
            jid, titolo = m.group(1), self._testo(m.group(2))
            if not titolo or jid in visti:
                continue
            visti.add(jid)
            celle = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            sede = (self._testo(celle[i_loc]) if i_loc is not None
                    and i_loc < len(celle) else None) or None
            dep = (self._testo(celle[i_dep]) if i_dep is not None
                   and i_dep < len(celle) else None) or None
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug, external_id=jid,
                title=titolo,
                url=f"https://{slug}.applicantstack.com/x/detail/{jid}",
                location=sede, city=sede, department=dep, raw={"id": jid}))
        return out


ADAPTERS["applicantstack"] = ApplicantStack


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
    # Ogni offerta e' una riga di tabella: titolo (con link e codice),
    # reparto, sede. Prima si prendeva solo il titolo e si buttava la
    # colonna `resumator-job-location-column` — 37k offerte senza paese.
    _RIGA = re.compile(
        r'href="(https?://[^"]*\.applytojob\.com/apply/([A-Za-z0-9]+)/[^"]+)"'
        r'[^>]*class="resumator-job-title-link">(.*?)</a>'
        r'(?:(?!resumator-job-title-link).)*?'
        r'resumator-job-location-column">\s*([^<]*?)\s*</td>', re.S)

    def jobs(self, slug: str) -> list[AtsJob]:
        r = self.client.get(f"https://{slug}.applytojob.com/")
        if r.status_code != 200:
            return []
        out: list[AtsJob] = []
        visti: set[str] = set()
        for url, code, txt, loc in self._RIGA.findall(r.text):
            if code in visti:
                continue
            visti.add(code)
            titolo = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", txt)).strip()
            if not titolo:
                continue
            sede = re.sub(r"\s+", " ", loc).strip() or None
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=code, title=titolo, url=url,
                location=sede, city=sede,
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
    # ogni offerta: il link /job/.../id/ seguito, nel blocco telefono, da
    # reparto (jobFacility) e sede (jobLocation, «Citta, CC, CAP»)
    _RIGA = re.compile(
        r'href="(/job/[^"]+/(\d+)/)"[^>]*>.*?</a>\s*</span>'
        r'(?:\s*<span class="jobFacility[^"]*"[^>]*>(.*?)</span>)?'
        r'\s*<span class="jobLocation[^"]*"[^>]*>\s*<span[^>]*>\s*'
        r'(.*?)\s*</span>', re.S)

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
                for m in self._RIGA.finditer(rp.text):
                    path, id_offerta = m.group(1), m.group(2)
                    if id_offerta in visti:
                        continue
                    visti.add(id_offerta)
                    nuove += 1
                    reparto = re.sub(r"<[^>]+>", "",
                                     m.group(3) or "").strip() or None
                    if reparto and "no-department" in (m.group(3) or ""):
                        reparto = None
                    # via l'eventuale coda HTML «<small>+1 meer…</small>»
                    sede = re.sub(r"<[^>]+>.*$", "", m.group(4) or "")
                    sede = re.sub(r"\s+", " ", sede).strip() or None
                    # la sede e' «Citta, CC, CAP»: il codice a due lettere
                    # e' il paese
                    paese = None
                    if sede:
                        cc = next((p.strip() for p in sede.split(",")
                                   if re.fullmatch(r"[A-Z]{2}", p.strip())),
                                  None)
                        paese = _iso(cc)
                    titolo_slug = path.rstrip("/").split("/")[-2]
                    out.append(AtsJob(
                        platform_id=self.platform_id, slug=slug,
                        external_id=id_offerta,
                        title=unquote(titolo_slug).replace("-", " ").strip(),
                        url=urljoin(base, path),
                        location=sede, city=(sede.split(",")[0].strip()
                                             if sede else None),
                        country=paese, department=reparto,
                        raw={"path": path, "location": sede}))
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
            luogo = item.get("jobLocation") or {}
            if isinstance(luogo, list):
                luogo = luogo[0] if luogo else {}
            addr = (luogo.get("address") if isinstance(luogo, dict)
                    else None) or {}
            citta = (addr.get("addressLocality") or "").strip() or None
            paese = (addr.get("addressCountry") or "").strip() or None
            regione = (addr.get("addressRegion") or "").strip() or None
            locstr = ", ".join(p for p in (citta, regione, paese) if p) or None
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=str(ident["value"]),
                title=item.get("title") or "",
                url=item["url"],
                location=locstr, city=citta, country=_iso(paese),
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


class Taleo(BaseAdapter):
    """Oracle Taleo — la REST rifiuta i replay, si cattura dal browser.

    searchjobs risponde 500 a ogni replay fuori dalla sessione, anche
    via fetch dentro la pagina. L'unico canale affidabile: far girare
    la pagina e catturare le risposte di searchjobs, cliccando il
    bottone 'pagina successiva' per paginare. Lo slug è
    'host#sezione' del portale (textron.taleo.net#kautex).
    """
    platform_id = "taleo"
    MAX_CLIC = 20

    def jobs(self, slug: str) -> list[AtsJob]:
        if "#" not in slug:
            return []
        host, sezione = slug.split("#", 1)
        from playwright.sync_api import sync_playwright
        requisitions: list[dict] = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()

                def on_resp(resp):
                    if "searchjobs" not in resp.url:
                        return
                    try:
                        d = resp.json()
                        requisitions.extend(d.get("requisitionList") or [])
                    except Exception:
                        pass

                page.on("response", on_resp)
                page.goto(f"https://{host}/careersection/{sezione}/"
                          f"jobsearch.ftl", wait_until="domcontentloaded",
                          timeout=40000)
                page.wait_for_timeout(10000)
                for _ in range(self.MAX_CLIC):
                    try:
                        btn = page.locator(
                            "a[title='Go to the next page'], .pagerNext"
                        ).first
                        if not btn.is_visible(timeout=2000):
                            break
                        btn.click()
                        page.wait_for_timeout(4000)
                    except Exception:
                        break
                browser.close()
        except Exception:
            pass

        out: list[AtsJob] = []
        visti: set[str] = set()
        for req in requisitions:
            rid = str(req.get("jobId") or "")
            col = req.get("column") or []
            if not rid or rid in visti or len(col) < 3:
                continue
            visti.add(rid)
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=rid,
                title=col[0] or "",
                url=(f"https://{host}/careersection/{sezione}/"
                     f"jobdetail.ftl?job={rid}"),
                location=(col[1] or "").strip('[]"'),
                raw=req))
        return out


ADAPTERS["taleo"] = Taleo


def _renderizza_estrai(url: str, selettore: str, attesa: int = 8000
                       ) -> list[dict]:
    """Renderizza una pagina con Playwright e ritorna href+testo dei link."""
    from playwright.sync_api import sync_playwright
    risultati: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(attesa)
            risultati = page.eval_on_selector_all(
                selettore,
                """els => els.map(e => ({href: e.href,
                    text: (e.textContent || '').replace(/\\s+/g, ' ')
                            .trim().slice(0, 200)}))""")
            browser.close()
    except Exception:
        pass
    return risultati


class Comeet(BaseAdapter):
    """Comeet — pagina offerte renderizzata via JS.

    Lo slug è il hostname del sito carriere; il link
    comeet.com/jobs/{azienda}/{token} sta nella homepage.
    """
    platform_id = "comeet"

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        m = None
        for host in (base, f"https://www.{slug}"):
            for path in ("/", "/careers", "/jobs", "/en", "/nl", "/de", "/fr",
                         "/en/careers", "/company/careers", "/about-us/careers"):
                try:
                    r = self.client.get(f"{host}{path}")
                except httpx.HTTPError:
                    continue
                if r.status_code != 200:
                    continue
                m = re.search(r'https://www\.comeet\.com/jobs/([a-z0-9-]+)/([0-9.]+)',
                              r.text)
                if m:
                    break
        if not m:
            return []
        porta = f"https://www.comeet.com/jobs/{m.group(1)}/{m.group(2)}"
        link = _renderizza_estrai(porta, "a[href*='/jobs/']")
        out: list[AtsJob] = []
        visti: set[str] = set()
        for l in link:
            if not l.get("text") or l["href"].rstrip("/") == porta:
                continue
            m2 = re.search(r'/jobs/[^/]+/[^/]+/([^/]+)/([0-9A-Fa-f.]+)$',
                           l["href"])
            if not m2 or m2.group(2) in visti:
                continue
            visti.add(m2.group(2))
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=m2.group(2),
                title=l["text"],
                url=l["href"],
                raw={"porta": porta}))
        return out


ADAPTERS["comeet"] = Comeet


class ApplicantPro(BaseAdapter):
    """ApplicantPro — listing JS su {azienda}.applicantpro.com/jobs/.

    Lo slug è il hostname del sito carriere dell'azienda.
    """
    platform_id = "applicantpro"

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        m = None
        for host in (base, f"https://www.{slug}"):
            for path in ("/", "/careers", "/jobs", "/en", "/nl", "/de", "/fr",
                         "/en/careers", "/company/careers", "/about-us/careers"):
                try:
                    r = self.client.get(f"{host}{path}")
                except httpx.HTTPError:
                    continue
                if r.status_code != 200:
                    continue
                m = re.search(r'https?://([a-z0-9-]+)\.applicantpro\.com', r.text)
                if m:
                    break
        if not m:
            return []
        link = _renderizza_estrai(
            f"https://{m.group(1)}.applicantpro.com/jobs/",
            "a[href*='/jobs/']")
        out: list[AtsJob] = []
        visti: set[str] = set()
        for l in link:
            m2 = re.search(r'/jobs/(\d+)$', l.get("href", ""))
            if not m2 or m2.group(1) in visti:
                continue
            visti.add(m2.group(1))
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=m2.group(1),
                title=l.get("text") or "",
                url=l["href"],
                raw={}))
        return out


ADAPTERS["applicantpro"] = ApplicantPro


class Jobtoolz(BaseAdapter):
    """Jobtoolz — portale belga JS su {azienda}.jobtoolz.com.

    Lo slug è il hostname del sito carriere; il link al portale sta
    nella homepage dell'azienda.
    """
    platform_id = "jobtoolz"

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        m = None
        for host in (base, f"https://www.{slug}"):
            for path in ("/", "/careers", "/jobs", "/en", "/nl", "/de", "/fr",
                         "/en/careers", "/company/careers", "/about-us/careers"):
                try:
                    r = self.client.get(f"{host}{path}")
                except httpx.HTTPError:
                    continue
                if r.status_code != 200:
                    continue
                m = re.search(r'https?://([a-z0-9-]+)\.jobtoolz\.com', r.text)
                if m:
                    break
        if not m:
            return []
        link = _renderizza_estrai(
            f"https://{m.group(1)}.jobtoolz.com/nl", "a[href*='vacature']")
        out: list[AtsJob] = []
        visti: set[str] = set()
        for l in link:
            m2 = re.search(r'/(\d+)', l.get("href", ""))
            if not m2 or m2.group(1) in visti or not l.get("text"):
                continue
            visti.add(m2.group(1))
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=m2.group(1),
                title=l["text"],
                url=l["href"],
                raw={}))
        return out


ADAPTERS["jobtoolz"] = Jobtoolz


class PageUp(BaseAdapter):
    """PageUp People — pagine /careers/vacancies/ renderizzate via JS.

    Le offerte sono link ?job={id} sulla pagina vacancies, caricati
    client-side. Lo slug è il hostname del sito carriere.
    """
    platform_id = "pageup"

    PERCORSI = ("/careers/vacancies/", "/vacancies/", "/careers/vacancies",
                "/en/careers/vacancies/", "/cw/en/listing/")

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        link: list[dict] = []
        for path in self.PERCORSI:
            link = _renderizza_estrai(f"{base}{path}", "a[href*='job=']",
                                      attesa=10000)
            if link:
                break
        out: list[AtsJob] = []
        visti: set[str] = set()
        for l in link:
            m = re.search(r'[?&]job=(\d+)', l.get("href", ""))
            if not m or m.group(1) in visti:
                continue
            visti.add(m.group(1))
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=m.group(1),
                title=l.get("text") or "",
                url=l["href"],
                raw={}))
        return out


ADAPTERS["pageup"] = PageUp


class WelcomeToTheJungle(BaseAdapter):
    """Welcome to the Jungle — pagina azienda con offerte a scroll.

    Le offerte si caricano con lazy loading sulla pagina
    /{lang}/companies/{slug}/jobs: si scrolla e si raccolgono i link
    con titolo. Pochi posti per azienda (piattaforma di employer
    branding), ma le aziende sono decine. Lo slug è il hostname del
    sito carriere dell'azienda.
    """
    platform_id = "welcometothejungle"
    SCROLL = 8

    def jobs(self, slug: str) -> list[AtsJob]:
        m = None
        for host in (f"https://{slug}", f"https://www.{slug}"):
            for path in ("/", "/careers", "/jobs", "/job-offers", "/en",
                         "/fr", "/en/careers", "/about-us/careers"):
                try:
                    r = self.client.get(f"{host}{path}")
                except httpx.HTTPError:
                    continue
                if r.status_code != 200:
                    continue
                m = re.search(r'welcometothejungle\.com/([a-z]{2})/companies/'
                              r'([a-z0-9-]+)', r.text)
                if m:
                    break
            if m:
                break
        if not m:
            return []
        lingua, azienda = m.group(1), m.group(2)
        porta = (f"https://www.welcometothejungle.com/{lingua}/companies/"
                 f"{azienda}/jobs")

        from playwright.sync_api import sync_playwright
        raccolti: dict[str, str] = {}
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(porta, wait_until="domcontentloaded",
                          timeout=40000)
                page.wait_for_timeout(6000)
                for _ in range(self.SCROLL):
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(2000)
                raccolti = page.evaluate("""() => {
                    const visti = {};
                    for (const e of document.querySelectorAll('a')) {
                        const t = (e.textContent || '').replace(/\\s+/g, ' ').trim();
                        const coda = (e.href.split('/').pop() || '');
                        if (e.href.includes('/jobs/') && t.length > 5
                                && coda.includes('_')) {
                            visti[e.href] = t.slice(0, 200);
                        }
                    }
                    return visti;
                }""")
                browser.close()
        except Exception:
            pass

        out: list[AtsJob] = []
        for href, titolo in raccolti.items():
            coda = href.rstrip("/").split("/")[-1]
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=coda,
                title=titolo,
                url=href,
                raw={"porta": porta}))
        return out


ADAPTERS["welcometothejungle"] = WelcomeToTheJungle


class Varbi(BaseAdapter):
    """Varbi — portali dei comuni svedesi ({comune}.varbi.com).

    Listing server-rendered in tabella: td.pos-title con l'ancora
    titolo, td.pos-town con la città. Lo slug è il hostname del
    portale varbi.
    """
    platform_id = "varbi"

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        try:
            r = self.client.get(f"{base}/")
        except httpx.HTTPError:
            return []
        if r.status_code != 200:
            return []
        righe = re.findall(
            r'<td class="[^"]*pos-title[^"]*">\s*'
            r'<a href="https?://[^"]*what:job/jobID:(\d+)/">([^<]+)</a>'
            r'.*?<td class="[^"]*pos-town[^"]*">\s*'
            r'<a href="[^"]*">([^<]*)</a>',
            r.text, re.S)
        import html as html_mod
        return [
            AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=id_offerta,
                title=html_mod.unescape(titolo).strip(),
                url=f"{base}/se/what:job/jobID:{id_offerta}/",
                location=html_mod.unescape(citta).strip() or None,
                city=html_mod.unescape(citta).strip() or None,
                raw={})
            for id_offerta, titolo, citta in righe
        ]


ADAPTERS["varbi"] = Varbi


class Factorial(BaseAdapter):
    """Factorial HR — portale {azienda}.factorial.it renderizzato via JS.

    Le offerte sono link /job_posting/{titolo-slug}-{id}; il titolo
    si legge dallo slug. Lo slug è il hostname del portale factorial.
    """
    platform_id = "factorial"

    def jobs(self, slug: str) -> list[AtsJob]:
        link = _renderizza_estrai(
            f"https://{slug}/", "a[href*='job_posting']", attesa=10000)
        out: list[AtsJob] = []
        visti: set[str] = set()
        for l in link:
            m = re.search(r'/job_posting/([a-z0-9-]+)-(\d+)$',
                          l.get("href", "").rstrip("/"))
            if not m or m.group(2) in visti:
                continue
            visti.add(m.group(2))
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=m.group(2),
                title=m.group(1).replace("-", " ").strip(),
                url=l["href"],
                raw={}))
        return out


ADAPTERS["factorial"] = Factorial


class InRecruiting(BaseAdapter):
    """In-recruiting / Intervieweb (Zucchetti) — l'ATS delle PMI italiane.

    Il portale del tenant è un login: la bacheca pubblica NON esiste
    sul sottodominio. Le offerte escono da annunci.php, che però vuole
    la chiave di pubblicazione — quella che le aziende incorporano
    negli iframe dei propri siti. Le chiavi si scavano dagli archivi
    (Wayback conserva gli embed) e vivono in ats_companies.pub_key,
    convalidate una a una prima di entrare.

    Con la chiave, l'endpoint è un'API JSON pulita: titolo, città,
    regione, contratto, date di pubblicazione e scadenza, e l'URL del
    dettaglio — che è pubblico. Prima si renderizzava il login con un
    browser e si raccoglieva zero.
    """
    platform_id = "inrecruiting"

    MESI = {"Italia": "IT", "Francia": "FR", "Germania": "DE",
            "Spagna": "ES", "Svizzera": "CH", "Regno Unito": "GB",
            "Austria": "AT", "Belgio": "BE", "Paesi Bassi": "NL"}

    def jobs(self, slug: str, chiave: str | None = None) -> list[AtsJob]:
        if not chiave:
            return []
        r = self.client.get(
            f"https://{slug}.intervieweb.it/annunci.php"
            f"?lang=it&k={chiave}&format=json_en&utype=0")
        if r.status_code != 200 or not r.text.lstrip().startswith("["):
            return []
        out: list[AtsJob] = []
        for a in r.json():
            aid = str(a.get("id") or "").strip()
            titolo = (a.get("title") or "").strip()
            if not aid or not titolo:
                continue
            # published: "28-07-2026 (15:42)" — data italiana con l'ora
            # tra parentesi, va presa per quello che è
            dt = None
            m = re.match(r"(\d{2})-(\d{2})-(\d{4})",
                         str(a.get("published") or ""))
            if m:
                dt = datetime(int(m.group(3)), int(m.group(2)),
                              int(m.group(1)), tzinfo=timezone.utc)
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=aid, title=titolo,
                url=(a.get("url") or
                     f"https://{slug}.intervieweb.it/jobs/{aid}/it/"),
                location=(a.get("city") or a.get("location") or None),
                city=(a.get("city") or None),
                country=self.MESI.get((a.get("nation") or "").strip()),
                posted_at=dt,
                department=(a.get("function") or None),
                raw=a))
        return out


ADAPTERS["inrecruiting"] = InRecruiting


class Zvoove(BaseAdapter):
    """Zvoove (ex inter-connect) — portali carriere tedeschi server-rendered.

    Le offerte sono link /stellenangebot/{titolo}-{luogo}/{uuid};
    titolo e luogo si leggono dallo slug dell'URL. Lo slug è il
    hostname del sito carriere.
    """
    platform_id = "zvoove"
    PERCORSI = ("/gute-jobs/", "/jobs/", "/stellen/", "/karriere/",
                "/stellenangebote/", "/job-offers/")

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        jobs_url = None
        for path in self.PERCORSI:
            try:
                r = self.client.get(f"{base}{path}")
            except httpx.HTTPError:
                continue
            if r.status_code == 200 and "stellenangebot" in r.text:
                jobs_url = f"{base}{path}"
                testo = r.text
                break
        if not jobs_url:
            return []
        import html as html_mod
        hrefs = re.findall(r'href="(https?://[^"]*/stellenangebot/[^"]+)"',
                           testo)
        out: list[AtsJob] = []
        visti: set[str] = set()
        for href in hrefs:
            id_offerta = href.rstrip("/").split("/")[-1]
            if id_offerta in visti:
                continue
            visti.add(id_offerta)
            slug_titolo = href.rstrip("/").split("/")[-2]
            titolo = html_mod.unescape(slug_titolo).replace("-", " ").strip()
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=id_offerta[:36],
                title=titolo,
                url=href,
                raw={}))
        return out


ADAPTERS["zvoove"] = Zvoove


class Manatal(BaseAdapter):
    """Manatal — portali su careers-page.com/{azienda}.

    Listing server-rendered con Vue: il titolo sta in un <h5> dentro
    l'ancora dell'offerta. Lo slug è '{azienda}' del percorso
    careers-page.com.
    """
    platform_id = "manatal"

    def jobs(self, slug: str) -> list[AtsJob]:
        try:
            r = self.client.get(f"https://www.careers-page.com/{slug}")
        except httpx.HTTPError:
            return []
        if r.status_code != 200:
            return []
        import html as html_mod
        righe = re.findall(
            r'href="/' + re.escape(slug) +
            r'/job/([A-Za-z0-9]+)"[^>]*>\s*<h5[^>]*>\s*([^<]+?)\s*</h5>',
            r.text)
        return [
            AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=jid,
                title=html_mod.unescape(titolo),
                url=f"https://www.careers-page.com/{slug}/job/{jid}",
                raw={})
            for jid, titolo in righe
        ]


ADAPTERS["manatal"] = Manatal


class Paradox(BaseAdapter):
    """Paradox (Olivia) — siti carriere su CDN paradox con rendering JS.

    La pagina /jobs elenca le offerte come /{titolo-slug}/job/{id}
    dopo il rendering. Lo slug è il hostname del sito carriere.
    """
    platform_id = "paradox"

    def jobs(self, slug: str) -> list[AtsJob]:
        link = _renderizza_estrai(
            f"https://{slug}/jobs", "a[href*='/job/']", attesa=10000)
        out: list[AtsJob] = []
        visti: set[str] = set()
        for l in link:
            m = re.search(r'/job/([A-Za-z0-9-]+)$', l.get("href", ""))
            if not m or m.group(1) in visti:
                continue
            visti.add(m.group(1))
            titolo = (l.get("text") or "").strip()
            if not titolo or titolo.lower() == "view job":
                # il titolo è nello slug dell'URL
                titolo = l["href"].rstrip("/").split("/")[-2]
                titolo = titolo.replace("-", " ").strip()
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=m.group(1),
                title=titolo,
                url=l["href"],
                raw={}))
        return out


ADAPTERS["paradox"] = Paradox


class Workbuster(BaseAdapter):
    """Workbuster (Svezia/Norvegia) — listing /all-jobs renderizzato.

    Le offerte sono /jobs/{id}-{titolo-slug}, il titolo si legge dallo
    slug. Lo slug è il hostname del portale.
    """
    platform_id = "workbuster"

    def jobs(self, slug: str) -> list[AtsJob]:
        link = _renderizza_estrai(
            f"https://{slug}/all-jobs", "a[href*='/jobs/']", attesa=10000)
        out: list[AtsJob] = []
        visti: set[str] = set()
        for l in link:
            m = re.search(r'/jobs/(\d+)-([a-z0-9-]+)$',
                          l.get("href", "").rstrip("/"))
            if not m or m.group(1) in visti:
                continue
            visti.add(m.group(1))
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=m.group(1),
                title=m.group(2).replace("-", " ").strip(),
                url=l["href"],
                raw={}))
        return out


ADAPTERS["workbuster"] = Workbuster


class Jobsoid(BaseAdapter):
    """Jobsoid — portali con offerte /j/{id}/{titolo-slug} renderizzate.

    Lo slug è il hostname del portale (jobs.azienda.com).
    """
    platform_id = "jobsoid"

    def jobs(self, slug: str) -> list[AtsJob]:
        link = _renderizza_estrai(
            f"https://{slug}/", "a[href*='/j/']", attesa=10000)
        out: list[AtsJob] = []
        visti: set[str] = set()
        for l in link:
            m = re.search(r'/j/(\d+)/([a-z0-9-]+)$',
                          l.get("href", "").rstrip("/"))
            if not m or m.group(1) in visti:
                continue
            visti.add(m.group(1))
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=m.group(1),
                title=m.group(2).replace("-", " ").strip(),
                url=l["href"],
                raw={}))
        return out


ADAPTERS["jobsoid"] = Jobsoid


class WelcomeKit(BaseAdapter):
    """WelcomeKit (gruppo WTTJ) — widget incorporato renderizzato via JS.

    Le offerte compaiono nel DOM dopo il rendering con classi CSS
    welcomekit-jobs-list-item. Lo slug è il hostname del sito carriere.
    """
    platform_id = "welcomekit"
    PERCORSI = ("/jobs", "/en/jobs", "/careers", "/en/careers", "/")

    def jobs(self, slug: str) -> list[AtsJob]:
        from playwright.sync_api import sync_playwright
        raccolti: list[dict] = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                for host in (f"https://{slug}", f"https://www.{slug}"):
                    for path in self.PERCORSI:
                        try:
                            page.goto(f"{host}{path}",
                                      wait_until="domcontentloaded",
                                      timeout=30000)
                        except Exception:
                            continue
                        page.wait_for_timeout(8000)
                        raccolti = page.eval_on_selector_all(
                            ".welcomekit-jobs-list-item a",
                            """els => els.map(e => ({
                                href: e.href,
                                text: (e.textContent || '')
                                        .replace(/\\s+/g, ' ').trim()
                                        .slice(0, 200)}))""")
                        if raccolti:
                            break
                    if raccolti:
                        break
                browser.close()
        except Exception:
            pass
        out: list[AtsJob] = []
        visti: set[str] = set()
        for l in raccolti:
            m = re.search(r'/jobs/([^/]+)$', l.get("href", "").rstrip("/"))
            if not m or m.group(1) in visti:
                continue
            visti.add(m.group(1))
            titolo = (l.get("text") or "").strip()
            if len(titolo) < 4:
                titolo = m.group(1).split("_")[0].replace("-", " ")
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=m.group(1),
                title=titolo,
                url=l["href"],
                raw={}))
        return out


ADAPTERS["welcomekit"] = WelcomeKit


class Hireserve(BaseAdapter):
    """Hireserve (iTris) — portali wd_portal con offerte /vacancy/.

    Listing server-rendered: le offerte sono link
    /vacancy/{titolo-slug}-{id}.html. Lo slug è il hostname del
    portale (jobs.azienda.com).
    """
    platform_id = "hireserve"

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        # la pagina dei lavori: la home può linkare il listing; anche il
        # listing wd_portal.list serve il site_id — si trova nella home
        r = None
        try:
            r = self.client.get(f"{base}/")
        except httpx.HTTPError:
            return []
        if r.status_code != 200:
            return []
        testo = r.text
        m = re.search(r'wd_portal\.list\?p_web_site_id=(\d+)', testo)
        if m:
            try:
                rl = self.client.get(
                    f"{base}/wd/plsql/wd_portal.list?p_web_site_id={m.group(1)}")
                if rl.status_code == 200:
                    testo = rl.text
            except httpx.HTTPError:
                pass
        righe = re.findall(
            r'href="(/vacancy/([a-z0-9-]+)-(\d+)\.html)"', testo)
        out: list[AtsJob] = []
        visti: set[str] = set()
        for href, titolo_slug, id_offerta in righe:
            if id_offerta in visti:
                continue
            visti.add(id_offerta)
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=id_offerta,
                title=titolo_slug.replace("-", " ").strip(),
                url=f"{base}{href}",
                raw={}))
        return out


ADAPTERS["hireserve"] = Hireserve


class Tribepad(BaseAdapter):
    """Tribepad (UK) — portali renderizzati con offerte /jobs/job/.

    Lo slug è il hostname del portale (jobsearch.azienda.com).
    """
    platform_id = "tribepad"

    def jobs(self, slug: str) -> list[AtsJob]:
        link = _renderizza_estrai(
            f"https://{slug}/", "a[href*='/jobs/job/']", attesa=10000)
        out: list[AtsJob] = []
        visti: set[str] = set()
        for l in link:
            m = re.search(r'/jobs/job/([^/]+)/(\d+)$',
                          l.get("href", "").rstrip("/"))
            if not m or m.group(2) in visti:
                continue
            visti.add(m.group(2))
            titolo = (l.get("text") or "").strip() or m.group(1).replace("-", " ")
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=m.group(2),
                title=titolo,
                url=l["href"],
                raw={}))
        return out


ADAPTERS["tribepad"] = Tribepad


class TalentSoft(BaseAdapter):
    """TalentSoft (CEA e simili) — listing ASPX paginato server-rendered.

    Le offerte sono /offre-de-emploi/emploi-{titolo}_{id}.aspx, la
    paginazione è ?page=N. Lo slug è il hostname del portale.
    """
    platform_id = "talentsoft"
    MAX_PAGINE = 20

    PERCORSI = ("/offre-de-emploi/liste-toutes-offres.aspx",
                "/offre-de-emploi/liste-offres.aspx", "/")

    def jobs(self, slug: str) -> list[AtsJob]:
        listing = None
        base = None
        for host in (f"https://{slug}", f"https://www.{slug}"):
            for path in self.PERCORSI:
                try:
                    r = self.client.get(f"{host}{path}")
                except httpx.HTTPError:
                    continue
                if r.status_code == 200 and "offre-de-emploi/emploi-" in r.text:
                    base, listing = host, path
                    break
            if listing:
                break
        if not listing:
            return []
        out: list[AtsJob] = []
        visti: set[str] = set()
        for pagina in range(1, self.MAX_PAGINE + 1):
            url = f"{base}{listing}"
            if pagina > 1:
                url += f"?page={pagina}"
            try:
                rp = self.client.get(url)
            except httpx.HTTPError:
                break
            if rp.status_code != 200:
                break
            righe = re.findall(
                r'href="(/offre-de-emploi/(?:emploi-)?([a-z0-9-]+)_(\d+)\.aspx)"',
                rp.text)
            nuove = 0
            for href, titolo_slug, id_offerta in righe:
                if id_offerta in visti:
                    continue
                visti.add(id_offerta)
                nuove += 1
                out.append(AtsJob(
                    platform_id=self.platform_id, slug=slug,
                    external_id=id_offerta,
                    title=titolo_slug.replace("-", " ").replace("emploi ", "").strip(),
                    url=f"{base}{href}",
                    raw={}))
            if nuove == 0:
                break
        return out


ADAPTERS["talentsoft"] = TalentSoft


class ADP(BaseAdapter):
    """ADP WorkforceNow — career center pubblico con API REST.

    Il link della pagina carriere contiene il cid del tenant; l'API
    job-requisitions è aperta e paginata con startAt. Lo slug è il
    hostname del sito carriere dell'azienda.
    """
    platform_id = "adp"
    PER_PAGINA = 20
    MAX_PAGINE = 25

    PERCORSI = ("/", "/careers", "/jobs", "/en", "/career")

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        cid = None
        for host in (base, f"https://www.{slug}"):
            for path in self.PERCORSI:
                try:
                    r = self.client.get(f"{host}{path}")
                except httpx.HTTPError:
                    continue
                if r.status_code != 200:
                    continue
                m = re.search(r'workforcenow\.adp\.com[^"]*?cid=([0-9a-f-]{36})',
                              r.text)
                if m:
                    cid = m.group(1)
                    break
            if cid:
                break
        if not cid:
            return []

        out: list[AtsJob] = []
        visti: set[str] = set()
        for pagina in range(self.MAX_PAGINE):
            url = ("https://workforcenow.adp.com/mascsr/default/careercenter"
                   f"/public/events/staffing/v1/job-requisitions"
                   f"?cid={cid}&startAt={pagina * self.PER_PAGINA}")
            try:
                rr = self.client.get(url)
            except httpx.HTTPError:
                break
            if rr.status_code != 200:
                break
            try:
                jr = rr.json().get("jobRequisitions") or []
            except ValueError:
                break
            if not jr:
                break
            for req in jr:
                rid = req.get("itemID")
                if not rid or rid in visti:
                    continue
                visti.add(rid)
                luoghi = req.get("requisitionLocations") or []
                citta = None
                if luoghi:
                    citta = (luoghi[0].get("nameCode") or {}).get("shortName")
                try:
                    dt = datetime.fromisoformat(req["postDate"]) \
                        if req.get("postDate") else None
                except ValueError:
                    dt = None
                out.append(AtsJob(
                    platform_id=self.platform_id, slug=slug,
                    external_id=rid,
                    title=req.get("requisitionTitle") or "",
                    url=("https://workforcenow.adp.com/mascsr/default/"
                         f"mdf/recruitment/recruitment.html?cid={cid}"),
                    location=citta,
                    posted_at=dt,
                    raw=req))
            if len(jr) < self.PER_PAGINA:
                break
        return out


ADAPTERS["adp"] = ADP


class Carerix(BaseAdapter):
    """Carerix (Benelux) — API public_api/fo/process visibile a runtime.

    L'UUID del processo si costruisce solo nel browser: l'adapter
    rende la pagina vacatures con Playwright e cattura la risposta
    dell'API, poi la ripete con paginazione via httpx. Lo slug è il
    hostname del portale.
    """
    platform_id = "carerix"
    PER_PAGINA = 25
    MAX_PAGINE = 20

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        from playwright.sync_api import sync_playwright
        api_url = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()

                def on_req(req):
                    nonlocal api_url
                    if "public_api/fo/process/" in req.url \
                            and "count=" in req.url:
                        api_url = req.url

                page.on("request", on_req)
                page.goto(f"{base}/jobportal/vacatures",
                          wait_until="domcontentloaded", timeout=40000)
                page.wait_for_timeout(12000)
                browser.close()
        except Exception:
            return []
        if not api_url:
            return []

        out: list[AtsJob] = []
        for pagina in range(self.MAX_PAGINE):
            url = re.sub(r"start=\d+",
                         f"start={pagina * self.PER_PAGINA}", api_url)
            try:
                rr = self.client.get(url)
            except httpx.HTTPError:
                break
            if rr.status_code != 200:
                break
            try:
                dati = rr.json().get("data") or []
            except ValueError:
                break
            if not dati:
                break
            for v in dati:
                pid = str(v.get("publicationID") or "")
                if not pid:
                    continue
                try:
                    dt = datetime.strptime(v["publicationStart"],
                                           "%d-%m-%Y") \
                        if v.get("publicationStart") else None
                except ValueError:
                    dt = None
                out.append(AtsJob(
                    platform_id=self.platform_id, slug=slug,
                    external_id=pid,
                    title=v.get("titleInformation") or "",
                    url=f"{base}/jobportal/vacatures",
                    location=v.get("workLocation"),
                    city=v.get("workLocation"),
                    posted_at=dt,
                    raw=v))
            if len(dati) < self.PER_PAGINA:
                break
        return out


ADAPTERS["carerix"] = Carerix


class Paylocity(BaseAdapter):
    """Paylocity — portali recruiting.paylocity.com renderizzati via JS.

    Lo slug è il hostname del sito carriere; il link al portale con
    l'UUID del tenant sta nella pagina. Le offerte sono
    /Recruiting/Jobs/Details/{id} dopo il rendering.
    """
    platform_id = "paylocity"
    PERCORSI = ("/", "/careers", "/career", "/jobs", "/en", "/de",
                 "/career/job-offers", "/en/careers", "/company/careers")

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        porta = None
        for host in (base, f"https://www.{slug}"):
            for path in self.PERCORSI:
                try:
                    r = self.client.get(f"{host}{path}")
                except httpx.HTTPError:
                    continue
                if r.status_code != 200:
                    continue
                m = re.search(
                    r'(https://recruiting\.paylocity\.com/recruiting/'
                    r'jobs/All/[0-9a-f-]{36}[^"\'\s]*)', r.text)
                if m:
                    porta = m.group(1).replace("&amp;", "&")
                    break
            if porta:
                break
        if not porta:
            return []
        link = _renderizza_estrai(porta, "a[href*='Jobs/Details/']",
                                  attesa=10000)
        out: list[AtsJob] = []
        visti: set[str] = set()
        for l in link:
            m = re.search(r'/Jobs/Details/(\d+)', l.get("href", ""))
            if not m or m.group(1) in visti:
                continue
            visti.add(m.group(1))
            out.append(AtsJob(
                platform_id=self.platform_id, slug=slug,
                external_id=m.group(1),
                title=(l.get("text") or "").strip(),
                url=l["href"],
                raw={}))
        return out


ADAPTERS["paylocity"] = Paylocity


class Jibe(BaseAdapter):
    """Jibe (gruppo iCIMS) — portali careers.azienda.com con /api/jobs.

    L'API REST del portale è pubblica e paginata con ?page=N; ogni
    offerta ha req_id, title e description completi. Lo slug è il
    hostname del portale carriere.
    """
    platform_id = "jibe"
    PER_PAGINA = 10
    MAX_PAGINE = 20

    def jobs(self, slug: str) -> list[AtsJob]:
        base = f"https://{slug}"
        out: list[AtsJob] = []
        visti: set[str] = set()
        for pagina in range(1, self.MAX_PAGINE + 1):
            url = f"{base}/api/jobs"
            if pagina > 1:
                url += f"?page={pagina}"
            try:
                r = self.client.get(url)
            except httpx.HTTPError:
                break
            if r.status_code != 200:
                break
            try:
                d = r.json()
                jobs = d.get("jobs") or []
            except ValueError:
                break
            if not jobs:
                break
            for j in jobs:
                dati = j.get("data") or {}
                rid = str(dati.get("req_id") or dati.get("slug") or "")
                if not rid or rid in visti:
                    continue
                visti.add(rid)
                out.append(AtsJob(
                    platform_id=self.platform_id, slug=slug,
                    external_id=rid,
                    title=dati.get("title") or "",
                    url=f"{base}/jobs/{rid}",
                    raw=dati))
            if len(jobs) < self.PER_PAGINA:
                break
        return out


ADAPTERS["jibe"] = Jibe
