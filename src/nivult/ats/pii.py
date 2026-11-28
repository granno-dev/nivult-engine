from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"

_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])"
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}"
    r"(?![A-Za-z0-9-])"
)

_PHONE_RE = re.compile(
    r"""
    (?<![\w])
    (?<![A-Za-z0-9][/_-])
    (?:
        (?:\+|00)[ \t]*[0-9](?:[ \t().-]*[0-9]){8,14}
        |
        \([0-9]{3}\)[ \t]*[0-9]{3}-[0-9]{4}
        |
        (?P<label>(?:phone|telephone|tel|mobile|whatsapp|telefono|telefon|telefoon|telefone|teléfono|cellulare)\b[ \t]*[.:=-]?[ \t]*)
        [0-9](?:[ \t().-]*[0-9]){8,14}
    )
    (?![\w])
    """,
    re.IGNORECASE | re.VERBOSE,
)

_REMOVED_KEYS = frozenset({"contact_email", "recruiter"})
_FREE_TEXT_KEYS = frozenset(
    {"about", "body", "company", "description", "salary_text", "summary", "title"}
)


def redact_emails(text: str) -> str:
    """Replace every email address with the common redaction marker."""
    return _EMAIL_RE.sub(REDACTED, text)


def redact_phones(text: str) -> str:
    """Redact international, parenthesized NANP, and explicitly labeled numbers."""
    return _PHONE_RE.sub(lambda match: f"{match.group('label') or ''}{REDACTED}", text)


def redact_pii(text: str) -> str:
    """Redact email addresses and supported phone numbers from free text."""
    return redact_phones(redact_emails(text))


def contains_pii(text: str) -> bool:
    """Return whether the shared redaction rules find PII in the text."""
    return redact_pii(text) != text


def _key_name(key: Any) -> str:
    return key.casefold() if isinstance(key, str) else ""


def _sanitize(value: Any, redact_strings: bool) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize(item, _key_name(key) in _FREE_TEXT_KEYS)
            for key, item in value.items()
            if _key_name(key) not in _REMOVED_KEYS
        }
    if isinstance(value, list):
        return [_sanitize(item, redact_strings) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize(item, redact_strings) for item in value)
    if isinstance(value, str) and redact_strings:
        return redact_pii(value)
    return value


def sanitize_record(record: Any) -> Any:
    """Return a sanitized copy of a nested record without modifying the input."""
    return _sanitize(record, False)
