from datetime import UTC, datetime, timedelta

import pytest

from nivult.ats.adapters import AtsJob, Vincere, _dt
from nivult.ats.jobposting import _data
from nivult.ats.qualita_dati import (
    content_hash,
    crollo_sospetto,
    decisione_ciclo_vita,
    sana_data,
    valuta_updated,
)

NOW = datetime(2026, 11, 28, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "value",
    [
        "0202-03-19T15:12:12+00:00",
        "2019-12-31T23:59:59+00:00",
        "1901-01-01",
        -1,
        0,
        "not-a-date",
        True,
    ],
)
def test_sana_data_rifiuta_date_corrotte_o_pre_2020(value):
    assert sana_data(value, adesso=NOW) is None


def test_sana_data_accetta_boundary_e_normalizza_epoch():
    assert sana_data("2020-01-01T00:00:00Z", adesso=NOW) == datetime(2020, 1, 1, tzinfo=UTC)
    assert sana_data("2026-11-28T13:00:00+01:00", adesso=NOW) == NOW
    assert sana_data(1_764_000_000_000, adesso=NOW).tzinfo == UTC


def test_sana_data_rifiuta_il_futuro_oltre_due_giorni():
    assert sana_data(NOW + timedelta(days=2), adesso=NOW) == NOW + timedelta(days=2)
    assert sana_data(NOW + timedelta(days=2, seconds=1), adesso=NOW) is None


def test_sanitizzazione_condivisa_dagli_ingressi_ats_e_jsonld():
    job = AtsJob("jsonld", "example", "1", "Role", "https://example.com/1", posted_at="0202-06-30")
    assert job.posted_at is None
    assert _dt("2015-05-28") is None
    assert _data({"datePosted": "0202-03-19", "datePublished": "2026-11-20"}) == datetime(
        2026, 11, 20, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("prima", "dopo", "atteso"),
    [
        (20_000, 1_523, True),
        (11_000, 98, True),
        (1_000, 950, False),
        (100, 0, True),
        (99, 0, False),
        (100, 100, False),
        (100, 120, False),
    ],
)
def test_circuit_breaker_tenant(prima, dopo, atteso):
    assert crollo_sospetto(prima, dopo) is atteso


def _vita(**override):
    values = {
        "last_seen": NOW - timedelta(days=8),
        "active_since": NOW - timedelta(days=100),
        "adesso": NOW,
        "assenze_consecutive": 2,
        "prima_assenza": NOW - timedelta(days=8),
    }
    values.update(override)
    return decisione_ciclo_vita(**values)


def test_last_seen_grace_e_due_conferme():
    assert _vita(assenze_consecutive=1) == "grace_last_seen"
    assert _vita(prima_assenza=NOW - timedelta(days=6)) == "grace_last_seen"
    assert _vita() == "chiusura_assenza"
    assert _vita(last_seen=NOW - timedelta(days=7)) == "attiva"
    assert _vita(last_seen=NOW - timedelta(hours=12)) == "attiva"


def test_zombie_oltre_365_giorni_e_riapertura_senza_loop():
    assert _vita(active_since=NOW - timedelta(days=365)) == "chiusura_assenza"
    assert _vita(active_since=NOW - timedelta(days=366), last_seen=NOW) == "chiusura_zombie"
    assert _vita(active_since=NOW - timedelta(days=2), last_seen=NOW) == "attiva"


def _record():
    return {
        "id": "job-1",
        "title": "Data  Engineer",
        "description": "Build   pipelines",
        "skills": ["SQL", "Python"],
        "salary_min": 1000,
        "first_seen": "2026-11-01T00:00:00Z",
        "last_seen": "2026-11-27T00:00:00Z",
        "updated_at": "2026-11-27T00:00:00Z",
        "raw": {"ripassata_at": "2026-11-27T00:00:00Z", "content": "same"},
    }


def test_content_hash_ignora_metadati_whitespace_e_ordine_degli_array():
    base = _record()
    rumore = _record()
    rumore.update(last_seen="2026-11-28T11:00:00Z", updated_at="2026-11-28T11:00:00Z")
    rumore["description"] = "Build pipelines"
    rumore["skills"] = ["Python", "SQL"]
    rumore["raw"]["ripassata_at"] = "2026-11-28T11:00:00Z"
    assert content_hash(base) == content_hash(rumore)


def test_updated_baseline_rumore_e_cambiamento_sostanziale():
    base = _record()
    baseline = valuta_updated(None, base)
    assert baseline.emetti is False
    assert baseline.motivo == "baseline"

    rumore = _record()
    rumore["updated_at"] = "2026-11-28T11:00:00Z"
    assert valuta_updated(baseline.digest, rumore).motivo == "immutato"

    for campo, valore in (
        ("title", "Senior Data Engineer"),
        ("description", "New"),
        ("salary_min", 2000),
        ("posted_at_estimated", True),
    ):
        cambiato = _record()
        cambiato[campo] = valore
        decisione = valuta_updated(baseline.digest, cambiato)
        assert decisione.emetti is True
        assert decisione.motivo == "cambiato"


def test_hash_coincide_col_json_che_omette_i_campi_vuoti():
    base = _record()
    for vuoto in (None, [], ""):
        variante = _record()
        variante["benefits"] = vuoto
        assert content_hash(variante) == content_hash(base)


def test_vincere_inizializza_il_contatore_e_legge_il_primo_annuncio():
    html = """
    <article class="job">
      <a href="https://acme.vincere.io/careers/job/123/data-engineer">Data Engineer</a>
      <div class="job__location">Milan</div>
      <div class="job__type">Full time</div>
      Published date: <div class="col-md-10">01-02-2026</div>
    </article>
    """

    class Client:
        def __init__(self):
            self.calls = 0

        def get(self, url, headers=None):
            self.calls += 1
            text = html if self.calls <= 2 else "<html>nessuna offerta</html>"
            return type("Response", (), {"status_code": 200, "text": text})()

    adapter = object.__new__(Vincere)
    adapter.client = Client()
    adapter.lettura_parziale = False
    jobs = adapter.jobs("acme")

    assert adapter.client.calls == 2

    assert len(jobs) == 1
    assert jobs[0].external_id == "123"
    assert jobs[0].posted_at == datetime(2026, 2, 1, tzinfo=UTC)
    assert adapter.lettura_parziale is False
