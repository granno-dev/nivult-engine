"""Il DuckDB dei clienti, rifatto ogni mattina DAGLI EXPORT.

Regola non negoziabile: l'API dei clienti non tocca il Postgres di
produzione — quel db regge gia' carico 20 coi demoni, e una query
lenta di un cliente non deve poter rallentare la raccolta. Qui si
leggono i .jsonl.gz che `nivult.ats.esporta` scrive in
/opt/nivult/exports e si costruisce un DuckDB a parte, che l'API apre
in sola lettura (vedi `dati.py`).

Atomico come esporta: si costruisce api-clienti.duckdb.tmp e poi
os.replace. Un lettore che ha il file aperto resta sul vecchio inode
(POSIX) e non si accorge di niente; i lettori nuovi vedono il nuovo.

Idempotente per costruzione: niente migrazioni ne' upsert, ogni giro
ricostruisce da zero. Con le ~800k righe delle attive il giro intero
e' dell'ordine del minuto — non vale la pena fare i furbi.

Schema: colonne tipizzate SOLO per quello che serve a filtrare e
ordinare; la riga intera resta in `raw` (il JSON originale, cosi' come
stava nel file). Quando esporta aggiunge un campo, il campo arriva ai
clienti senza toccare questo codice, e lo schema non si rompe mai per
una chiave in piu' o in meno.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import gzip
import json
import logging
import os
import re

import duckdb

log = logging.getLogger("nivult.api_clienti.aggiorna")

CARTELLA = "/opt/nivult/exports"
NOME_DB = "api-clienti.duckdb"
LOTTO = 5000  # righe per executemany: sotto, la conversione Python->DuckDB domina

_DATA_RX = re.compile(r"-(\d{4}-\d{2}-\d{2})\.jsonl\.gz$")
_FLUSSO_RX = re.compile(r"novita-(\d{4})(\d{2})(\d{2})-")


def _ts(v) -> dt.datetime | None:
    """ISO -> datetime NAIVE in UTC; None se assente o illeggibile.

    Negli export le date arrivano sia col «T» (isoformat, es. il flusso)
    sia con lo spazio (str(datetime) di Postgres, es. posted_at):
    fromisoformat di 3.11 le legge entrambe, e anche la «Z». Una data
    senza fuso la trattiamo come UTC: in produzione nasce da un
    timestamptz di un server in UTC, e un'ipotesi dichiarata batte un
    fuso ereditato dalla macchina che gira.

    Naive perche' TIMESTAMPTZ in duckdb-python tira dentro pytz, che
    non e' tra le nostre dipendenze: normalizziamo a UTC NOI al
    confine (qui nel builder, di nuovo nel lettore coi parametri) e nel
    db restano istanti UTC confrontabili senza fusi.
    """
    if not isinstance(v, str) or not v:
        return None
    try:
        d = dt.datetime.fromisoformat(v)
    except ValueError:
        return None
    if d.tzinfo:
        return d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d


def _intero(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _ultimo(cartella: str, prefisso: str) -> str | None:
    """Il .jsonl.gz datato piu' recente per quel prefisso (None se manca).

    Il confronto e' sul nome: la data nel nome ordina da sola. Restano
    fuori «-ultimo» (link simbolico, stessa roba di quello datato) e
    «-campione» (collaudi ed assaggi, non sono il dataset).
    """
    cand = [p for p in glob.glob(os.path.join(cartella, f"{prefisso}-*.jsonl.gz"))
            if _DATA_RX.search(p) and "-campione" not in p]
    return max(cand) if cand else None


def _crea_tabelle(con: duckdb.DuckDBPyConnection) -> None:
    # le tabelle si creano SEMPRE, anche vuote: un export mancante deve
    # dare risposte vuote ai clienti, non errori SQL all'API
    con.execute("""CREATE TABLE offerte(
        id VARCHAR, title VARCHAR, ats VARCHAR, company_slug VARCHAR,
        company VARCHAR, country VARCHAR, city VARCHAR, language VARCHAR,
        seniority VARCHAR, remote VARCHAR, category VARCHAR,
        posted_at TIMESTAMP, technologies VARCHAR[], raw VARCHAR)""")
    con.execute("""CREATE TABLE aziende(
        ats VARCHAR, company_slug VARCHAR, company VARCHAR, country VARCHAR,
        industry VARCHAR, employees BIGINT, technologies VARCHAR[], raw VARCHAR)""")
    # t: l'istante dell'evento (first_seen per le new, closed_at per le
    # closed) — la chiave di ordinamento del delta feed
    con.execute("CREATE TABLE flusso(t TIMESTAMP, id VARCHAR, event VARCHAR,"
                " raw VARCHAR)")
    con.execute("CREATE TABLE copertura(chiave VARCHAR, valore DOUBLE)")
    con.execute("CREATE TABLE meta(chiave VARCHAR, valore VARCHAR)")


def _carica(con: duckdb.DuckDBPyConnection, percorso: str, sql: str,
            mappa) -> int:
    """Un file .jsonl.gz in una tabella, a lotti.

    Con 800k righe gli INSERT a una a una fanno un round-trip a riga e
    il giro dura minuti; a lotti il collo di bottiglia torna la lettura
    gzip, che e' quella giusta. Una riga col JSON rotto si salta: non
    deve fermare il giro di tutto il dataset.
    """
    n = 0
    lotto: list = []
    with gzip.open(percorso, "rt", encoding="utf-8") as f:
        for linea in f:
            try:
                r = json.loads(linea)
            except json.JSONDecodeError:
                continue
            riga = mappa(r, linea.strip())
            if riga is None:
                continue
            lotto.append(riga)
            if len(lotto) >= LOTTO:
                con.executemany(sql, lotto)
                n += len(lotto)
                lotto.clear()
    if lotto:
        con.executemany(sql, lotto)
        n += len(lotto)
    return n


def _mappa_offerta(r: dict, raw: str) -> tuple:
    return (r.get("id"), r.get("title"), r.get("ats"), r.get("company_slug"),
            r.get("company"), r.get("country"), r.get("city"), r.get("language"),
            r.get("seniority"), r.get("remote"), r.get("category"),
            _ts(r.get("posted_at")),
            [t for t in r.get("technologies") or [] if isinstance(t, str)],
            raw)


def _mappa_azienda(r: dict, raw: str) -> tuple:
    # technologies dell'azienda sono {technology, active_jobs, ...}: in
    # colonna va solo il nome, per il filtro; il resto resta in raw
    return (r.get("ats"), r.get("company_slug"), r.get("company"),
            r.get("country"), r.get("industry"), _intero(r.get("employees")),
            [t["technology"] for t in r.get("technologies") or []
             if isinstance(t, dict) and t.get("technology")],
            raw)


def _mappa_flusso(r: dict, raw: str) -> tuple | None:
    # new -> first_seen, closed -> closed_at: ogni evento ha solo il suo
    t = _ts(r.get("first_seen") or r.get("closed_at"))
    if t is None:
        return None  # senza tempo non si puo' ne' ordinare ne' filtrare: fuori
    return (t, r.get("id"), r.get("event"), raw)


def _carica_flusso(con: duckdb.DuckDBPyConnection, cartella: str,
                   giorni: int) -> int:
    """I novita-* degli ultimi `giorni` giorni, scelti dalla data nel nome.

    Il file delle 09:15 contiene gli eventi fra il giro delle 09:00 e
    quello delle 09:15: la data nel nome e' la data degli eventi, e
    scegliere per nome basta — non serve rileggere il contenuto per
    decidere. Un file rotto non ferma il giro: il delta feed si ricostruisce
    da solo al file successivo.
    """
    taglio = dt.date.today() - dt.timedelta(days=giorni)
    n = 0
    sql = "INSERT INTO flusso VALUES (?, ?, ?, ?)"
    for percorso in sorted(glob.glob(
            os.path.join(cartella, "flusso", "novita-*.jsonl.gz"))):
        m = _FLUSSO_RX.search(os.path.basename(percorso))
        if not m or dt.date(*map(int, m.groups())) < taglio:
            continue
        try:
            n += _carica(con, percorso, sql, _mappa_flusso)
        except OSError as e:
            log.warning("flusso: %s illeggibile (%s), salto", percorso, e)
    return n


def _carica_copertura(con: duckdb.DuckDBPyConnection, cartella: str) -> dict:
    """Il manifest di esporta, che dichiara i fill-rate: il cliente li
    vede in /copertura invece di scoprirli da solo."""
    try:
        with open(os.path.join(cartella, "manifest-ultimo.json")) as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    copertura = manifest.get("coverage") or {}
    con.executemany("INSERT INTO copertura VALUES (?, ?)",
                    [(k, float(v)) for k, v in copertura.items()])
    return manifest


def costruisci(cartella: str = CARTELLA, flusso_giorni: int = 30) -> dict:
    os.makedirs(cartella, exist_ok=True)
    destinazione = os.path.join(cartella, NOME_DB)
    tmp = destinazione + ".tmp"
    for p in (tmp, tmp + ".wal"):  # resti di un giro morto a meta'
        try:
            os.remove(p)
        except OSError:
            pass
    f_offerte = _ultimo(cartella, "offerte-attive")
    f_aziende = _ultimo(cartella, "aziende-segnali")
    mancanti = [nome for nome, p in (("offerte-attive", f_offerte),
                                     ("aziende-segnali", f_aziende))
                if p is None]
    data_export = next((_DATA_RX.search(p).group(1)
                        for p in (f_offerte, f_aziende) if p), None)
    if mancanti:
        log.warning("export mancanti: %s — costruisco comunque il db, "
                    "con le tabelle vuote e lo stato dichiarato", mancanti)
    con = duckdb.connect(tmp)
    _crea_tabelle(con)
    n_offerte = _carica(con, f_offerte,
                        "INSERT INTO offerte VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        _mappa_offerta) if f_offerte else 0
    n_aziende = _carica(con, f_aziende,
                        "INSERT INTO aziende VALUES (?,?,?,?,?,?,?,?)",
                        _mappa_azienda) if f_aziende else 0
    n_flusso = _carica_flusso(con, cartella, flusso_giorni)
    manifest = _carica_copertura(con, cartella)
    meta = {
        "stato": "mancano gli export di oggi" if mancanti else "ok",
        "data_export": data_export or "",
        "offerte": n_offerte,
        "aziende": n_aziende,
        "flusso": n_flusso,
        "flusso_giorni": flusso_giorni,
        # i path servono a /v1/exports/latest/{nome}: il download punta a
        # questi, non a un nome inventato dal client
        "file_offerte": f_offerte or "",
        "file_aziende": f_aziende or "",
        "generato_at": dt.datetime.now(dt.timezone.utc)
                       .isoformat(timespec="seconds"),
    }
    con.executemany("INSERT INTO meta VALUES (?, ?)",
                    [(k, str(v)) for k, v in meta.items()])
    con.close()  # la chiusura pulita svuota il WAL dentro il .tmp
    os.replace(tmp, destinazione)  # atomico: i lettori restano sul vecchio inode
    log.info("api-clienti.duckdb: %d offerte, %d aziende, %d eventi flusso, "
             "copertura %d campi, export del %s — %.1f MB",
             n_offerte, n_aziende, n_flusso, len(manifest.get("coverage") or {}),
             data_export, os.path.getsize(destinazione) / 1e6)
    return meta


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.api_clienti.aggiorna")
    ap.add_argument("--cartella", default=CARTELLA,
                    help="dove stanno gli export (default %(default)s)")
    ap.add_argument("--flusso-giorni", type=int, default=30,
                    help="quanti giorni di delta feed tenere (default %(default)s)")
    args = ap.parse_args(argv)
    print(json.dumps(costruisci(args.cartella, args.flusso_giorni),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
