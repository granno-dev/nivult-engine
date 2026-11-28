import gzip
import json
from datetime import UTC, datetime

import duckdb
import pytest

from nivult.api_clienti import aggiorna
from nivult.ats import esporta, mantenimento, runner
from nivult.ats.adapters import OracleRecruiting
from nivult.ats.qualita_dati import content_hash, valuta_updated


class Cursor:
    def __init__(self, rows=(), one=None):
        self.rows = list(rows)
        self.one = one
        self.calls = []
        self.itersize = 0
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def __iter__(self):
        return iter(self.rows)

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self

    def executemany(self, sql, batch):
        self.calls.append((sql, list(batch)))
        return self

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, updated=(), report=None, new=()):
        self.updated = list(updated)
        self.new = list(new)
        self.report = report
        self.cursors = []
        self.commits = 0
        self.closed = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def cursor(self, name=None, **kwargs):
        if name == "fl_aggiornate":
            cur = Cursor(self.updated)
        elif name == "fl_nuove":
            cur = Cursor(self.new)
        elif self.report is not None:
            cur = Cursor(one=self.report)
        else:
            cur = Cursor()
        self.cursors.append(cur)
        return cur

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed += 1


def _row(hash_precedente=None, title="Data Engineer"):
    row = [None] * 53
    values = {
        0: "6c2c38de-7bb4-4f1e-b72b-92df9a0d06f1",
        1: title,
        2: "greenhouse",
        3: "example",
        4: "Example",
        5: "https://example.com/jobs/1",
        6: "US",
        7: "New York",
        8: "New York",
        9: "en",
        10: "mid",
        11: "hybrid",
        12: ["Python"],
        16: datetime(2026, 11, 25, tzinfo=UTC),
        17: datetime(2026, 11, 20, tzinfo=UTC),
        18: datetime(2026, 11, 25, tzinfo=UTC),
        19: "Build and maintain production data pipelines with enough text.",
        20: "Data & Analytics",
        21: "full_time",
        22: ["en"],
        23: "job-1",
        24: "Data",
        25: "full_time",
        26: "permanent",
        27: "year",
        28: "observed",
        29: False,
        30: ["Python"],
        33: None,
        34: False,
        38: [],
        41: "NY",
        42: "10001",
        48: [],
        49: datetime(2026, 11, 25, tzinfo=UTC),
        50: "direct",
        51: "example.com",
        52: hash_precedente,
    }
    for index, value in values.items():
        row[index] = value
    return row


def test_flusso_dry_run_crea_baseline_logica_senza_effetti(tmp_path, monkeypatch):
    conn = Connection(updated=[_row()])
    monkeypatch.setattr(esporta.psycopg, "connect", lambda dsn: conn)
    monkeypatch.setattr(esporta, "CARTELLA", str(tmp_path / "exports"))

    stats = esporta.flusso("postgresql://local-test", dry_run=True)

    assert stats["aggiornate"] == 0
    assert stats["hash_baseline"] == 1
    assert conn.commits == 0
    assert not (tmp_path / "exports").exists()
    sql = "\n".join(call[0] for cur in conn.cursors for call in cur.calls)
    assert "INSERT INTO ats_delta_hash" not in sql


def test_expira_dry_run_non_scrive_ne_interroga_le_pagine(monkeypatch):
    report = ([{"piattaforma": "oracle/careers.marriott.com", "scadrebbero": 18477, "attive": 20000}], 0)
    conn = Connection(report=report)
    monkeypatch.setattr(mantenimento.psycopg, "connect", lambda dsn: conn)
    monkeypatch.setattr(
        mantenimento,
        "_prova_vita_jsonld",
        lambda *args: (_ for _ in ()).throw(AssertionError("probe HTTP nel dry-run")),
    )
    monkeypatch.setattr(
        mantenimento,
        "_segna_rifiuto",
        lambda *args: (_ for _ in ()).throw(AssertionError("file incidente nel dry-run")),
    )

    assert mantenimento.expira("postgresql://local-test", dry_run=True) == 0
    assert conn.commits == 0
    assert len(conn.cursors) == 1
    assert len(conn.cursors[0].calls) == 1


def test_quarantena_crollo_non_avanza_last_ok():
    conn = Connection()
    conn.cursor = lambda *args, **kwargs: Cursor(one=(20_000,))
    calls = []

    class QuarantineCursor(Cursor):
        def execute(self, sql, params=None):
            calls.append(sql)
            return super().execute(sql, params)

    cur = QuarantineCursor(one=(20_000,))
    conn.cursor = lambda *args, **kwargs: cur

    assert runner._quarantena_crollo(conn, {"platform_id": "oracle", "slug": "careers.marriott.com"}, 1523)
    assert conn.commits == 1
    assert any("letture_sospette" in sql for sql in calls)
    assert any("lettura_parziale = true" in sql for sql in calls)
    assert not any("last_ok_at" in sql for sql in calls)


def test_oracle_template_irriconoscibile_non_e_una_bacheca_vuota_affidabile():
    class Client:
        def get(self, url):
            return type("Response", (), {"text": "<html>nuovo template</html>"})()

    adapter = object.__new__(OracleRecruiting)
    adapter.client = Client()
    adapter.lettura_parziale = False

    assert adapter.jobs("careers.marriott.com") == []
    assert adapter.lettura_parziale is True


def test_hash_filtra_gli_update_prima_del_caricamento_delta(tmp_path):
    record = {"id": "job-1", "title": "Engineer", "description": "Build", "last_seen": "2026-11-27"}
    baseline = valuta_updated(None, record)
    eventi = []
    if baseline.emetti:
        eventi.append({"event": "updated", **record})

    rumore = dict(record, last_seen="2026-11-28", updated_at="2026-11-28T10:00:00Z")
    if valuta_updated(baseline.digest, rumore).emetti:
        eventi.append({"event": "updated", **rumore})

    cambiato = dict(rumore, title="Senior Engineer")
    decisione = valuta_updated(baseline.digest, cambiato)
    assert decisione.digest != content_hash(record)
    if decisione.emetti:
        eventi.append({"event": "updated", **cambiato})

    export = tmp_path / "novita-test.jsonl.gz"
    with gzip.open(export, "wt", encoding="utf-8") as handle:
        for evento in eventi:
            handle.write(json.dumps(evento) + "\n")

    con = duckdb.connect(":memory:")
    aggiorna._crea_tabelle(con)
    caricati = aggiorna._carica_nativa(
        con, str(export), "flusso", aggiorna._COLONNE_FLUSSO, dove="t IS NOT NULL"
    )
    assert caricati == 1
    assert con.execute("SELECT json_extract_string(raw, '$.title') FROM flusso").fetchone()[0] == "Senior Engineer"


def test_new_inizializza_hash_e_primo_update_viene_pubblicato(tmp_path, monkeypatch):
    monkeypatch.setattr(esporta, "CARTELLA", str(tmp_path / "exports"))
    prima = Connection(new=[_row()])
    monkeypatch.setattr(esporta.psycopg, "connect", lambda dsn: prima)

    stats_new = esporta.flusso("postgresql://local-test")
    assert stats_new["nuove"] == 1
    assert prima.commits == 1
    hash_sql = [
        (sql, params)
        for cur in prima.cursors
        for sql, params in cur.calls
        if "INSERT INTO ats_delta_hash" in sql
    ]
    assert len(hash_sql) == 1
    hash_salvato = hash_sql[0][1][0][1]

    seconda = Connection(updated=[_row(hash_salvato, title="Senior Data Engineer")])
    monkeypatch.setattr(esporta.psycopg, "connect", lambda dsn: seconda)
    stats_update = esporta.flusso("postgresql://local-test")

    assert stats_update["aggiornate"] == 1
    assert stats_update["hash_baseline"] == 0
    assert seconda.commits == 1


def test_errore_pubblicazione_non_committa_hash(tmp_path, monkeypatch):
    conn = Connection(updated=[_row("hash-precedente", title="Changed")])
    monkeypatch.setattr(esporta, "CARTELLA", str(tmp_path / "exports"))
    monkeypatch.setattr(esporta.psycopg, "connect", lambda dsn: conn)
    monkeypatch.setattr(
        esporta.os,
        "replace",
        lambda *args: (_ for _ in ()).throw(OSError("disco pieno")),
    )

    with pytest.raises(OSError, match="disco pieno"):
        esporta.flusso("postgresql://local-test")

    assert conn.commits == 0
    assert conn.closed == 1
    assert list(tmp_path.rglob("*.tmp")) == []


def test_breaker_delta_blocca_file_e_cursore(tmp_path, monkeypatch):
    conn = Connection()
    monkeypatch.setattr(esporta, "CARTELLA", str(tmp_path / "exports"))
    monkeypatch.setattr(esporta.psycopg, "connect", lambda dsn: conn)
    monkeypatch.setattr(
        esporta,
        "_chiusure_massive_delta",
        lambda *args: [("oracle/careers.marriott.com", 18477, 20000)],
    )

    with pytest.raises(RuntimeError, match="Delta fermo"):
        esporta.flusso("postgresql://local-test")

    assert conn.commits == 0
    assert conn.closed == 1
    assert not (tmp_path / "exports" / "flusso" / "cursore.json").exists()
    assert list(tmp_path.rglob("*.tmp")) == []


def test_update_scadenza_ha_guardia_ottimistica(monkeypatch):
    conn = Connection(report=([], 0))
    monkeypatch.setattr(mantenimento.psycopg, "connect", lambda dsn: conn)
    monkeypatch.setattr(mantenimento, "_prova_vita_jsonld", lambda *args: (0, 0))
    monkeypatch.setattr(mantenimento, "_segna_rifiuto", lambda *args: None)

    assert mantenimento.expira("postgresql://local-test") == 0
    sql = "\n".join(call[0] for cur in conn.cursors for call in cur.calls)
    assert "j.fetched_at = s.fetched_at" in sql
    assert "j.active_since = s.active_since" in sql
    assert conn.commits == 1


def test_commit_fallito_non_avanza_il_cursore_legacy(tmp_path, monkeypatch):
    cartella = tmp_path / "exports" / "flusso"
    cartella.mkdir(parents=True)
    cursore = cartella / "cursore.json"
    cursore.write_text(json.dumps({"t": "2026-11-28T10:00:00Z"}), encoding="utf-8")
    conn = Connection(updated=[_row("hash-precedente", title="Changed")])
    monkeypatch.setattr(esporta, "CARTELLA", str(tmp_path / "exports"))
    monkeypatch.setattr(esporta.psycopg, "connect", lambda dsn: conn)
    monkeypatch.setattr(
        conn,
        "commit",
        lambda: (_ for _ in ()).throw(RuntimeError("commit fallito")),
    )

    with pytest.raises(RuntimeError, match="commit fallito"):
        esporta.flusso("postgresql://local-test")

    assert json.loads(cursore.read_text(encoding="utf-8"))["t"] == "2026-11-28T10:00:00Z"
    assert conn.closed == 1
    sql = "\n".join(call[0] for cur in conn.cursors for call in cur.calls)
    assert "INSERT INTO ats_delta_state" in sql
