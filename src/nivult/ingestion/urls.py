"""Canonicalizzazione degli URL e classificazione del link.

La canonicalizzazione è il cuore della deduplica dura: `jobs.canonical_url` è
UNIQUE, quindi due URL che indicano la stessa offerta devono ridursi alla stessa
stringa, e due che indicano offerte diverse non devono mai collidere.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Parametri di tracciamento da buttare.
#
# DENYLIST e non allowlist, deliberatamente: moltissimi career site
# identificano l'offerta proprio con un parametro di query (?jobId=, ?gh_jid=,
# ?id=). Una allowlist li romperebbe tutti insieme e in silenzio, riducendo
# offerte diverse allo stesso canonical_url — che con un vincolo UNIQUE
# significa scartarle come duplicate.
TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "gclid", "fbclid", "msclkid", "igshid", "mc_cid",
    "mc_eid", "ref", "referer", "referrer", "source", "src", "trk",
    "trackingid", "tracking_id", "_ga", "_gl", "yclid", "wbraid", "gbraid",
})

DEFAULT_PORTS = {"http": "80", "https": "443"}

# Domini degli enti pubblici del lavoro: ammessi, ma etichettati.
NATIONAL_AGENCY_DOMAINS = frozenset({
    "francetravail.fr", "candidat.francetravail.fr", "pole-emploi.fr",
    "arbeitsagentur.de", "jobboerse.arbeitsagentur.de",
    "arbetsformedlingen.se", "nav.no", "tyomarkkinatori.fi",
})

# Aggregatori commerciali: è contro questi che nasce la regola del career site.
JOB_BOARD_DOMAINS = frozenset({
    "linkedin.com", "indeed.com", "glassdoor.com", "monster.com",
    "stepstone.de", "infojobs.it", "welcometothejungle.com", "jobijoba.com",
    "talent.com", "jooble.org", "adzuna.com", "ziprecruiter.com",
})


def canonicalize(url: str) -> str:
    """Riduce un URL alla sua forma canonica.

    Solleva ValueError su input che non è un URL http(s) utilizzabile: meglio
    fermarsi qui che scrivere una riga con un canonical_url senza senso.
    """
    if not url or not url.strip():
        raise ValueError("URL vuoto")

    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"schema non supportato: {scheme or '(assente)'}")
    if not parts.hostname:
        raise ValueError(f"host assente in {url!r}")

    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]

    netloc = host
    if parts.port and str(parts.port) != DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"

    # I parametri superstiti vengono ordinati: ?a=1&b=2 e ?b=2&a=1 sono lo
    # stesso URL, e senza ordinamento produrrebbero due righe.
    kept = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    )
    query = urlencode(kept)

    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    # Il fragment sparisce sempre: non raggiunge mai il server.
    return urlunsplit((scheme, netloc, path, query, ""))


def registrable_domain(url: str) -> str:
    """Host senza www. Non è un vero suffisso pubblico, ma per raggruppare
    per azienda basta e non aggiunge una dipendenza."""
    host = urlsplit(url).hostname or ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def _matches(host: str, domains: frozenset[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def classify_link(url: str) -> str:
    """career_site | national_agency | job_board.

    Il default è career_site, ed è una scelta che va guardata con sospetto: un
    dominio sconosciuto viene presentato all'utente come "candidatura diretta".
    Le due liste vanno tenute aggiornate, e il conteggio per dominio in
    ingestion_runs serve proprio a vedere cosa sta entrando.
    """
    host = registrable_domain(url)
    if not host:
        raise ValueError(f"host assente in {url!r}")
    if _matches(host, NATIONAL_AGENCY_DOMAINS):
        return "national_agency"
    if _matches(host, JOB_BOARD_DOMAINS):
        return "job_board"
    return "career_site"


def normalize_title(title: str) -> str:
    """Titolo normalizzato per il fingerprint della deduplica morbida."""
    return " ".join(title.lower().split())
