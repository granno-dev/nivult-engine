"""Il primo stadio del matching: i filtri deterministici, prima del modello.

REGOLA DEFINITIVA (misurata sul campo): un campo NULL — o una lista vuota, che
è il modo in cui una fonte dice "non lo so" — NON esclude mai. Escludere per
assenza di dato nasconderebbe un'offerta per un motivo che non è una scelta
dell'utente: chi cerca grandi aziende perderebbe tutte le offerte francesi e
svedesi soltanto perché passate da una fonte che la dimensione non la espone.

I filtri vivono nelle colonne di user_clusters, una riga per cluster seguito:
la personalizzazione è per coppia utente-cluster, e la stessa offerta può
passare per un cluster e restare fuori per un altro. La seniority è un
intervallo di rank in experience_levels, non un elenco di codici, perché
min e max possono essere aperti da un lato.

ECCEZIONE, una sola: il bisogno di visto non e' un filtro deterministico ma
una preferenza pesata dal modello. Il campo dell'offerta e' inaffidabile
nella direzione negativa (false = «non menzionato», misurato 2.332 false
contro 2 true) e un'esclusione dura svuotava il digest di chiunque la
chiedesse. Vedi il commento dove si costruisce il desiderio.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

# Il paese non è fra i filtri: è il cluster stesso (famiglia × paese) a
# delimitarlo. In chiamata alle fonti vanno SOLO paese e famiglia — qui è la
# stessa idea vista dal lato utente.
_CANDIDATI_SQL = """
SELECT j.id::text, j.source_job_id, j.title, j.organization, j.cities,
       j.countries, j.source,
       j.url, j.link_kind, j.employer_kind, j.date_posted, j.salary,
       j.ai_job_language, j.ai_experience_level, j.ai_work_arrangement,
       j.ai_employment_type, j.ai_visa_sponsorship, j.ai_key_skills,
       j.ai_requirements_summary
FROM jobs j
JOIN job_clusters jc ON jc.job_id = j.id
WHERE jc.cluster_id = %(cluster_id)s
  AND j.status = 'active'
  AND j.duplicate_of_job_id IS NULL
  -- L'anti-ripetizione lavora qui, non dopo: un'offerta già valutata per
  -- quest'utente non si ripaga, e la UNIQUE su matches è la garanzia.
  AND NOT EXISTS (SELECT 1 FROM matches m
                  WHERE m.user_id = %(user_id)s AND m.job_id = j.id)
  AND (cardinality(%(languages)s::text[]) = 0 OR j.ai_job_language IS NULL
       OR j.ai_job_language = ANY(%(languages)s::text[]))
  AND (cardinality(%(arrangements)s::text[]) = 0 OR j.ai_work_arrangement IS NULL
       OR j.ai_work_arrangement = ANY(%(arrangements)s::text[]))
  AND (cardinality(%(employment_types)s::text[]) = 0 OR j.ai_employment_type IS NULL
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
ORDER BY j.date_posted DESC
"""


def _filtri(cur, user_id: str) -> list[dict]:
    """Una riga di filtri per ogni cluster attivo seguito dall'utente.

    Vuole un cursore dict_row: le righe diventano i parametri della query dei
    candidati, chiave per chiave.
    """
    cur.execute(
        "SELECT uc.cluster_id::text, uc.min_seniority, uc.max_seniority, "
        "       uc.work_arrangements, uc.languages, uc.employment_types, "
        "       uc.needs_visa_sponsorship, uc.accepted_employer_kinds, "
        "       uc.min_headcount, uc.max_headcount, uc.wants, uc.target_role, "
        "       uc.industries "
        "FROM user_clusters uc JOIN clusters c ON c.id = uc.cluster_id "
        "WHERE uc.user_id = %s AND NOT uc.is_paused AND c.status = 'active'",
        (user_id,))
    return list(cur.fetchall())


def candidati(conn: psycopg.Connection, user_id: str) -> list[dict]:
    """Le offerte da valutare per un utente: filtri deterministici applicati.

    Un'offerta presente in due cluster seguiti compare una volta sola: la
    UNIQUE (user_id, job_id) su matches la vieta comunque, e deduplicare qui
    evita di pagarla due volte. L'ordine è dal più recente: se il budget di
    valutazione è tirato, ciò che si taglia è il più vecchio.
    """
    visti: dict[str, dict] = {}
    with conn.cursor(row_factory=dict_row) as cur, conn.cursor() as tcur:
        for f in _filtri(cur, user_id):
            # La fascia di seniority accettabile, in codici: con entrambi i
            # limiti NULL è tutto il vocabolario, cioè filtro inattivo.
            tcur.execute(
                "SELECT code FROM experience_levels WHERE rank BETWEEN "
                "COALESCE((SELECT rank FROM experience_levels WHERE code = %s), 0) "
                "AND COALESCE((SELECT rank FROM experience_levels WHERE code = %s), 5)",
                (f["min_seniority"], f["max_seniority"]))
            livelli = [r[0] for r in tcur.fetchall()]

            cur.execute(_CANDIDATI_SQL, {
                "user_id": user_id, "cluster_id": f["cluster_id"],
                "languages": f["languages"] or [],
                "arrangements": f["work_arrangements"] or [],
                "employment_types": f["employment_types"] or [],
                "employer_kinds": f["accepted_employer_kinds"],
                "livelli": livelli,
                "industries": f["industries"] or [],
                "min_headcount": f["min_headcount"],
                "max_headcount": f["max_headcount"],
            })
            for j in cur.fetchall():
                # Il desiderio viaggia con l'offerta perché è del CLUSTER, e
                # il worker qui sotto non sa più da quale ricerca venga.
                # Un'offerta presente in due cluster tiene quello del primo
                # che l'ha trovata: è già la regola con cui `visti` dedupla.
                parti = []
                if f.get("target_role"):
                    parti.append(f"Ruolo a cui punta: {f['target_role']}")
                # Il visto NON esclude in SQL, deliberatamente, e la ragione
                # e' una misura: Fantastic marca ai_visa_sponsorship=false
                # anche quando l'annuncio semplicemente non ne parla — 2.332
                # false contro 2 true sulle attive. Un'esclusione dura su
                # quel campo filtrava «chi lo scrive nell'annuncio», non
                # «chi sponsorizza», e chi spuntava la casella riceveva
                # digest vuoti per sempre. Quindi il bisogno viaggia come
                # preferenza forte nel prompt: GLM premia chi la dichiara
                # senza azzerare tutto il resto.
                if f.get("needs_visa_sponsorship"):
                    parti.append(
                        "Ha bisogno di sponsorizzazione del visto per "
                        "lavorare: un datore esplicitamente disposto a "
                        "sponsorizzare conta molto a suo favore.")
                if f.get("wants"):
                    parti.append(f["wants"])
                j["_wants"] = "\n".join(parti) or None
                visti.setdefault(j["id"], j)
    return sorted(visti.values(), key=lambda j: j["date_posted"], reverse=True)
