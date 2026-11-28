"""Il lettore del DuckDB dei clienti: l'unica porta da cui l'API legge.

read_only PER CONTRATTO: il file lo riscrive `aggiorna.py` ogni mattina
con os.replace (lo costruisce a parte e lo rinomina). Un lettore in
scrittura prenderebbe un lock sul file e il builder non potrebbe piu'
sostituirlo; in sola lettura DuckDB non prende lock, e chi aveva il
file aperto resta sul vecchio inode finche' non riapre.

Paginazione a chiave (keyset), mai OFFSET: con OFFSET 100000 il db
rilegge e butta 100k righe a ogni richiesta, e quando il builder
sostituisce il file fra una pagina e l'altra l'offset punta a una riga
diversa — buchi e doppioni silenziosi. Con la chiave dell'ultima riga
vista, la pagina dopo riparte ESATTAMENTE da li', su qualunque versione
del file: al peggio il cliente vede una riga sparita, mai un buco.

Cursore opaco: base64 del JSON con la chiave di ordinamento. Opaco per
contratto (il cliente non lo costruisce a mano), ma resta leggibile in
debug senza dover decifrare niente.

Ogni risposta restituisce la riga dell'export nel formato documentato,
senza i campi PII recruiter rimossi dal contratto. Le colonne tipizzate
servono solo a filtrare e ordinare.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import gzip
import json
import logging
import math
import os
import re
import threading
import time
import zlib

import duckdb

from .explorer import clean_company_name, employer_key

log = logging.getLogger("nivult.api_clienti.dati")

PERCORSO = os.environ.get("API_CLIENTI_DB",
                          "/opt/nivult/exports/api-clienti.duckdb")
LIMITE_MAX = 500  # oltre, il cliente vuole un export bulk, non un'API
_CAMPI_PII = frozenset({"contact_email", "recruiter"})


def _senza_pii(valore):
    if isinstance(valore, dict):
        return {k: _senza_pii(v) for k, v in valore.items() if k not in _CAMPI_PII}
    if isinstance(valore, list):
        return [_senza_pii(v) for v in valore]
    return valore


def stream_export_senza_pii(percorso: str):
    """Gzip JSONL sanitizzato riga per riga, anche da un export precedente."""
    def genera():
        compressore = zlib.compressobj(wbits=31)
        with gzip.open(percorso, "rt", encoding="utf-8") as f:
            for linea in f:
                riga = _senza_pii(json.loads(linea))
                dati = (json.dumps(riga, ensure_ascii=False, separators=(",", ":"))
                        + "\n").encode()
                blocco = compressore.compress(dati)
                if blocco:
                    yield blocco
        yield compressore.flush()
    return genera()


class DeltaRetentionError(ValueError):
    code = "delta_retention_exceeded"

    def __init__(self, since: str, retention_since: str):
        self.since = since
        self.retention_since = retention_since
        self.resync_required = True
        self.detail = {
            "code": self.code,
            "error": self.code,
            "message": "since predates the retained delta history; "
                       "perform a full resync",
            "since": since,
            "retention_since": retention_since,
            "action": "resync",
            "resync_required": True,
        }
        super().__init__(self.detail["message"])

    def as_dict(self) -> dict:
        return dict(self.detail)


_con: duckdb.DuckDBPyConnection | None = None
_con_mtime: float | None = None
_percorso = PERCORSO
_lock = threading.Lock()
# il lock della gestione della connessione (apri/chiudi/riapri): separato
# da _lock delle query, perche' _pagina chiama _conn tenendo il suo
_lock_conn = threading.Lock()
# duckdb-python: una connessione NON e' thread-safe. Il lock serializza
# le query dell'API; una scansione filtrata su 800k righe di colonnare
# si misura in millisecondi, quindi il collo di bottiglia e' altrove.


def _conn() -> duckdb.DuckDBPyConnection:
    global _con, _con_mtime
    # il lock della connessione e' SEPARATO da _lock (quello delle query):
    # _pagina tiene _lock mentre ci chiama — con lo stesso lock non
    # rientrante sarebbe deadlock (26/09/2026)
    with _lock_conn:
        try:
            m = os.path.getmtime(_percorso)
        except OSError:
            m = None
        if _con is not None and m != _con_mtime:
            # il builder ha sostituito il file stamattina: chi l'ha aperto
            # resta sull'inode vecchio (POSIX) — si riapre, o l'API servirebbe
            # l'export di ieri fino al prossimo riavvio
            _con.close()
            _con = None
        if _con is None:
            _con = duckdb.connect(_percorso, read_only=True)
            # 28/09/2026: senza un tetto di memoria, leggere la colonna
            # `raw` (17 GB) con qualunque filtro gonfiava il processo
            # finche' il kernel lo ammazzava (misurato: 4,9 GB di RSS
            # per 937 righe; con il tetto: 1,2 GB e 25s). DuckDB oltre il
            # tetto svuota su disco — sul volume, non sul root da 75 GB.
            tmp = os.path.join(os.path.dirname(os.path.abspath(_percorso)),
                               "duckdb-tmp")
            os.makedirs(tmp, exist_ok=True)
            _con.execute("SET memory_limit = '3GB'")
            _con.execute("SET threads = 4")
            _con.execute("SET temp_directory = '" + tmp.replace("'", "''") + "'")
            _con.execute("SET TimeZone = 'UTC'")
            _con_mtime = m
        return _con


def apri(percorso: str | None = None) -> None:
    """Sceglie il file da leggere e chiude la connessione precedente.

    Lazy: il file si apre davvero alla prima query. Chi importa il
    modulo prima che il builder sia mai girato (file inesistente) non
    deve fallire all'import.
    """
    global _con, _con_mtime, _percorso
    # entrambi i lock: _con e' di _lock_conn, ma chi ci chiama puo' gia'
    # tenere _lock — l'ordine di acquisizione e' sempre questo (_lock
    # poi _lock_conn), come in _pagina -> _conn
    with _lock, _lock_conn:
        if _con is not None:
            _con.close()
            _con = None
        _con_mtime = None
        _percorso = percorso or os.environ.get("API_CLIENTI_DB") or PERCORSO


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _norm_ts(v) -> str | None:
    """Una data in ingresso (cursore, dal, da) -> ISO naive in UTC.

    Nel db i timestamp sono naive in UTC (aggiorna._TS_SQL e _configura):
    chi arriva col fuso va convertito, non troncato — «12:00+02:00» e'
    le 10:00.
    """
    if not v:
        return None
    d = dt.datetime.fromisoformat(str(v))
    if d.tzinfo:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d.isoformat()


def _timestamp_utc(valore: str) -> dt.datetime:
    d = dt.datetime.fromisoformat(str(valore))
    if d.tzinfo is None:
        return d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def _cursore(chiave: list) -> str:
    return base64.urlsafe_b64encode(json.dumps(chiave).encode()).decode()


def _leggi_cursore(cursore: str) -> list:
    try:
        chiave = json.loads(base64.urlsafe_b64decode(cursore.encode()))
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"unreadable cursor: {cursore!r}") from e
    if not isinstance(chiave, list):
        raise ValueError(f"unreadable cursor: {cursore!r}")
    return chiave


def _pagina(sql: str, par: dict, limite: int,
            chiave_di) -> tuple[list[dict], str | None]:
    """Esegue chiedendo UNA riga in piu': se arriva, esiste la pagina
    dopo e il cursore punta all'ultima riga restituita."""
    with _lock:
        righe = _conn().execute(sql, par).fetchall()
    prossimo = None
    if len(righe) > limite:
        righe = righe[:limite]
        prossimo = _cursore(chiave_di(righe[-1]))
    return [_senza_pii(json.loads(r[0])) for r in righe], prossimo


def _limite(limite: int | None) -> int:
    return max(1, min(int(limite or 50), LIMITE_MAX))


# insensibile alle maiuscole: nel corpus la forma canonica c'e'
# («Python», misurato sul golden), ma il cliente la indovina come gli
# capita («python») — e una risposta vuota per colpa di una maiuscola
# e' un ticket di supporto, non un dato
_TEC = ("list_contains(list_transform(technologies, lambda x: lower(x)),"
        " lower(${nome}))")
_SEARCH_DOC = ("COALESCE(NULLIF(raw->>'description', ''), NULLIF(raw->>'content', ''), "
               "NULLIF(raw->>'descriptionHtml', ''), NULLIF(raw->>'descriptionPlain', ''), "
               "NULLIF(raw->>'externalDescription', ''), NULLIF(raw->>'jobDescription', ''), "
               "NULLIF(raw->>'job_description', ''), NULLIF(raw->>'Job_Description', ''), "
               "NULLIF(raw->>'body', ''), NULLIF(raw->>'content_html', ''), "
               "NULLIF(raw->>'description_html', ''), NULLIF(raw->>'descriptionBody', ''), "
               "NULLIF(raw->>'text', ''), NULLIF(raw->'_jobposting'->>'description', ''), "
               "NULLIF(raw->>'ShortDescriptionStr', ''), '')")


def _like(testo: str) -> str:
    """ILIKE con jolly spenti: il % e il _ del cliente sono LETTERE,
    non metacaratteri — «100%» deve trovare 100%, non tutto."""
    return ("%" + testo.replace("\\", "\\\\").replace("%", "\\%")
            .replace("_", "\\_") + "%")


def offerte(filtri: dict, cursore: str | None,
            limite: int) -> tuple[list[dict], str | None]:
    filtri = filtri or {}
    limite = _limite(limite)
    # posted_at nel futuro (data d'inizio contratto, guardia del 28/09):
    # finche' l'export non e' ricostruito, fuori dalla risposta.
    dove = ["(posted_at IS NULL OR posted_at <= current_timestamp)"]
    par: dict = {}
    for campo in ("country", "category", "ats", "seniority", "language"):
        if filtri.get(campo):
            dove.append(f"{campo} = ${campo}")
            par[campo] = filtri[campo]
    if filtri.get("remote") is not None:
        # il campo e' una stringa ('remote', 'onsite', 'hybrid'): il
        # booleano del client si traduce, il valore passa com'e'
        v = filtri["remote"]
        par["remote"] = {True: "remote", False: "onsite"}.get(v, str(v)) \
            if isinstance(v, bool) else str(v)
        dove.append("remote = $remote")
    if filtri.get("q"):
        dove.append(f"(title || ' ' || {_SEARCH_DOC}) ILIKE $q ESCAPE '\\'")
        par["q"] = _like(str(filtri["q"]))
    if filtri.get("technology"):
        dove.append(_TEC.format(nome="technology"))
        par["technology"] = str(filtri["technology"])
    if filtri.get("dal"):
        dove.append("posted_at >= CAST($dal AS TIMESTAMP)")
        par["dal"] = _norm_ts(filtri["dal"])
    if cursore:
        ts, pid = _leggi_cursore(cursore)
        par["pid"] = pid
        if ts is None:
            # posted_at NULL stanno in fondo (NULLS LAST): dopo una riga
            # senza data ci sono solo altre senza data, in ordine di id
            dove.append("posted_at IS NULL AND id > $pid")
        else:
            dove.append("(posted_at IS NULL"
                        " OR posted_at < CAST($pts AS TIMESTAMP)"
                        " OR (posted_at = CAST($pts AS TIMESTAMP)"
                        "     AND id > $pid))")
            par["pts"] = _norm_ts(ts)
    sql = f"""SELECT raw, posted_at, id
                FROM offerte
               WHERE {' AND '.join(dove)}
               ORDER BY posted_at DESC NULLS LAST, id
               LIMIT {limite + 1}"""
    return _pagina(sql, par, limite, lambda r: [_iso(r[1]), r[2]])


def cerca_portale(filtri: dict, cursore: str | None,
                  limite: int = 20) -> tuple[list[dict], str | None, int]:
    """Anteprima: il testo libero cerca solo nel titolo privato del datore."""
    filtri = filtri or {}
    limite = max(1, min(int(limite or 20), 20))
    # posted_at nel futuro: le «Lehre» scrivono la data d'inizio contratto
    # (guardia del 28/09 in adapters._dt); l'export di oggi e' nato prima
    # della pulizia e, con l'ordinamento DESC, quelle righe finirebbero
    # in cima alla vetrina. Fino al prossimo export si filtrano qui.
    dove = ["(posted_at IS NULL OR posted_at <= current_timestamp)"]
    par: dict = {}
    for campo in ("country", "category", "ats", "seniority", "language"):
        if filtri.get(campo):
            dove.append(f"{campo} = ${campo}")
            par[campo] = filtri[campo]
    if filtri.get("remote"):
        dove.append("remote = $remote")
        par["remote"] = str(filtri["remote"])
    if filtri.get("q"):
        titolo_sicuro = ("CASE WHEN NULLIF(trim(company), '') IS NULL "
                         "THEN '' ELSE replace(lower(coalesce(title, '')), "
                         "lower(company), 'hidden company') END")
        dove.append(f"({titolo_sicuro}) ILIKE $q ESCAPE '\\'")
        dove.append("(company IS NULL OR company NOT ILIKE $q ESCAPE '\\')")
        par["q"] = _like(str(filtri["q"]))
        q_normalizzato = re.sub(r"[^a-z0-9]+", "", str(filtri["q"]).lower())
        if len(q_normalizzato) >= 3:
            dove.append("(company IS NULL OR regexp_replace(lower(company), "
                        "'[^a-z0-9]+', '', 'g') NOT ILIKE $q_brand ESCAPE '\\')")
            dove.append("(raw->>'company_domain' IS NULL OR regexp_replace("
                        "lower(raw->>'company_domain'), '[^a-z0-9]+', '', 'g') "
                        "NOT ILIKE $q_brand ESCAPE '\\')")
            par["q_brand"] = _like(q_normalizzato)
    if filtri.get("technology"):
        dove.append(_TEC.format(nome="technology"))
        par["technology"] = str(filtri["technology"])
    if filtri.get("dal"):
        dove.append("posted_at >= CAST($dal AS TIMESTAMP)")
        par["dal"] = _norm_ts(filtri["dal"])
    if cursore:
        ts, pid = _leggi_cursore(cursore)
        par["pid"] = pid
        if ts is None:
            dove.append("posted_at IS NULL AND id > $pid")
        else:
            dove.append("(posted_at IS NULL"
                        " OR posted_at < CAST($pts AS TIMESTAMP)"
                        " OR (posted_at = CAST($pts AS TIMESTAMP)"
                        "     AND id > $pid))")
            par["pts"] = _norm_ts(ts)
    with _lock:
        totale = _conn().execute(
            f"SELECT count(*) FROM offerte WHERE {' AND '.join(dove)}",
            par).fetchone()[0]
        righe = _conn().execute(
            f"""SELECT id, title, country, city, seniority, remote,
                       category, posted_at, technologies, ats,
                       company, company_slug
                  FROM (
                    SELECT id, title, country, city, seniority, remote,
                           category, posted_at, technologies, ats,
                           company, company_slug,
                           row_number() OVER (
                             PARTITION BY coalesce(company_slug, company, id)
                             ORDER BY posted_at DESC NULLS LAST, id) AS _rn
                      FROM offerte
                     WHERE {' AND '.join(dove)})
                 WHERE _rn <= 3
                 ORDER BY posted_at DESC NULLS LAST, id
                 LIMIT {limite + 1}""", par).fetchall()
    prossimo = None
    if len(righe) > limite:
        righe = righe[:limite]
        prossimo = _cursore([_iso(righe[-1][7]), righe[-1][0]])
    return ([{"id": r[0], "title": r[1], "country": r[2], "city": r[3],
              "seniority": r[4], "remote": r[5], "category": r[6],
              "posted_at": _iso(r[7]), "technologies": r[8] or [],
              "ats": r[9], "company": r[10], "company_slug": r[11]}
             for r in righe], prossimo, totale)


def rivela_offerta(offerta_id: str) -> dict | None:
    """La riga intera, per chi ha pagato il reveal: azienda e URL dentro."""
    with _lock:
        r = _conn().execute(
            "SELECT raw FROM offerte WHERE id = $id",
            {"id": offerta_id}).fetchone()
    return _senza_pii(json.loads(r[0])) if r else None


_EXPORT_MULTIPLI = {
    "countries": "country", "technologies": "technology",
    "categories": "category", "industries": "industry",
}
_EXPORT_SOURCE_TYPES = frozenset({"direct", "aggregator", "staffing", "public_board"})


def _valori_export(filtri: dict, plurale: str, singolare: str) -> list[str]:
    raw = filtri.get(plurale)
    if raw is None or raw == "" or raw == []:
        raw = filtri.get(singolare)
    if raw is None or raw == "":
        return []
    if isinstance(raw, (str, bool)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise TypeError(f"{plurale} must be an array or a comma-separated string")
    if len(raw) > 50:
        raise ValueError(f"{plurale} accepts at most 50 values")
    values = []
    seen = set()
    for item in raw:
        if singolare == "remote" and isinstance(item, bool):
            item = "remote" if item else "onsite"
        if not isinstance(item, str):
            raise TypeError(f"{plurale} values must be strings")
        for value in item.split(","):
            value = value.strip()
            if not value:
                continue
            if len(value) > 120:
                raise ValueError(f"{plurale} values must be at most 120 characters")
            if singolare == "country":
                value = value.upper()
                if not re.fullmatch(r"[A-Z]{2}", value):
                    raise ValueError("countries must contain ISO 3166-1 alpha-2 codes")
            marker = value.casefold()
            if marker not in seen:
                seen.add(marker)
                values.append(value)
    return values


def _numero_export(valore, nome: str, intero: bool = False):
    if isinstance(valore, bool):
        raise TypeError(f"{nome} must be a non-negative number")
    try:
        numero = float(valore)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{nome} must be a non-negative number") from exc
    if not math.isfinite(numero) or numero < 0 or (intero and not numero.is_integer()):
        raise ValueError(f"{nome} must be a non-negative number")
    return int(numero) if intero else numero


def normalizza_filtri_export(corpo: dict, tipo: str) -> dict:
    """Valida e canonicalizza una sola volta i filtri di quote e download."""
    if not isinstance(corpo, dict):
        raise TypeError("export filters must be a JSON object")
    multipli = [("countries", "country"), ("technologies", "technology")]
    if tipo == "companies":
        multipli.append(("industries", "industry"))
        extra = {"employees_min", "employees_max", "min_active_jobs"}
    else:
        multipli.extend((plurale, singolare) for plurale, singolare in (
            ("categories", "category"), ("seniorities", "seniority"),
            ("remote_values", "remote"), ("employment_types", "employment_type"),
            ("languages", "language"), ("source_types", "source_type"),
        ))
        extra = {"q", "title", "posted_after", "posted_before", "dal", "has_salary",
                 "salary_min", "salary_max", "ats", "exclude_ats"}
    allowed = {"tipo", "max_rows", "limit", "tech_match", *extra}
    for plurale, singolare in multipli:
        allowed.update((plurale, singolare))
    unknown = sorted(set(corpo) - allowed)
    if unknown:
        raise ValueError(f"unsupported export filter: {unknown[0]}")

    out: dict = {}
    for plurale, singolare in multipli:
        values = _valori_export(corpo, plurale, singolare)
        if singolare == "source_type":
            values = ["public_board" if value.lower() == "public" else value.lower()
                      for value in values]
            if any(value not in _EXPORT_SOURCE_TYPES for value in values):
                raise ValueError("source_type must use direct, aggregator, staffing or public_board")
        if values:
            out[plurale] = values
    match = str(corpo.get("tech_match") or "any").lower()
    if match not in ("any", "all"):
        raise ValueError("tech_match must be any or all")
    out["tech_match"] = match

    for campo in ("q", "title"):
        if corpo.get(campo) is not None:
            if not isinstance(corpo[campo], str) or len(corpo[campo].strip()) > 200:
                raise ValueError(f"{campo} must be a string of at most 200 characters")
            if corpo[campo].strip():
                out[campo] = corpo[campo].strip()
    for campo in ("salary_min", "salary_max"):
        if corpo.get(campo) is not None:
            out[campo] = _numero_export(corpo[campo], campo)
    if out.get("salary_min") is not None and out.get("salary_max") is not None \
            and out["salary_min"] > out["salary_max"]:
        raise ValueError("salary_min cannot exceed salary_max")
    for campo in ("employees_min", "employees_max", "min_active_jobs"):
        if corpo.get(campo) is not None:
            out[campo] = _numero_export(corpo[campo], campo, intero=True)
    if out.get("employees_min") is not None and out.get("employees_max") is not None \
            and out["employees_min"] > out["employees_max"]:
        raise ValueError("employees_min cannot exceed employees_max")
    if corpo.get("has_salary") is not None:
        value = corpo["has_salary"]
        if isinstance(value, str) and value.lower() in ("true", "false"):
            value = value.lower() == "true"
        if not isinstance(value, bool):
            raise ValueError("has_salary must be true or false")
        out["has_salary"] = value
    for campo in ("ats", "exclude_ats"):
        if corpo.get(campo):
            values = _valori_export(corpo, campo + "_values", campo)
            if values:
                out[campo] = [value.lower() for value in values]
    dopo = corpo.get("posted_after") or corpo.get("dal")
    if dopo:
        out["dal"] = _norm_ts(dopo)
    if corpo.get("posted_before"):
        out["posted_before"] = _norm_ts(corpo["posted_before"])
    return out


def _where_in(dove: list[str], par: dict, expression: str, nome: str,
              values: list[str], upper: bool = False, negate: bool = False) -> None:
    if not values:
        return
    keys = []
    for index, value in enumerate(values):
        key = f"{nome}_{index}"
        par[key] = value.upper() if upper else value.lower()
        keys.append("$" + key)
    column = f"upper({expression})" if upper else f"lower(coalesce({expression}, ''))"
    dove.append(f"{column} {'NOT IN' if negate else 'IN'} ({', '.join(keys)})")


def _where_tech(dove: list[str], par: dict, filtri: dict) -> None:
    values = _valori_export(filtri, "technologies", "technology")
    conditions = []
    for index, value in enumerate(values):
        key = f"technology_{index}"
        par[key] = value
        conditions.append(_TEC.format(nome=key))
    if conditions:
        joiner = " AND " if filtri.get("tech_match") == "all" else " OR "
        dove.append("(" + joiner.join(conditions) + ")")


def _dove_export(filtri: dict) -> tuple[str, dict]:
    """I filtri del download, condivisi fra stima e scrittura."""
    dove = ["(posted_at IS NULL OR posted_at <= current_timestamp)"]
    par: dict = {}
    _where_in(dove, par, "country", "country",
              _valori_export(filtri, "countries", "country"), upper=True)
    _where_tech(dove, par, filtri)
    for plurale, singolare, column in (
            ("categories", "category", "category"),
            ("seniorities", "seniority", "seniority"),
            ("remote_values", "remote", "remote"),
            ("languages", "language", "language")):
        _where_in(dove, par, column, singolare,
                  _valori_export(filtri, plurale, singolare))
    _where_in(dove, par, "raw->>'employment_type'", "employment_type",
              _valori_export(filtri, "employment_types", "employment_type"))
    _where_in(dove, par, "coalesce(raw->>'source_type', 'direct')", "source_type",
              _valori_export(filtri, "source_types", "source_type"))
    _where_in(dove, par, "ats", "ats", _valori_export(filtri, "ats_values", "ats"))
    _where_in(dove, par, "ats", "exclude_ats",
              _valori_export(filtri, "exclude_ats_values", "exclude_ats"), negate=True)
    if filtri.get("q"):
        dove.append(f"(title || ' ' || {_SEARCH_DOC}) ILIKE $q ESCAPE '\\'")
        par["q"] = _like(str(filtri["q"]))
    if filtri.get("title"):
        dove.append("title ILIKE $title ESCAPE '\\'")
        par["title"] = _like(str(filtri["title"]))
    for campo in ("dal", "posted_before"):
        if filtri.get(campo):
            operator = ">=" if campo == "dal" else "<"
            dove.append(f"posted_at {operator} CAST(${campo} AS TIMESTAMP)")
            par[campo] = _norm_ts(filtri[campo])
    salary_present = ("(TRY_CAST(raw->>'salary_min' AS DOUBLE) IS NOT NULL OR "
                      "TRY_CAST(raw->>'salary_max' AS DOUBLE) IS NOT NULL)")
    if filtri.get("has_salary") is not None:
        dove.append(salary_present if filtri["has_salary"] else f"NOT {salary_present}")
    if filtri.get("salary_min") is not None:
        dove.append("TRY_CAST(raw->>'salary_min' AS DOUBLE) >= $salary_min")
        par["salary_min"] = float(filtri["salary_min"])
    if filtri.get("salary_max") is not None:
        dove.append("TRY_CAST(raw->>'salary_max' AS DOUBLE) <= $salary_max")
        par["salary_max"] = float(filtri["salary_max"])
    return " AND ".join(dove), par


def stima_export(filtri: dict) -> int:
    """Quante righe uscirebbero: il costo in crediti si calcola su questo."""
    dove, par = _dove_export(filtri)
    with _lock:
        return _conn().execute(
            f"SELECT count(*) FROM offerte WHERE {dove}", par).fetchone()[0]


def scrivi_export(filtri: dict, percorso: str, tetto: int = 0) -> int:
    """Le offerte: vedi _scrivi_export."""
    dove, par = _dove_export(filtri)
    return _scrivi_export("offerte", dove, par, percorso, tetto,
                          "posted_at DESC NULLS LAST, id DESC")


def _dove_export_aziende(filtri: dict) -> tuple[str, dict]:
    dove = ["company IS NOT NULL"]
    par: dict = {}
    _where_in(dove, par, "country", "country",
              _valori_export(filtri, "countries", "country"), upper=True)
    _where_tech(dove, par, filtri)
    _where_in(dove, par, "industry", "industry",
              _valori_export(filtri, "industries", "industry"))
    for campo, operator in (("employees_min", ">="), ("employees_max", "<="),
                            ("min_active_jobs", ">=")):
        if filtri.get(campo) is not None:
            column = ("employees" if campo != "min_active_jobs"
                      else "TRY_CAST(raw->>'active_jobs' AS BIGINT)")
            dove.append(f"{column} {operator} ${campo}")
            par[campo] = int(filtri[campo])
    return " AND ".join(dove), par


def stima_export_aziende(filtri: dict) -> int:
    dove, par = _dove_export_aziende(filtri)
    with _lock:
        return _conn().execute(
            f"SELECT count(*) FROM aziende WHERE {dove}", par).fetchone()[0]


def scrivi_export_aziende(filtri: dict, percorso: str,
                          tetto: int = 0) -> int:
    """Lo spine aziende filtrato, una riga per datore — il record intero
    con stack, sedi, dimensione e conteggi (03/10/2026)."""
    dove, par = _dove_export_aziende(filtri)
    ordine = ("TRY_CAST(raw->>'jobs_posted_30d' AS INTEGER) DESC NULLS LAST, "
              "TRY_CAST(raw->>'active_jobs' AS INTEGER) DESC NULLS LAST, "
              "ats, company_slug")
    return _scrivi_export("aziende", dove, par, percorso, tetto, ordine)


def _scrivi_export(tabella: str, dove: str, par: dict, percorso: str,
                   tetto: int, ordine: str) -> int:
    """Scrive al massimo `tetto` righe JSONL, nell'ordine richiesto.

    La TEMP TABLE su una connessione dedicata evita il lock delle ricerche
    e permette a DuckDB di svuotare su disco; la lettura resta a pezzi da
    10.000 per non materializzare l'intero export in RAM. `tetto=0` conserva
    il significato originale di nessun limite per gli altri chiamanti."""
    import gzip
    if isinstance(tetto, bool) or not isinstance(tetto, int) or tetto < 0:
        raise ValueError("tetto export non valido")
    nome = f"ex_{int(time.time() * 1000)}"   # solo cifre: nome sicuro
    tmp = os.path.join(os.path.dirname(os.path.abspath(_percorso)), "duckdb-tmp")
    os.makedirs(tmp, exist_ok=True)
    con = duckdb.connect(_percorso, read_only=True)
    try:
        con.execute("SET memory_limit = '3GB'")
        con.execute("SET threads = 4")
        con.execute("SET temp_directory = '" + tmp.replace("'", "''") + "'")
        con.execute("SET TimeZone = 'UTC'")
        limite = f" LIMIT {tetto}" if tetto else ""
        con.execute(
            f"CREATE TEMP TABLE {nome} AS SELECT raw FROM {tabella} "
            f"WHERE {dove} ORDER BY {ordine}{limite}", par)
        scritte = 0
        with gzip.open(percorso, "wt", encoding="utf-8") as f:
            while True:
                pezzo = con.execute(
                    f"SELECT raw FROM {nome} LIMIT 10000 "
                    f"OFFSET {scritte}").fetchall()
                if not pezzo:
                    break
                for (raw,) in pezzo:
                    pulito = _senza_pii(json.loads(raw))
                    f.write(json.dumps(pulito, ensure_ascii=False,
                                       separators=(",", ":")) + "\n")
                scritte += len(pezzo)
        return scritte
    finally:
        con.close()


def offerta_dettaglio_portale(offerta_id: str) -> dict | None:
    """Record interno; la proiezione HTTP decide se il datore e' sbloccato."""
    return rivela_offerta(offerta_id)


def cerca_aziende_portale(filtri: dict, cursore: str | None,
                          limite: int = 20) -> tuple[list[dict], str | None, int]:
    """Record interni: l'allowlist HTTP e' applicata dal chiamante."""
    filtri = filtri or {}
    limite = max(1, min(int(limite or 20), 20))
    dove = ["company IS NOT NULL"]
    par: dict = {}
    for campo in ("country", "industry"):
        if filtri.get(campo):
            dove.append(f"{campo} = ${campo}")
            par[campo] = filtri[campo]
    if filtri.get("technology"):
        dove.append(_TEC.format(nome="technology"))
        par["technology"] = str(filtri["technology"])
    if cursore:
        emp, a, s = _leggi_cursore(cursore)
        par.update(emp=emp, c_ats=a, c_slug=s)
        if emp is None:
            # i NULL stanno in fondo: dopo un NULL solo altri NULL
            dove.append("employees IS NULL AND (ats > $c_ats"
                        " OR (ats = $c_ats AND company_slug > $c_slug))")
        else:
            dove.append("(employees IS NULL OR employees < $emp"
                        " OR (employees = $emp AND (ats > $c_ats"
                        "     OR (ats = $c_ats AND company_slug > $c_slug))))")
    with _lock:
        totale = _conn().execute(
            f"SELECT count(*) FROM aziende WHERE {' AND '.join(dove)}",
            par).fetchone()[0]
        righe = _conn().execute(
            f"""SELECT ats, company, country, industry, employees,
                       len(technologies), technologies, company_slug,
                       raw->>'domain', raw->>'logo', raw->>'website',
                       TRY_CAST(raw->>'active_jobs' AS INTEGER),
                       COALESCE(raw->>'size_range', raw->>'employee_range') AS size_range,
                       TRY_CAST(raw->>'jobs_posted_30d' AS INTEGER),
                       CAST(raw->'languages' AS VARCHAR[]) AS languages,
                       raw->>'source_type'
                  FROM aziende
                 WHERE {' AND '.join(dove)}
                 ORDER BY employees DESC NULLS LAST, ats, company_slug
                 LIMIT {limite + 1}""", par).fetchall()
    prossimo = None
    if len(righe) > limite:
        righe = righe[:limite]
        prossimo = _cursore([righe[-1][4], righe[-1][0], righe[-1][7]])
    return ([{"country": r[2], "industry": r[3], "employees": r[4],
              "n_tecnologie": r[5], "technologies": r[6] or [],
              "ref": r[0] + ":" + r[7], "company": r[1], "company_slug": r[7],
              "ats": r[0], "domain": r[8], "logo": r[9], "website": r[10],
              "open_jobs_count": r[11], "active_jobs": r[11],
              "employee_range": r[12], "jobs_posted_30d": r[13],
              "languages": r[14] or [], "source_type": r[15]}
             for r in righe], prossimo, totale)


def _parse_company_ref(riferimento: str) -> tuple[str, str, str | None] | None:
    ats, separator, rest = str(riferimento or "").partition(":")
    slug, marker, employer = rest.partition("::employer:")
    if not separator or not ats or not slug:
        return None
    if marker and (not employer or not employer.isalnum()):
        return None
    return ats, slug, employer if marker else None


def board_multi_employer(riferimento: str) -> bool:
    parti = _parse_company_ref(riferimento)
    if not parti or parti[2] is not None:
        return False
    ats, slug, _ = parti
    with _lock:
        count = _conn().execute(
            "SELECT count(DISTINCT company) FROM offerte "
            "WHERE ats = $a AND company_slug = $s AND company IS NOT NULL",
            {"a": ats, "s": slug}).fetchone()[0]
    return int(count) > 1


def _profili_scoped(riferimenti: list[str]) -> dict[str, dict]:
    parsed = [(riferimento, _parse_company_ref(riferimento))
              for riferimento in riferimenti]
    scoped = [(riferimento, parti) for riferimento, parti in parsed
              if parti and parti[2] is not None]
    if not scoped:
        return {}
    basi = sorted({(parti[0], parti[1]) for _, parti in scoped})
    par: dict = {}
    condizioni = []
    for i, (ats, slug) in enumerate(basi):
        par[f"a{i}"], par[f"s{i}"] = ats, slug
        condizioni.append(f"(ats = $a{i} AND company_slug = $s{i})")
    with _lock:
        righe = _conn().execute(
            """SELECT ats, company_slug, company, country, count(*) AS n,
                      count(*) FILTER (
                        WHERE posted_at >= current_timestamp - INTERVAL '30 days') AS recent
                 FROM offerte
                WHERE company IS NOT NULL AND (""" + " OR ".join(condizioni) + """)
                GROUP BY ats, company_slug, company, country""", par).fetchall()
        base_profiles = {}
        names_by_ats: dict[str, set[str]] = {}
        for riferimento, (ats, slug, employer) in scoped:
            matching = [r for r in righe if r[0] == ats and r[1] == slug
                        and employer_key(r[2]) == employer]
            if not matching:
                continue
            ordered = sorted(matching, key=lambda r: (
                -int(r[4]), clean_company_name(r[2]), str(r[2]), str(r[3] or "")))
            nome = clean_company_name(ordered[0][2])
            paese = next((r[3] for r in ordered if r[3]), None)
            active = sum(int(r[4]) for r in matching)
            recent = sum(int(r[5]) for r in matching)
            base_profiles[riferimento] = (
                {"company": nome, "ats": ats, "company_slug": slug,
                 "country": paese, "active_jobs": active, "open_jobs_count": active,
                 "active_postings_count": active, "posted_30d_count": recent,
                 "jobs_posted_30d": recent, "profilo": "snello",
                 "_source_company": ordered[0][2]},
                nome, paese, employer)
            names_by_ats.setdefault(ats, set()).update(
                [nome.casefold(), *(str(r[2]).casefold() for r in matching)])
        candidate_par: dict = {}
        candidate_conditions = []
        for i, (ats, names) in enumerate(sorted(names_by_ats.items())):
            candidate_par[f"ca{i}"] = ats
            name_conditions = []
            for j, name in enumerate(sorted(names)):
                candidate_par[f"cn{i}_{j}"] = name
                name_conditions.append(f"lower(company) = $cn{i}_{j}")
            candidate_conditions.append(
                f"(ats = $ca{i} AND (" + " OR ".join(name_conditions) + "))")
        candidate = []
        if candidate_conditions:
            candidate = _conn().execute(
                "SELECT ats, company, country, raw FROM aziende WHERE "
                + " OR ".join(candidate_conditions), candidate_par).fetchall()
    out = {}
    for riferimento, (base, nome, paese, employer) in base_profiles.items():
        ats = base["ats"]
        profiles = [raw for candidate_ats, company, country, raw in candidate
                    if candidate_ats == ats and employer_key(company) == employer
                    and not (paese and country and country != paese)]
        if len(profiles) == 1:
            profile = _senza_pii(json.loads(profiles[0]))
            out[riferimento] = {
                **base, **profile, "company": nome,
                "active_jobs": base["active_jobs"],
                "open_jobs_count": base["open_jobs_count"],
                "active_postings_count": base["active_postings_count"],
                "posted_30d_count": base["posted_30d_count"],
                "jobs_posted_30d": base["jobs_posted_30d"]}
        else:
            out[riferimento] = base
    return out


def profili_aziende(riferimenti: list[str]) -> dict[str, dict]:
    parsed = [(riferimento, _parse_company_ref(riferimento))
              for riferimento in riferimenti]
    direct = [(riferimento, parti) for riferimento, parti in parsed
              if parti and parti[2] is None]
    out = {}
    if direct:
        par: dict = {}
        condizioni = []
        for i, (_, (ats, slug, _)) in enumerate(direct):
            par[f"a{i}"], par[f"s{i}"] = ats, slug
            condizioni.append(f"(ats = $a{i} AND company_slug = $s{i})")
        with _lock:
            righe = _conn().execute(
                "SELECT ats, company_slug, raw FROM aziende WHERE "
                + " OR ".join(condizioni), par).fetchall()
        for ats, slug, raw in righe:
            out[f"{ats}:{slug}"] = _senza_pii(json.loads(raw))
    out.update(_profili_scoped(riferimenti))
    return out


def rivela_azienda(riferimento: str) -> dict | None:
    """L'azienda intera, con il datore obbligatorio per i riferimenti scoped."""
    parti = _parse_company_ref(riferimento)
    if parti is None:
        return None
    ats, slug, employer = parti
    if employer is not None:
        return _profili_scoped([riferimento]).get(riferimento)
    multi = board_multi_employer(riferimento)
    with _lock:
        r = _conn().execute(
            "SELECT raw FROM aziende WHERE ats = $a AND company_slug = $s "
            "LIMIT 1", {"a": ats, "s": slug}).fetchone()
        if r:
            return {**_senza_pii(json.loads(r[0])), "_multi_employer": multi}
        nome_r = _conn().execute(
            """SELECT company, count(*) AS n FROM offerte
                WHERE ats = $a AND company_slug = $s AND company IS NOT NULL
                GROUP BY 1 ORDER BY n DESC LIMIT 1""",
            {"a": ats, "s": slug}).fetchone()
        if not nome_r or not nome_r[0]:
            return None
        nome = nome_r[0]
        paese_r = _conn().execute(
            """SELECT country, count(*) AS n FROM offerte
                WHERE ats = $a AND company_slug = $s AND company = $n
                  AND country IS NOT NULL
                GROUP BY 1 ORDER BY n DESC LIMIT 1""",
            {"a": ats, "s": slug, "n": nome}).fetchone()
        paese = paese_r[0] if paese_r else None
        par = {"a": ats, "n": nome}
        vincolo_paese = "country IS NULL"
        if paese:
            vincolo_paese = "country = $p"
            par["p"] = paese
        candidate = _conn().execute(
            "SELECT raw FROM aziende WHERE ats = $a"
            " AND lower(company) = lower($n) AND " + vincolo_paese +
            " LIMIT 2", par).fetchall()
        if len(candidate) == 1:
            return {**_senza_pii(json.loads(candidate[0][0])), "_multi_employer": multi}
        return {"company": nome, "ats": ats, "company_slug": slug,
                "country": paese, "active_jobs": int(nome_r[1]),
                "profilo": "snello", "_multi_employer": multi}


def azienda_jobs_portale(riferimento: str) -> list[dict]:
    """Le offerte attive dell'azienda rivelata (titolo, luogo, data)."""
    parti = _parse_company_ref(riferimento)
    if parti is None:
        return []
    ats, slug, employer = parti
    profilo = None
    if employer is not None:
        profilo = _profili_scoped([riferimento]).get(riferimento)
        if not profilo:
            return []
    with _lock:
        if employer is None:
            righe = _conn().execute(
                """SELECT id, title, country, city, category, posted_at, company
                     FROM offerte
                    WHERE ats = $a AND company_slug = $s
                    ORDER BY posted_at DESC NULLS LAST LIMIT 50""",
                {"a": ats, "s": slug}).fetchall()
        else:
            nome = profilo.get("_source_company") or profilo["company"]
            righe = _conn().execute(
                """SELECT id, title, country, city, category, posted_at, company
                     FROM offerte
                    WHERE ats = $a AND company_slug = $s
                      AND lower(company) = lower($n)
                    ORDER BY posted_at DESC NULLS LAST LIMIT 50""",
                {"a": ats, "s": slug, "n": nome}).fetchall()
    return [{"id": r[0], "title": r[1], "country": r[2], "city": r[3],
             "category": r[4], "posted_at": _iso(r[5])} for r in righe]


def cerca_chiuse_portale(filtri: dict, cursore: str | None,
                         limite: int = 20) -> tuple[list[dict], str | None, int]:
    """Offerte chiuse: il datore e' ricostruito dallo spine aziende."""
    filtri = filtri or {}
    limite = max(1, min(int(limite or 20), 20))
    azienda_sql = ("(SELECT a.company FROM aziende a WHERE a.ats = (f.raw->>'ats') "
                   "AND a.company_slug = (f.raw->>'company_slug') LIMIT 1)")
    dominio_sql = ("(SELECT a.raw->>'domain' FROM aziende a "
                   "WHERE a.ats = (f.raw->>'ats') "
                   "AND a.company_slug = (f.raw->>'company_slug') LIMIT 1)")
    dove = ["f.event = 'closed'"]
    par: dict = {}
    if filtri.get("country"):
        dove.append("(f.raw->>'country') = $country")
        par["country"] = str(filtri["country"])
    if filtri.get("remote"):
        dove.append("(f.raw->>'remote') = $remote")
        par["remote"] = str(filtri["remote"])
    if filtri.get("q"):
        titolo_sicuro = ("replace(lower(coalesce(f.raw->>'title', '')), lower("
                         + azienda_sql + "), 'hidden company')")
        dove.append(f"{azienda_sql} IS NOT NULL")
        dove.append(f"({titolo_sicuro}) ILIKE $q ESCAPE '\\'")
        dove.append(f"({azienda_sql}) NOT ILIKE $q ESCAPE '\\'")
        par["q"] = _like(str(filtri["q"]))
        q_normalizzato = re.sub(r"[^a-z0-9]+", "", str(filtri["q"]).lower())
        if len(q_normalizzato) >= 3:
            dove.append("regexp_replace(lower(" + azienda_sql + "), "
                        "'[^a-z0-9]+', '', 'g') NOT ILIKE $q_brand ESCAPE '\\'")
            dove.append("(" + dominio_sql + " IS NULL OR regexp_replace(lower("
                        + dominio_sql + "), '[^a-z0-9]+', '', 'g') "
                        "NOT ILIKE $q_brand ESCAPE '\\')")
            par["q_brand"] = _like(q_normalizzato)
    if filtri.get("technology"):
        dove.append("list_contains(list_transform("
                    "CAST(coalesce(f.raw->'skills', '[]'::json) AS VARCHAR[]), "
                    "lambda x: lower(x)), lower($technology))")
        par["technology"] = str(filtri["technology"])
    if cursore:
        t, cid = _leggi_cursore(cursore)
        dove.append("(f.t < CAST($ct AS TIMESTAMP)"
                    " OR (f.t = CAST($ct AS TIMESTAMP) AND f.id > $cid))")
        par.update(ct=_norm_ts(t), cid=cid)
    with _lock:
        totale = _conn().execute(
            f"SELECT count(*) FROM flusso f WHERE {' AND '.join(dove)}",
            par).fetchone()[0]
        righe = _conn().execute(
            f"""SELECT id, titolo, paese, citta, t, skills, azienda, slug, fonte,
                       company_domain
                  FROM (
                    SELECT f.id, f.raw->>'title' AS titolo,
                           f.raw->>'country' AS paese, f.raw->>'city' AS citta,
                           f.t, coalesce(f.raw->'skills', '[]'::json) AS skills,
                           {azienda_sql} AS azienda,
                           f.raw->>'company_slug' AS slug, f.raw->>'ats' AS fonte,
                           {dominio_sql} AS company_domain,
                           row_number() OVER (
                             PARTITION BY coalesce(f.raw->>'company_slug',
                                                   f.raw->>'company', f.id)
                             ORDER BY f.t DESC, f.id) AS _rn
                      FROM flusso f
                     WHERE {' AND '.join(dove)})
                 WHERE _rn <= 3
                 ORDER BY t DESC, id LIMIT {limite + 1}""", par).fetchall()
    prossimo = None
    if len(righe) > limite:
        righe = righe[:limite]
        prossimo = _cursore([_iso(righe[-1][4]), righe[-1][0]])
    return ([{"id": r[0], "title": r[1], "country": r[2], "city": r[3],
              "closed_at": _iso(r[4]), "category": None,
              "technologies": r[5] or [], "company": r[6],
              "company_slug": r[7], "ats": r[8], "company_domain": r[9],
              "title_requires_redaction": not bool(r[6])}
             for r in righe], prossimo, totale)


def aziende(filtri: dict, cursore: str | None,
            limite: int) -> tuple[list[dict], str | None]:
    filtri = filtri or {}
    limite = _limite(limite)
    dove = ["TRUE"]
    par: dict = {}
    for campo in ("country", "industry"):
        if filtri.get(campo):
            dove.append(f"{campo} = ${campo}")
            par[campo] = filtri[campo]
    if filtri.get("q"):
        dove.append("company ILIKE $q ESCAPE '\\'")
        par["q"] = _like(str(filtri["q"]))
    if filtri.get("technology"):
        dove.append(_TEC.format(nome="technology"))
        par["technology"] = str(filtri["technology"])
    if filtri.get("employees_min") is not None:
        dove.append("employees >= $employees_min")
        par["employees_min"] = int(filtri["employees_min"])
    if filtri.get("employees_max") is not None:
        dove.append("employees <= $employees_max")
        par["employees_max"] = int(filtri["employees_max"])
    if cursore:
        c, a, s = _leggi_cursore(cursore)
        par.update(c=c, a=a, s=s)
        if c is None:
            dove.append("company IS NULL AND (ats > $a"
                        " OR (ats = $a AND company_slug > $s))")
        else:
            # company NULL ordinano in fondo: dopo una riga col nome ci
            # sono anche TUTTE le senza nome, qualunque sia il cursore
            dove.append("(company IS NULL OR company > $c"
                        " OR (company = $c AND (ats > $a"
                        "     OR (ats = $a AND company_slug > $s))))")
    sql = f"""SELECT raw, company, ats, company_slug
                FROM aziende
               WHERE {' AND '.join(dove)}
               ORDER BY company, ats, company_slug
               LIMIT {limite + 1}"""
    return _pagina(sql, par, limite, lambda r: [r[1], r[2], r[3]])


def azienda(piattaforma: str, slug: str) -> dict | None:
    with _lock:
        r = _conn().execute(
            "SELECT raw FROM aziende"
            " WHERE ats = $ats AND company_slug = $slug LIMIT 1",
            {"ats": piattaforma, "slug": slug}).fetchone()
    return _senza_pii(json.loads(r[0])) if r else None


_CATALOGO_CAMPI = {
    "technology": "offerte", "country": "offerte", "category": "offerte",
    "ats": "offerte", "industry": "aziende",
}
_CatalogoItem = tuple[str, str, int]
_catalogo_cache: dict[tuple[str, str], tuple[float | None, tuple[_CatalogoItem, ...]]] = {}
_catalogo_cache_lock = threading.Lock()
_catalogo_fetch_locks: dict[tuple[str, str], threading.Lock] = {}


def _carica_catalogo(campo: str) -> tuple[_CatalogoItem, ...]:
    if campo == "technology":
        sorgente = ("SELECT unnest(technologies) AS value FROM offerte "
                    "WHERE technologies IS NOT NULL")
    else:
        sorgente = f"SELECT {campo} AS value FROM {_CATALOGO_CAMPI[campo]}"
    with _lock:
        righe = _conn().execute(f"""
            SELECT min(value) AS value, count(*) AS count
              FROM ({sorgente}) AS catalogo
             WHERE value IS NOT NULL AND trim(value) <> ''
             GROUP BY lower(value)""").fetchall()
    return tuple(sorted(
        ((str(r[0]), str(r[0]).casefold(), int(r[1])) for r in righe),
        key=lambda item: (-item[2], item[1])))


def _catalogo_snapshot(campo: str, mtime: float | None) -> tuple[_CatalogoItem, ...]:
    key = (_percorso, campo)
    with _catalogo_cache_lock:
        cached = _catalogo_cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        fetch_lock = _catalogo_fetch_locks.setdefault(key, threading.Lock())
    with fetch_lock:
        with _catalogo_cache_lock:
            cached = _catalogo_cache.get(key)
            if cached is not None and cached[0] == mtime:
                return cached[1]
        items = _carica_catalogo(campo)
        with _catalogo_cache_lock:
            _catalogo_cache[key] = (mtime, items)
        return items


def catalogo_export(campo: str, q: str = "", limite: int = 25) -> list[dict]:
    """Catalogo completo per dimensione; prefissi filtrati fuori dai lock."""
    if campo not in _CATALOGO_CAMPI:
        raise ValueError("unknown catalog field")
    q_cf = str(q or "").strip().casefold()
    if len(q_cf) > 80:
        raise ValueError("q must be at most 80 characters")
    limite = max(1, min(int(limite), 30))
    try:
        mtime = os.path.getmtime(_percorso)
    except OSError:
        mtime = None
    items = _catalogo_snapshot(campo, mtime)
    out = []
    for value, value_cf, count in items:
        if q_cf and not value_cf.startswith(q_cf):
            continue
        out.append({"value": value, "count": count})
        if len(out) == limite:
            break
    return out


def demo_tecnologie(q: str) -> dict:
    """Demo pubblica anonima: tecnologie e metadati, mai identita' aziendali."""
    with _lock:
        righe = _conn().execute("""
            SELECT company, country, industry,
                   list_filter(technologies,
                               lambda x: contains(lower(x), lower($q))) AS tec
              FROM aziende
             WHERE len(list_filter(technologies,
                                  lambda x: contains(lower(x), lower($q)))) > 0
               AND company IS NOT NULL
             ORDER BY employees DESC NULLS LAST, company
             LIMIT 24""", {"q": q}).fetchall()
    visti: set[str] = set()
    out = []
    for c, paese, settore, tec in righe:
        if c.lower() in visti or re.fullmatch(r"Q\d+", c):
            continue
        visti.add(c.lower())
        out.append({"country": paese, "industry": settore,
                    "technologies": list(tec)[:5]})
        if len(out) == 2:
            break
    return {"esempi": out, "altre": len(righe) > len(out)}


def demo_stats() -> dict:
    """I contatori della vetrina, da UNA fonte sola: il manifest dell'export.

    08/10/2026 — prima il payload mescolava la cache del cruscotto
    (ogni 4 minuti: offerte, aziende, paesi) con l'export del giorno
    (piattaforme, nuove, copertura), e i campi mancanti lasciavano in
    pagina i numeri embedded: stesso sito, due indici. Ora tutti i
    contatori vengono dal blocco `vetrina` del manifest, scritto dalle
    fasi dell'export nello stesso mattino: la pagina racconta sempre
    un solo indice, e si muove tutta insieme una volta al giorno.
    Il vecchio giro (cruscotto, poi DuckDB) resta come ripiego."""
    try:
        import json as _json
        with open("/opt/nivult/exports/manifest-ultimo.json") as f:
            m = _json.load(f)
        v = m.get("vetrina") or {}
        if v.get("offerte") and v.get("aziende") and v.get("paesi"):
            nuove = v.get("nuove_24h")
            chiuse_oggi = v.get("chiuse_24h")
            cambiamenti = (None if nuove is None or chiuse_oggi is None
                           else nuove + chiuse_oggi)
            return {"offerte": v["offerte"], "aziende": v["aziende"],
                    "paesi": v["paesi"],
                    "piattaforme": v.get("piattaforme"),
                    "nuove_24h": nuove,
                    "cambiamenti_24h": cambiamenti,
                    "chiuse_storico": v.get("chiuse_storico"),
                    "copertura": m.get("coverage") or {},
                    "copertura_aziende": m.get("coverage_aziende") or {}}
    except Exception:                                # noqa: BLE001
        pass                                         # ripiego sotto
    try:
        import json as _json
        d = _json.load(open("/opt/nivult/cruscotto-cache.json"))
        sal = d.get("v", {}).get("salute", {})
        if sal.get("offerte_attive") and time.time() - d["t"] < 900:
            nuove = None
            with _lock:
                nuove = _conn().execute(
                    "SELECT count(*) FROM flusso WHERE event = 'new' "
                    "AND t >= now() - INTERVAL '1 day'").fetchone()[0]
                cambiamenti = _conn().execute(
                    "SELECT count(*) FROM flusso "
                    "WHERE t >= now() - INTERVAL '1 day'").fetchone()[0]
                piattaforme = _conn().execute(
                    "SELECT count(DISTINCT ats) FROM offerte").fetchone()[0]
                righe_cop = _conn().execute(
                    "SELECT chiave, valore FROM copertura").fetchall()
            cop = {k: v for k, v in righe_cop if not k.startswith("az:")}
            cop_az = {k[3:]: v for k, v in righe_cop if k.startswith("az:")}
            return {"offerte": sal["offerte_attive"],
                    "aziende": sal.get("aziende_con_offerte"),
                    "paesi": sal.get("paesi"),
                    "piattaforme": piattaforme,
                    "nuove_24h": nuove,
                    # 04/10/2026: TUTTI i numeri della landing vengono da
                    # qui — sezioni diverse, stessa fonte, mai mele e pere
                    "cambiamenti_24h": cambiamenti,
                    "chiuse_storico": (d.get("v", {}).get("magazzino", {})
                                       .get("storico_chiuse")),
                    "copertura": cop,
                    "copertura_aziende": cop_az}
    except Exception:                                # noqa: BLE001
        pass                                         # ripiego sotto
    with _lock:
        offerte = _conn().execute("SELECT count(*) FROM offerte").fetchone()[0]
        aziende = _conn().execute("SELECT count(*) FROM aziende").fetchone()[0]
        paesi = _conn().execute(
            "SELECT count(DISTINCT country) FROM offerte").fetchone()[0]
        piattaforme = _conn().execute(
            "SELECT count(DISTINCT ats) FROM offerte").fetchone()[0]
        nuove = _conn().execute(
            "SELECT count(*) FROM flusso WHERE event = 'new' "
            "AND t >= now() - INTERVAL '1 day'").fetchone()[0]
        cambiamenti = _conn().execute(
            "SELECT count(*) FROM flusso "
            "WHERE t >= now() - INTERVAL '1 day'").fetchone()[0]
        righe_cop = _conn().execute(
            "SELECT chiave, valore FROM copertura").fetchall()
    cop = {k: v for k, v in righe_cop if not k.startswith("az:")}
    cop_az = {k[3:]: v for k, v in righe_cop if k.startswith("az:")}
    return {"offerte": offerte, "aziende": aziende,
            "paesi": paesi, "piattaforme": piattaforme,
            "nuove_24h": nuove, "cambiamenti_24h": cambiamenti,
            "chiuse_storico": None,
            "copertura": cop,
            "copertura_aziende": cop_az}


def _inizio_retention_delta() -> dt.datetime | None:
    meta = _meta()
    inizio = meta.get("flusso_dal")
    if inizio:
        return _timestamp_utc(inizio)
    generato = meta.get("generato_at")
    if not generato:
        return None
    giorni = int(meta.get("flusso_giorni", 7))
    return (_timestamp_utc(generato) - dt.timedelta(days=giorni)).replace(
        hour=0, minute=0, second=0, microsecond=0)


def cambiamenti(da: str, cursore: str | None,
                limite: int) -> tuple[list[dict], str | None]:
    limite = _limite(limite)
    dove = ["TRUE"]
    par: dict = {}
    if da:
        richiesto = _timestamp_utc(da)
        inizio = _inizio_retention_delta()
        if inizio is not None and richiesto < inizio:
            raise DeltaRetentionError(richiesto.isoformat(), inizio.isoformat())
        dove.append("t >= CAST($da AS TIMESTAMP)")
        par["da"] = _norm_ts(da)
    if cursore:
        t, cid = _leggi_cursore(cursore)
        dove.append("(t > CAST($ct AS TIMESTAMP)"
                    " OR (t = CAST($ct AS TIMESTAMP) AND id > $cid))")
        par.update(ct=_norm_ts(t), cid=cid)
    sql = f"""SELECT raw, t, id
                FROM flusso
               WHERE {' AND '.join(dove)}
               ORDER BY t, id
               LIMIT {limite + 1}"""
    return _pagina(sql, par, limite, lambda r: [_iso(r[1]), r[2]])


def copertura() -> dict:
    """I fill-rate dichiarati dal manifest dell'export.

    Forma: {campo: pct} per le offerte, piu' la chiave "aziende" col
    fill-rate dei campi azienda. I campi PII recruiter non sono esposti,
    nemmeno leggendo un DuckDB generato prima della loro rimozione."""
    with _lock:
        righe = _conn().execute(
            "SELECT chiave, valore FROM copertura").fetchall()
    out = {k: v for k, v in righe
           if not k.startswith("az:") and k not in {"contact_email", "recruiter"}}
    az = {k[3:]: v for k, v in righe if k.startswith("az:")}
    if az:
        out["aziende"] = az
    return out


def stato_export() -> dict:
    """Lo stato dell'export, nella forma servita dal router.

    Regola dell'API: le chiavi grezze di meta restano TUTTE (stato,
    data_export, offerte, aziende, flusso, flusso_giorni, flusso_dal,
    generato_at — le consuma il cruscotto interno) e ACCANTO la vista HTTP documentata
    (data, righe, file): i campi si aggiungono, non si rinominano mai.
    """
    meta = _meta()
    out = dict(meta)
    out["data"] = meta.get("data_export")
    out["righe"] = {"offerte": meta.get("offerte"),
                    "aziende": meta.get("aziende"),
                    "flusso": meta.get("flusso")}
    out["file"] = {"offerte": meta.get("file_offerte") or None,
                   "aziende": meta.get("file_aziende") or None}
    return out


def _meta() -> dict:
    """La tabella meta come dict; i conteggi tornano numeri, non stringhe."""
    with _lock:
        righe = _conn().execute("SELECT chiave, valore FROM meta").fetchall()
    out: dict = {}
    for k, v in righe:
        try:
            out[k] = int(v)
        except (TypeError, ValueError):
            out[k] = v
    return out
