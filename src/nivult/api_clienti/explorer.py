from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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


def _title_pattern(candidate: str) -> str:
    words = re.findall(r"[^\W_]+", candidate, flags=re.UNICODE)
    return r"[^\w]+".join(re.escape(word) for word in words)


def safe_title(title, company=None, domain=None) -> str:
    result = str(title or "")
    company = str(company or "").strip()
    candidates = {company, _brand(domain)}
    words = re.findall(r"[^\W_]+", company, flags=re.UNICODE)
    suffixes = {"inc", "llc", "ltd", "gmbh", "ag", "sa", "sas", "sarl",
                "bv", "nv", "spa", "srl", "plc", "limited", "corporation",
                "corp", "company", "co", "group", "holding", "holdings"}
    while words and words[-1].lower() in suffixes:
        words.pop()
    if words:
        candidates.add(" ".join(words))
    for candidate in sorted((c for c in candidates if len(c) >= 3), key=len, reverse=True):
        pattern = _title_pattern(candidate)
        if pattern:
            result = re.sub(r"(?<!\w)" + pattern + r"(?!\w)", "Hidden company",
                            result, flags=re.IGNORECASE)
    result = re.sub(r"(?:Hidden company)(?:\s*[-–—|:]\s*Hidden company)+",
                    "Hidden company", result, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", result).strip(" -–—|:") or "Hidden company"


_JOB_SAFE = (
    "id", "title", "ats", "country", "seniority", "remote", "category",
    "posted_at", "closed_at", "employment_type", "language", "technologies", "skills",
    "languages_required", "benefits", "has_salary", "salary_min", "salary_max",
    "salary_currency", "salary_period", "hours_type", "duration_type",
    "management_level", "valid_through",
)
_JOB_REVEALED = _JOB_SAFE + (
    "city", "location", "company", "company_slug", "company_domain", "logo_url",
    "url", "source_type", "description", "summary", "summary_source", "department",
    "salary_source", "salary_text", "salary_is_estimate", "salary_estimate_min",
    "salary_estimate_median", "salary_estimate_max", "salary_estimate_currency",
    "salary_estimate_basis", "first_seen", "last_seen", "updated_at",
    "posted_at_estimated", "external_id", "state", "postal_code", "geo",
    "is_decision_maker", "shift_schedule", "work_hours", "is_urgently_hiring",
    "applicants_count", "is_easy_apply", "job_sources",
)


def job_dto(row: dict, revealed: bool, token: str, group: str,
            profile: dict | None = None) -> dict:
    source = dict(row)
    if profile:
        source["company"] = profile.get("company") or source.get("company")
        source["company_domain"] = (profile.get("domain") or profile.get("website")
                                    or source.get("company_domain"))
        source["logo_url"] = profile.get("logo")
    fields = _JOB_REVEALED if revealed else _JOB_SAFE
    out = {key: source[key] for key in fields if key in source and source[key] is not None}
    if not revealed:
        out.pop("city", None)
        nascondi_titolo = (source.get("title_requires_redaction")
                           or not (source.get("company") or source.get("company_domain")))
        out["title"] = ("Hidden company" if nascondi_titolo
                        else safe_title(source.get("title"), source.get("company"),
                                        source.get("company_domain")))
    out.update({"revealed": revealed, "az_rivelata": revealed,
                "reveal_token": token, "reveal_group": group})
    return out


def detail_dto(row: dict, revealed: bool, token: str, group: str,
               profile: dict | None = None) -> dict:
    return job_dto(row, revealed, token, group, profile)


_COMPANY_SAFE = ("country", "industry", "employees", "employee_range",
                 "n_tecnologie", "open_jobs_count")
_COMPANY_REVEALED = _COMPANY_SAFE + (
    "company", "company_slug", "ats", "domain", "logo", "website", "active_jobs",
)


def company_dto(row: dict, revealed: bool, token: str, group: str) -> dict:
    fields = _COMPANY_REVEALED if revealed else _COMPANY_SAFE
    out = {key: row[key] for key in fields if key in row and row[key] is not None}
    out.update({"revealed": revealed, "reveal_token": token, "reveal_group": group})
    return out
