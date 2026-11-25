from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["ATS_DATABASE_URL"] = "postgresql://test@127.0.0.1/nivult_ats"
sys.path.insert(0, str(ROOT / "scripts"))

import catchup_v2 as modulo


def test_catchup_multibatch_uuid(monkeypatch, tmp_path):
    stato_file = tmp_path / "catchup.json"
    monkeypatch.setattr(modulo, "STATE", stato_file)
    monkeypatch.setattr(modulo, "_attendi", lambda event: None)
    a = uuid.UUID("10000000-0000-0000-0000-000000000000")
    b = uuid.UUID("20000000-0000-0000-0000-000000000000")
    chiamate = []
    lotti = [[{"id": a, "expired_at": None}], [{"id": b, "expired_at": datetime.now(UTC)}], []]

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def execute(self, *args, **kwargs): return None

    class Client:
        def __enter__(self): return self
        def __exit__(self, *args): return None

    monkeypatch.setattr(modulo.psycopg, "connect",
                        lambda *args, **kwargs: kwargs["row_factory"] is modulo.dict_row
                        and Conn())
    monkeypatch.setattr(modulo.httpx, "Client", lambda *args, **kwargs: Client())
    monkeypatch.setattr(modulo, "_batch_ids", lambda conn, da, fine, cursor: lotti.pop(0))
    monkeypatch.setattr(modulo.opensearch_sync, "_righe",
                        lambda ids: [{"id": uuid.UUID(ids[0])}])
    monkeypatch.setattr(modulo.opensearch_sync, "_documenti", lambda righe: {
        str(r["id"]): {"id": str(r["id"]), "title": "test"} for r in righe})
    monkeypatch.setattr(modulo, "_azioni", lambda client, azioni, event: chiamate.extend(azioni) or 0)

    stato = modulo.run(datetime(2026, 1, 1, tzinfo=UTC),
                       datetime(2026, 1, 2, tzinfo=UTC))
    assert stato["status"] == "complete"
    assert stato["errors"] == 0
    assert stato["upsert"] == 1 and stato["delete"] == 1
    assert json.loads(stato_file.read_text())["cursor"] == str(b)
    assert [op for op, _ in chiamate] == ["index", "delete"]
