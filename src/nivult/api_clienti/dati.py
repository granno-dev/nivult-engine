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
import re
import threading
import time

import duckdb

log = logging.getLogger("nivult.api_clienti.dati")

PERCORSO = os.environ.get("API_CLIENTI_DB",
                          "/opt/nivult/exports/api-clienti.duckdb")
LIMITE_MAX = 500  # oltre, il cliente vuole un export bulk, non un'API

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
            tmp = os.path.join(os.path.dirname(_percorso), "duckdb-tmp")
            os.makedirs(tmp, exist_ok=True)
            _con.execute("SET memory_limit = '1500MB'")
            _con.execute(f"SET temp_directory = '{tmp}'")
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
        _percorso = percorso or PERCORSO


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
        dove.append("title ILIKE $q ESCAPE '\\'")
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
                  limite: int = 25) -> tuple[list[dict], str | None, int]:
    """La ricerca del portale: righe MASCHERATE (azienda e URL si
    rivelano con un credito, il modello visto su TheirStack il 27/09).

    Cercare e' gratis, rivelare no: e' il gancio che trasforma la
    curiosita' in piano. Stessi filtri di offerte(), ma la SELECT non
    tocca raw: il contenuto dell'annuncio non esce da qui."""
    filtri = filtri or {}
    limite = max(1, min(int(limite or 25), 50))
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
        dove.append("title ILIKE $q ESCAPE '\\'")
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
        dove.append("(posted_at IS NULL"
                    " OR posted_at < CAST($pts AS TIMESTAMP)"
                    " OR (posted_at = CAST($pts AS TIMESTAMP)"
                    "     AND id > $pid))")
        par["pts"] = _norm_ts(ts)
    with _lock:
        totale = _conn().execute(
            f"SELECT count(*) FROM offerte WHERE {' AND '.join(dove)}",
            par).fetchone()[0]
        # la regola della vetrina (28/09): max 3 righe per datore — una
        # bacheca sola non deve occupare la pagina e far pensare al
        # cliente «c'e' solo quella». Chi senza azienda non si tappa
        # (partition per id = nessun tappo). Il TOTALE resta quello
        # pieno: la diversita' e' presentazione, non misura.
        righe = _conn().execute(
            f"""SELECT id, title, country, city, seniority, remote,
                       category, posted_at, technologies, ats,
                       company, company_slug
                  FROM (
                    SELECT *, row_number() OVER (
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
    return json.loads(r[0]) if r else None


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
    tmp = os.path.join(os.path.dirname(_percorso), "duckdb-tmp")
    os.makedirs(tmp, exist_ok=True)
    con = duckdb.connect(_percorso, read_only=True)
    try:
        con.execute("SET memory_limit = '1500MB'")
        con.execute(f"SET temp_directory = '{tmp}'")
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
                    f.write(raw + "\n")
                scritte += len(pezzo)
        return scritte
    finally:
        con.close()


def offerta_dettaglio_portale(offerta_id: str) -> dict | None:
    """Il dettaglio COMPLETO dell'offerta: dal 28/09 l'identita' del
    datore e' gratis (modello TheirStack — l'annuncio e' comunque
    pubblico alla fonte); il prodotto in vendita e' il PROFILO azienda."""
    return rivela_offerta(offerta_id)


def cerca_aziende_portale(filtri: dict, cursore: str | None,
                          limite: int = 25) -> tuple[list[dict], str | None, int]:
    """Le aziende MASCHERATE: settore, paese, dimensione e il numero di
    tecnologie si vedono; il nome e la chiave si rivelano a credito."""
    filtri = filtri or {}
    limite = max(1, min(int(limite or 25), 50))
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
                       len(technologies), company_slug
                  FROM aziende
                 WHERE {' AND '.join(dove)}
                 ORDER BY employees DESC NULLS LAST, ats, company_slug
                 LIMIT {limite + 1}""", par).fetchall()
    prossimo = None
    if len(righe) > limite:
        righe = righe[:limite]
        # il cursore porta slug e piattaforma, mai il nome: la
        # mascheratura non deve svelarsi in base64
        prossimo = _cursore([righe[-1][4], righe[-1][0], righe[-1][6]])
    # il nome resta nel conteggio delle tecnologie, mai nel payload
    return ([{"country": r[2], "industry": r[3], "employees": r[4],
              "n_tecnologie": r[5], "ref": r[0] + ":" + r[6]}
            for r in righe], prossimo, totale)


def rivela_azienda(riferimento: str) -> dict | None:
    """L'azienda intera, per chi ha pagato: nome, dominio, scheda.

    I portali nazionali hanno slug fittizi (jobboerse, pole-emploi,
    platsbanken…): la riga in `aziende` non esiste perche' l'azienda non
    e' il portale. Ma il datore vero, quando l'offerta lo dichiara, sta
    in `offerte.company`: si cerca la sua scheda VERA per nome (Randstad
    esiste come azienda, anche se l'offerta e' arrivata via agenzie), e
    se non c'e' si risponde con una scheda snella onesta — nome, paese,
    quante offerte attive ha adesso. Solo quando nemmeno il nome esiste
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
            return json.loads(r[0])
        nome_r = _conn().execute(
            """SELECT company, count(*) AS n FROM offerte
                WHERE ats = $a AND company_slug = $s AND company IS NOT NULL
                GROUP BY 1 ORDER BY n DESC LIMIT 1""",
            {"a": ats, "s": slug}).fetchone()
        if not nome_r or not nome_r[0]:
            return None
        nome = nome_r[0]
        r = _conn().execute(
            "SELECT raw FROM aziende WHERE lower(company) = lower($n) "
            "LIMIT 1", {"n": nome}).fetchone()
        if r:
            return json.loads(r[0])
        paese = _conn().execute(
            """SELECT country, count(*) AS n FROM offerte
                WHERE ats = $a AND company_slug = $s AND country IS NOT NULL
                GROUP BY 1 ORDER BY n DESC LIMIT 1""",
            {"a": ats, "s": slug}).fetchone()
        return {"company": nome, "ats": ats, "company_slug": slug,
                "country": paese[0] if paese else None,
                "active_jobs": int(nome_r[1]), "profilo": "snello"}


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
                         limite: int = 25) -> tuple[list[dict], str | None, int]:
    """Le offerte CHIUSE, dal flusso: stesso mascheramento delle attive."""
    filtri = filtri or {}
    limite = max(1, min(int(limite or 25), 50))
    dove = ["event = 'closed'"]
    par: dict = {}
    if filtri.get("q"):
        dove.append("raw->>'title' ILIKE $q ESCAPE '\\'")
        par["q"] = _like(str(filtri["q"]))
    if filtri.get("technology"):
        # nel flusso il campo si chiama skills (le technologies vengono
        # dopo, dal modello: una riga chiusa e' il ricordo dell'ultima
        # foto, non l'arricchimento completo)
        dove.append("list_contains(list_transform("
                    "CAST(coalesce(raw->'skills', '[]'::json) AS VARCHAR[]), "
                    "x -> lower(x)), lower($technology))")
        par["technology"] = str(filtri["technology"])
    if cursore:
        t, cid = _leggi_cursore(cursore)
        dove.append("(t < CAST($ct AS TIMESTAMP)"
                    " OR (t = CAST($ct AS TIMESTAMP) AND id > $cid))")
        par.update(ct=_norm_ts(t), cid=cid)
    with _lock:
        totale = _conn().execute(
            f"SELECT count(*) FROM flusso WHERE {' AND '.join(dove)}",
            par).fetchone()[0]
        # anche le chiuse: max 3 righe per datore (la stessa regola
        # della vetrina delle attive)
        righe = _conn().execute(
            f"""SELECT id, titolo, paese, citta, t, skills, azienda, slug, fonte
                  FROM (
                    SELECT id, raw->>'title' AS titolo,
                           raw->>'country' AS paese, raw->>'city' AS citta,
                           t, coalesce(raw->'skills', '[]'::json) AS skills,
                           raw->>'company' AS azienda,
                           raw->>'company_slug' AS slug, raw->>'ats' AS fonte,
                           row_number() OVER (
                             PARTITION BY coalesce(raw->>'company_slug',
                                                   raw->>'company', id)
                             ORDER BY t DESC, id) AS _rn
                      FROM flusso
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
              "company_slug": r[7], "ats": r[8]}
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
    return json.loads(r[0]) if r else None


def demo_tecnologie(q: str) -> dict:
    """La demo della vetrina pubblica: DUE aziende che assumono con la
    tecnologia q. Il resto si sblocca con la chiave di prova: se la pagina
    mostrasse la lista intera regaleremmo il prodotto (25/09/2026), e il
    TOTALE esatto non si da' piu' (26/09/2026, decisione di Giuseppe): la
    dimensione per-tecnologia e' merce, non marketing."""
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
             LIMIT 24""", {"q": q}).fetchall()
    # niente tenant duplicati ne' id Wikidata non risolti («Q689791»)
    visti: set[str] = set()
    out = []
    for c, paese, settore, tec in righe:
        if c.lower() in visti or re.fullmatch(r"Q\d+", c):
            continue
        visti.add(c.lower())
        out.append({"company": c, "country": paese, "industry": settore,
                    "technologies": list(tec)[:5]})
        if len(out) == 2:
            break
    # «ci sono altre aziende?» si', il numero no
    return {"esempi": out, "altre": len(righe) > len(out)}


def demo_stats() -> dict:
    """I quattro contatori della vetrina, freschi come il cruscotto.

    Endpoint pubblico senza parametri: niente input, niente superficie.
    Prima la cache del cruscotto (ricalcolata ogni 4 minuti: gli stessi
    numeri che vede Giuseppe), poi l'export del giorno come ripiego.
    """
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
    """I fill-rate dichiarati dal manifest dell'export.

    Forma: {campo: pct} per le offerte (invariata dal giorno uno) piu'
    la chiave "aziende" (27/09/2026) col fill-rate dei campi azienda.
    Le chiavi si aggiungono, non si rinominano mai."""
    with _lock:
        righe = _conn().execute(
            "SELECT chiave, valore FROM copertura").fetchall()
    out = {k: v for k, v in righe if not k.startswith("az:")}
    az = {k[3:]: v for k, v in righe if k.startswith("az:")}
    if az:
        out["aziende"] = az
    return out


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
