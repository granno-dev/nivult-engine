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
import json
import logging
import os
import re
import tempfile
import time

import duckdb

log = logging.getLogger("nivult.api_clienti.aggiorna")

CARTELLA = "/opt/nivult/exports"
NOME_DB = "api-clienti.duckdb"
# dove vive il db dell'API: di default accanto agli export, ma il disco di
# Hetzner e' piccolo e il file e' grande — su un volume dedicato si
# configura con API_CLIENTI_DB, senza toccare il codice (23/09/2026)
DB_PATH = os.environ.get("API_CLIENTI_DB",
                         os.path.join(CARTELLA, NOME_DB))

_DATA_RX = re.compile(r"-(\d{4}-\d{2}-\d{2})(?:-[0-9a-f]{12})?\.jsonl\.gz$")
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


def _ultimo_di_oggi(cartella: str, prefisso: str) -> str | None:
    """Come _ultimo, ma solo se il file e' datato oggi (data nel nome)."""
    p = _ultimo(cartella, prefisso)
    if p and _DATA_RX.search(p).group(1) == dt.date.today().isoformat():
        return p
    return None


def _aspetta_export_oggi(cartella: str, minuti: int) -> None:
    """Pazienta che gli export DI OGGI ci siano entrambi.

    L'export delle 05:45 finisce quando finisce (07:17-07:21 misurati al
    momento del cron delle 07:40, gia' 07:58 il 29/09/2026: cresce coi
    dati) e l'archivia delle 06:20 porta via i file di ieri NEL
    FRATTEMPO — chi parte a orario fisso puo' trovare la cartella senza
    il file di oggi e costruire un db con le tabelle vuote (successo il
    29/09: aziende a zero per ore, reveal dei clienti a vuoto). Si
    aspetta a passi di 30 secondi; scaduta l'attesa si costruisce con
    quello che c'e' (il file di ieri vale piu' del vuoto).
    """
    if minuti <= 0:
        return
    scadenza = time.monotonic() + minuti * 60
    while True:
        mancanti = [p for p in ("offerte-attive", "aziende-segnali")
                    if not _ultimo_di_oggi(cartella, p)]
        if not mancanti:
            return
        if time.monotonic() >= scadenza:
            log.warning("export di oggi non comparsi entro %d minuti (%s): "
                        "costruisco con quello che c'e'", minuti, mancanti)
            return
        log.info("export di oggi ancora mancanti (%s): riprovo fra 30 secondi",
                 mancanti)
        time.sleep(30)


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
# 09/10/2026: un record-flusso con un testo gigante (33 MB: oltre il
# tetto di default di read_json, 16 MB) ha ucciso la build a valle, dopo
# offerte e aziende caricate. Il tetto si alza QUI, argomento della
# funzione — non esiste come parametro di sessione (provato, Catalog
# Error). E' un tetto di sicurezza, non un budget.
_READ = ("SELECT row_number() OVER () AS rn, * "
         "FROM read_json(?, format='newline_delimited', compression='gzip', "
         "maximum_object_size=134217728)")
# la riga com'e': read_csv con un delimitatore che il JSONL non puo'
# contenere e quoting spento. Il gzip si legge solo in sequenza, quindi
# l'ordine delle due passate e' identico e il join per rn e' esatto.
_READT = ("SELECT row_number() OVER () AS rn, linea FROM read_csv(?, "
          "delim='\\x01', header=false, columns={'linea': 'VARCHAR'}, "
          "quote='', escape='', "
          # il JSONL reale ha righe da 3 MB (immagini base64 dentro le
          # descrizioni): il tetto di linea di default (2 MB) tagliava
          # il flusso a meta' (23/09/2026). Alzato a 256 MB il 09/10/2026
          # per una riga da 33 MB. Senza buffer_size, DuckDB 1.5 alloca
          # 16 buffer da max_line_size (768 MB) anche con threads=1 e
          # memory_limit=1GB. buffer_size=max_line_size evita quel blocco
          # senza tagliare il testo: chi supera il tetto fa comunque errore.
          "max_line_size=50331648, buffer_size=50331648)")

# I timestamp dell'export (Postgres «2026-09-23 07:00:00+00:00» o ISO col
# fuso): TIMESTAMPTZ converte davvero («+02:00» diventa l'istante UTC,
# non troncato), ::TIMESTAMP toglie il fuso. La sessione e' sempre UTC
# (_configura), altrimenti DuckDB restituirebbe l'ora locale del server.
_TS_SQL = "timezone('UTC', TRY_CAST({c} AS TIMESTAMPTZ))"

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
    ("technologies", 'list_transform("technologies", lambda x: x.technology)',
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
         + _TS_SQL.format(c='"updated_at"' if "updated_at" in disp else "NULL")
         + ", "
         + _TS_SQL.format(c='"first_seen"' if "first_seen" in disp else "NULL")
         + ", "
         + _TS_SQL.format(c='"closed_at"' if "closed_at" in disp else "NULL")
         + ")",
     ("updated_at", "first_seen", "closed_at")),
    ("id", '"id"', ("id",)), ("event", '"event"', ("event",)),
)


def _json_espr(expr: str) -> str:
    if expr == 'list_transform("technologies", lambda x: x.technology)':
        return ("list_transform(CAST(json_extract(linea,'$.technologies') AS JSON[]), "
                "lambda x: json_extract_string(x,'$.technology'))")
    if expr == '"remote"::VARCHAR':
        return "json_extract_string(linea,'$.remote')::VARCHAR"
    if re.fullmatch(r'"[A-Za-z_][A-Za-z0-9_]*"', expr):
        return f"json_extract_string(linea,'$.{expr[1:-1]}')"
    return re.sub(r'"([A-Za-z_][A-Za-z0-9_]*)"',
                  lambda m: f"json_extract_string(linea,'$.{m.group(1)}')", expr)


def _id_raw_mismatch(con: duckdb.DuckDBPyConnection) -> int:
    return con.execute(
        "SELECT count(*) FROM offerte WHERE id<>json_extract_string(raw,'$.id')").fetchone()[0]


def _carica_nativa(con: duckdb.DuckDBPyConnection, percorso: str,
                   tabella: str, colonne: tuple, dove: str = "") -> int:
    disponibili = {r[0] for r in con.execute(
        f"DESCRIBE {_READ}", [percorso]).fetchall()}
    disponibili.discard("rn")
    sel = ", ".join(f"{_json_espr(expr(disponibili) if callable(expr) else expr)} AS {nome}"
                    if any(f in disponibili for f in fonti)
                    else f"NULL AS {nome}"
                    for nome, expr, fonti in colonne)
    interna = f"SELECT {sel}, linea AS raw FROM ({_READT})"
    if dove:
        interna = f"SELECT * FROM ({interna}) WHERE {dove}"
    n = con.execute(f"INSERT INTO {tabella} {interna}", [percorso]).fetchone()
    if tabella == "offerte":
        mismatch = _id_raw_mismatch(con)
        if mismatch:
            raise ValueError(f"id/raw mismatch nel caricamento: {mismatch}")
    return int(n[0]) if n else 0


def _carica_flusso(con: duckdb.DuckDBPyConnection, cartella: str,
                   giorni: int, temp_dir: str) -> int:
    """I novita-* degli ultimi `giorni` giorni, scelti dalla data nel nome.

    Il file delle 09:15 contiene gli eventi fra il giro delle 09:00 e
    quello delle 09:15: la data nel nome e' la data degli eventi, e
    scegliere per nome basta — non serve rileggere il contenuto per
    decidere. Un file rotto non ferma il giro: il delta feed si ricostruisce
    da solo al file successivo.
    """
    taglio = dt.date.today() - dt.timedelta(days=giorni)
    n = 0
    with tempfile.TemporaryDirectory(prefix="flusso-stage-", dir=temp_dir) as staging:
        stage = os.path.join(staging, "flusso.duckdb")
        con.execute("ATTACH " + _sql_stringa(stage) + " AS stage")
        try:
            con.execute("CREATE TABLE stage.flusso("
                        "t TIMESTAMP, id VARCHAR, event VARCHAR, raw VARCHAR)")
            con.execute("SET max_temp_directory_size = '100GB'")
            con.execute("SET preserve_insertion_order = false")
            for percorso in sorted(glob.glob(
                    os.path.join(cartella, "flusso", "novita-*.jsonl.gz"))):
                try:
                    m = _FLUSSO_RX.search(os.path.basename(percorso))
                    if not m:
                        continue
                    data_file = dt.date(*map(int, m.groups()))
                    if data_file < taglio:
                        continue
                    caricati = _carica_nativa(
                        con, percorso, "stage.flusso", _COLONNE_FLUSSO,
                        dove="t IS NOT NULL")
                    con.execute("CHECKPOINT stage")
                    n += caricati
                except Exception as e:               # noqa: BLE001
                    log.warning("flusso: %s illeggibile (%s), salto", percorso, e)
            con.execute("INSERT INTO flusso SELECT * FROM stage.flusso")
            con.execute("CHECKPOINT")
        finally:
            con.execute("DETACH stage")
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
    # la copertura delle aziende viaggia nella stessa tabella, col
    # prefisso che la distingue (27/09/2026): `az:domain` ecc.
    copertura_az = manifest.get("coverage_aziende") or {}
    if copertura_az:
        con.executemany("INSERT INTO copertura VALUES (?, ?)",
                        [("az:" + k, float(v)) for k, v in copertura_az.items()])
    return manifest


def _tabella_non_vuota(db_path: str, tabella: str) -> bool:
    """La build live attuale ha righe in quella tabella? (False anche se
    il db non esiste o non si apre: in quel caso non c'e' nulla da
    proteggere e si costruisce normalmente.)"""
    try:
        con = duckdb.connect(db_path, read_only=True)
    except Exception:                            # noqa: BLE001
        return False
    try:
        return con.execute(f"SELECT 1 FROM {tabella} LIMIT 1"
                           ).fetchone() is not None
    except Exception:                            # noqa: BLE001
        return False
    finally:
        con.close()


def _sql_stringa(valore: str) -> str:
    return "'" + valore.replace("'", "''") + "'"


def _configura(con: duckdb.DuckDBPyConnection, temp_dir: str) -> None:
    memoria = os.environ.get("AGG_MEMORIA")
    if not memoria:
        try:
            ram = (os.sysconf("SC_PHYS_PAGES")
                   * os.sysconf("SC_PAGE_SIZE"))
            memoria = f"{max(512, int(ram * 0.55 / 1048576))}MB"
        except (ValueError, OSError):
            memoria = "3GB"
    thread = int(os.environ.get("AGG_THREADS", "2"))
    if thread < 1:
        raise ValueError("AGG_THREADS deve essere almeno 1")
    con.execute("SET memory_limit = " + _sql_stringa(memoria))
    con.execute("SET preserve_insertion_order = false")
    con.execute(f"SET threads = {thread}")
    con.execute("SET temp_directory = " + _sql_stringa(temp_dir))
    con.execute("SET TimeZone = 'UTC'")


def _manifest(cartella: str) -> dict:
    try:
        with open(os.path.join(cartella, "manifest-ultimo.json")) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _sorgenti_export(cartella: str) -> tuple[str | None, str | None, dict]:
    manifest = _manifest(cartella)
    files = manifest.get("files") or {}
    if manifest.get("state") == "ready" and files:
        def percorso(*nomi):
            nome = next((files.get(n) for n in nomi if files.get(n)), None)
            if not nome or os.path.basename(nome) != nome:
                raise ValueError("nome file non sicuro nel manifest")
            return os.path.join(cartella, nome)
        return (percorso("jobs", "offerte"), percorso("companies", "aziende"), manifest)
    return (_ultimo(cartella, "offerte-attive"),
            _ultimo(cartella, "aziende-segnali"), manifest)


def _controlla_invarianti(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    conteggi = {}
    conteggi["orphan_jobs"] = con.execute(
        "SELECT count(*) FROM offerte o LEFT JOIN aziende a "
        "ON a.ats=o.ats AND a.company_slug=o.company_slug "
        "WHERE a.company_slug IS NULL").fetchone()[0]
    conteggi["companies_without_jobs"] = con.execute(
        "SELECT count(*) FROM aziende a LEFT JOIN offerte o "
        "ON a.ats=o.ats AND a.company_slug=o.company_slug "
        "WHERE o.id IS NULL").fetchone()[0]
    job_tec = ("SELECT DISTINCT ats,company_slug,trim(t) technology "
               "FROM offerte,UNNEST(technologies) u(t) WHERE trim(t)<>''")
    co_tec = ("SELECT DISTINCT ats,company_slug,trim(t) technology "
              "FROM aziende,UNNEST(technologies) u(t) WHERE trim(t)<>''")
    conteggi["missing_tech_pairs"] = con.execute(
        f"WITH j AS ({job_tec}), c AS ({co_tec}) "
        "SELECT count(*) FROM j LEFT JOIN c USING(ats,company_slug,technology) "
        "WHERE c.technology IS NULL").fetchone()[0]
    conteggi["ghost_tech_pairs"] = con.execute(
        f"WITH j AS ({job_tec}), c AS ({co_tec}) "
        "SELECT count(*) FROM c LEFT JOIN j USING(ats,company_slug,technology) "
        "WHERE j.technology IS NULL").fetchone()[0]
    conteggi["company_count_mismatch"] = con.execute(
        "WITH n AS (SELECT ats,company_slug,count(*) n FROM offerte GROUP BY 1,2) "
        "SELECT count(*) FROM aziende a LEFT JOIN n "
        "ON a.ats=n.ats AND a.company_slug=n.company_slug "
        "WHERE coalesce(n.n,0)<>TRY_CAST(json_extract_string(a.raw,'$.active_jobs') AS BIGINT)").fetchone()[0]
    actual = ("SELECT ats,company_slug,trim(t) technology,count(distinct id) n "
              "FROM offerte,UNNEST(technologies) u(t) WHERE trim(t)<>'' GROUP BY 1,2,3")
    declared = ("SELECT ats,company_slug,trim(t->>'technology') technology,"
                "TRY_CAST(t->>'active_jobs' AS BIGINT) n "
                "FROM aziende,UNNEST(CAST(json_extract(raw,'$.technologies') AS JSON[])) u(t) "
                "WHERE trim(t->>'technology')<>''")
    conteggi["tech_count_mismatch"] = con.execute(
        f"WITH a AS ({actual}), d AS ({declared}) "
        "SELECT count(*) FROM d LEFT JOIN a USING(ats,company_slug,technology) "
        "WHERE coalesce(a.n,0)<>d.n").fetchone()[0]
    return conteggi


def _file_valido(percorso: str | None) -> bool:
    return bool(percorso and os.path.isfile(percorso) and os.path.getsize(percorso) > 0)


def costruisci(cartella: str = CARTELLA, flusso_giorni: int = 7,
               db_path: str | None = None, attesa_minuti: int = 60) -> dict:
    destinazione = (db_path or os.environ.get("API_CLIENTI_DB")
                    or os.path.join(cartella, NOME_DB))
    cartella_dest = os.path.dirname(os.path.abspath(destinazione))
    if cartella_dest.startswith("/mnt/") and not os.path.ismount(cartella_dest):
        raise SystemExit(f"il volume {cartella_dest} non e' montato: "
                         "non costruisco il db sul disco di root")
    os.makedirs(cartella_dest, exist_ok=True)
    tmp = destinazione + ".tmp"
    for p in (tmp, tmp + ".wal"):
        try:
            os.remove(p)
        except OSError:
            pass
    _aspetta_export_oggi(cartella, attesa_minuti)
    f_offerte, f_aziende, manifest = _sorgenti_export(cartella)
    mancanti = [nome for nome, p in (("offerte-attive", f_offerte),
                                     ("aziende-segnali", f_aziende))
                if p is None]
    data_offerte = _DATA_RX.search(f_offerte).group(1) if f_offerte else None
    data_aziende = _DATA_RX.search(f_aziende).group(1) if f_aziende else None
    if data_offerte and data_aziende and data_offerte != data_aziende:
        raise SystemExit(f"export non allineati: offerte {data_offerte}, "
                         f"aziende {data_aziende} — non sostituisco il db live")
    data_export = data_offerte or data_aziende
    if not mancanti and (not _file_valido(f_offerte) or not _file_valido(f_aziende)):
        raise FileNotFoundError("file referenziati dal manifest assenti o vuoti")
    try:
        with tempfile.TemporaryDirectory(prefix=".duckdb-build-", dir=cartella_dest) as temp_dir:
            con = duckdb.connect(tmp)
            _configura(con, temp_dir)
            _crea_tabelle(con)
            n_offerte = _carica_nativa(con, f_offerte, "offerte",
                                       _COLONNE_OFFERTE) if f_offerte else 0
            n_aziende = _carica_nativa(con, f_aziende, "aziende",
                                       _COLONNE_AZIENDE) if f_aziende else 0
            con.execute("CHECKPOINT")
            con.close()
            con = duckdb.connect(tmp)
            _configura(con, temp_dir)
            n_flusso = _carica_flusso(con, cartella, flusso_giorni, temp_dir)
            manifest = _carica_copertura(con, cartella)
            invarianti = (_controlla_invarianti(con)
                          if n_offerte and n_aziende else {})
            if any(invarianti.values()):
                raise ValueError(f"invarianti export falliti: {invarianti}")
            inizio_flusso = dt.datetime.combine(
                dt.date.today() - dt.timedelta(days=flusso_giorni), dt.time.min,
                tzinfo=dt.UTC)
            meta = {
                "stato": "mancano gli export di oggi" if mancanti else "ok",
                "data_export": data_export or "",
                "offerte": n_offerte,
                "aziende": n_aziende,
                "flusso": n_flusso,
                "flusso_giorni": flusso_giorni,
                "flusso_dal": inizio_flusso.isoformat(),
                "file_offerte": f_offerte or "",
                "file_aziende": f_aziende or "",
                "invarianti": json.dumps(invarianti, sort_keys=True),
                "generato_at": dt.datetime.now(dt.UTC)
                               .isoformat(timespec="seconds"),
            }
            con.executemany("INSERT INTO meta VALUES (?, ?)",
                            [(k, str(v)) for k, v in meta.items()])
            con.close()
        svuotate = [t for t, n in (("offerte", n_offerte), ("aziende", n_aziende))
                    if n == 0 and _tabella_non_vuota(destinazione, t)]
        n_cop = len(manifest.get("coverage") or {}) + \
            len(manifest.get("coverage_aziende") or {})
        if n_cop == 0 and _tabella_non_vuota(destinazione, "copertura"):
            svuotate.append("copertura")
        if svuotate:
            os.remove(tmp)
            log.error("tabelle che diventerebbero vuote: %s — il db live "
                      "precedente resta al suo posto", svuotate)
            meta["stato"] = ("export mancanti: conservato il db precedente "
                             "con le tabelle piene")
            return meta
        if not mancanti and (not _file_valido(f_offerte) or not _file_valido(f_aziende)):
            raise FileNotFoundError("file referenziati dal manifest assenti o vuoti")
        expected_jobs = manifest.get("rows") if manifest.get("state") == "ready" else None
        expected_companies = (manifest.get("companies_rows")
                              if manifest.get("state") == "ready" else None)
        if expected_jobs is not None and int(expected_jobs) != n_offerte:
            raise ValueError(f"righe jobs diverse dal manifest: {expected_jobs} != {n_offerte}")
        if expected_companies is not None and int(expected_companies) != n_aziende:
            raise ValueError(
                f"righe companies diverse dal manifest: {expected_companies} != {n_aziende}")
        os.replace(tmp, destinazione)
    except Exception:
        for p in (tmp, tmp + ".wal"):
            try:
                os.remove(p)
            except OSError:
                pass
        raise
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
    ap.add_argument("--attesa-export", type=int, default=60, metavar="MINUTI",
                    help="quanto aspettare gli export di oggi prima di "
                         "costruire comunque (default %(default)s; 0 = subito)")
    args = ap.parse_args(argv)
    print(json.dumps(costruisci(args.cartella, args.flusso_giorni,
                                attesa_minuti=args.attesa_export),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
