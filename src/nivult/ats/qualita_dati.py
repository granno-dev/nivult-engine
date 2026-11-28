"""Regole deterministiche per il ciclo di vita e il Delta delle offerte."""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

DATA_MINIMA = datetime(2020, 1, 1, tzinfo=UTC)
FUTURO_MASSIMO = timedelta(days=2)
GIORNI_LAST_SEEN = 7
CONFERME_ASSENZA = 2
GIORNI_ZOMBIE = 365
RIFIUTO_TENANT_MINIMO = 100
RIFIUTO_TENANT_QUOTA = 0.30


def sana_data(value: Any, *, adesso: datetime | None = None) -> datetime | None:
    """Accetta solo date di pubblicazione plausibili e le normalizza in UTC."""
    if value is None or isinstance(value, bool):
        return None
    ora = adesso or datetime.now(UTC)
    if ora.tzinfo is None:
        ora = ora.replace(tzinfo=UTC)
    try:
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if not math.isfinite(timestamp):
                return None
            if timestamp > 1e12:
                timestamp /= 1000
            parsed = datetime.fromtimestamp(timestamp, tz=UTC)
        else:
            parsed = datetime.fromisoformat(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        return None
    parsed = parsed.astimezone(UTC)
    if parsed < DATA_MINIMA or parsed > ora + FUTURO_MASSIMO:
        return None
    return parsed


def crollo_sospetto(
    attive_prima: int,
    trovate: int,
    *,
    minimo: int = RIFIUTO_TENANT_MINIMO,
    quota: float = RIFIUTO_TENANT_QUOTA,
) -> bool:
    """Un crollo grande di un singolo tenant non è una fotografia affidabile."""
    if attive_prima <= 0 or trovate < 0 or trovate >= attive_prima:
        return False
    mancanti = attive_prima - trovate
    return mancanti >= minimo and mancanti > attive_prima * quota


def decisione_ciclo_vita(
    *,
    last_seen: datetime,
    active_since: datetime,
    adesso: datetime,
    assenze_consecutive: int,
    prima_assenza: datetime | None,
) -> Literal["attiva", "grace_last_seen", "chiusura_assenza", "chiusura_zombie"]:
    """Applica prima il tetto zombie, poi il grace sulle assenze."""
    if active_since < adesso - timedelta(days=GIORNI_ZOMBIE):
        return "chiusura_zombie"
    if last_seen < adesso - timedelta(days=GIORNI_LAST_SEEN):
        if (
            assenze_consecutive >= CONFERME_ASSENZA
            and prima_assenza is not None
            and prima_assenza < adesso - timedelta(days=GIORNI_LAST_SEEN)
        ):
            return "chiusura_assenza"
        return "grace_last_seen"
    return "attiva"


_METADATI_HASH = frozenset(
    {
        "id",
        "first_seen",
        "last_seen",
        "updated_at",
        "closed_at",
        "content_updated_at",
        "created_at",
        "fetched_at",
        "ingested_at",
        "classified_at",
        "normalized_at",
        "profiled_at",
        "calcolato_at",
        "creato_at",
        "ripassata_at",
        "dettaglio_letto",
    }
)


def _canonico(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _canonico(item)
            for key, item in value.items()
            if str(key) not in _METADATI_HASH
        }
    if isinstance(value, str):
        return " ".join(unicodedata.normalize("NFC", value).split())
    if isinstance(value, datetime):
        normalized = value if value.tzinfo else value.replace(tzinfo=UTC)
        return normalized.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        elementi = [_canonico(item) for item in value]
        return sorted(
            elementi,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
            ),
        )
    if isinstance(value, (set, frozenset)):
        elementi = [_canonico(item) for item in value]
        return sorted(
            elementi,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
            ),
        )
    return value


def campi_esportati(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if value not in (None, [], "")}


def content_hash(record: Mapping[str, Any]) -> str:
    """SHA-256 della sola fotografia pubblica e sostanziale dell'offerta."""
    payload = json.dumps(
        _canonico(campi_esportati(record)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class DecisioneUpdated:
    digest: str
    emetti: bool
    motivo: Literal["baseline", "immutato", "cambiato"]


def valuta_updated(
    hash_precedente: str | None, record: Mapping[str, Any]
) -> DecisioneUpdated:
    """La prima fotografia è una baseline silenziosa, mai un update retroattivo."""
    digest = content_hash(record)
    if hash_precedente is None:
        return DecisioneUpdated(digest, False, "baseline")
    if hash_precedente == digest:
        return DecisioneUpdated(digest, False, "immutato")
    return DecisioneUpdated(digest, True, "cambiato")
