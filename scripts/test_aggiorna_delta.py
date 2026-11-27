import gzip
import json

import duckdb

from nivult.api_clienti import aggiorna, dati


def test_delta_usa_updated_at_senza_rompere_i_cursori(tmp_path, monkeypatch):
    eventi = [
        {
            "event": "updated",
            "id": "job-updated",
            "first_seen": "2026-11-20T09:00:00+00:00",
            "updated_at": "2026-11-27T10:00:00+00:00",
        },
        {
            "event": "new",
            "id": "job-new",
            "first_seen": "2026-11-27T10:00:00+00:00",
        },
        {
            "event": "closed",
            "id": "job-closed",
            "closed_at": "2026-11-27T11:00:00+00:00",
        },
    ]
    export = tmp_path / "novita-20261127-120000.jsonl.gz"
    with gzip.open(export, "wt", encoding="utf-8") as f:
        for evento in eventi:
            f.write(json.dumps(evento) + "\n")

    con = duckdb.connect(":memory:")
    aggiorna._crea_tabelle(con)
    caricati = aggiorna._carica_nativa(
        con, str(export), "flusso", aggiorna._COLONNE_FLUSSO, dove="t IS NOT NULL"
    )
    assert caricati == len(eventi)

    monkeypatch.setattr(dati, "_conn", lambda: con)
    monkeypatch.setattr(dati, "_meta", dict)
    prima, cursore = dati.cambiamenti("2026-11-26T00:00:00+00:00", None, 2)
    assert [r["event"] for r in prima] == ["new", "updated"]
    assert prima[1]["updated_at"] == "2026-11-27T10:00:00+00:00"
    assert isinstance(dati._leggi_cursore(cursore), list)

    seconda, cursore_finale = dati.cambiamenti(None, cursore, 2)
    assert [r["event"] for r in seconda] == ["closed"]
    assert cursore_finale is None

    successive, _ = dati.cambiamenti("2026-11-27T10:30:00+00:00", None, 10)
    assert [r["event"] for r in successive] == ["closed"]
