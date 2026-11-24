from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import httpx

_SPORCO = re.compile(
    r"(?:/|jobboard|https?://)|^[A-Z]{2,4}[0-9]{3,4}|"
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_GENERICI = frozenset({
    "retail stores opportunities", "external careers", "current openings",
    "job opportunities", "careers", "employment opportunities", "opportunities",
    "jobs", "job board", "career site", "work with us", "join us", "home",
})
_PREFISSI = (
    "Opportunities with ", "Careers at ", "Jobs at ", "Careers with ",
    "Employment opportunities at ", "Job opportunities at ",
)
_SUFFISSI = (
    " Employment Opportunities", " Job Opportunities", " Career Opportunities",
    " Opportunities", " Careers", " Jobs", " Job Board", " Career Site",
)


def nome_sporco(nome: str | None) -> bool:
    return not nome or not nome.strip() or bool(_SPORCO.search(nome.strip()))


def nome_da_slug(slug: str | None) -> str | None:
    if not slug or _SPORCO.search(slug):
        return None
    return slug.replace("-", " ").title()


def pulisci_nome(nome: str | None) -> str | None:
    if not nome:
        return None
    n = re.sub(r"\s+", " ", nome).strip(" \t\r\n-|–—")
    if n.casefold() in _GENERICI:
        return None
    for prefisso in _PREFISSI:
        if n.lower().startswith(prefisso.lower()):
            n = n[len(prefisso):].strip()
            break
    for suffisso in _SUFFISSI:
        if n.lower().endswith(suffisso.lower()):
            n = n[:-len(suffisso)].strip()
            break
    if not n or n.casefold() in _GENERICI or nome_sporco(n):
        return None
    return n


def _dominio(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()
    except ValueError:
        return None
    host = host.removeprefix("www.")
    if "." not in host or host in {"ultipro.com", "ukg.net", "ukg.com"}:
        return None
    return host


def _nome_home(html: str) -> str | None:
    for tag in re.findall(r"<meta[^>]+og:site_name[^>]*>", html, re.IGNORECASE):
        contenuto = re.search(r"content\s*=\s*\"([^\"]+)\"", tag, re.IGNORECASE)
        if not contenuto:
            contenuto = re.search(r"content\s*=\s*'([^']+)'", tag, re.IGNORECASE)
        if contenuto:
            nome = pulisci_nome(contenuto.group(1))
            if nome:
                return nome
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not title:
        return None
    testo = re.sub(r"<[^>]+>", " ", title.group(1))
    candidato = re.split(r"\s+[|–—]\s+|\s+-\s+", testo, maxsplit=1)[0]
    return pulisci_nome(candidato)


def leggi_board(client: httpx.Client, slug: str, home: bool = True) -> dict:
    url = f"https://recruiting.ultipro.com/{slug}"
    r = client.get(url)
    r.raise_for_status()
    html = r.text
    marker = re.search(r"\bjobBoard\s*:\s*", html)
    board = {}
    if marker:
        try:
            board = json.JSONDecoder().raw_decode(html[marker.end():])[0]
        except (json.JSONDecodeError, TypeError):
            board = {}
    nome = pulisci_nome(board.get("Name"))
    logo = re.search(r"logoHref\s*:\s*['\"]([^'\"]+)", html)
    dominio = _dominio(logo.group(1) if logo else None)
    if not nome and dominio and home:
        try:
            pagina = client.get(f"https://{dominio}")
            if pagina.status_code == 200:
                nome = _nome_home(pagina.text)
        except httpx.HTTPError:
            pass
    return {"company_name": nome, "site_domain": dominio,
            "board_name": board.get("Name")}
