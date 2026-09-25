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

Ogni risposta restituisce la riga dell'export COSI' COM'E' (il campo
`raw` riparsato): le colonne tipizzate servono solo a filtrare e
ordinare, il contratto col cliente e' il formato dell'export.
"""
from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import logging
import os
import threading

import duckdb

log = logging.getLogger("nivult.api_clienti.dati")

PERCORSO = os.environ.get("API_CLIENTI_DB",
                          "/opt/nivult/exports/api-clienti.duckdb")
LIMITE_MAX = 500  # oltre, il cliente vuole un export bulk, non un'API

_con: duckdb.DuckDBPyConnection | None = None
_con_mtime: float | None = None
_percorso = PERCORSO
_lock = threading.Lock()
# duckdb-python: una connessione NON e' thread-safe. Il lock serializza
# le query dell'API; una scansione filtrata su 800k righe di colonnare
# si misura in millisecondi, quindi il collo di bottiglia e' altrove.


def _conn() -> duckdb.DuckDBPyConnection:
    global _con, _con_mtime
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
        _con_mtime = m
    return _con


def apri(percorso: str | None = None) -> None:
    """Sceglie il file da leggere e chiude la connessione precedente.

    Lazy: il file si apre davvero alla prima query. Chi importa il
    modulo prima che il builder sia mai girato (file inesistente) non
    deve fallire all'import.
    """
    global _con, _con_mtime, _percorso
    with _lock:
        if _con is not None:
            _con.close()
            _con = None
        _con_mtime = None
        _percorso = percorso or PERCORSO


def _conn() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        _con = duckdb.connect(_percorso, read_only=True)
    return _con


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


def _norm_ts(v) -> str | None:
    """Una data in ingresso (cursore, dal, da) -> ISO naive in UTC.

    Nel db i timestamp sono naive in UTC (vedi aggiorna._ts: TIMESTAMPTZ
    richiederebbe pytz, che non e' tra le dipendenze): chi arriva col
    fuso va convertito, non troncato — «12:00+02:00» e' le 10:00.
    """
    if not v:
        return None
    d = dt.datetime.fromisoformat(str(v))
    if d.tzinfo:
        d = d.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return d.isoformat()


def _cursore(chiave: list) -> str:
    return base64.urlsafe_b64encode(json.dumps(chiave).encode()).decode()


def _leggi_cursore(cursore: str) -> list:
    try:
        chiave = json.loads(base64.urlsafe_b64decode(cursore.encode()))
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"cursore illeggibile: {cursore!r}") from e
    if not isinstance(chiave, list):
        raise ValueError(f"cursore illeggibile: {cursore!r}")
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
    return [json.loads(r[0]) for r in righe], prossimo


def _limite(limite: int | None) -> int:
    return max(1, min(int(limite or 50), LIMITE_MAX))


# insensibile alle maiuscole: nel corpus la forma canonica c'e'
# («Python», misurato sul golden), ma il cliente la indovina come gli
# capita («python») — e una risposta vuota per colpa di una maiuscola
# e' un ticket di supporto, non un dato
_TEC = ("list_contains(list_transform(technologies, x -> lower(x)),"
        " lower(${nome}))")


def offerte(filtri: dict, cursore: str | None,
            limite: int) -> tuple[list[dict], str | None]:
    filtri = filtri or {}
    limite = _limite(limite)
    dove = ["TRUE"]
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
        dove.append("title ILIKE $q")
        par["q"] = f"%{filtri['q']}%"
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
        dove.append("company ILIKE $q")
        par["q"] = f"%{filtri['q']}%"
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
    return json.loads(r[0]) if r else None


def demo_tecnologie(q: str) -> list[dict]:
    """La demo della vetrina pubblica: le aziende che assumono con la
    tecnologia q. Sola lettura, parametrizzata, otto righe al massimo,
    solo i campi che la pagina mostra. L'input e' gia' passato dalla
    whitelist caratteri della route (v1.demo_tecnologie)."""
    with _lock:
        righe = _conn().execute("""
            SELECT company, country, industry,
                   list_filter(technologies,
                               x -> contains(lower(x), lower($q))) AS tec
              FROM aziende
             WHERE len(list_filter(technologies,
                                  x -> contains(lower(x), lower($q)))) > 0
               AND company IS NOT NULL
             ORDER BY employees DESC NULLS LAST, company
             LIMIT 8""", {"q": q}).fetchall()
    return [{"company": c, "country": paese, "industry": settore,
             "technologies": list(tec)[:5]}
            for c, paese, settore, tec in righe]


def cambiamenti(da: str, cursore: str | None,
                limite: int) -> tuple[list[dict], str | None]:
    limite = _limite(limite)
    dove = ["TRUE"]
    par: dict = {}
    if da:
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
    """I fill-rate dichiarati dal manifest dell'export: {campo: pct}."""
    with _lock:
        righe = _conn().execute(
            "SELECT chiave, valore FROM copertura").fetchall()
    return {k: v for k, v in righe}


def stato_export() -> dict:
    """Lo stato dell'export, nella forma servita dal router.

    Regola dell'API: le chiavi grezze di meta restano TUTTE (stato,
    data_export, offerte, aziende, flusso, flusso_giorni, generato_at —
    le consuma il cruscotto interno) e ACCANTO la vista HTTP documentata
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
