#!/usr/bin/env python3
"""Banco del canarino operativo: nessun database, rete o messaggio reale."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
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
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {"ok": True}
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
    check("--no-notify esegue tutti i controlli", controlli.call_count == 9)
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
          bool(builder) and "AGG_THREADS=1 AGG_MEMORIA=2GB" in builder[0])
    check("quote CPU con percentuale escaped nel crontab",
          all("%" not in r.replace("\\%", "") for r in lavori))
    check("cron conserva le righe di altri prodotti",
          "altrui=$(grep -v '/opt/nivult/'" in testo)


def main() -> int:
    prova_telegram()
    prova_no_notify()
    prova_fallimento_notifica()
    prova_cron()
    print(f"\n{len(PASSED)} ok, {len(FAILED)} falliti")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
