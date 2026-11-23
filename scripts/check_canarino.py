#!/usr/bin/env python3
"""Banco del canarino operativo: nessun database, rete o messaggio reale."""
from __future__ import annotations

import contextlib
import gzip
import importlib.util
import io
import json
import sys
import tempfile
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from nivult import tracker_notifications as tracker  # noqa: E402

SPEC = importlib.util.spec_from_file_location("canarino", ROOT / "deploy" / "canarino.py")
canarino = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(canarino)

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, condizione: bool) -> None:
    if condizione:
        PASSED.append(label)
        print(f"  ok    {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}")


class RispostaFinta:
    def __init__(self, payload=None, content=None, headers=None, status_code=200):
        self.payload = payload if payload is not None else {"ok": True}
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code
        self.controllata = False

    def raise_for_status(self):
        self.controllata = True

    def json(self):
        return self.payload


class CursoreFinto:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, *args):
        return None

    def fetchone(self):
        return ("chat-sintetica",)


class ConnessioneFinta:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def cursor(self):
        return CursoreFinto()


def _allarme(nome, funzione):
    if nome == "API":
        canarino.ALLARMI.append("guasto sintetico")


def prova_telegram() -> None:
    risposta = RispostaFinta()
    with mock.patch.object(canarino.httpx, "post", return_value=risposta) as post:
        canarino.telegram("token-sintetico", "chat-sintetica", "prova")
    check("Telegram verifica lo status HTTP", risposta.controllata)
    check("Telegram usa il canale atteso", post.call_count == 1)

    with mock.patch.object(canarino.httpx, "post",
                           return_value=RispostaFinta({"ok": False})):
        try:
            canarino.telegram("token-sintetico", "chat-sintetica", "prova")
        except RuntimeError:
            check("payload Telegram negativo rifiutato", True)
        else:
            check("payload Telegram negativo rifiutato", False)


def prova_no_notify() -> None:
    with (mock.patch.object(canarino, "env_di", return_value={}),
          mock.patch.object(canarino, "prova", side_effect=_allarme),
          mock.patch.object(canarino, "telegram") as invio,
          contextlib.redirect_stdout(io.StringIO()),
          contextlib.redirect_stderr(io.StringIO()) as stderr):
        esito = canarino.main(["--no-notify"])
    check("--no-notify segnala gli allarmi", esito == 1)
    check("--no-notify stampa l'allarme", "guasto sintetico" in stderr.getvalue())
    check("--no-notify non invia mai Telegram", invio.call_count == 0)

    with (mock.patch.object(canarino, "env_di", return_value={}),
          mock.patch.object(canarino, "prova") as controlli,
          mock.patch.object(canarino, "telegram") as invio,
          contextlib.redirect_stdout(io.StringIO()),
          contextlib.redirect_stderr(io.StringIO())):
        esito = canarino.main(["--no-notify"])
    check("--no-notify esegue tutti i controlli", controlli.call_count == 11)
    check("--no-notify sano restituisce zero", esito == 0 and invio.call_count == 0)


def prova_fallimento_notifica() -> None:
    env = {"TELEGRAM_BOT_TOKEN": "token-riservato-test",
           "DATABASE_URL": "postgresql://database-sintetico"}
    with (mock.patch.object(canarino, "env_di", return_value=env),
          mock.patch.object(canarino, "prova", side_effect=_allarme),
          mock.patch.object(canarino.psycopg, "connect", return_value=ConnessioneFinta()),
          mock.patch.object(canarino, "telegram",
                            side_effect=RuntimeError("token-riservato-test")) as invio,
          contextlib.redirect_stdout(io.StringIO()),
          contextlib.redirect_stderr(io.StringIO()) as stderr):
        esito = canarino.main([])
    check("notifica fallita restituisce errore", esito == 1 and invio.call_count == 1)
    check("errore notifica non rivela il token", "token-riservato-test" not in stderr.getvalue())


def prova_cron() -> None:
    testo = (ROOT / "deploy" / "cron.sh").read_text()
    blocco = testo.split("RIGHE=$(cat <<'EOF'", 1)[1].split("\nEOF\n)", 1)[0]
    lavori = [r for r in blocco.splitlines() if r and not r.startswith("#")]
    canarini = [r for r in lavori if "deploy/canarino.py" in r]
    builder = [r for r in lavori if "nivult.api_clienti.aggiorna" in r]
    check("cron non contiene duplicati", len(lavori) == len(set(lavori)))
    check("un solo canarino schedulato", len(canarini) == 1)
    check("un solo builder DuckDB schedulato", len(builder) == 1)
    check("builder cron con limiti conservativi",
          bool(builder) and "AGG_THREADS=1 AGG_MEMORIA=4GB" in builder[0])
    check("builder cron serializzato col recupero manuale",
          bool(builder) and "flock -w 7200 /var/run/nivult-api-clienti-aggiorna.lock" in builder[0])
    check("quote CPU con percentuale escaped nel crontab",
          all("%" not in r.replace("\\%", "") for r in lavori))
    check("cron conserva le righe di altri prodotti",
          "altrui=$(grep -v '/opt/nivult/'" in testo)


def prova_campione() -> None:
    adesso = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
    testo = json.dumps({"id": "sintetico", "description": "prima\u2028seconda"},
                       ensure_ascii=False) + "\n"
    risposta = RispostaFinta(
        content=gzip.compress(testo.encode("utf-8")),
        headers={"Last-Modified": format_datetime(adesso)})
    with mock.patch.object(canarino.httpx, "get", return_value=risposta):
        canarino.ALLARMI.clear()
        canarino.NOTE.clear()
        canarino.check_campione(adesso)
    check("campione JSONL con U+2028 nel testo",
          not canarino.ALLARMI and len(canarino.NOTE) == 2)

    rotta = RispostaFinta(content=b"non gzip",
                          headers={"Last-Modified": format_datetime(adesso)})
    with mock.patch.object(canarino.httpx, "get", return_value=rotta):
        canarino.ALLARMI.clear()
        canarino.check_campione(adesso)
    check("campione gzip rotto resta un allarme", len(canarino.ALLARMI) == 2)


def prova_retrieval_v3() -> None:
    with (mock.patch.object(canarino, "env_di", return_value={}),
          mock.patch.object(canarino.httpx, "get", return_value=RispostaFinta({"ok": True})),
          mock.patch.object(canarino.httpx, "post",
                            return_value=RispostaFinta({"scores": [0.8, 0.1]})) as post):
        canarino.ALLARMI.clear()
        canarino.NOTE.clear()
        canarino.check_retrieval_v3()
    check("retrieval E5 sano non allarma", not canarino.ALLARMI)
    check("retrieval E5 usa l'endpoint rank", post.call_args.args[0].endswith("/rank"))

    with (mock.patch.object(canarino, "env_di", return_value={}),
          mock.patch.object(canarino.httpx, "get", return_value=RispostaFinta({"ok": True})),
          mock.patch.object(canarino.httpx, "post",
                            return_value=RispostaFinta({"scores": [0.1, 0.8]}))):
        canarino.ALLARMI.clear()
        canarino.check_retrieval_v3()
    check("retrieval E5 con ranking insensato allarma", len(canarino.ALLARMI) == 1)


def prova_scrittore() -> None:
    risposte = RispostaFinta({"choices": [{"message": {"content": '{"ok": true}'}}]})
    with (mock.patch.object(canarino, "env_di", return_value={"SCRITTORE_URL": "http://scrittore.test"}),
          mock.patch.object(canarino.httpx, "get", return_value=RispostaFinta({"status": "ok"})),
          mock.patch.object(canarino.httpx, "post", return_value=risposte) as post):
        canarino.ALLARMI.clear()
        canarino.NOTE.clear()
        canarino.check_scrittore()
    check("scrittore sano non allarma", not canarino.ALLARMI and "inferenza ok" in canarino.NOTE[-1])
    check("scrittore usa la chat completions", post.call_args.args[0] == "http://scrittore.test/chat/completions")
    check("scrittore disabilita il pensiero", post.call_args.kwargs["json"]["chat_template_kwargs"]["enable_thinking"] is False)

    with (mock.patch.object(canarino, "env_di", return_value={"SCRITTORE_URL": "http://scrittore.test"}),
          mock.patch.object(canarino.httpx, "get", side_effect=RuntimeError("irraggiungibile"))):
        canarino.ALLARMI.clear()
        canarino.check_scrittore()
    check("scrittore irraggiungibile allarma", len(canarino.ALLARMI) == 1)

    with mock.patch.object(canarino, "env_di", return_value={}):
        canarino.ALLARMI.clear()
        canarino.check_scrittore()
    check("scrittore non configurato non e' un guasto", not canarino.ALLARMI)


def prova_log_errori() -> None:
    adesso = datetime(2026, 10, 26, 12, tzinfo=UTC)
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "digest.log"
        avvisi = ("2026-10-26T07:10:01+00:00 === digest ===\n" +
                  "\n".join(["WARNING scrittore-locale: errore di rete "
                              "(tentativo 4): [Errno 111] Connection refused"] * 25))
        p.write_text(avvisi)
        canarino.ALLARMI.clear()
        canarino.NOTE.clear()
        canarino.check_log_errori(adesso, [str(p)])
        check("retry di rete non diventano errori gravi", not canarino.ALLARMI)
        check("retry di rete restano visibili come note",
              any("25 avvisi di rete" in riga for riga in canarino.NOTE))

        p.write_text(avvisi + "\n" + "\n".join(["ERROR digest fallito"] * 21))
        canarino.ALLARMI.clear()
        canarino.check_log_errori(adesso, [str(p)])
        check("gli ERROR veri superano la soglia",
              len(canarino.ALLARMI) == 1 and "21 errori" in canarino.ALLARMI[0])

        p.write_text("2026-10-26T08:00:00+00:00 INFO HTTP Request: GET "
                     "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4477714013 "
                     '"HTTP/1.1 200 OK"\n')
        canarino.ALLARMI.clear()
        canarino.NOTE.clear()
        canarino.check_log_errori(adesso, [str(p)])
        check("un id che contiene 401 non e' un errore HTTP", not canarino.ALLARMI)

        p.write_text("2026-10-26T08:01:00+00:00 INFO HTTP Request: GET "
                     "https://data.fantastic.jobs/v1/expired-ats "
                     '"HTTP/1.1 401 Unauthorized"\n'
                     "2026-10-26T08:01:00+00:00 ERROR fantastic: scadute (401)\n"
                     '  "status": 401,\n')
        canarino.ALLARMI.clear()
        canarino.NOTE.clear()
        canarino.check_log_errori(adesso, [str(p)])
        check("un 401 del provider allarma subito",
              len(canarino.ALLARMI) == 1 and "autenticazione provider" in canarino.ALLARMI[0])
        check("il dettaglio JSON non viene contato come un altro errore",
              any("2 errori in 24 ore" in riga for riga in canarino.NOTE))

        p.write_text("ERROR senza data\n")
        canarino.ALLARMI.clear()
        canarino.NOTE.clear()
        canarino.check_log_errori(adesso, [str(p)])
        check("errore senza data resta segnalato ma fuori finestra",
              not canarino.ALLARMI and any("senza data" in riga for riga in canarino.NOTE))


def prova_digest_override() -> None:
    cursore = mock.MagicMock()
    cursore.__enter__.return_value = cursore
    cursore.fetchone.side_effect = [(0, 0), (0, 0), (1, 0), (0, 0), (0, 0), (0, 0)]
    connessione = mock.MagicMock()
    connessione.__enter__.return_value = connessione
    connessione.cursor.return_value = cursore
    with mock.patch.object(canarino.psycopg, "connect", return_value=connessione):
        canarino.ALLARMI.clear()
        canarino.NOTE.clear()
        canarino.check_digest({"DATABASE_URL": "nivult-sintetico"},
                              {"DATABASE_URL": "mavult-sintetico"})
    query_dovuti = cursore.execute.call_args_list[2]
    sql = query_dovuti.args[0]
    check("canarino considera anche le scadenze degli override",
          "user_clusters" in sql and "EXISTS" in sql
          and len(query_dovuti.args[1]) == 2)
    check("canarino filtra pause e cluster inattivi per override ed ereditate",
          all(clausola in sql for clausola in (
              "NOT uc_o.is_paused", "c_o.status = 'active'",
              "uc_o.frequency IS NOT NULL", "NOT uc_e.is_paused",
              "c_e.status = 'active'", "uc_e.frequency IS NULL")))
    check("un override attivo dovuto senza consegne fa scattare l'allarme",
          any("digest nivult: 1 utenti dovuti" in testo
              for testo in canarino.ALLARMI))


def prova_digest_nessun_falso_dovuto() -> None:
    cursore = mock.MagicMock()
    cursore.__enter__.return_value = cursore
    cursore.fetchone.side_effect = [(0, 0), (0, 0), (0, 0), (0, 0), (0, 0), (0, 0)]
    connessione = mock.MagicMock()
    connessione.__enter__.return_value = connessione
    connessione.cursor.return_value = cursore
    with mock.patch.object(canarino.psycopg, "connect", return_value=connessione):
        canarino.ALLARMI.clear()
        canarino.NOTE.clear()
        canarino.check_digest({"DATABASE_URL": "nivult-sintetico"},
                              {"DATABASE_URL": "mavult-sintetico"})
    check("pause, cluster inattivi e globale senza ereditate non danno falsi dovuti",
          not any("utenti dovuti" in testo for testo in canarino.ALLARMI))
    check("senza ricerche attive dovute il canarino resta informativo, non allarmato",
          all("0 consegnati in 30 ore" in testo for testo in canarino.NOTE))


def prova_digest_budget() -> None:
    cursore = mock.MagicMock()
    cursore.__enter__.return_value = cursore
    connessione = mock.MagicMock()
    connessione.__enter__.return_value = connessione
    connessione.cursor.return_value = cursore
    with mock.patch.object(canarino.psycopg, "connect", return_value=connessione):
        cursore.fetchone.side_effect = [(0, 0), (0, 1), (0, 0), (0, 0), (0, 0), (0, 0)]
        canarino.ALLARMI.clear()
        canarino.NOTE.clear()
        canarino.check_digest({"DATABASE_URL": "nivult-sintetico"},
                              {"DATABASE_URL": "mavult-sintetico"})
        check("budget mensile esaurito è una nota, non un guasto",
              not canarino.ALLARMI and any("budget mensile esaurito" in riga
                                            for riga in canarino.NOTE))
        cursore.fetchone.side_effect = [(0, 0), (1, 0), (0, 0), (0, 0), (0, 0), (0, 0)]
        canarino.ALLARMI.clear()
        canarino.NOTE.clear()
        canarino.check_digest({"DATABASE_URL": "nivult-sintetico"},
                              {"DATABASE_URL": "mavult-sintetico"})
        check("un fallimento non budget resta un allarme",
              any("1 falliti in 24 ore" in riga for riga in canarino.ALLARMI))


def prova_tracker_notifications() -> None:
    lunedi = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
    check("riepilogo settimanale usa il lunedi' alle 09:00 locali",
          tracker.chiave_settimanale(lunedi, "Europe/Rome") == "2026-10-26")
    check("riepilogo non anticipato prima delle 09:00 locali",
          tracker.chiave_settimanale(lunedi.replace(hour=6), "Europe/Rome") is None)
    check("riepilogo non ripetuto nella domenica UTC",
          tracker.chiave_settimanale(datetime(2026, 10, 25, 23, 0, tzinfo=UTC),
                                     "UTC") is None)
    evento = tracker.Evento("u1", "interview", "i1", "Colloquio", "Acme", "m1")
    check("deep link push al match canonico",
          evento.dati["url"] == "mavult://match/m1")

    cursore = mock.MagicMock()
    cursore.__enter__.return_value = cursore
    cursore.fetchone.side_effect = [None, (1,)]
    connessione = mock.MagicMock()
    connessione.cursor.return_value = cursore
    with (mock.patch.object(tracker, "eventi_dovuti", return_value=[evento]),
          mock.patch.object(tracker.push, "invia_notifica", return_value=1) as invio):
        primo = tracker.esegui(connessione, lunedi)
        secondo = tracker.esegui(connessione, lunedi)
    check("deduplica scritta soltanto dopo un invio riuscito",
          (primo["inviati"], secondo["duplicati"], invio.call_count) == (1, 1, 1))

    cursore.fetchone.side_effect = None
    cursore.fetchone.return_value = None
    cursore.execute.reset_mock()
    with (mock.patch.object(tracker, "eventi_dovuti", return_value=[evento]),
          mock.patch.object(tracker.push, "invia_notifica", return_value=0)):
        fallito = tracker.esegui(connessione, lunedi)
    check("invio fallito resta ritentabile e non viene deduplicato",
          fallito["senza_dispositivi"] == 1
          and not any("INSERT INTO personal_notification_dedup" in str(chiamata)
                      for chiamata in cursore.execute.call_args_list))
    cron = (ROOT / "deploy" / "cron.sh").read_text()
    wrapper = (ROOT / "deploy" / "tracker-notifications.sh").read_text()
    check("worker tracker presente una sola volta nel cron",
          cron.count("deploy/tracker-notifications.sh") == 1)
    check("wrapper tracker serializzato con flock", "flock -w 900" in wrapper)


def main() -> int:
    prova_telegram()
    prova_no_notify()
    prova_fallimento_notifica()
    prova_cron()
    prova_campione()
    prova_log_errori()
    prova_retrieval_v3()
    prova_scrittore()
    prova_digest_override()
    prova_digest_nessun_falso_dovuto()
    prova_digest_budget()
    prova_tracker_notifications()
    print(f"\n{len(PASSED)} ok, {len(FAILED)} falliti")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
