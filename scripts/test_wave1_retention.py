import json
import re
from pathlib import Path

import duckdb
import pytest

from nivult.api_clienti import retention

ROOT = Path(__file__).resolve().parents[1]


def _pair(cartella: Path, data: str, *, campione: bool = False, vuota: bool = False) -> set[str]:
    nomi = set()
    infix = "-campione" if campione else ""
    for tipo in retention._TIPI:
        nome = f"{tipo}{infix}-{data}.jsonl.gz"
        (cartella / nome).write_bytes(b"" if vuota else b"{}\n")
        nomi.add(nome)
    return nomi


def _db(percorso: Path, offerte: Path, aziende: Path) -> None:
    con = duckdb.connect(str(percorso))
    con.execute("CREATE TABLE meta(chiave VARCHAR, valore VARCHAR)")
    con.executemany(
        "INSERT INTO meta VALUES (?, ?)",
        [("file_offerte", str(offerte)), ("file_aziende", str(aziende))],
    )
    con.close()


def _manifest(cartella: Path, offerte: Path, aziende: Path) -> None:
    (cartella / "manifest-ultimo.json").write_text(
        json.dumps({"files": {"jobs": str(offerte), "companies": str(aziende)}}),
        encoding="utf-8",
    )


def _tutti_esistono(cartella: Path, nomi: set[str]) -> bool:
    return all((cartella / nome).exists() for nome in nomi)


def test_protegge_attivo_precedente_e_campione_e_cancella_solo_vecchio(
    tmp_path, capsys
):
    export = tmp_path / "exports"
    export.mkdir()
    vecchia = _pair(export, "2026-11-25")
    precedente = _pair(export, "2026-11-26")
    attiva = _pair(export, "2026-11-27")
    campione = _pair(export, "2026-11-27", campione=True)
    vuota = _pair(export, "2026-11-28", vuota=True)
    filtrati = {
        "offerte-attive-filtrate-2026-11-20.jsonl.gz",
        "aziende-segnali-filtrate-2026-11-20.jsonl.gz",
    }
    for nome in filtrati:
        (export / nome).write_bytes(b"{}\n")
    flusso = export / "flusso"
    flusso.mkdir()
    delta = flusso / "novita-20261120-120000.jsonl.gz"
    delta.write_bytes(b"{}\n")

    attive_path = export / "offerte-attive-2026-11-27.jsonl.gz"
    aziende_path = export / "aziende-segnali-2026-11-27.jsonl.gz"
    _manifest(export, attive_path, aziende_path)
    db = tmp_path / "clienti.duckdb"
    _db(db, attive_path, aziende_path)

    assert retention.main(
        ["--exports", str(export), "--db", str(db), "--protected"]
    ) == 0
    protetti = set(capsys.readouterr().out.splitlines())
    assert attiva | precedente | campione <= protetti
    assert not vecchia & protetti

    eliminati = retention.esegui_retention(export, db)
    assert set(eliminati) == vecchia
    assert _tutti_esistono(export, attiva | precedente | campione | vuota | filtrati)
    assert delta.exists()


def test_manifest_rotto_blocca_qualunque_cancellazione(tmp_path):
    export = tmp_path / "exports"
    export.mkdir()
    nomi = _pair(export, "2026-11-25") | _pair(export, "2026-11-26")
    nomi |= _pair(export, "2026-11-27")
    offerte = export / "offerte-attive-2026-11-27.jsonl.gz"
    aziende = export / "aziende-segnali-2026-11-27.jsonl.gz"
    db = tmp_path / "clienti.duckdb"
    _db(db, offerte, aziende)
    (export / "manifest-ultimo.json").write_text("{non-json", encoding="utf-8")

    with pytest.raises(retention.RetentionError, match="manifest"):
        retention.esegui_retention(export, db)
    assert _tutti_esistono(export, nomi)


def test_duckdb_rotto_blocca_qualunque_cancellazione(tmp_path):
    export = tmp_path / "exports"
    export.mkdir()
    nomi = _pair(export, "2026-11-25") | _pair(export, "2026-11-26")
    nomi |= _pair(export, "2026-11-27")
    offerte = export / "offerte-attive-2026-11-27.jsonl.gz"
    aziende = export / "aziende-segnali-2026-11-27.jsonl.gz"
    _manifest(export, offerte, aziende)
    db = tmp_path / "clienti.duckdb"
    db.write_bytes(b"questo non e' un DuckDB")

    with pytest.raises(retention.RetentionError, match="DuckDB"):
        retention.esegui_retention(export, db)
    assert _tutti_esistono(export, nomi)


def test_cron_non_ha_delete_non_protetti_e_pulisce_le_vecchie_righe():
    testo = (ROOT / "deploy/cron.sh").read_text(encoding="utf-8")
    righe = re.search(r"RIGHE=\$\(cat <<'EOF'\n(.*?)\nEOF", testo, re.DOTALL).group(1)
    obsolete = re.search(
        r"OBSOLETE=\$\(cat <<'EOF'\n(.*?)\nEOF", testo, re.DOTALL
    ).group(1)
    vecchie = [
        '15 6 * * * find /opt/nivult/exports -name "*.jsonl.gz" -mtime +7 -delete',
        '50 6 * * * find /opt/nivult/exports -type f -name "offerte-attive-*.jsonl.gz" -mtime +1 -delete',
        '15 6 * * * find /opt/nivult/exports -maxdepth 1 -name "offerte-*-2026*.jsonl.gz" -mtime +2 -delete; find /opt/nivult/exports -maxdepth 1 -name "aziende-segnali-2026*.jsonl.gz" -mtime +2 -delete',
    ]
    for riga in vecchie:
        assert riga not in righe
        assert riga in obsolete.splitlines()
    for riga in righe.splitlines():
        if "-delete" in riga:
            assert "novita-" in riga
    builder = next(riga for riga in righe.splitlines() if "nivult.api_clienti.aggiorna" in riga)
    assert builder.startswith("30 13 ")
    protetta = next(riga for riga in righe.splitlines() if "mantieni-export.sh" in riga)
    assert protetta.startswith("30 15 ")
    assert "archivia-sul-n5.sh" in protetta
    assert " && " in protetta


def test_archivio_e_spazzino_hanno_guardie_statiche():
    archivio = (ROOT / "deploy/archivia-sul-n5.sh").read_text(encoding="utf-8")
    assert "if ! protetti=" in archivio
    assert "--protected" in archivio
    assert "PROTETTI[$nome]" in archivio
    assert archivio.index('$SCP "$f"') < archivio.index('rm -f "$f"')

    spazzino = (ROOT / "deploy/spazzino-disco.sh").read_text(encoding="utf-8")
    assert "rimuovi_se_stale" in spazzino
    assert '-type f -mtime +1' in spazzino
    assert "rm -rf" not in spazzino
