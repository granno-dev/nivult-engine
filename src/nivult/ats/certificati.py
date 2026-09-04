"""Certificate Transparency: i career site del mondo, mentre nascono.

Ogni certificato HTTPS emesso finisce nei registri CT pubblici. Quando
un'azienda accende `careers.acme.com` o `jobs.acme.it`, il nome passa
nel flusso GLOBALE entro minuti — e siccome i certificati si rinnovano
ogni ~90 giorni, ascoltare il flusso per un trimestre significa veder
passare quasi tutti i career site attivi del pianeta, non solo i nuovi.

Il demone:
  1. sceglie i registri grossi e "usable" dalla log list ufficiale di
     Google (niente URL cablati: i registri ruotano ogni anno);
  2. scarica le entry a lotti dal punto in cui era rimasto (cursore su
     file), estrae i nomi DNS dal DER con una regex ASCII — volutamente
     rozza: niente ASN.1 fragile, ci servono solo i nomi;
  3. tiene i sottodomini di assunzione (careers./jobs./karriere./
     lavora-con-noi/werkenbij…), riduce al dominio radice con le regole
     della riscoperta (che gia' scarta gli host degli ATS stessi) e
     inserisce in company_domains, source='certificati';
  4. da li' e' filiera collaudata: detector -> vanity -> offerte.

Se resta indietro oltre la soglia salta avanti: meglio freschi che
completi — il giro dopo ripassa comunque.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time

import httpx
import psycopg

from nivult.ats.riscoperta import _radice

log = logging.getLogger("nivult.ats.certificati")

STATO = "/opt/nivult/certificati-stato.json"
LOG_LIST = "https://www.gstatic.com/ct/log_list/v3/log_list.json"
# i registri che coprono il grosso delle emissioni mondiali
_REGISTRI_VOLUTI = ("argon", "oak", "xenon", "nessie")
_MAX_RITARDO = 300_000        # entry: oltre, si salta avanti
_LOTTO = 256                  # entry per get-entries (i log ne danno meno)

# nomi DNS dentro il DER: ASCII puro, la SAN e' scritta cosi'
_NOME = re.compile(rb"(?:[*a-zA-Z0-9_-]{1,63}\.){1,6}[a-zA-Z]{2,24}")

# la prima etichetta che grida «assunzioni»: esatte + qualche prefisso
_ETICHETTE = {
    "careers", "career", "jobs", "job", "apply", "recruiting",
    "recruitment", "recruit", "talent", "talents", "hiring", "vacancies",
    "vacatures", "karriere", "karriar", "carriere", "carrieres",
    "lavoro", "empleo", "emplois", "recrutement", "stillinger",
    "ledigestillinger", "rekrytering", "tyopaikat", "kariera",
    "praca", "stellen", "stellenangebote", "bewerbung",
}
_PREFISSI = ("werkenbij", "lavoracon", "jobs-", "careers-", "karriere-",
             "trabajaen", "jobba-")


def _interessante(host: str) -> bool:
    prima = host.split(".", 1)[0]
    return (prima in _ETICHETTE
            or any(prima.startswith(p) for p in _PREFISSI))


def _registri(cli: httpx.Client) -> list[dict]:
    """I registri 'usable' dalla lista ufficiale, filtrati per nome."""
    r = cli.get(LOG_LIST)
    r.raise_for_status()
    fuori = []
    for op in r.json().get("operators", []):
        for lg in op.get("logs", []):
            stato = lg.get("state", {})
            nome = (lg.get("description") or "").lower()
            if "usable" in stato and any(v in nome for v in _REGISTRI_VOLUTI):
                fuori.append({"nome": lg["description"], "url": lg["url"]})
    return fuori


def _testa(cli: httpx.Client, url: str) -> int | None:
    try:
        r = cli.get(url.rstrip("/") + "/ct/v1/get-sth")
        if r.status_code == 200:
            return int(r.json()["tree_size"])
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    return None


def _entries(cli: httpx.Client, url: str, inizio: int, fine: int) -> list:
    try:
        r = cli.get(url.rstrip("/") + "/ct/v1/get-entries",
                    params={"start": inizio, "end": fine})
        if r.status_code != 200:
            return []
        return r.json().get("entries", [])
    except (httpx.HTTPError, ValueError):
        return []


def _nomi_da_entry(entry: dict) -> set[str]:
    fuori: set[str] = set()
    for campo in ("leaf_input", "extra_data"):
        try:
            der = base64.b64decode(entry.get(campo) or "")
        except Exception:                            # noqa: BLE001
            continue
        for m in _NOME.finditer(der):
            nome = m.group(0).decode("ascii", "ignore").lower()
            nome = nome.lstrip("*.")
            if nome and _interessante(nome):
                fuori.add(nome)
        break        # leaf_input basta quasi sempre; extra solo se vuoto
    return fuori


def giro(dsn: str, secondi: int = 55) -> dict:
    """Un giro di raccolta: avanza i cursori per ~`secondi`, inserisce
    i domini trovati, salva lo stato. Ritorna le statistiche."""
    stats = {"entry": 0, "nomi": 0, "domini_nuovi": 0, "salti": 0}
    try:
        stato = json.load(open(STATO))
    except Exception:                                # noqa: BLE001
        stato = {}
    cli = httpx.Client(timeout=20, headers={
        "User-Agent": "nivult-ats/1.0 (ct-watch)"})
    visti: set[str] = set()
    scadenza = time.monotonic() + secondi
    with psycopg.connect(dsn, autocommit=True) as conn:
        for reg in _registri(cli):
            url = reg["url"]
            testa = _testa(cli, url)
            if testa is None:
                continue
            cur = stato.get(url, testa)       # prima volta: dalla testa
            if testa - cur > _MAX_RITARDO:
                cur = testa - _MAX_RITARDO
                stats["salti"] += 1
            while cur < testa and time.monotonic() < scadenza:
                lotto = _entries(cli, url, cur, min(cur + _LOTTO - 1,
                                                    testa - 1))
                if not lotto:
                    break
                for e in lotto:
                    stats["entry"] += 1
                    for host in _nomi_da_entry(e):
                        if host in visti:
                            continue
                        visti.add(host)
                        stats["nomi"] += 1
                        dominio = _radice(host)
                        if not dominio:
                            continue
                        n = conn.execute(
                            """INSERT INTO company_domains
                                   (domain, careers_url, source)
                               VALUES (%s, %s, 'certificati')
                               ON CONFLICT (domain) DO NOTHING""",
                            (dominio, f"https://{host}")).rowcount
                        stats["domini_nuovi"] += n
                cur += len(lotto)
            stato[url] = cur
    # scrittura atomica: un crash a meta' non deve corrompere i cursori
    with open(STATO + ".tmp", "w") as f:
        json.dump(stato, f)
    os.replace(STATO + ".tmp", STATO)
    cli.close()
    return stats


def demone(dsn: str) -> None:
    while True:
        try:
            s = giro(dsn)
            if s["domini_nuovi"] or s["nomi"]:
                log.info("certificati: %s", s)
        except Exception:                            # noqa: BLE001
            log.exception("giro certificati fallito, riprovo")
            time.sleep(60)
        time.sleep(5)


def main(argv: list[str] | None = None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.certificati")
    ap.add_argument("--demone", action="store_true")
    ap.add_argument("--giro", action="store_true",
                    help="un giro solo e statistiche (collaudo)")
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    if args.demone:
        demone(dsn)
    else:
        print(giro(dsn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
