from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import time
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from nivult.ats.pii import redact_pii

REVEAL_TTL = 30 * 60
CURSOR_TTL = 24 * 60 * 60
MAX_LIMIT = 20
MAX_PAGE = 5


class TokenError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _key() -> bytes:
    secret = os.environ.get("EXPLORER_REVEAL_SECRET", "")
    if len(secret.encode()) < 32:
        raise RuntimeError("EXPLORER_REVEAL_SECRET must contain at least 32 bytes")
    return hashlib.sha256(secret.encode()).digest()


def _seal(payload: dict) -> str:
    nonce = os.urandom(12)
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
    encrypted = AESGCM(_key()).encrypt(nonce, raw, b"nivult-explorer-v1")
    return base64.urlsafe_b64encode(nonce + encrypted).rstrip(b"=").decode()


def _open(token: str, expected_kind: str) -> dict:
    try:
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        if (len(raw) < 29
                or base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != token):
            raise ValueError
        decrypted = AESGCM(_key()).decrypt(
            raw[:12], raw[12:], b"nivult-explorer-v1")
        payload = json.loads(decrypted)
        if not isinstance(payload, dict) or payload.get("kind") != expected_kind:
            raise ValueError
        if not isinstance(payload.get("exp"), int) or payload["exp"] <= int(time.time()):
            raise TokenError("token expired")
        return payload
    except TokenError:
        raise
    except (ValueError, TypeError, InvalidTag, json.JSONDecodeError) as exc:
        raise TokenError("invalid token") from exc


def reveal_token(user_id: str, company_id: str) -> str:
    return _seal({"kind": "reveal", "company_id": company_id,
                  "user_id": user_id, "exp": int(time.time()) + REVEAL_TTL})


def company_from_token(token: str, user_id: str) -> str:
    payload = _open(token, "reveal")
    token_user = payload.get("user_id")
    company_id = payload.get("company_id")
    if not isinstance(token_user, str) or not hmac.compare_digest(token_user, user_id):
        raise TokenError("token belongs to another user", 403)
    if (not isinstance(company_id, str) or len(company_id) > 300
            or ":" not in company_id):
        raise TokenError("invalid token")
    return company_id


def reveal_group(company_id: str) -> str:
    digest = hmac.new(_key(), ("company-group\0" + company_id).encode(),
                      hashlib.sha256).digest()[:18]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def seal_cursor(user_id: str, search_kind: str, inner: str, page: int) -> str:
    return _seal({"kind": "cursor", "search": search_kind, "user_id": user_id,
                  "cursor": inner, "page": page, "exp": int(time.time()) + CURSOR_TTL})


def open_cursor(token: str, user_id: str, search_kind: str) -> tuple[str, int]:
    payload = _open(token, "cursor")
    token_user = payload.get("user_id")
    if (not isinstance(token_user, str)
            or not hmac.compare_digest(token_user, user_id)
            or payload.get("search") != search_kind):
        raise TokenError("cursor is not valid for this search", 403)
    inner, page = payload.get("cursor"), payload.get("page")
    if not isinstance(inner, str) or isinstance(page, bool) or not isinstance(page, int):
        raise TokenError("invalid cursor")
    if page < 2 or page > MAX_PAGE:
        raise TokenError("preview pagination limit reached")
    return inner, page


def _brand(domain: str | None) -> str:
    value = str(domain or "").strip().lower()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    host = (urlsplit(value).hostname or "").removeprefix("www.")
    first = host.split(".")[0]
    return first if len(first) >= 3 else ""


_COMPANY_PREFIX = re.compile(
    r"^(?:careers(?:\s+at)?|jobs\s+at|hiring(?:\s+(?:at|for))?)\b[\s:|\-–—]*",
    re.IGNORECASE)
_IDENTITY_SUFFIXES = frozenset({
    "inc", "llc", "ltd", "gmbh", "ag", "sa", "sas", "sarl", "bv", "nv",
    "spa", "srl", "plc", "limited", "corporation", "corp", "company", "co",
    "group", "holding", "holdings", "franchise", "franchising", "franchisee",
})


def clean_company_name(company) -> str:
    value = re.sub(r"\s+", " ", str(company or "")).strip()
    for _ in range(3):
        cleaned = _COMPANY_PREFIX.sub("", value, count=1).strip(" -–—|:")
        if cleaned == value:
            break
        value = cleaned
    return value


def _identity_words(company) -> list[str]:
    words = re.findall(r"[^\W_]+", clean_company_name(company), flags=re.UNICODE)
    while words and words[-1].casefold() in _IDENTITY_SUFFIXES:
        words.pop()
    return words


def employer_key(company) -> str:
    key = "".join(char for word in _identity_words(company)
                  for char in word.casefold() if char.isalnum())
    return key if len(key) >= 3 else ""


def _title_pattern(candidate: str) -> str:
    words = re.findall(r"[^\W_]+", candidate, flags=re.UNICODE)
    return r"[\W_]*".join(re.escape(word) for word in words)


def _identity_candidates(company, domain) -> set[str]:
    original = str(company or "").strip()
    candidates = {original, clean_company_name(original), _brand(domain)}
    for candidate in tuple(candidates):
        words = _identity_words(candidate)
        if words:
            candidates.add(" ".join(words))
    return {c for c in candidates if len(c) >= 3}


def _redact_identity(text: str, company=None, domain=None, slug=None) -> str:
    result = text
    candidates = _identity_candidates(company, domain)
    if slug and len(str(slug)) >= 3:
        candidates.add(str(slug))
        candidates.add(clean_company_name(re.sub(r"[-_]+", " ", str(slug))))
    for candidate in sorted(candidates, key=len, reverse=True):
        pattern = _title_pattern(candidate)
        if pattern:
            result = re.sub(r"(?<!\w)" + pattern + r"(?!\w)", "Hidden company",
                            result, flags=re.IGNORECASE)
    return result


def safe_title(title, company=None, domain=None, slug=None) -> str:
    result = _redact_identity(str(title or ""), company, domain, slug)
    result = re.sub(r"(?:Hidden company)(?:\s*[-–—|:]\s*Hidden company)+",
                    "Hidden company", result, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", result).strip(" -–—|:") or "Hidden company"


def sanitized_description(description, company=None, domain=None, slug=None) -> str:
    text = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ",
                  str(description or ""), flags=re.IGNORECASE | re.DOTALL)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"(?:https?:)?//[^\s<>]+|www\.[^\s<>]+", "[link hidden]", text,
                  flags=re.IGNORECASE)
    text = redact_pii(text)
    text = _redact_identity(text, company, domain, slug)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_PRODUCTIVITY_TECH = frozenset({
    "excel", "word", "outlook", "powerpoint", "microsoft office", "ms office",
    "google workspace", "microsoft excel", "microsoft word", "microsoft outlook",
    "microsoft powerpoint",
})


def grouped_technologies(values) -> list[dict[str, str]]:
    grouped = []
    seen = set()
    for item in values or []:
        if isinstance(item, dict):
            name = item.get("name") or item.get("technology")
            previous_group = item.get("group")
        else:
            name = item
            previous_group = None
        name = str(name or "").strip()
        if not name:
            continue
        normalized = re.sub(r"[^a-z0-9]+", " ", name.casefold()).strip()
        group = ("productivity" if normalized in _PRODUCTIVITY_TECH
                 else previous_group if previous_group in ("core", "productivity")
                 else "core")
        marker = name.casefold()
        if marker not in seen:
            grouped.append({"name": name, "group": group})
            seen.add(marker)
    return grouped


def _first(row: dict, *keys):
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _languages(row: dict) -> list:
    values = _first(row, "languages", "languages_required")
    if values is None and row.get("language"):
        values = [row["language"]]
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        values = [values]
    return list(dict.fromkeys(value for value in values if value is not None))


def _known_board(row: dict) -> bool:
    for key in ("company_domain", "company_slug", "ats", "url"):
        value = str(row.get(key) or "").strip().lower()
        if not value:
            continue
        candidate = value if "://" in value else "https://" + value
        try:
            host = (urlsplit(candidate).hostname or "").removeprefix("www.")
        except ValueError:
            continue
        if host in ("jobgether.com", "justjoin.it"):
            return True
        if key == "ats" and re.sub(r"[^a-z0-9]", "", value) in ("jobgether", "justjoin"):
            return True
    return False


def is_aggregator(row: dict) -> bool:
    return row.get("source_type") == "aggregator" or _known_board(row)


def source_label(ats) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", str(ats or "").casefold())
    if normalized == "jsonld":
        return "Direct Career Site"
    if normalized == "icims":
        return "iCIMS"
    if normalized == "lever":
        return "Lever"
    if normalized == "successfactors":
        return "SuccessFactors"
    if normalized.startswith("zoho"):
        return "Zoho Recruit"
    return {
        "greenhouse": "Greenhouse", "workday": "Workday",
        "ashby": "Ashby", "recruitee": "Recruitee",
        "smartrecruiters": "SmartRecruiters", "bamboohr": "BambooHR",
    }.get(normalized, "External job source")


def _validated_http_url(value) -> str | None:
    url = str(value or "").strip()
    if not url or any(char.isspace() for char in url):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return None
        if parts.username or parts.password:
            return None
    except ValueError:
        return None
    return url


def revealed_company_profile(row: dict) -> dict:
    out = dict(row)
    display_name = clean_company_name(out.get("company") or out.get("name"))
    if display_name:
        if "company" in out or out.get("name") is None:
            out["company"] = display_name
        out["name"] = display_name
    size_band = _first(out, "company_size_band", "employee_range", "size_range")
    if size_band is not None:
        out["company_size_band"] = size_band
    size = _first(out, "company_size", "company_size_band", "employees")
    if size is not None:
        out["company_size"] = size
    about = _first(out, "about", "description")
    if about is not None:
        out["about"] = about
    locations_count = _first(out, "locations_count", "n_locations")
    if locations_count is None and isinstance(out.get("locations"), list):
        locations_count = len(out["locations"])
    if locations_count is not None:
        out["locations_count"] = locations_count
    active = _first(out, "active_postings_count", "active_jobs", "open_jobs_count")
    if active is not None:
        out["active_postings_count"] = active
    posted = _first(out, "posted_30d_count", "jobs_posted_30d")
    if posted is not None:
        out["posted_30d_count"] = posted
    out["technologies"] = grouped_technologies(out.get("technologies"))
    out["languages"] = _languages(out)
    out["is_aggregator"] = is_aggregator(out)
    return out


_JOB_SAFE = (
    "id", "title", "category", "seniority", "work_mode", "posted_at", "country",
    "employment_type", "source_label", "technologies", "company_size_band",
    "industry", "active_postings_count", "posted_30d_count", "languages",
    "is_aggregator",
)
_JOB_REVEALED = _JOB_SAFE + (
    "ats", "remote", "language", "languages_required", "skills", "closed_at",
    "benefits", "has_salary", "salary_min", "salary_max", "salary_currency",
    "salary_period", "hours_type", "duration_type", "management_level", "valid_through",
    "city", "location", "company", "company_slug", "company_domain", "logo_url",
    "url", "source_type", "description", "summary", "summary_source", "department",
    "salary_source", "salary_text", "salary_is_estimate", "salary_estimate_min",
    "salary_estimate_median", "salary_estimate_max", "salary_estimate_currency",
    "salary_estimate_basis", "first_seen", "last_seen", "updated_at",
    "posted_at_estimated", "external_id", "state", "postal_code", "geo",
    "is_decision_maker", "shift_schedule", "work_hours", "is_urgently_hiring",
    "applicants_count", "is_easy_apply", "job_sources",
)


def _structured_job(source: dict) -> dict:
    out = {key: source[key] for key in (
        "id", "category", "seniority", "posted_at", "country", "employment_type",
        "industry") if source.get(key) is not None}
    if source.get("remote") is not None:
        out["work_mode"] = source["remote"]
    size = _first(source, "company_size_band", "employee_range", "size_range")
    if size is not None:
        out["company_size_band"] = size
    active = _first(source, "active_postings_count", "active_jobs", "open_jobs_count")
    if active is not None:
        out["active_postings_count"] = active
    posted = _first(source, "posted_30d_count", "jobs_posted_30d")
    if posted is not None:
        out["posted_30d_count"] = posted
    out.update({
        "source_label": source_label(source.get("ats")),
        "technologies": grouped_technologies(source.get("technologies")),
        "languages": _languages(source),
        "is_aggregator": is_aggregator(source),
    })
    return out


def job_dto(row: dict, revealed: bool, token: str, group: str,
            profile: dict | None = None, job_revealed: bool = False) -> dict:
    source = dict(row)
    if profile:
        for key in ("employee_range", "size_range", "company_size_band", "company_size",
                    "industry", "active_postings_count", "active_jobs", "open_jobs_count",
                    "posted_30d_count", "jobs_posted_30d"):
            if key in profile:
                source.setdefault(key, profile[key])
        source["company"] = profile.get("company") or source.get("company")
        source["company_domain"] = (profile.get("domain") or profile.get("website")
                                    or source.get("company_domain"))
        source["logo_url"] = profile.get("logo") or source.get("logo_url")
    identity_company = source.get("company")
    if source.get("company"):
        source["company"] = clean_company_name(source["company"])
    structured = _structured_job(source)
    if revealed:
        out = {key: source[key] for key in _JOB_REVEALED
               if key in source and source[key] is not None}
        out.update({key: value for key, value in structured.items() if key != "title"})
        source_url = _validated_http_url(source.get("url") or source.get("source_url"))
        if source_url:
            out["source_url"] = source_url
    else:
        out = structured
        nascondi_titolo = (
            source.get("title_requires_redaction")
            or not (clean_company_name(source.get("company"))
                    or _brand(source.get("company_domain"))))
        out["title"] = ("Hidden company" if nascondi_titolo
                        else safe_title(source.get("title"), identity_company,
                                        source.get("company_domain"), source.get("company_slug")))
        if job_revealed and source.get("description"):
            out["description"] = sanitized_description(
                source["description"], identity_company, source.get("company_domain"),
                source.get("company_slug"))
    out.update({"revealed": revealed, "az_rivelata": revealed,
                "job_revealed": revealed or job_revealed,
                "reveal_token": token, "reveal_group": group})
    return out


def detail_dto(row: dict, revealed: bool, token: str, group: str,
               profile: dict | None = None, job_revealed: bool = False) -> dict:
    return job_dto(row, revealed, token, group, profile, job_revealed)


_COMPANY_SAFE = (
    "country", "industry", "company_size_band", "active_postings_count",
    "posted_30d_count", "technologies", "languages", "is_aggregator",
)
_COMPANY_REVEALED = _COMPANY_SAFE + (
    "company", "name", "company_slug", "ats", "domain", "logo", "website",
    "active_jobs", "employees", "employee_range", "n_tecnologie", "open_jobs_count",
    "jobs_posted_30d", "n_locations", "company_size", "about", "locations_count",
)


def company_dto(row: dict, revealed: bool, token: str, group: str) -> dict:
    source = revealed_company_profile(row)
    fields = _COMPANY_REVEALED if revealed else _COMPANY_SAFE
    out = {key: source[key] for key in fields if key in source and source[key] is not None}
    out.update({"revealed": revealed, "reveal_token": token, "reveal_group": group})
    return out
