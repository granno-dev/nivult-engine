import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

MODULE_PATH = Path(__file__).parents[1] / "apify" / "src" / "main.py"
SPEC = importlib.util.spec_from_file_location("nivult_apify_actor", MODULE_PATH)
actor_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(actor_module)


class FakeLog:
    def info(self, *args):
        pass


class FakeActor:
    def __init__(self, inp, events, budget_at=None):
        self.inp = inp
        self.events = events
        self.budget_at = budget_at
        self.rows = []
        self.push_calls = []
        self.summary = None
        self.log = FakeLog()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get_input(self):
        return self.inp

    def get_charging_manager(self):
        return self

    def get_pricing_info(self):
        return SimpleNamespace(per_event_prices=self.events)

    async def push_data(self, data, *, charged_event_name=None):
        self.push_calls.append((data, charged_event_name))
        if charged_event_name is None:
            self.rows.extend(data)
            return None
        self.rows.append(data)
        reached = self.budget_at is not None and len(self.rows) >= self.budget_at
        return SimpleNamespace(charged_count=1, event_charge_limit_reached=reached)

    async def set_value(self, key, value):
        self.summary = value


class FakeResponse:
    def __init__(self, rows, next_cursor):
        self.payload = {"data": rows, "next_cursor": next_cursor}

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, page_size, always_next):
        self.page_size = page_size
        self.always_next = always_next
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, url, *, params, headers):
        self.calls.append((url, params, headers))
        rows = [{"id": str(len(self.calls)), "index": i}
                for i in range(params["limit"])]
        return FakeResponse(rows, "next" if self.always_next else None)


def run_actor(monkeypatch, inp, events, *, budget_at=None, page_size=2,
              always_next=False):
    actor = FakeActor(inp, events, budget_at)
    client = FakeClient(page_size, always_next)
    monkeypatch.setattr(actor_module, "Actor", actor)
    monkeypatch.setattr(actor_module.httpx, "AsyncClient",
                        lambda *args, **kwargs: client)
    monkeypatch.setenv("NIVULT_API_KEY", "test-only")
    asyncio.run(actor_module.main())
    return actor, client


def test_legacy_dataset_item_enforces_cap_without_custom_event(monkeypatch):
    actor, client = run_actor(
        monkeypatch, {"tipo": "jobs", "limite": 10_000},
        {"apify-default-dataset-item": 1}, page_size=500, always_next=True)
    assert len(actor.rows) == 5_000
    assert actor.summary["righe"] == 5_000
    assert len(client.calls) == 10
    assert all(event is None for _, event in actor.push_calls)


def test_custom_job_event_charges_each_item(monkeypatch):
    actor, _ = run_actor(monkeypatch, {"tipo": "jobs", "limite": 2},
                         {"job-result": 1})
    assert len(actor.rows) == 2
    assert [event for _, event in actor.push_calls] == ["job-result"] * 2
    assert all(isinstance(data, dict) for data, _ in actor.push_calls)


def test_custom_company_event_stops_cleanly_at_user_budget(monkeypatch):
    actor, _ = run_actor(monkeypatch, {"tipo": "companies", "limite": 3},
                         {"company-result": 1}, budget_at=2, page_size=3)
    assert len(actor.rows) == 2
    assert actor.summary["righe"] == 2
    assert [event for _, event in actor.push_calls] == ["company-result"] * 2
