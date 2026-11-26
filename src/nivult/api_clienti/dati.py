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
import os
import re
import threading
import time
import zlib

import duckdb

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


def _dove_export(filtri: dict) -> tuple[str, dict]:
    """I filtri del download, condivisi fra stima e scrittura."""
    # posted_at nel futuro (data d'inizio contratto): mai venduta come
    # data di pubblicazione, la stessa guardia delle ricerche (28/09).
    dove = ["(posted_at IS NULL OR posted_at <= current_timestamp)"]
    par: dict = {}
    if filtri.get("country"):
        dove.append("country = $country")
        par["country"] = str(filtri["country"])[:2].upper()
    if filtri.get("technology"):
        dove.append(_TEC.format(nome="technology"))
        par["technology"] = str(filtri["technology"])
    if filtri.get("dal"):
        dove.append("posted_at >= CAST($dal AS TIMESTAMP)")
        par["dal"] = _norm_ts(filtri["dal"])
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
                          "posted_at DESC NULLS LAST, id")


def _dove_export_aziende(filtri: dict) -> tuple[str, dict]:
    """I filtri del download aziende: paese e tecnologia, come le offerte
    (la data non ha senso sullo spine)."""
    dove = ["company IS NOT NULL"]
    par: dict = {}
    if filtri.get("country"):
        dove.append("country = $country")
        par["country"] = str(filtri["country"])[:2].upper()
    if filtri.get("technology"):
        dove.append(_TEC.format(nome="technology"))
        par["technology"] = str(filtri["technology"])
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
    return _scrivi_export("aziende", dove, par, percorso, tetto,
                          "employees DESC NULLS LAST, ats, company_slug")


def _scrivi_export(tabella: str, dove: str, par: dict, percorso: str,
                   tetto: int, ordine: str) -> int:
    """Scrive il JSONL filtrato e torna le righe scritte. Il download
    del portale: la riga intera, gia' pagata in crediti.

    La strada misurata il 28/09/2026 (due OOM-kill e un box in ginocchio
    per impararla): `SELECT raw` filtrata fa leggere a DuckDB l'intera
    colonna da 17 GB; fetchall di una fetta grossa materializza ~25 KB a
    riga in RAM python (83k righe = 2,3 GB). Quindi: una TEMP TABLE su
    una CONNESSIONE DEDICATA (il build da ~1 min non tiene il lock delle
    ricerche), che sotto memory_limit svuota su disco, poi lettura a
    pezzi da 10.000 — RSS misurato 1,8 GB e stabile. Se `tetto` e' >0 e
    le righe lo superano, alza ValueError: il chiamante ha gia' rifiutato
    la stima, questa e' la cintura."""
    import gzip
    nome = f"ex_{int(time.time() * 1000)}"   # solo cifre: nome sicuro
    tmp = os.path.join(os.path.dirname(os.path.abspath(_percorso)), "duckdb-tmp")
    os.makedirs(tmp, exist_ok=True)
    con = duckdb.connect(_percorso, read_only=True)
    try:
        con.execute("SET memory_limit = '3GB'")
        con.execute("SET threads = 4")
        con.execute("SET temp_directory = '" + tmp.replace("'", "''") + "'")
        con.execute("SET TimeZone = 'UTC'")
        con.execute(
            f"CREATE TEMP TABLE {nome} AS SELECT raw FROM {tabella} "
            f"WHERE {dove} ORDER BY {ordine}", par)
        tot = con.execute(f"SELECT count(*) FROM {nome}").fetchone()[0]
        if tetto and tot > tetto:
            raise ValueError(f"export oltre il tetto ({tetto} righe)")
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
                       len(technologies), company_slug, raw->>'domain', raw->>'logo',
                       raw->>'website', TRY_CAST(raw->>'active_jobs' AS INTEGER),
                       raw->>'size_range'
                  FROM aziende
                 WHERE {' AND '.join(dove)}
                 ORDER BY employees DESC NULLS LAST, ats, company_slug
                 LIMIT {limite + 1}""", par).fetchall()
    prossimo = None
    if len(righe) > limite:
        righe = righe[:limite]
        prossimo = _cursore([righe[-1][4], righe[-1][0], righe[-1][6]])
    return ([{"country": r[2], "industry": r[3], "employees": r[4],
              "n_tecnologie": r[5], "ref": r[0] + ":" + r[6],
              "company": r[1], "company_slug": r[6], "ats": r[0],
              "domain": r[7], "logo": r[8], "website": r[9],
              "open_jobs_count": r[10], "active_jobs": r[10],
              "employee_range": r[11]}
             for r in righe], prossimo, totale)


def profili_aziende(riferimenti: list[str]) -> dict[str, dict]:
    coppie = [r.split(":", 1) for r in riferimenti if ":" in r]
    if not coppie:
        return {}
    par: dict = {}
    condizioni = []
    for i, (ats, slug) in enumerate(coppie):
        par[f"a{i}"], par[f"s{i}"] = ats, slug
        condizioni.append(f"(ats = $a{i} AND company_slug = $s{i})")
    with _lock:
        righe = _conn().execute(
            "SELECT ats, company_slug, raw FROM aziende WHERE "
            + " OR ".join(condizioni), par).fetchall()
    out = {}
    for ats, slug, raw in righe:
        out[f"{ats}:{slug}"] = _senza_pii(json.loads(raw))
    return out


def rivela_azienda(riferimento: str) -> dict | None:
    """L'azienda intera, per chi ha pagato: nome, dominio, scheda.

    I portali nazionali hanno slug fittizi (jobboerse, pole-emploi,
    platsbanken…): la riga in `aziende` non esiste perche' l'azienda non
    e' il portale. Il ripiego per nome resta nello stesso ATS e nello
    stesso paese dell'offerta, e viene accettato solo se identifica UNA
    scheda. Altrimenti si risponde con la scheda snella esplicita, mai
    col profilo di un omonimo globale. Solo quando nemmeno il nome esiste
    (annunci anonimi dei PUP) si torna None: quello e' un 404 vero.
    """
    try:
        ats, slug = riferimento.split(":", 1)
    except ValueError:
        return None
    with _lock:
        r = _conn().execute(
            "SELECT raw FROM aziende WHERE ats = $a AND company_slug = $s "
            "LIMIT 1", {"a": ats, "s": slug}).fetchone()
        if r:
            return _senza_pii(json.loads(r[0]))
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
            return _senza_pii(json.loads(candidate[0][0]))
        return {"company": nome, "ats": ats, "company_slug": slug,
                "country": paese, "active_jobs": int(nome_r[1]),
                "profilo": "snello"}


def azienda_jobs_portale(riferimento: str) -> list[dict]:
    """Le offerte attive di un'azienda rivelata (titolo, luogo, data)."""
    try:
        ats, slug = riferimento.split(":", 1)
    except ValueError:
        return []
    with _lock:
        righe = _conn().execute(
            """SELECT id, title, country, city, category, posted_at
                 FROM offerte
                WHERE ats = $a AND company_slug = $s
                ORDER BY posted_at DESC NULLS LAST LIMIT 50""",
            {"a": ats, "s": slug}).fetchall()
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
