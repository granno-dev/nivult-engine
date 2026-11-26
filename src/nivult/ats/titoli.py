from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

_NUMERIC = re.compile(r"^#?\d+$")
_US_POSTAL = re.compile(
    r"\s+\b(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC|PR)"
    r"\s+\d{5}(?:-\d{4})?(?:\s+\d{1,6})?\s*$",
    re.IGNORECASE,
)
_CA_POSTAL = re.compile(
    r"\s+\b(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)"
    r"\s+[A-Z]\d[A-Z]\s*\d[A-Z]\d\s*$",
    re.IGNORECASE,
)
_IGNORED_SEGMENTS = {"job", "jobs", "career", "careers", "position", "positions"}


def numerico(title) -> bool:
    return bool(_NUMERIC.fullmatch(str(title or "").strip()))


def da_url(url) -> str | None:
    if not url:
        return None
    try:
        segments = unquote(urlsplit(str(url)).path).split("/")
    except ValueError:
        return None
    candidates = []
    for segment in segments:
        segment = re.sub(r"\.(?:html?|aspx)$", "", segment.strip(),
                         flags=re.IGNORECASE)
        if (len(segment) < 5 or segment.lower() in _IGNORED_SEGMENTS
                or numerico(segment)
                or re.fullmatch(r"[a-z]{2}(?:-[a-z]{2})?", segment, re.IGNORECASE)
                or not re.search(r"[^\W\d_]", segment, re.UNICODE)):
            continue
        if "-" not in segment and len(segment) < 8:
            continue
        candidates.append(segment)
    if not candidates:
        return None
    slug = max(candidates, key=len)
    title = re.sub(r"\s+", " ", re.sub(r"[-_]+", " ", slug)).strip(" -–—|,")
    if not title or numerico(title):
        return None
    return title[0].upper() + title[1:]


def pulisci(title, url=None) -> str:
    value = re.sub(r"\s+", " ", str(title or "")).strip()
    if numerico(value):
        return da_url(url) or "Job title unavailable"
    value = _CA_POSTAL.sub("", _US_POSTAL.sub("", value)).strip(" -–—|,")
    return value or da_url(url) or "Job title unavailable"
