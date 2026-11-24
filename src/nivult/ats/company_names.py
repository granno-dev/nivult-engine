from __future__ import annotations

import re
from urllib.parse import urlsplit

_GENERICI = frozenset({
    "retail stores opportunities", "external careers", "current openings",
    "job opportunities", "careers", "employment opportunities", "opportunities",
    "jobs", "job board", "career site", "work with us", "join us", "home",
})
_PREFIX = re.compile(
    r"^(?:opportunities with|careers at|jobs at|careers with|"
    r"employment opportunities at|job opportunities at)\s+", re.IGNORECASE)
_SUFFIX = re.compile(
    r"\s+(?:employment opportunities|job opportunities|career opportunities|"
    r"opportunities|careers|jobs|job board|career site)$", re.IGNORECASE)
_SPORCO = re.compile(
    r"https?://|jobboard|^[A-Z]{2,4}[0-9]{3,4}|"
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE)
_PATHS = (
    ("company", "name"), ("hiringOrganization", "name"),
    ("_jobposting", "hiringOrganization", "name"), ("employer", "name"),
    ("entreprise", "nom"), ("organization", "name"), ("account", "name"),
)


def pulisci(nome) -> str | None:
    if not isinstance(nome, str):
        return None
    n = re.sub(r"\s+", " ", nome).strip(" \t\r\n-|–—")
    n = _PREFIX.sub("", n)
    n = _SUFFIX.sub("", n).strip()
    if not n or n.casefold() in _GENERICI or _SPORCO.search(n):
        return None
    return n


def nomi_da_raw(raw) -> list[str]:
    if not isinstance(raw, dict):
        return []
    valori = [raw.get("firma"), raw.get("company_name"), raw.get("Company_Name"),
              raw.get("companyName"), raw.get("tenantName"), raw.get("accountName")]
    for percorso in _PATHS:
        corrente = raw
        for chiave in percorso:
            corrente = corrente.get(chiave) if isinstance(corrente, dict) else None
        valori.append(corrente)
    for chiave in ("company", "employer", "organization"):
        if isinstance(raw.get(chiave), str):
            valori.append(raw[chiave])
    puliti = [pulisci(v) for v in valori]
    return list(dict.fromkeys(n for n in puliti if n))


def nome_unico(raws) -> str | None:
    nomi = {n.casefold(): n for raw in raws for n in nomi_da_raw(raw)}
    return next(iter(nomi.values())) if len(nomi) == 1 else None


def board_url(platform_id: str, slug: str) -> str | None:
    if platform_id == "icims":
        return f"https://{slug}.icims.com"
    if platform_id == "successfactors" and "." in slug:
        return f"https://{slug}"
    if platform_id == "lever":
        return f"https://jobs.lever.co/{slug}"
    if platform_id == "ashby":
        return f"https://jobs.ashbyhq.com/{slug}"
    if platform_id == "workable":
        return f"https://apply.workable.com/{slug}"
    if platform_id == "bamboohr":
        return f"https://{slug}.bamboohr.com/careers"
    if platform_id == "teamtailor":
        return f"https://{slug}.teamtailor.com"
    if platform_id == "rippling":
        return f"https://ats.rippling.com/{slug}/jobs"
    if platform_id == "personio":
        return f"https://{slug}.jobs.personio.com"
    return None


def dominio(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return None
    return host or None


def nome_da_html(html: str) -> str | None:
    for tag in re.findall(r"<meta[^>]+og:site_name[^>]*>", html, re.IGNORECASE):
        contenuto = re.search(r"content\s*=\s*\"([^\"]+)\"", tag, re.IGNORECASE)
        if not contenuto:
            contenuto = re.search(r"content\s*=\s*'([^']+)'", tag, re.IGNORECASE)
        if contenuto:
            nome = pulisci(contenuto.group(1))
            if nome:
                return nome
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not title:
        return None
    testo = re.sub(r"<[^>]+>", " ", title.group(1))
    for candidato in re.split(r"\s+[|–—]\s+|\s+-\s+", testo):
        nome = pulisci(candidato)
        if nome:
            return nome
    return None
