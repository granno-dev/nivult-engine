"""Il primo stadio del matching: i filtri deterministici, prima del modello.

REGOLA DEFINITIVA (misurata sul campo): un campo NULL — o una lista vuota, che
è il modo in cui una fonte dice "non lo so" — NON esclude mai. Escludere per
assenza di dato nasconderebbe un'offerta per un motivo che non è una scelta
dell'utente: chi cerca grandi aziende perderebbe tutte le offerte francesi e
svedesi soltanto perché passate da una fonte che la dimensione non la espone.

I filtri vivono nelle colonne di user_clusters, una riga per ricerca utente:
la personalizzazione è per coppia utente-ricerca, e la stessa offerta può
passare per una ricerca e restare fuori per un'altra. La seniority è un
intervallo di rank in experience_levels, non un elenco di codici, perché
min e max possono essere aperti da un lato.

ECCEZIONE, una sola: il bisogno di visto non e' un filtro deterministico ma
una preferenza pesata dal modello. Il campo dell'offerta e' inaffidabile
nella direzione negativa (false = «non menzionato», misurato 2.332 false
contro 2 true) e un'esclusione dura svuotava il digest di chiunque la
chiedesse. Vedi il commento dove si costruisce il desiderio.
"""

from __future__ import annotations

import json
import math
import re

import psycopg
from psycopg.rows import dict_row

from nivult.matching.llm import VERSIONE_RUBRICA

from nivult.ats.testo import evidenza_stage
from nivult.matching.retrieval import HASH_DOCUMENTO_SQL, lingue_mancanti

DEFAULT_MIN_SCORE = 70

# Predicato condiviso con l'API: il match resta visibile se almeno una ricerca
# attiva collegata all'offerta lo accetta con la sua soglia CORRENTE. Equivale
# a confrontare lo score con la soglia minima, senza dipendere da m.passed.
MATCH_ATTIVO_SQL = """EXISTS (
    SELECT 1 FROM job_clusters jc_vis
    JOIN user_clusters uc_vis ON uc_vis.cluster_id = jc_vis.cluster_id
    JOIN clusters c_vis ON c_vis.id = uc_vis.cluster_id
    WHERE jc_vis.job_id = m.job_id AND uc_vis.user_id = m.user_id
      AND NOT uc_vis.is_paused AND c_vis.status = 'active'
      AND m.score >= COALESCE(uc_vis.min_score, 70)
)"""


def match_attivo_sql() -> str:
    """SQL da affiancare a `m.user_id = %s` nelle query API con alias `m`."""
    return MATCH_ATTIVO_SQL


def elimina_match_da_rivalutare(cur, user_id: str) -> int:
    """Solo match non consegnati e senza alcuna azione personale."""
    cur.execute(
        "DELETE FROM matches m WHERE m.user_id = %s "
        "AND m.action_version = 0 "
        "AND NOT EXISTS (SELECT 1 FROM digest_items di "
        "                WHERE di.match_id = m.id)", (user_id,))
    return cur.rowcount


# I vocabolari utente parlano per nomi ("Italian"), le offerte per codici ISO
# 639-1 ("it"): e' la STESSA lingua in due alfabeti. Chi selezionava lingue si
# trovava il mercato svuotato in silenzio — 'it' non e' mai uguale a
# 'Italian'. Il filtro riconosce entrambe le forme.
_LINGUA_A_ISO = {
    "English": "en", "German": "de", "French": "fr", "Italian": "it",
    "Swedish": "sv", "Portuguese": "pt", "Bulgarian": "bg", "Croatian": "hr",
    "Czech": "cs", "Danish": "da", "Dutch": "nl", "Estonian": "et",
    "Finnish": "fi", "Greek": "el", "Hungarian": "hu", "Icelandic": "is",
    "Irish": "ga", "Latvian": "lv", "Lithuanian": "lt", "Maltese": "mt",
    "Norwegian": "no", "Polish": "pl", "Romanian": "ro", "Slovak": "sk",
    "Slovenian": "sl", "Spanish": "es",
}


def _lingue_normalizzate(lingue: list[str] | None) -> list[str]:
    """Nomi e codici insieme: qualunque alfabeto parlino i dati, si trova."""
    if not lingue:
        return []
    out = set(lingue)
    for lingua in lingue:
        codice = _LINGUA_A_ISO.get(lingua)
        if codice:
            out.add(codice)
    return sorted(out)


_TITOLO_JUNIOR_RX = re.compile(
    r"(?<![a-zÀ-ɏ])(?:junior|entry[ -]level)(?![a-zÀ-ɏ])", re.IGNORECASE)
_SENZA_ESPERIENZA_RX = re.compile(
    r"(?<![a-zÀ-ɏ])(?:anche\s+senza\s+esperienza(?:\s+pregressa)?"
    r"|senza\s+esperienza\s+richiesta|nessuna\s+esperienza\s+richiesta"
    r"|esperienza\s+non\s+richiesta|non\s+(?:è|e')\s+richiesta\s+esperienza"
    r"|no\s+(?:(?:prior|previous)\s+)?experience\s+required)(?![a-zÀ-ɏ])", re.IGNORECASE)


def titolo_sotto_seniority(titolo: str | None, min_seniority: str | None,
                           lingua: str | None = None,
                           requisiti: str | None = None) -> bool:
    """Titolo o requisiti provano un livello sotto il minimo richiesto?

    Un metadato NULL continua a non escludere. Qui, invece, è l'annuncio
    stesso a dire junior/stage o a non richiedere alcuna esperienza. Per
    stage inglese resta decisiva la lingua; non si usa il generico «senza
    esperienza» perché potrebbe comparire in una frase di segno contrario.
    """
    if min_seniority not in {"2-5", "5-10", "10+"}:
        return False
    if requisiti and _SENZA_ESPERIENZA_RX.search(requisiti):
        return True
    if not titolo:
        return False
    lingua_iso = _LINGUA_A_ISO.get(lingua, lingua)
    return bool(_TITOLO_JUNIOR_RX.search(titolo)) or evidenza_stage(
        titolo, None, lingua_iso)


# Il paese non è fra i filtri: è il cluster stesso (famiglia × paese) a
# delimitarlo. In chiamata alle fonti vanno SOLO paese e famiglia — qui è la
# stessa idea vista dal lato utente.
_CANDIDATI_SQL = """
SELECT j.id::text, j.source_job_id, j.title, j.organization, j.cities,
       j.countries, j.source,
       j.url, j.link_kind, j.employer_kind, j.date_posted, j.salary,
       j.ai_job_language, j.ai_experience_level, j.ai_work_arrangement,
       j.ai_employment_type, j.ai_visa_sponsorship, j.ai_key_skills,
       j.ai_requirements_summary,
       COALESCE(j.ai_core_responsibilities, j.raw->'ats'->>'description',
                j.raw->>'descrizione', j.raw->>'description') AS ai_core_responsibilities,
       j.raw->'ai_benefits' AS ai_benefits, j.ai_working_hours
FROM jobs j
JOIN job_clusters jc ON jc.job_id = j.id
WHERE jc.cluster_id = %(cluster_id)s
  AND j.status = 'active'
  AND j.duplicate_of_job_id IS NULL
  -- L'anti-ripetizione lavora qui, non dopo: si ripaga soltanto una rubrica
  -- precedente, e la UNIQUE su matches conserva una riga per utente e offerta.
  AND NOT EXISTS (SELECT 1 FROM matches m
                  WHERE m.user_id = %(user_id)s AND m.job_id = j.id
                    AND m.rubric_version = %(rubric_version)s)
  AND (cardinality(%(languages)s::text[]) = 0 OR j.ai_job_language IS NULL
       OR j.ai_job_language = ANY(%(languages)s::text[]))
  AND (cardinality(%(arrangements)s::text[]) = 0 OR j.ai_work_arrangement IS NULL
       OR j.ai_work_arrangement = ANY(%(arrangements)s::text[]))
  -- `OTHER` non esclude, come NULL: e' un'assenza travestita da valore.
  -- Misurato sul cluster Risorse umane x Italia: 116 offerte su 281 —
  -- il gruppo piu' numeroso — portano OTHER, e leggendole sono normali
  -- posizioni a tempo indeterminato («Addetto/a risorse umane a tempo
  -- indeterminato», classificata OTHER). Fantastic ci mette dentro cio'
  -- che non sa classificare, non un tipo di contratto insolito.
  --
  -- Escluderle e' lo stesso errore del `false` sul visto e della stringa
  -- vuota sul settore: chi chiede «solo tempo pieno» perde offerte a
  -- tempo pieno, per un motivo che non ha scelto.
  AND (cardinality(%(employment_types)s::text[]) = 0 OR j.ai_employment_type IS NULL
       OR j.ai_employment_type = 'OTHER'
       OR j.ai_employment_type = ANY(%(employment_types)s::text[]))
  -- accepted_employer_kinds non è mai vuoto: il default accetta tutti e tre
  -- i tipi e restringere è una scelta esplicita (vincolo in 0019).
  AND j.employer_kind = ANY(%(employer_kinds)s::text[])
  AND (j.ai_experience_level IS NULL
       OR j.ai_experience_level = ANY(%(livelli)s::text[]))
  AND (cardinality(%(industries)s::text[]) = 0 OR j.org_industry IS NULL
       OR j.org_industry = ANY(%(industries)s::text[]))
  AND (%(min_headcount)s::int IS NULL OR j.org_headcount IS NULL
       OR j.org_headcount >= %(min_headcount)s::int)
  AND (%(max_headcount)s::int IS NULL OR j.org_headcount IS NULL
       OR j.org_headcount <= %(max_headcount)s::int)
  AND (%(min_salary_annual)s::numeric IS NULL
       OR j.salary_min IS NULL OR j.salary_period IS NULL
       OR j.salary_currency IS NULL OR j.salary_da IS NULL
       OR j.salary_da <> 'dichiarato'
       OR j.salary_currency <> %(salary_currency)s
       OR lower(j.salary_period) NOT IN ('year', 'month')
       OR j.salary_min * CASE lower(j.salary_period) WHEN 'year' THEN 1 ELSE 12 END
          >= %(min_salary_annual)s::numeric)
ORDER BY j.date_posted DESC
"""

MODELLO_EMBEDDING = "intfloat/multilingual-e5-large-instruct"
_ANN_ID_SQL = """SELECT e.job_id::text FROM job_embeddings e
ORDER BY e.embedding <=> %(query_embedding)s::vector LIMIT %(ann_limit)s"""
_ANN_SQL = _CANDIDATI_SQL.replace(
    "FROM jobs j\nJOIN job_clusters jc ON jc.job_id = j.id",
    "FROM jobs j\nJOIN job_embeddings e ON e.job_id = j.id AND e.model = %(model)s\n"
    "JOIN job_clusters jc ON jc.job_id = j.id")
_ANN_SQL = _ANN_SQL.replace(
    "ORDER BY j.date_posted DESC",
    "AND j.id = ANY(%(ann_ids)s::uuid[]) "
    "ORDER BY e.embedding <=> %(query_embedding)s::vector")


def _candidati_ann(cur, tcur, params: dict, f: dict, *, totale: int,
                   overfetch: int, lingue_profilo: set[str] | None,
                   cache: dict[int, list[str]]) -> list[dict] | None:
    if totale == 0:
        return []
    limite = min(max(overfetch, 300), totale)
    tcur.execute("SHOW hnsw.ef_search")
    ef_precedente = tcur.fetchone()[0]
    try:
        while True:
            if limite not in cache:
                tcur.execute("SELECT set_config('hnsw.ef_search', %s, %s)",
                             (str(min(limite, 1000)), not tcur.connection.autocommit))
                tcur.execute(_ANN_ID_SQL, {
                    "model": params["model"],
                    "query_embedding": params["query_embedding"],
                    "ann_limit": limite,
                })
                cache[limite] = [r[0] for r in tcur.fetchall()]
            params["ann_ids"] = cache[limite]
            cur.execute(_ANN_SQL, params)
            righe = []
            for j in cur.fetchall():
                if titolo_sotto_seniority(j["title"], f["min_seniority"],
                                          j["ai_job_language"], j["ai_requirements_summary"]):
                    continue
                if lingue_profilo and lingue_mancanti(
                        j["ai_requirements_summary"] or "", lingue_profilo):
                    continue
                righe.append(j)
            if len(righe) >= overfetch:
                return righe[:overfetch]
            if limite >= totale:
                return righe
            if limite >= 1000:
                return None
            limite = min(limite * 2, totale)
    finally:
        if tcur.connection.autocommit:
            tcur.execute("SELECT set_config('hnsw.ef_search', %s, false)", (ef_precedente,))


def _filtri(cur, user_id: str, ricerca_ids: list[str] | None = None,
            includi_ereditate: bool = True) -> list[dict]:
    """Filtri delle ricerche ammesse nello slot corrente.

    Senza argomenti di slot conserva il comportamento legacy e le legge tutte.
    Con argomenti espliciti, le ereditate entrano solo se lo slot globale e'
    dovuto e gli override solo se il loro UUID e' fra quelli dovuti.
    """
    sql = (
        "SELECT uc.id::text AS ricerca_id, uc.cluster_id::text, c.family, "
        "       uc.min_seniority, uc.max_seniority, "
        "       uc.work_arrangements, uc.languages, uc.employment_types, "
        "       uc.needs_visa_sponsorship, uc.accepted_employer_kinds, "
        "       uc.min_headcount, uc.max_headcount, uc.wants, uc.target_role, "
        "       uc.industries, uc.min_salary_annual, uc.salary_currency, "
        "       uc.desired_technologies, uc.min_score "
        "FROM user_clusters uc JOIN clusters c ON c.id = uc.cluster_id "
        "WHERE uc.user_id = %s AND NOT uc.is_paused AND c.status = 'active'")
    params: list = [user_id]
    if ricerca_ids is not None or not includi_ereditate:
        sql += (" AND ((%s AND uc.frequency IS NULL) "
                "OR uc.id = ANY(%s::uuid[]))")
        params += [includi_ereditate, list(ricerca_ids or [])]
    cur.execute(sql, params)
    return list(cur.fetchall())


def desiderio_ricerca(f: dict) -> str | None:
    """Formato unico di `_wants`, sia per candidati nuovi sia per match salvati."""
    parti = []
    if f.get("target_role"):
        parti.append(f"Ruolo a cui punta: {f['target_role']}")
    if f.get("desired_technologies"):
        parti.append("Tecnologie che vorrebbe usare: "
                     + ", ".join(f["desired_technologies"])
                     + ". Sono preferenze di punteggio, non requisiti rigidi.")
    if f.get("needs_visa_sponsorship"):
        parti.append("Ha bisogno di sponsorizzazione del visto per lavorare: "
                     "un datore esplicitamente disposto a sponsorizzare conta "
                     "molto a suo favore.")
    if f.get("wants"):
        parti.append(f["wants"])
    return "\n".join(parti) or None


def copertura_embedding(conn: psycopg.Connection, user_id: str,
                        ricerca_ids: list[str] | None = None,
                        includi_ereditate: bool = True) -> float:
    """Quota minima di offerte coperte; sotto 98% non si usa ANN."""
    quota = 1.0
    with conn.cursor(row_factory=dict_row) as cur:
        for f in _filtri(cur, user_id, ricerca_ids, includi_ereditate):
            cur.execute(f"""SELECT count(j.id) AS totale,
                    count(e.job_id) FILTER (WHERE e.content_hash = {HASH_DOCUMENTO_SQL}) AS coperte
                FROM jobs j JOIN job_clusters jc ON jc.job_id = j.id
                LEFT JOIN job_embeddings e ON e.job_id = j.id AND e.model = %s
                WHERE jc.cluster_id = %s AND j.status = 'active'
                  AND j.duplicate_of_job_id IS NULL""", (MODELLO_EMBEDDING, f["cluster_id"]))
            riga = cur.fetchone()
            totale, coperte = riga["totale"], riga["coperte"]
            if totale:
                quota = min(quota, coperte / totale)
    return quota


def candidati(conn: psycopg.Connection, user_id: str,
              ricerca_ids: list[str] | None = None,
              includi_ereditate: bool = True, *,
              vettore: list[float] | None = None, overfetch: int = 300,
              lingue_profilo: set[str] | None = None) -> list[dict]:
    """Le offerte da valutare per le ricerche ammesse, una sola volta per job."""
    if vettore is not None:
        if len(vettore) != 1024 or any(not math.isfinite(v) for v in vettore):
            raise ValueError("query embedding non valido")
        if overfetch <= 0:
            raise ValueError("overfetch deve essere positivo")
    visti: dict[str, dict] = {}
    cache_ann: dict[int, list[str]] = {}
    with conn.cursor(row_factory=dict_row) as cur, conn.cursor() as tcur:
        totale_ann = 0
        if vettore is not None:
            tcur.execute("SELECT count(*) FROM job_embeddings")
            totale_ann = tcur.fetchone()[0]
        for f in _filtri(cur, user_id, ricerca_ids, includi_ereditate):
            # La fascia di seniority accettabile, in codici: con entrambi i
            # limiti NULL è tutto il vocabolario, cioè filtro inattivo.
            tcur.execute(
                "SELECT code FROM experience_levels WHERE rank BETWEEN "
                "COALESCE((SELECT rank FROM experience_levels WHERE code = %s), 0) "
                "AND COALESCE((SELECT rank FROM experience_levels WHERE code = %s), 5)",
                (f["min_seniority"], f["max_seniority"]))
            livelli = [r[0] for r in tcur.fetchall()]
            params = {
                "user_id": user_id, "cluster_id": f["cluster_id"],
                "languages": _lingue_normalizzate(f["languages"]),
                "arrangements": f["work_arrangements"] or [],
                "employment_types": f["employment_types"] or [],
                "employer_kinds": f["accepted_employer_kinds"],
                "livelli": livelli,
                "industries": f["industries"] or [],
                "min_headcount": f["min_headcount"],
                "max_headcount": f["max_headcount"],
                "min_salary_annual": f["min_salary_annual"],
                "salary_currency": f["salary_currency"],
                "model": MODELLO_EMBEDDING,
                "rubric_version": VERSIONE_RUBRICA,
                "query_embedding": json.dumps(vettore, separators=(",", ":")),
                "overfetch": overfetch,
            }
            if vettore is None:
                cur.execute(_CANDIDATI_SQL, params)
                righe = cur.fetchall()
            else:
                righe = _candidati_ann(
                    cur, tcur, params, f, totale=totale_ann, overfetch=overfetch,
                    lingue_profilo=lingue_profilo, cache=cache_ann)
                if righe is None:
                    return candidati(conn, user_id, ricerca_ids, includi_ereditate,
                                     overfetch=overfetch, lingue_profilo=lingue_profilo)
            for j in righe:
                if titolo_sotto_seniority(j["title"], f["min_seniority"],
                                          j["ai_job_language"],
                                          j["ai_requirements_summary"]):
                    continue
                if lingue_profilo and lingue_mancanti(
                        j["ai_requirements_summary"] or "", lingue_profilo):
                    continue
                j["_wants"] = desiderio_ricerca(f)
                j["_min_score"] = f.get("min_score")
                j["_ricerca_ids"] = [f["ricerca_id"]]
                j["_families"] = [f["family"]]
                precedente = visti.get(j["id"])
                if precedente is None:
                    visti[j["id"]] = j
                else:
                    # Soglia e desiderio restano della stessa ricerca, così le
                    # due passate non cambiano il contesto della valutazione.
                    a = (precedente["_min_score"] if precedente["_min_score"] is not None
                         else DEFAULT_MIN_SCORE)
                    b = j["_min_score"] if j["_min_score"] is not None else DEFAULT_MIN_SCORE
                    if b < a or not precedente.get("_wants"):
                        precedente["_wants"] = j["_wants"]
                    precedente["_min_score"] = min(a, b)
                    if f["family"] not in precedente["_families"]:
                        precedente["_families"].append(f["family"])
                    if f["ricerca_id"] not in precedente["_ricerca_ids"]:
                        precedente["_ricerca_ids"].append(f["ricerca_id"])
    return sorted(visti.values(), key=lambda j: j["date_posted"], reverse=True)
