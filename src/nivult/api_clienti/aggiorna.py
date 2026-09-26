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
# dove vive il db dell'API: di default accanto agli export, ma il disco di
# Hetzner e' piccolo e il file e' grande — su un volume dedicato si
# configura con API_CLIENTI_DB, senza toccare il codice (23/09/2026)
DB_PATH = os.environ.get("API_CLIENTI_DB",
                         os.path.join(CARTELLA, NOME_DB))

_DATA_RX = re.compile(r"-(\d{4}-\d{2}-\d{2})\.jsonl\.gz$")
_FLUSSO_RX = re.compile(r"novita-(\d{4})(\d{2})(\d{2})-")


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


# ── il caricamento nativo: DuckDB legge il .jsonl.gz in C++ ──────────
# La via Python riga-per-riga (executemany a lotti) sul corpus reale e'
# stata misurata il 23/09/2026: MAI finita in 2 ore (fsync su disco
# lento, 60 GB scritti per un file da 8, e il kernel l'ha uccisa una
# volta per memoria). read_json fa tutto nel motore: minuti, memoria
# piatta. La riga resta in `raw` VERBATIM (lettura a due passate sul
# file, unite per numero di riga): la ri-serializzazione con
# to_json(struct_pack) la gonfiava quasi doppia (\uXXXX per l'unicode)
# e la cambiava di forma (23/09/2026).
_READ = ("SELECT row_number() OVER () AS rn, * "
         "FROM read_json(?, format='newline_delimited', compression='gzip')")
# la riga com'e': read_csv con un delimitatore che il JSONL non puo'
# contenere e quoting spento. Il gzip si legge solo in sequenza, quindi
# l'ordine delle due passate e' identico e il join per rn e' esatto.
_READT = ("SELECT row_number() OVER () AS rn, linea FROM read_csv(?, "
          "delim='\\x01', header=false, columns={'linea': 'VARCHAR'}, "
          "quote='', escape='', "
          # il JSONL reale ha righe da 3 MB (immagini base64 dentro le
          # descrizioni): il tetto di linea di default (2 MB) tagliava
          # il flusso a meta' (23/09/2026)
          "max_line_size=20000000)")

# I timestamp dell'export (Postgres «2026-09-23 07:00:00+00:00» o ISO col
# fuso): TIMESTAMPTZ converte davvero («+02:00» diventa l'istante UTC,
# non troncato), ::TIMESTAMP toglie il fuso senza dover importare pytz.
_TS_SQL = "TRY_CAST(TRY_CAST({c} AS TIMESTAMPTZ)::TIMESTAMP AS TIMESTAMP)"

_COLONNE_OFFERTE = (
    ("id", '"id"', ("id",)), ("title", '"title"', ("title",)),
    ("ats", '"ats"', ("ats",)), ("company_slug", '"company_slug"', ("company_slug",)),
    ("company", '"company"', ("company",)), ("country", '"country"', ("country",)),
    ("city", '"city"', ("city",)), ("language", '"language"', ("language",)),
    ("seniority", '"seniority"', ("seniority",)),
    ("remote", '"remote"::VARCHAR', ("remote",)),
    ("category", '"category"', ("category",)),
    ("posted_at", _TS_SQL.format(c='"posted_at"'), ("posted_at",)),
    ("technologies", '"technologies"', ("technologies",)),
)
_COLONNE_AZIENDE = (
    ("ats", '"ats"', ("ats",)), ("company_slug", '"company_slug"', ("company_slug",)),
    ("company", '"company"', ("company",)), ("country", '"country"', ("country",)),
    ("industry", '"industry"', ("industry",)),
    ("employees", 'TRY_CAST("employees" AS BIGINT)', ("employees",)),
    # l'export aziende ha technologies come lista di {technology, ...}:
    # in colonna i nomi soli (servono al filtro); il resto resta in raw
    ("technologies", 'list_transform("technologies", x -> x.technology)',
     ("technologies",)),
)
# (nome colonna, espressione SQL o callable(set_sorgente)->SQL, colonne
# della sorgente che servono): se NESSUNA delle colonne servite esiste nel
# file, la colonna e' NULL. L'espressione puo' dipendere da PIU' colonne
# (la «t» del flusso): il callable riceve le colonne presenti e ripiega
# su NULL per quelle assenti — un file di sole «new» non ha closed_at.
_COLONNE_FLUSSO = (
    ("t",
     lambda disp: "COALESCE("
         + _TS_SQL.format(c='"first_seen"' if "first_seen" in disp else "NULL")
         + ", "
         + _TS_SQL.format(c='"closed_at"' if "closed_at" in disp else "NULL")
         + ")",
     ("first_seen", "closed_at")),
    ("id", '"id"', ("id",)), ("event", '"event"', ("event",)),
)


def _carica_nativa(con: duckdb.DuckDBPyConnection, percorso: str,
                   tabella: str, colonne: tuple, dove: str = "") -> int:
    """Un file .jsonl.gz in una tabella, tutto nel motore DuckDB.

    `colonne`: (nome, espressione o callable, colonne della sorgente che
    servono); una colonna le cui fonti mancano tutte diventa NULL — lo
    schema dell'export si evolve senza rompere il builder. `raw` e' la
    riga VERBATIM dell'export (lettura testuale parallela, join per rn).
    """
    disponibili = {r[0] for r in con.execute(
        f"DESCRIBE {_READ}", [percorso]).fetchall()}
    disponibili.discard("rn")     # la colonna tecnica, non dell'export
    sel = ", ".join(f"{(expr(disponibili) if callable(expr) else expr)} AS {nome}"
                    if any(f in disponibili for f in fonti)
                    else f"NULL AS {nome}"
                    for nome, expr, fonti in colonne)
    interna = (f"SELECT {sel}, r.linea AS raw "
               f"FROM ({_READ}) j JOIN ({_READT}) r USING (rn)")
    if dove:
        interna = f"SELECT * FROM ({interna}) WHERE {dove}"
    n = con.execute(f"INSERT INTO {tabella} {interna}",
                    [percorso, percorso]).fetchone()
    return int(n[0]) if n else 0


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
    for percorso in sorted(glob.glob(
            os.path.join(cartella, "flusso", "novita-*.jsonl.gz"))):
        m = _FLUSSO_RX.search(os.path.basename(percorso))
        if not m or dt.date(*map(int, m.groups())) < taglio:
            continue
        try:
            n += _carica_nativa(con, percorso, "flusso", _COLONNE_FLUSSO,
                                dove="t IS NOT NULL")
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


def costruisci(cartella: str = CARTELLA, flusso_giorni: int = 7,
               db_path: str | None = None) -> dict:
    # dove finisce il db: l'argomento vince, poi l'ambiente, poi il
    # default accanto agli export. I banchi passano --cartella e restano
    # nella loro sabbia; in produzione API_CLIENTI_DB manda sul volume.
    destinazione = (db_path or os.environ.get("API_CLIENTI_DB")
                    or os.path.join(cartella, NOME_DB))
    # il volume dedicato (26/09): se la destinazione e' sul volume ma il
    # volume non e' montato, makedirs la creerebbe sul DISCO DI ROOT e li'
    # il builder scriverebbe 15 GB — esattamente lo scenario che il volume
    # doveva evitare. Si misura il mount, non si suppone.
    cartella_dest = os.path.dirname(destinazione)
    if cartella_dest.startswith("/mnt/") and not os.path.ismount(cartella_dest):
        raise SystemExit(f"il volume {cartella_dest} non e' montato: "
                         "non costruisco il db sul disco di root")
    os.makedirs(cartella_dest, exist_ok=True)
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
    # il builder gira su Hetzner (7,6 GB in tutto, con Postgres accanto):
    # DuckDB per default si prende l'80% della RAM e il 23/09/2026 il
    # kernel ha ucciso il giro a 4,1 GB di RSS. Tetto dichiarato a 2 GB
    # (oltre si riversa su disco, come deve essere) e niente ordine di
    # inserzione da preservare: l'ordine lo da' l'export, non il db.
    con.execute("SET memory_limit = '2GB'")
    con.execute("SET preserve_insertion_order = false")
    _crea_tabelle(con)
    n_offerte = _carica_nativa(con, f_offerte, "offerte",
                               _COLONNE_OFFERTE) if f_offerte else 0
    n_aziende = _carica_nativa(con, f_aziende, "aziende",
                               _COLONNE_AZIENDE) if f_aziende else 0
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
    ap.add_argument("--flusso-giorni", type=int, default=7,
                    help="quanti giorni di delta feed tenere (default %(default)s; "
                         "il cron conserva i file novita per 7 giorni)")
    args = ap.parse_args(argv)
    print(json.dumps(costruisci(args.cartella, args.flusso_giorni),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
