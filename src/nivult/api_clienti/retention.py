"""Retention protetta degli snapshot giornalieri dei clienti."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import duckdb

EXPORT_DIR = "/opt/nivult/exports"
API_CLIENTI_DB = "/mnt/HC_Volume_106941692/api-clienti.duckdb"

_TIPI = ("offerte-attive", "aziende-segnali")
_SNAPSHOT_RX = re.compile(
    r"^(?P<tipo>offerte-attive|aziende-segnali)-(?P<data>\d{4}-\d{2}-\d{2})"
    r"(?:-[0-9a-f]{12})?\.jsonl\.gz$"
)
_CAMPIONE_RX = re.compile(
    r"^(?P<tipo>offerte-attive|aziende-segnali)-campione-"
    r"(?P<data>\d{4}-\d{2}-\d{2})(?:-[0-9a-f]{12})?\.jsonl\.gz$"
)


class RetentionError(RuntimeError):
    """Metadati illeggibili o incoerenti: la retention deve restare ferma."""


def _nomi_nel_manifest(valore: Any) -> set[str]:
    nomi: set[str] = set()
    if isinstance(valore, str):
        if valore.endswith(".jsonl.gz"):
            nomi.add(Path(valore).name)
    elif isinstance(valore, dict):
        for figlio in valore.values():
            nomi.update(_nomi_nel_manifest(figlio))
    elif isinstance(valore, (list, tuple)):
        for figlio in valore:
            nomi.update(_nomi_nel_manifest(figlio))
    return nomi


def _leggi_manifest(cartella: Path) -> set[str]:
    percorso = cartella / "manifest-ultimo.json"
    try:
        with percorso.open(encoding="utf-8") as f:
            manifest = json.load(f)
        if not isinstance(manifest, dict):
            raise TypeError("la radice non e' un oggetto")
        if "files" in manifest and not isinstance(manifest["files"], dict):
            raise TypeError("files non e' una mappa")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise RetentionError(f"manifest illeggibile: {percorso}: {exc}") from exc
    return _nomi_nel_manifest(manifest)


def _leggi_meta(db_path: Path) -> set[str]:
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            righe = con.execute(
                "SELECT valore FROM meta WHERE chiave IN "
                "('file_offerte', 'file_aziende')"
            ).fetchall()
        finally:
            con.close()
    except (duckdb.Error, OSError) as exc:
        raise RetentionError(f"DuckDB illeggibile: {db_path}: {exc}") from exc
    return {Path(valore).name for (valore,) in righe if valore}


def _coppie(cartella: Path, *, campione: bool) -> list[tuple[str, set[str]]]:
    rx = _CAMPIONE_RX if campione else _SNAPSHOT_RX
    per_data: dict[str, dict[str, str]] = {}
    try:
        for percorso in cartella.iterdir():
            match = rx.fullmatch(percorso.name)
            if not match:
                continue
            try:
                dt.date.fromisoformat(match.group("data"))
                valido = percorso.is_file() and percorso.stat().st_size > 0
            except (OSError, ValueError):
                continue
            if valido:
                per_data.setdefault(match.group("data"), {})[match.group("tipo")] = percorso.name
    except OSError as exc:
        raise RetentionError(f"export illeggibili: {cartella}: {exc}") from exc
    return [
        (data, set(tipi.values()))
        for data, tipi in sorted(per_data.items(), reverse=True)
        if set(tipi) == set(_TIPI)
    ]


def _pin_campione_corrente(cartella: Path) -> set[str]:
    protetti: set[str] = set()
    for tipo in _TIPI:
        percorso = cartella / f"{tipo}-campione-ultimo.jsonl.gz"
        try:
            if not percorso.is_file() or percorso.stat().st_size == 0:
                continue
            protetti.add(percorso.name)
            protetti.add(percorso.resolve().name)
        except OSError:
            continue
    return protetti


def file_protetti(cartella: str | Path, db_path: str | Path) -> set[str]:
    """Basenames da non archiviare ne' cancellare; solleva errore in fail-closed."""
    cartella = Path(cartella)
    protetti = _leggi_manifest(cartella)
    protetti.update(_leggi_meta(Path(db_path)))
    for _, nomi in _coppie(cartella, campione=False)[:2]:
        protetti.update(nomi)
    for _, nomi in _coppie(cartella, campione=True)[:1]:
        protetti.update(nomi)
    protetti.update(_pin_campione_corrente(cartella))
    return protetti


def esegui_retention(cartella: str | Path, db_path: str | Path) -> list[str]:
    """Elimina solo coppie giornaliere complete piu' vecchie delle due protette."""
    cartella = Path(cartella)
    protetti = file_protetti(cartella, db_path)
    eliminati: list[str] = []
    for _, nomi in _coppie(cartella, campione=False)[2:]:
        if nomi & protetti:
            continue
        for nome in sorted(nomi):
            try:
                (cartella / nome).unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise RetentionError(f"non posso eliminare {nome}: {exc}") from exc
            eliminati.append(nome)
    return eliminati


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exports", "--cartella", dest="cartella",
                        default=os.environ.get("EXPORT_DIR", EXPORT_DIR))
    parser.add_argument("--db", dest="db_path",
                        default=os.environ.get("API_CLIENTI_DB", API_CLIENTI_DB))
    modalita = parser.add_mutually_exclusive_group()
    modalita.add_argument("--protected", "--protetti", action="store_true",
                          help="stampa un basename protetto per riga (default)")
    modalita.add_argument("--apply", action="store_true",
                          help="elimina le coppie giornaliere non protette")
    args = parser.parse_args(argv)
    try:
        if args.apply:
            for nome in esegui_retention(args.cartella, args.db_path):
                print(f"rimosso {nome}")
        else:
            for nome in sorted(file_protetti(args.cartella, args.db_path)):
                print(nome)
    except RetentionError as exc:
        print(f"retention: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
