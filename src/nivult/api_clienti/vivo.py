"""Il vivo: le letture OLTP dell'API clienti servite da Postgres.

Nato il 03/10/2026 dalla misura, non da un'idea: la stessa pagina da 101
offerte costava 47,8 secondi sul DuckDB dei clienti (la colonna raw da
20 GB sul volume di rete si rilegge INTERA a ogni query che la tocca)
e 0,5 secondi senza raw. Le liste paginate per chiave sono lavoro OLTP:
l'indice `ats_jobs_attive_posted_idx` le serve in millisecondi.

La regola «l'API non tocca il Postgres di produzione» si evolve, non si
abroga: nasceva contro le scansioni non indicizzate. Qui entra SOLO il
ruolo `nivult_api_lettura` (SELECT su cinque tabelle, statement_timeout
8s, connection limit 6), tutte le query sono per chiave o per indice, e
i valori viaggiano parametrizzati. DuckDB continua a fare il suo:
export bulk, copertura, flusso — il colonnare vince li'.

La risposta e' IDENTICA alla riga dell'export: stessa forma, stessi
nomi, stesse guardie (doppioni, date dal futuro). Cambia il motore,
non il contratto.
"""

from __future__ import annotations

import json
import logging
import os
import threading

import psycopg
from psycopg.rows import dict_row

from nivult.ats.esporta import _skills_pulite
from nivult.ats.testo import pulito

from .dati import (_cursore, _iso, _leggi_cursore, _like, _norm_ts)

log = logging.getLogger("nivult.api_clienti.vivo")

_DSN = os.environ.get("API_LETTURA_DSN", "")

_con: psycopg.Connection | None = None
# RLock, MAI Lock: _bench_stipendi tiene il lock e chiama _conn() che lo
# ripende — con un Lock semplice il primo stipendio stimato inchiodava
# l'API per sempre (03/10/2026, trovato in banco live).
_lock = threading.RLock()
_bench: dict | None = None


def _conn() -> psycopg.Connection:
    global _con
    with _lock:
        if _con is None or _con.closed:
            # autocommit: ogni statement e' una transazione a se' —
            # niente lock tenuti fra una pagina e l'altra
            _con = psycopg.connect(_DSN, autocommit=True,
                                   row_factory=dict_row)
        return _con


def _bench_stipendi() -> dict:
    """Il benchmark salari in memoria, come lo tiene esporta."""
    global _bench
    if _bench is None:
        with _lock:
            if _bench is None:
                b = {}
                for r in _conn().execute(
                        "SELECT country, currency, family, seniority,"
                        " p25, p50, p75, n FROM stipendi_benchmark"):
                    b[(r["country"], r["family"], r["seniority"])] = (
                        r["currency"], r["p25"], r["p50"], r["p75"], r["n"])
                _bench = b
    return _bench


# la stessa estrazione della descrizione dell'export (esporta._DESCR),
# valutata SOLO sulle righe della pagina
_DESCR = ("coalesce((SELECT v FROM unnest(ARRAY[raw->>'description', "
          "raw->>'content', raw->>'descriptionHtml', raw->>'descriptionPlain', "
          "raw->>'externalDescription', raw->>'jobDescription', "
          "raw->>'job_description', raw->>'Job_Description', raw->>'body', "
          "raw->>'content_html', raw->>'description_html', "
          "raw->>'descriptionBody', raw->>'text', "
          "raw->'_jobposting'->>'description', raw->>'ShortDescriptionStr']) v "
          "WHERE length(v) >= 80 LIMIT 1), '')")


def _dove(filtri: dict) -> tuple[str, dict]:
    """I filtri della lista, identici a dati.offerte. Ogni valore e' un
    PARAMETRO: mai concatenato, mai."""
    dove = ["j.expired_at IS NULL",
            "(j.posted_at IS NULL OR j.posted_at <= now())",
            # un annuncio per chiave di dedup: il piu' vecchio del gruppo,
            # la stessa regola dell'export
            "NOT EXISTS (SELECT 1 FROM ats_jobs d"
            "  WHERE d.duplicate_key = j.duplicate_key"
            "    AND j.duplicate_key IS NOT NULL"
            "    AND d.expired_at IS NULL"
            "    AND (COALESCE(d.created_at, d.posted_at, d.fetched_at), d.id)"
            "        < (COALESCE(j.created_at, j.posted_at, j.fetched_at),"
            "           j.id))"]
    par: dict = {}
    for campo, colonna in (("country", "j.country"), ("category", "x.family"),
                           ("ats", "j.platform_id"),
                           ("seniority", "j.seniority"),
                           ("language", "j.lang")):
        if filtri.get(campo):
            dove.append(f"{colonna} = %({campo})s")
            par[campo] = str(filtri[campo])
    if filtri.get("remote") is not None:
        v = filtri["remote"]
        par["remote"] = {True: "remote", False: "onsite"}.get(v, str(v)) \
            if isinstance(v, bool) else str(v)
        dove.append("j.remote = %(remote)s")
    if filtri.get("q"):
        dove.append("j.title ILIKE %(q)s ESCAPE '\\'")
        par["q"] = _like(str(filtri["q"]))
    if filtri.get("technology"):
        dove.append("EXISTS (SELECT 1 FROM tecnologie_v1 tv"
                    "  WHERE tv.job_id = j.id"
                    "    AND EXISTS (SELECT 1 FROM"
                    "      jsonb_array_elements_text(tv.tecnologie) e(x)"
                    "      WHERE lower(e.x) = lower(%(technology)s)))")
        par["technology"] = str(filtri["technology"])
    if filtri.get("dal"):
        dove.append("j.posted_at >= %(dal)s::timestamptz")
        par["dal"] = _norm_ts(filtri["dal"])
    return " AND ".join(dove), par


def _riga_export(j: dict) -> dict:
    """La riga JSON dell'export, ricostruita identica dalle colonne."""
    stima: dict = {}
    if j["salary_min"] is None and j.get("country") and j.get("category"):
        bench = _bench_stipendi()
        cella = bench.get((j["country"], j["category"], j["seniority"] or ""))
        livello = f"{j['country']} x {j['category']} x {j['seniority']}"
        if not cella:
            cella = bench.get((j["country"], j["category"], ""))
            livello = f"{j['country']} x {j['category']}, all seniorities"
        if cella:
            val, p25, p50, p75, camp = cella
            stima = {"salary_is_estimate": True, "salary_estimate_min": p25,
                     "salary_estimate_median": p50,
                     "salary_estimate_max": p75,
                     "salary_estimate_currency": val,
                     "salary_estimate_basis":
                         f"Nivult benchmark: {camp} observed postings, "
                         f"{livello}"}
    geo = ({"latitude": j["latitude"], "longitude": j["longitude"],
            "source": j["geo_da"]} if j.get("latitude") is not None else None)
    riga = {
        "id": str(j["id"]), "title": j["title"], "ats": j["platform_id"],
        "company_slug": j["slug"], "company": j["company"], "url": j["url"],
        "country": j["country"], "city": j["city"], "location": j["location"],
        "language": j["lang"], "seniority": j["seniority"],
        "remote": j["remote"], "skills": _skills_pulite(j["skills"]),
        "salary_min": (float(j["salary_min"])
                       if j["salary_min"] is not None else None),
        "salary_max": (float(j["salary_max"])
                       if j["salary_max"] is not None else None),
        "salary_currency": j["salary_currency"],
        "posted_at": _iso(j["posted_at"]),
        "first_seen": _iso(j["first_seen"]), "last_seen": _iso(j["last_seen"]),
        "description": pulito(j["description"]),
        "category": j["category"], "employment_type": j["employment_type"],
        "contact_email": j["contact_email"],
        "languages_required": list(j["languages_required"] or []), **stima,
        "external_id": j["external_id"], "department": j["department"],
        "hours_type": j["orario"], "duration_type": j["durata"],
        "salary_period": j["salary_period"], "salary_source": j["salary_da"],
        "salary_text": j["salary_text"],
        "posted_at_estimated": True if j["posted_at_estimated"] else None,
        "technologies": list(j["tecnologie"] or []),
        "summary": j["sintesi"], "summary_source": j["sintesi_da"],
        "management_level": j["management_level"],
        "is_decision_maker": True if j["is_decision_maker"] else None,
        "shift_schedule": j["shift_schedule"], "work_hours": j["work_hours"],
        "is_urgently_hiring": True if j["is_urgently_hiring"] else None,
        "benefits": list(j["benefits"] or []),
        "valid_through": j["valid_through"], "state": j["state"],
        "postal_code": j["postal_code"], "geo": geo,
        "applicants_count": j["applicants_count"],
        "is_easy_apply": True if j["is_easy_apply"] else None,
        "recruiter": j["recruiter"],
        "job_sources": list(j["job_sources"] or []),
    }
    return {k: v for k, v in riga.items() if v not in (None, [], "")}


_COLONNE = f"""
    j.id, j.title, j.platform_id, j.slug,
    COALESCE(co.company_name, j.raw->'company'->>'name') AS company,
    j.url, j.country, j.city, j.location, j.lang, j.seniority, j.remote,
    j.skills, j.salary_min, j.salary_max, j.salary_currency, j.posted_at,
    COALESCE(j.created_at, j.posted_at, j.fetched_at) AS first_seen,
    j.fetched_at AS last_seen, {_DESCR} AS description, x.family AS category,
    j.employment_type, j.contact_email, j.languages_required, j.external_id,
    j.department, j.orario, j.durata, j.salary_period, j.salary_da,
    j.posted_at_estimated, t.tecnologie, sf.sintesi, sf.da AS sintesi_da,
    d.management_level, d.is_decision_maker, d.shift_schedule, d.work_hours,
    d.is_urgently_hiring, d.benefits, d.salary_text, d.valid_through,
    d.state, d.postal_code, d.latitude, d.longitude, d.geo_da,
    d.applicants_count, d.is_easy_apply, d.recruiter, fo.job_sources
  FROM ats_jobs j
  LEFT JOIN ats_companies co ON co.platform_id = j.platform_id
                            AND co.slug = j.slug
  LEFT JOIN job_classifications x ON x.job_id = j.id
  LEFT JOIN tecnologie_v1 t ON t.job_id = j.id
  LEFT JOIN sintesi_finali sf ON sf.job_id = j.id
  LEFT JOIN offerte_dettagli d ON d.job_id = j.id
  LEFT JOIN LATERAL (
       SELECT jsonb_agg(jsonb_build_object(
                'id', k.id, 'ats', k.platform_id, 'company_slug', k.slug,
                'url', k.url, 'posted_at', k.posted_at,
                'status', CASE WHEN k.expired_at IS NULL
                               THEN 'active' ELSE 'expired' END)
                ORDER BY k.posted_at DESC NULLS LAST) AS job_sources
         FROM ats_jobs k
        WHERE j.duplicate_key IS NOT NULL
          AND k.duplicate_key = j.duplicate_key AND k.id <> j.id) fo ON true"""


def offerte(filtri: dict, cursore: str | None,
            limite: int) -> tuple[list[dict], str | None]:
    """La pagina di offerte VIVE, per chiave: prima gli id via indice,
    poi le righe piene per quegli id — mai una scansione."""
    limite = max(1, min(int(limite or 50), 500))
    dove, par = _dove(filtri)
    if cursore:
        ts, pid = _leggi_cursore(cursore)
        par["pid"] = pid
        if ts is None:
            dove += (" AND j.posted_at IS NULL AND j.id > %(pid)s::uuid")
        else:
            dove += (" AND (j.posted_at IS NULL"
                     "  OR j.posted_at < %(pts)s::timestamptz"
                     "  OR (j.posted_at = %(pts)s::timestamptz"
                     "      AND j.id > %(pid)s::uuid))")
            par["pts"] = _norm_ts(ts)
    # passo 1: la pagina di chiavi, sull'indice
    chiavi = _conn().execute(
        f"""SELECT j.id, j.posted_at
              FROM ats_jobs j
              LEFT JOIN job_classifications x ON x.job_id = j.id
             WHERE {dove}
             ORDER BY j.posted_at DESC NULLS LAST, j.id
             LIMIT {limite + 1}""", par).fetchall()
    prossimo = None
    if len(chiavi) > limite:
        chiavi = chiavi[:limite]
        prossimo = _cursore([_iso(chiavi[-1]["posted_at"]),
                             str(chiavi[-1]["id"])])
    if not chiavi:
        return [], prossimo
    # passo 2: le righe piene, per chiave primaria
    righe = _conn().execute(
        f"SELECT {_COLONNE} WHERE j.id = ANY(%(ids)s::uuid[])",
        {"ids": [str(c["id"]) for c in chiavi]}).fetchall()
    per_id = {str(r["id"]): r for r in righe}
    return ([_riga_export(per_id[str(c["id"])]) for c in chiavi
             if str(c["id"]) in per_id], prossimo)


def rivela_offerta(offerta_id: str) -> dict | None:
    """Il dettaglio, per chiave primaria: millisecondi, non 50 secondi."""
    try:
        riga = _conn().execute(
            f"SELECT {_COLONNE} WHERE j.id = %(id)s::uuid",
            {"id": offerta_id}).fetchone()
    except Exception:                                # noqa: BLE001
        return None                                  # id malformato = 404
    return _riga_export(riga) if riga else None
