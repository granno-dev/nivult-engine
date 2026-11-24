from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import ClassVar

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ["ATS_DATABASE_URL"] = "postgresql://test@127.0.0.1/nivult_ats"
sys.path.insert(0, str(ROOT / "scripts"))

import reindex_v2 as modulo


class Response:
    def __init__(self, payload=None):
        self.payload = payload or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    chiamate: ClassVar[list] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def get(self, path):
        if path.endswith("/_count"):
            return Response({"count": 0})
        return Response({"nivult_jobs_v1": {"settings": {"index": {"refresh_interval": "5s"}}}})

    def put(self, path, json):
        self.chiamate.append(("PUT", path, json))
        return Response()

    def post(self, path, **kwargs):
        self.chiamate.append(("POST", path, kwargs))
        return Response({"count": 0})


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, *args, **kwargs):
        return None


def _stato_corrente():
    return {
        "t0": "2026-11-24T15:19:20.033688+00:00",
        "last_id": "7f14033d-986a-497e-997f-a3935220dd5e",
        "completed_prefix_until": "3e9e09f8-7cec-4a47-9d56-0c0357af25b6",
        "indexed": 2_987_426,
        "errors": 0,
        "status": "running",
        "workers": [
            {"worker": 0, "start": "3e9e09f8-7cec-4a47-9d56-0c0357af25b6",
             "end": "7f1406a5-a89d-86da-68e4-08023a74c3ce",
             "cursor": "7f14033d-986a-497e-997f-a3935220dd5e", "done": True},
            {"worker": 1, "start": "7f1406a5-a89d-86da-68e4-08023a74c3ce",
             "end": "bf8a0352-d44e-c36d-3472-04011d3a61e6",
             "cursor": "7f22fb6e-35c0-4c21-9b89-a603493b23a7", "done": False},
            {"worker": 2, "start": "bf8a0352-d44e-c36d-3472-04011d3a61e6",
             "end": "ffffffff-ffff-ffff-ffff-ffffffffffff",
             "cursor": "bf989104-7dee-4b46-9f6d-187ad2077fbd", "done": False},
        ],
    }


def test_resume_uuid_multibatch_e_finalize(monkeypatch, tmp_path):
    stato_file = tmp_path / "state.json"
    stato_file.write_text(json.dumps(_stato_corrente()))
    monkeypatch.setattr(modulo, "STATE", stato_file)
    monkeypatch.setattr(modulo.psycopg, "connect", lambda *args, **kwargs: FakeConn())
    monkeypatch.setattr(modulo.httpx, "Client", FakeClient)
    FakeClient.chiamate = []
    contatori = {}
    righe = {}

    for worker in (1, 2):
        fine = _stato_corrente()["workers"][worker]["end"]
        base = uuid.UUID(_stato_corrente()["workers"][worker]["cursor"]).int
        righe[fine] = [[{"id": uuid.UUID(int=base + n * 1500 + i)} for i in range(1, 1501)]
                       for n in range(3)]

    def query(conn, inizio, fine, limite):
        assert isinstance(inizio, uuid.UUID)
        assert isinstance(fine, uuid.UUID)
        chiave = str(fine)
        n = contatori.get(chiave, 0)
        contatori[chiave] = n + 1
        return righe[chiave][n] if n < len(righe[chiave]) else []

    monkeypatch.setattr(modulo, "_query_batch", query)
    monkeypatch.setattr(modulo, "_documenti", lambda righe: {
        str(r["id"]): {"id": str(r["id"])} for r in righe})
    monkeypatch.setattr(modulo, "_bulk", lambda client, documenti, stop_event: 0)
    monkeypatch.setattr(sys, "argv", ["reindex_v2.py", "--resume", "--finalize"])

    assert modulo.main() == 0
    finale = json.loads(stato_file.read_text())
    assert all(worker["done"] for worker in finale["workers"])
    assert finale["status"] == "complete"
    assert finale["t0"] == "2026-11-24T15:19:20.033688+00:00"
    assert finale["completed_prefix_until"] == "3e9e09f8-7cec-4a47-9d56-0c0357af25b6"
    assert len(contatori) == 2 and all(n >= 4 for n in contatori.values())
    assert ("PUT", "/nivult_jobs_v2/_settings",
            {"index": {"refresh_interval": "5s"}}) in FakeClient.chiamate
    assert any(c[:2] == ("POST", "/nivult_jobs_v2/_refresh") for c in FakeClient.chiamate)


def test_worker_errore_attiva_stop_event(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(modulo, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(modulo.psycopg, "connect", lambda *args, **kwargs: FakeConn())
    monkeypatch.setattr(modulo.httpx, "Client", FakeClient)
    monkeypatch.setattr(modulo, "_query_batch", lambda *args: (_ for _ in ()).throw(
        RuntimeError("errore simulato")))
    stato = _stato_corrente()
    stato["workers"] = [stato["workers"][1]]
    stato["run_indexed_start"] = stato["indexed"]
    stop = threading.Event()
    with pytest.raises(RuntimeError, match="errore simulato"):
        modulo._worker(stato, 0, modulo.time.monotonic(), stop)
    assert stop.is_set()
    assert "Traceback (most recent call last)" in capsys.readouterr().err
