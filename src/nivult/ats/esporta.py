"""I dataset da vendere: il magazzino si riempie prima di aprire la porta.

Tre confezioni, nello stile dei venditori di dati (TheirStack, Coresignal)
ma coi nostri argomenti — solo fonti dirette del datore, lingua
dell'annuncio, scadenze oneste, e l'ATS usato da ogni azienda (il
"technographic" che loro vendono caro e noi abbiamo per costruzione):

  attive   — le offerte vive, un JSON per riga, con ciclo di vita
             (first_seen/last_seen), lingua, competenze, salario, testo.
  aziende  — una riga per azienda che assume: ATS usato, dominio, logo,
             dipendenti (dove Wikidata li sa), ritmo di assunzione a 30
             giorni e le tecnologie/competenze più chieste nei SUOI
             annunci — l'aggregato che trasforma offerte in segnali.
  scadute  — lo storico delle chiusure: quando è apparsa, quando è
             sparita. Il tempo-di-riempimento è un dato che pochi hanno.

Scrittura in streaming (cursore lato server): 800k righe senza mangiare
la RAM. File .jsonl.gz datati + un link simbolico "-ultimo" stabile.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import os
import re

import psycopg

from nivult.ats.aziende_dettagli import dipendenti
from nivult.ats.testo import pulito

log = logging.getLogger("nivult.ats.esporta")

CARTELLA = "/opt/nivult/exports"

_DESCR = "coalesce((SELECT v FROM unnest(ARRAY[raw->>'description', raw->>'content', raw->>'descriptionHtml', raw->>'descriptionPlain', raw->>'externalDescription', raw->>'jobDescription', raw->>'job_description', raw->>'Job_Description', raw->>'body', raw->>'content_html', raw->>'description_html', raw->>'descriptionBody', raw->>'text', raw->'_jobposting'->>'description', raw->>'ShortDescriptionStr']) v WHERE length(v) >= 80 LIMIT 1), '')"


def _apri(nome: str):
    os.makedirs(CARTELLA, exist_ok=True)
    oggi = dt.date.today().isoformat()
    percorso = f"{CARTELLA}/{nome}-{oggi}.jsonl.gz"
    return percorso, gzip.open(percorso + ".tmp", "wt", encoding="utf-8")


def _chiudi(percorso: str, f, righe: int) -> None:
    f.close()
    os.replace(percorso + ".tmp", percorso)
    stabile = percorso.rsplit("-", 3)[0] + "-ultimo.jsonl.gz"
    tmp = stabile + ".tmp"
    try:
        os.remove(tmp)
    except OSError:
        pass
    os.symlink(os.path.basename(percorso), tmp)
    os.replace(tmp, stabile)
    log.info("%s: %d righe, %.1f MB", percorso, righe,
             os.path.getsize(percorso) / 1e6)


# ── competenze: il campo `skills` e' un sacco di parole chiave, non una
# lettura. Misurato il 21/09/2026 sulle attive: «dental» la piu' frequente
# (218k, dalla frase «medical, dental, vision» dei benefit), «English» ed
# «english» separate, «computer science» (una laurea), etichette ESCO con la
# parentesi («Python (computer programming)»). Il compratore ha
# `technologies` (misurato sul golden) e `languages_required`; qui si toglie
# cio' che e' certamente un'altra cosa e si normalizza il resto.
_SKILL_NO = {"dental", "vision", "medical", "life insurance", "401k", "401(k)", "pto", "benefits",
             "english", "german", "french", "spanish", "italian", "dutch", "portuguese", "chinese", "japanese",
             "swedish", "norwegian", "danish", "finnish", "polish", "russian", "arabic", "georgian",
             "computer science", "bachelor", "master", "phd", "degree", "geography", "history"}
_SKILL_PAR_RX = re.compile(r"\s*\((computer programming|programming language|software|framework|database|tool|technology|methodology)\)\s*$", re.I)


def _skills_pulite(valori) -> list[str]:
    viste: set[str] = set()
    out: list[str] = []
    for v in valori or []:
        if not isinstance(v, str):
            continue
        t = _SKILL_PAR_RX.sub("", v.strip())
        chiave = t.lower()
        if not chiave or chiave in _SKILL_NO or chiave in viste or len(chiave) < 2:
            continue
        viste.add(chiave)
        out.append(t if t.isupper() or any(ch.isupper() for ch in t[1:]) else chiave)
    return out[:20]


def _riga(**kv) -> str:
    return json.dumps({k: v for k, v in kv.items() if v not in (None, [], "")},
                      ensure_ascii=False, default=str) + "\n"


def attive(dsn: str, campione: int | None = None) -> int:
    # --campione N: le prime N righe in un file a parte (collaudo di una
    # modifica, o un assaggio da mandare a un compratore) senza toccare il
    # file del giorno ne' il manifest
    percorso, f = _apri("offerte-attive" + ("-campione" if campione else ""))
    n = 0
    copertura = {k: 0 for k in ("salary_observed", "salary_estimate",
                                "description", "language", "category",
                                "country", "skills", "employment_type",
                                "contact_email", "seniority", "remote",
                                "technologies", "summary", "benefits",
                                "shift_schedule", "work_hours",
                                "management_level", "valid_through",
                                "state", "geo", "recruiter", "job_sources",
                                "department", "salary_text")}
    with psycopg.connect(dsn) as conn:
        # il benchmark in memoria: stima SOLO dove l'annuncio tace, in
        # campi SEPARATI e dichiarati — mai mescolata con l'osservato
        bench: dict = {}
        with conn.cursor() as cur:
            cur.execute("""SELECT country, currency, family, seniority,
                                  p25, p50, p75, n
                             FROM stipendi_benchmark""")
            for r in cur:
                bench[(r[0], r[2], r[3])] = (r[1], r[4], r[5], r[6], r[7])
        with conn.cursor(name="esp_attive") as cur:
            cur.itersize = 2000
            cur.execute(f"""
                SELECT j.id, j.title, j.platform_id, j.slug,
                       COALESCE(co.company_name, j.raw->'company'->>'name'),
                       j.url, j.country, j.city, j.location, j.lang,
                       j.seniority, j.remote, j.skills,
                       j.salary_min, j.salary_max, j.salary_currency,
                       j.posted_at, COALESCE(j.created_at, j.posted_at,
                                             j.fetched_at), j.fetched_at,
                       {_DESCR}, x.family,
                       j.employment_type, j.contact_email,
                       j.languages_required,
                       -- 21/09/2026: i campi che il mercato (Coresignal,
                       -- TheirStack) espone e noi avevamo o abbiamo appena
                       -- calcolato; tutti dichiarati per quello che sono
                       j.external_id, j.department, j.orario, j.durata,
                       j.salary_period, j.salary_da, j.posted_at_estimated,
                       t.tecnologie, sf.sintesi, sf.da,
                       d.management_level, d.is_decision_maker,
                       d.shift_schedule, d.work_hours, d.is_urgently_hiring,
                       d.benefits, d.salary_text, d.valid_through, d.state,
                       d.postal_code, d.latitude, d.longitude, d.geo_da,
                       d.applicants_count, d.is_easy_apply, d.recruiter,
                       fo.job_sources
                  FROM ats_jobs j
                  LEFT JOIN ats_companies co
                         ON co.platform_id = j.platform_id
                        AND co.slug = j.slug
                  LEFT JOIN job_classifications x ON x.job_id = j.id
                  LEFT JOIN tecnologie_v1 t ON t.job_id = j.id
                  LEFT JOIN sintesi_finali sf ON sf.job_id = j.id
                  LEFT JOIN offerte_dettagli d ON d.job_id = j.id
                  -- le altre copie della stessa offerta (job_sources):
                  -- correlata e non la vista offerte_fonti, che il
                  -- pianificatore materializzerebbe per intero
                  LEFT JOIN LATERAL (
                       SELECT jsonb_agg(jsonb_build_object(
                                'id', k.id, 'ats', k.platform_id,
                                'company_slug', k.slug, 'url', k.url,
                                'posted_at', k.posted_at,
                                'status', CASE WHEN k.expired_at IS NULL
                                               THEN 'active' ELSE 'expired' END)
                                ORDER BY k.posted_at DESC NULLS LAST) AS job_sources
                         FROM ats_jobs k
                        WHERE j.duplicate_key IS NOT NULL
                          AND k.duplicate_key = j.duplicate_key
                          AND k.id <> j.id) fo ON true
                 WHERE j.expired_at IS NULL
                   -- un annuncio per chiave (titolo+azienda+citta'): nel
                   -- corpus i doppi restano (10.573 gruppi il 06/09), al
                   -- cliente arriva il piu' vecchio di ciascun gruppo
                   AND NOT EXISTS (SELECT 1 FROM ats_jobs d
                                    WHERE d.duplicate_key = j.duplicate_key
                                      AND d.expired_at IS NULL AND d.id < j.id)"""
                        + (" LIMIT %s" if campione else ""),
                        (campione,) if campione else None)
            for r in cur:
                stima = {}
                if r[13] is None and r[6] and r[20]:
                    cella = bench.get((r[6], r[20], r[10] or ""))
                    livello = f"{r[6]} x {r[20]} x {r[10]}"
                    if not cella:
                        cella = bench.get((r[6], r[20], ""))
                        livello = f"{r[6]} x {r[20]}, all seniorities"
                    if cella:
                        val, p25, p50, p75, camp = cella
                        stima = {
                            "salary_is_estimate": True,
                            "salary_estimate_min": p25,
                            "salary_estimate_median": p50,
                            "salary_estimate_max": p75,
                            "salary_estimate_currency": val,
                            "salary_estimate_basis":
                                f"Nivult benchmark: {camp} observed "
                                f"postings, {livello}"}
                (ext_id, department, orario, durata, sal_period, sal_da,
                 posted_est, tecnologie, sintesi, sintesi_da, mgmt,
                 decisore, turni, ore, urgente, benefits, sal_testo,
                 valid_through, state, cap, lat, lon, geo_da, candidati,
                 easy, recruiter, fonti) = r[24:51]
                geo = ({"latitude": lat, "longitude": lon, "source": geo_da}
                       if lat is not None else None)
                f.write(_riga(
                    id=str(r[0]), title=r[1], ats=r[2], company_slug=r[3],
                    company=r[4], url=r[5], country=r[6], city=r[7],
                    location=r[8], language=r[9], seniority=r[10],
                    remote=r[11], skills=_skills_pulite(r[12]),
                    salary_min=float(r[13]) if r[13] is not None else None,
                    salary_max=float(r[14]) if r[14] is not None else None,
                    salary_currency=r[15], posted_at=r[16],
                    first_seen=r[17], last_seen=r[18],
                    # testo piano: niente tag, niente entita' (Greenhouse le
                    # codifica due volte; misurato il 21/09/2026)
                    description=pulito(r[19]),
                    category=r[20], employment_type=r[21],
                    contact_email=r[22],
                    languages_required=list(r[23] or []), **stima,
                    external_id=ext_id, department=department,
                    hours_type=orario, duration_type=durata,
                    salary_period=sal_period, salary_source=sal_da,
                    salary_text=sal_testo,
                    posted_at_estimated=True if posted_est else None,
                    technologies=list(tecnologie or []),
                    summary=sintesi, summary_source=sintesi_da,
                    management_level=mgmt,
                    is_decision_maker=True if decisore else None,
                    shift_schedule=turni, work_hours=ore,
                    is_urgently_hiring=True if urgente else None,
                    benefits=list(benefits or []),
                    valid_through=valid_through, state=state,
                    postal_code=cap, geo=geo,
                    applicants_count=candidati,
                    is_easy_apply=True if easy else None,
                    recruiter=recruiter, job_sources=list(fonti or [])))
                n += 1
                copertura["salary_observed"] += r[13] is not None
                copertura["salary_estimate"] += bool(stima)
                copertura["description"] += bool(r[19])
                copertura["language"] += r[9] is not None
                copertura["category"] += r[20] is not None
                copertura["country"] += r[6] is not None
                copertura["skills"] += bool(r[12])
                copertura["employment_type"] += r[21] is not None
                copertura["contact_email"] += r[22] is not None
                copertura["seniority"] += r[10] is not None
                copertura["remote"] += r[11] is not None
                copertura["technologies"] += bool(tecnologie)
                copertura["summary"] += bool(sintesi)
                copertura["benefits"] += bool(benefits)
                copertura["shift_schedule"] += turni is not None
                copertura["work_hours"] += ore is not None
                copertura["management_level"] += mgmt is not None
                copertura["valid_through"] += valid_through is not None
                copertura["state"] += state is not None
                copertura["geo"] += geo is not None
                copertura["recruiter"] += bool(recruiter)
                copertura["job_sources"] += bool(fonti)
                copertura["department"] += bool(department)
                copertura["salary_text"] += sal_testo is not None
    _chiudi(percorso, f, n)
    # il manifest: la copertura di ogni campo, dichiarata. I venditori
    # seri pubblicano i fill-rate; i buchi dichiarati sono un argomento
    # di vendita, quelli scoperti dal cliente sono un rimborso.
    manifest = {"date": dt.date.today().isoformat(), "rows": n,
                "coverage": {k: round(100 * v / max(n, 1), 1)
                             for k, v in copertura.items()}}
    nome_manifest = "manifest-campione" if campione else "manifest-ultimo"
    with open(f"{CARTELLA}/{nome_manifest}.json.tmp", "w") as mf:
        json.dump(manifest, mf, indent=1)
    os.replace(f"{CARTELLA}/{nome_manifest}.json.tmp",
               f"{CARTELLA}/{nome_manifest}.json")
    log.info("manifest: %s", manifest["coverage"])
    return n


def aziende(dsn: str, campione: int | None = None) -> int:
    percorso, f = _apri("aziende-segnali" + ("-campione" if campione else ""))
    n = 0
    with psycopg.connect(dsn) as conn:
        # le competenze più chieste da ciascuna azienda: l'aggregato che
        # trasforma un mucchio di annunci in un segnale su chi assume cosa
        skill_per_azienda: dict = {}
        with conn.cursor(name="esp_skill") as cur:
            cur.itersize = 5000
            cur.execute("""
                SELECT platform_id, slug, s.skill, count(*)
                  FROM ats_jobs, LATERAL unnest(skills) AS s(skill)
                 WHERE expired_at IS NULL
                 GROUP BY 1, 2, 3""")
            for pid, slug, skill, cnt in cur:
                skill_per_azienda.setdefault((pid, slug), []).append(
                    (cnt, skill))
        # le tecnologie DELL'AZIENDA, con prima e ultima data in cui sono
        # comparse in un suo annuncio (vista materializzata, rinfrescata
        # ogni mattina da deploy/rinfresca-viste.sh)
        tec_per_azienda: dict = {}
        with conn.cursor(name="esp_tec") as cur:
            cur.itersize = 5000
            cur.execute("""
                SELECT platform_id, slug, technology, annunci, annunci_attivi,
                       first_verified_at, last_verified_at
                  FROM azienda_tecnologie""")
            for pid, slug, tec, tot, att, primo, ultimo in cur:
                tec_per_azienda.setdefault((pid, slug), []).append(
                    (att, tot, tec, primo, ultimo))
        with conn.cursor(name="esp_az") as cur:
            cur.itersize = 2000
            cur.execute("""
                SELECT ac.platform_id, ac.slug, ac.company_name, ac.country,
                       ac.logo_domain, ac.logo_url, ac.job_count,
                       COALESCE(ac.employees_reg, ac.employees_wd,
                                ac.employees_site, ac.employees_self,
                                cd.employees),
                       COALESCE(ac.industry_reg, ac.industry,
                                ac.industry_site, ac.industry_mix),
                       ac.employees_reg_band, ac.reg_source,
                       -- la fonte dei dipendenti la decide la regola unica
                       -- (aziende_dettagli.dipendenti): qui solo il nome
                       -- del registro, per size_range_source
                       ac.reg_source,
                       CASE WHEN ac.industry_reg IS NOT NULL
                            THEN ac.reg_source
                            WHEN ac.industry IS NOT NULL THEN 'wikidata'
                            WHEN ac.industry_site IS NOT NULL
                            THEN 'company_site'
                            WHEN ac.industry_mix IS NOT NULL
                            THEN 'job_mix' END,
                       (SELECT count(*) FROM ats_jobs j
                         WHERE j.platform_id = ac.platform_id
                           AND j.slug = ac.slug AND j.expired_at IS NULL
                           AND COALESCE(j.posted_at, j.created_at)
                               > now() - interval '30 days'),
                       (SELECT array_agg(DISTINCT j.lang) FROM ats_jobs j
                         WHERE j.platform_id = ac.platform_id
                           AND j.slug = ac.slug AND j.expired_at IS NULL
                           AND j.lang IS NOT NULL),
                       -- 21/09/2026: la scheda azienda (aziende_dettagli)
                       ac.site_domain, ac.lei, ac.company_name_source,
                       ad.size_range, ad.size_da, ad.locations,
                       ad.n_locations, ad.hq_country, ad.hq_state,
                       ad.hq_city, ad.hq_street, ad.hq_zipcode,
                       ad.hq_full_address, ad.hq_da, ad.description,
                       ad.description_da, ad.external_urls, ad.keywords,
                       ad.employees_best, ad.employees_scope, ad.legal_name,
                       ad.legal_form, ad.legal_form_code, ad.registration_id,
                       ad.founded, ad.hq_latitude, ad.hq_longitude,
                       ac.employees_reg, ac.employees_wd, ac.employees_site,
                       ac.employees_self_n
                  FROM ats_companies ac
                  LEFT JOIN company_domains cd ON cd.domain = ac.logo_domain
                  LEFT JOIN aziende_dettagli ad ON ad.company_id = ac.id
                 WHERE ac.is_active AND ac.job_count > 0"""
                        + (" ORDER BY ac.job_count DESC LIMIT %s" if campione else ""),
                        (campione,) if campione else None)
            for r in cur:
                cime = sorted(skill_per_azienda.get((r[0], r[1]), []),
                              reverse=True)[:15]
                tec = sorted(tec_per_azienda.get((r[0], r[1]), []),
                             reverse=True)[:25]
                (sito, lei, nome_da, size_range, size_da, sedi, n_sedi,
                 hq_paese, hq_stato, hq_citta, hq_via, hq_cap, hq_indirizzo,
                 hq_da, descr, descr_da, urls, keywords, dip_best, dip_scope,
                 legal_name, legal_form, legal_form_code, reg_id, fondata,
                 hq_lat, hq_lon, e_reg, e_wd, e_site, e_self) = r[15:46]
                # la scheda puo' non esserci ancora (tenant nuovo): allora la
                # regola unica si applica qui, sugli stessi ingressi
                if dip_best is None and size_range is None:
                    dip_best, dip_da, dip_scope, size_range, size_da = dipendenti(
                        e_reg, e_site, e_self, e_wd, r[9], None)
                else:
                    # la fonte del NUMERO: quella il cui valore e' il numero
                    # scelto (la fascia puo' venire da un'altra, es. INSEE GE)
                    dip_da = next((f for f, n in (("wikidata", e_wd), ("sito", e_site), ("registro", e_reg),
                                                  ("dichiarato", e_self)) if n and int(n) == dip_best), None)
                hq = ({"country": hq_paese, "state": hq_stato,
                       "city": hq_citta, "street": hq_via,
                       "zipcode": hq_cap, "full_address": hq_indirizzo,
                       "latitude": hq_lat, "longitude": hq_lon,
                       "source": hq_da} if hq_da else None)
                f.write(_riga(
                    ats=r[0], company_slug=r[1], company=r[2], country=r[3],
                    domain=r[4], logo=r[5], active_jobs=r[6],
                    employees=dip_best, employees_scope=dip_scope,
                    employees_source=dip_da, industry=r[8],
                    employees_legal_entity=e_reg,
                    employees_legal_entity_band=r[9],
                    employees_legal_entity_source=r[11],
                    industry_source=r[12],
                    legal_name=legal_name, legal_form=legal_form,
                    legal_form_code=legal_form_code,
                    registration_id=reg_id, founded=fondata,
                    jobs_posted_30d=r[13],
                    languages=list(r[14] or []),
                    top_skills=[{"skill": s, "jobs": c} for c, s in cime],
                    website=sito, lei=lei, company_name_source=nome_da,
                    size_range=size_range, size_range_source=size_da,
                    locations=list(sedi or []), n_locations=n_sedi,
                    headquarters=hq, description=pulito(descr) or None,
                    description_source=descr_da,
                    external_urls=list(urls or []), keywords=keywords,
                    technologies=[{"technology": t, "active_jobs": a,
                                   "jobs": tot, "first_verified_at": p,
                                   "last_verified_at": u}
                                  for a, tot, t, p, u in tec]))
                n += 1
    _chiudi(percorso, f, n)
    return n


def scadute(dsn: str, giorni: int | None = None) -> int:
    percorso, f = _apri("offerte-chiuse")
    filtro = ("AND expired_at > now() - make_interval(days => %s)"
              if giorni else "")
    n = 0
    with psycopg.connect(dsn) as conn:
        with conn.cursor(name="esp_scadute") as cur:
            cur.itersize = 2000
            cur.execute(f"""
                SELECT id, title, platform_id, slug, url, country, city,
                       lang, seniority, posted_at, expired_at,
                       COALESCE(created_at, posted_at, fetched_at)
                  FROM ats_jobs
                 WHERE expired_at IS NOT NULL {filtro}""",
                (giorni,) if giorni else None)
            for r in cur:
                f.write(_riga(
                    id=str(r[0]), title=r[1], ats=r[2], company_slug=r[3],
                    url=r[4], country=r[5], city=r[6], language=r[7],
                    seniority=r[8], posted_at=r[9], closed_at=r[10],
                    first_seen=r[11]))
                n += 1
    _chiudi(percorso, f, n)
    return n


def flusso(dsn: str) -> dict:
    """Il magazzino in tempo reale: le novita' dall'ultimo giro.

    Nessuna richiesta esterna — si legge il NOSTRO database, quindi
    zero rischio ban: il rischio sta nella raccolta, che ha i suoi
    ritmi; qui si confeziona soltanto. Ogni giro produce un file con
    le offerte NUOVE (prima volta viste) e CHIUSE dall'ultimo cursore:
    e' il formato che i clienti dati chiamano delta feed, e che
    trasforma «foto del mattino» in «flusso continuo».
    """
    cartella = f"{CARTELLA}/flusso"
    os.makedirs(cartella, exist_ok=True)
    cursore_file = f"{cartella}/cursore.json"
    try:
        da = json.load(open(cursore_file))["t"]
    except Exception:                                # noqa: BLE001
        da = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(minutes=15)).isoformat()
    adesso = dt.datetime.now(dt.timezone.utc)
    marca = adesso.strftime("%Y%m%d-%H%M%S")
    percorso = f"{cartella}/novita-{marca}.jsonl.gz"
    stats = {"nuove": 0, "chiuse": 0}
    with psycopg.connect(dsn) as conn, \
            gzip.open(percorso + ".tmp", "wt", encoding="utf-8") as f:
        with conn.cursor(name="fl_nuove") as cur:
            cur.itersize = 2000
            cur.execute(f"""
                SELECT j.id, j.title, j.platform_id, j.slug,
                       COALESCE(co.company_name, j.raw->'company'->>'name'),
                       j.url, j.country, j.city, j.lang, j.seniority,
                       j.remote, j.skills, j.salary_min, j.salary_max,
                       j.salary_currency, j.posted_at, j.created_at,
                       {_DESCR}
                  FROM ats_jobs j
                  LEFT JOIN ats_companies co
                         ON co.platform_id = j.platform_id
                        AND co.slug = j.slug
                 WHERE j.created_at > %s::timestamptz
                   AND j.expired_at IS NULL""", (da,))
            for r in cur:
                f.write(_riga(
                    event="new", id=str(r[0]), title=r[1], ats=r[2],
                    company_slug=r[3], company=r[4], url=r[5],
                    country=r[6], city=r[7], language=r[8],
                    seniority=r[9], remote=r[10],
                    skills=list(r[11] or []),
                    salary_min=float(r[12]) if r[12] is not None else None,
                    salary_max=float(r[13]) if r[13] is not None else None,
                    salary_currency=r[14], posted_at=r[15],
                    first_seen=r[16], description=r[17]))
                stats["nuove"] += 1
        with conn.cursor(name="fl_chiuse") as cur:
            cur.itersize = 2000
            cur.execute("""
                SELECT id, title, platform_id, slug, url, country,
                       posted_at, expired_at
                  FROM ats_jobs
                 WHERE expired_at > %s::timestamptz""", (da,))
            for r in cur:
                f.write(_riga(
                    event="closed", id=str(r[0]), title=r[1], ats=r[2],
                    company_slug=r[3], url=r[4], country=r[5],
                    posted_at=r[6], closed_at=r[7]))
                stats["chiuse"] += 1
    os.replace(percorso + ".tmp", percorso)
    with open(cursore_file + ".tmp", "w") as f:
        json.dump({"t": adesso.isoformat()}, f)
    os.replace(cursore_file + ".tmp", cursore_file)
    log.info("flusso %s: %s", marca, stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.esporta")
    ap.add_argument("--attive", action="store_true")
    ap.add_argument("--aziende", action="store_true")
    ap.add_argument("--scadute", action="store_true")
    ap.add_argument("--flusso", action="store_true")
    ap.add_argument("--giorni", type=int, default=None,
                    help="per --scadute: solo le chiuse negli ultimi N giorni")
    ap.add_argument("--campione", type=int, default=None,
                    help="per --attive/--aziende: solo N righe, in un file "
                         "«-campione» a parte (collaudo o assaggio)")
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    if args.attive:
        print("attive:", attive(dsn, args.campione))
    if args.aziende:
        print("aziende:", aziende(dsn, args.campione))
    if args.scadute:
        print("scadute:", scadute(dsn, args.giorni))
    if args.flusso:
        print("flusso:", flusso(dsn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
