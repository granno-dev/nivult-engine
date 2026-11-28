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
import tempfile
import uuid
from contextlib import nullcontext

import psycopg

from nivult.ats.aziende_dettagli import dipendenti
from nivult.ats.export_quality import (
    aggiorna_indice,
    carica_indice,
    indice_nuovo,
    pubblica_generazione,
    scrivi_indice,
    stratifica,
)
from nivult.ats.qualita_dati import campi_esportati, valuta_updated
from nivult.ats.source_type import COMPANY_SQL, SOURCE_TYPE_SQL
from nivult.ats.testo import pulito

log = logging.getLogger("nivult.ats.esporta")

CARTELLA = "/opt/nivult/exports"
RUN_ID = uuid.uuid4().hex[:12]

_DESCR = "coalesce((SELECT v FROM unnest(ARRAY[raw->>'description', raw->>'content', raw->>'descriptionHtml', raw->>'descriptionPlain', raw->>'externalDescription', raw->>'jobDescription', raw->>'job_description', raw->>'Job_Description', raw->>'body', raw->>'content_html', raw->>'description_html', raw->>'descriptionBody', raw->>'text', raw->'_jobposting'->>'description', raw->>'ShortDescriptionStr']) v WHERE length(v) >= 80 LIMIT 1), '')"

_ATTIVE_SELECT = f"""
    SELECT j.id, j.title, j.platform_id, j.slug,
           {COMPANY_SQL},
           j.url, j.country, j.city, j.location, j.lang,
           j.seniority, j.remote, j.skills,
           j.salary_min, j.salary_max, j.salary_currency,
           j.posted_at, COALESCE(j.created_at, j.posted_at, j.fetched_at),
           j.fetched_at, {_DESCR}, x.family,
           j.employment_type, j.languages_required,
           j.external_id, j.department, j.orario, j.durata,
           j.salary_period, j.salary_da, j.posted_at_estimated,
           t.tecnologie, sf.sintesi, sf.da,
           d.management_level, d.is_decision_maker,
           d.shift_schedule, d.work_hours, d.is_urgently_hiring,
           d.benefits, d.salary_text, d.valid_through, d.state,
           d.postal_code, d.latitude, d.longitude, d.geo_da,
           d.applicants_count, d.is_easy_apply,
           fo.job_sources, j.content_updated_at,
           {SOURCE_TYPE_SQL} AS source_type, co.site_domain, h.content_hash
      FROM ats_jobs j
      LEFT JOIN ats_companies co
             ON co.platform_id = j.platform_id AND co.slug = j.slug
      LEFT JOIN ats_delta_hash h ON h.job_id = j.id
      LEFT JOIN job_classifications x ON x.job_id = j.id
      LEFT JOIN tecnologie_v1 t ON t.job_id = j.id
      LEFT JOIN sintesi_finali sf ON sf.job_id = j.id
      LEFT JOIN offerte_dettagli d ON d.job_id = j.id
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
"""


def _ultimo_file(cartella: str, prefisso: str) -> str | None:
    candidati = [os.path.join(cartella, nome) for nome in os.listdir(cartella)
                 if nome.startswith(prefisso + "-")
                 and re.search(r"-\d{4}-\d{2}-\d{2}(?:-[0-9a-f]{12})?\.jsonl\.gz$", nome)
                 and (prefisso.endswith("campione") or "-campione-" not in nome)]
    return max(candidati) if candidati else None


def _apri(nome: str):
    os.makedirs(CARTELLA, exist_ok=True)
    oggi = f"{dt.date.today().isoformat()}-{RUN_ID}"
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
_SKILL_PAR_RX = re.compile(r"\s*\((computer programming|programming language|software|framework|database|tool|technology|methodology)\)\s*$", re.IGNORECASE)


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
    return json.dumps(campi_esportati(kv), ensure_ascii=False, default=str) + "\n"


def _benchmark(conn) -> dict:
    bench = {}
    with conn.cursor() as cur:
        cur.execute("""SELECT country, currency, family, seniority,
                              p25, p50, p75, n
                         FROM stipendi_benchmark""")
        for r in cur:
            bench[(r[0], r[2], r[3])] = (r[1], r[4], r[5], r[6], r[7])
    return bench


def _riga_attiva(r, bench: dict) -> dict:
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
                "salary_estimate_basis": f"Nivult benchmark: {camp} observed "
                                         f"postings, {livello}"}
    (ext_id, department, orario, durata, sal_period, sal_da,
     posted_est, tecnologie, sintesi, sintesi_da, mgmt,
     decisore, turni, ore, urgente, benefits, sal_testo,
     valid_through, state, cap, lat, lon, geo_da, candidati,
     easy, fonti) = r[23:49]
    geo = ({"latitude": lat, "longitude": lon, "source": geo_da}
           if lat is not None else None)
    return {
        "id": str(r[0]), "title": pulito(r[1]), "ats": r[2], "company_slug": r[3],
        "company": pulito(r[4]), "url": r[5], "country": r[6], "city": r[7],
        "location": r[8], "language": r[9], "seniority": r[10],
        "remote": r[11], "skills": _skills_pulite(r[12]),
        "company_domain": (str(r[51]).strip().lower()
                           if len(r) > 51 and r[51] else None),
        "has_salary": r[13] is not None or r[14] is not None,
        "salary_min": float(r[13]) if r[13] is not None else None,
        "salary_max": float(r[14]) if r[14] is not None else None,
        "salary_currency": r[15], "posted_at": r[16],
        "first_seen": r[17], "last_seen": r[18],
        "description": pulito(r[19]), "category": r[20],
        "employment_type": r[21],
        "languages_required": list(r[22] or []), **stima,
        "external_id": ext_id, "department": department,
        "hours_type": orario, "duration_type": durata,
        "salary_period": sal_period, "salary_source": sal_da,
        "salary_text": pulito(sal_testo),
        "posted_at_estimated": True if posted_est else None,
        "technologies": list(tecnologie or []),
        "summary": pulito(sintesi), "summary_source": sintesi_da,
        "management_level": mgmt,
        "is_decision_maker": True if decisore else None,
        "shift_schedule": turni, "work_hours": ore,
        "is_urgently_hiring": True if urgente else None,
        "benefits": list(benefits or []), "valid_through": valid_through,
        "state": state, "postal_code": cap, "geo": geo,
        "applicants_count": candidati,
        "is_easy_apply": True if easy else None,
        "job_sources": list(fonti or []),
        "updated_at": r[49] if len(r) > 49 else None,
        "source_type": r[50] if len(r) > 50 else "direct",
    }


def _vetrina_merge(campi: dict) -> None:
    """Il blocco `vetrina` del manifest: i contatori del sito pubblico.

    08/10/2026 — prima i numeri della landing nascevano da DUE fonti di
    freschezza diversa (la cache del cruscotto, ogni 4 minuti, e l'export
    del giorno): dentro la stessa pagina il titolo e le scatole potevano
    raccontare indici diversi, e fra un'apertura e l'altra i numeri
    ballavano. Ora ogni fase dell'export scrive qui i suoi conteggi, dal
    suo stesso passaggio sul database: una fonte sola, tutta dello stesso
    mattino, che si muove tutta insieme una volta al giorno. Il merge
    per-giorno evita che un giro parziale (solo --aziende, per dire)
    mescoli ieri e oggi."""
    nome = f"{CARTELLA}/manifest-ultimo.json"
    try:
        with open(nome) as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError):
        manifest = {"date": dt.date.today().isoformat()}
    vet = manifest.get("vetrina") or {}
    if vet.get("date") != manifest.get("date"):
        vet = {"date": manifest["date"]}
    vet.update(campi)
    manifest["vetrina"] = vet
    with open(nome + ".tmp", "w") as f:
        json.dump(manifest, f, indent=1)
    os.replace(nome + ".tmp", nome)


def attive(dsn: str, campione: int | None = None) -> int:
    # 09/10/2026: l'export e' morto a META' per DiskFull — la query
    # principale riversa diversi GB di temporanei in pgdata (volume) e il
    # file da ~6 GB finisce in exports (root). Il preflight misura i due
    # dischi PRIMA: se non c'e' spazio si fallisce subito e in chiaro,
    # non dopo due ore a meta' file. La sentinella allarma per l'export
    # mancante: il messaggio nel log dice il perche'.
    import shutil as _sh
    if not campione:
        vol = _sh.disk_usage("/mnt/HC_Volume_106941692").free / 2**30
        rad = _sh.disk_usage("/").free / 2**30
        if vol < 10 or rad < 9:
            raise SystemExit(
                f"esporta: spazio insufficiente — volume {vol:.0f} GB, "
                f"root {rad:.0f} GB (servono ~10 GB di temporanei Postgres "
                f"sul volume e ~9 GB di file su root). Liberare spazio e "
                f"rilanciare: l'export NON parte a meta'.")
    percorso, f = _apri("offerte-attive" + ("-campione" if campione else ""))
    n = 0
    # i contatori della vetrina si accumulano nel passaggio che c'e' gia':
    # paesi e piattaforme come insiemi, le nuove dal first_seen di riga
    vet_paesi: set = set()
    vet_piattaforme: set = set()
    vet_nuove = 0
    soglia_24h = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    copertura = {k: 0 for k in ("salary_observed", "salary_estimate",
                                "description", "language", "category",
                                "country", "skills", "employment_type",
                                "seniority", "remote",
                                "technologies", "summary", "benefits",
                                "shift_schedule", "work_hours",
                                "management_level", "valid_through",
                                "state", "geo", "job_sources",
                                "department", "salary_text")}
    indice = indice_nuovo()

    def scrivi(riga: dict) -> None:
        nonlocal n, vet_nuove
        if riga.get("country"):
            vet_paesi.add(riga["country"])
        if riga.get("ats"):
            vet_piattaforme.add(riga["ats"])
        fs = riga.get("first_seen")
        if fs:
            if not isinstance(fs, dt.datetime):
                fs = dt.datetime.combine(fs, dt.time.min, tzinfo=dt.UTC)
            elif fs.tzinfo is None:
                fs = fs.replace(tzinfo=dt.UTC)
            if fs >= soglia_24h:
                vet_nuove += 1
        f.write(_riga(**riga))
        aggiorna_indice(indice, riga)
        n += 1
        copertura["salary_observed"] += bool(riga["has_salary"])
        copertura["salary_estimate"] += bool(riga.get("salary_is_estimate"))
        copertura["description"] += bool(riga.get("description"))
        copertura["salary_text"] += bool(riga.get("salary_text"))
        for chiave, campo in (("language", "language"), ("category", "category"),
                              ("country", "country"), ("employment_type", "employment_type"),
                              ("seniority", "seniority"), ("remote", "remote"),
                              ("valid_through", "valid_through"), ("state", "state"),
                              ("geo", "geo"), ("department", "department"),
                              ("management_level", "management_level"),
                              ("shift_schedule", "shift_schedule"), ("work_hours", "work_hours")):
            copertura[chiave] += riga.get(campo) is not None
        for chiave, campo in (("skills", "skills"), ("technologies", "technologies"),
                              ("summary", "summary"), ("benefits", "benefits"),
                              ("job_sources", "job_sources")):
            copertura[chiave] += bool(riga.get(campo))

    with psycopg.connect(dsn) as conn:
        bench = _benchmark(conn)
        with conn.cursor(name="esp_attive") as cur:
            cur.itersize = 2000
            cur.execute(f"""{_ATTIVE_SELECT}
                 WHERE j.expired_at IS NULL
                   AND NOT EXISTS (SELECT 1 FROM ats_jobs d
                                    WHERE d.duplicate_key = j.duplicate_key
                                      AND d.expired_at IS NULL
                                      AND (COALESCE(d.created_at, d.posted_at, d.fetched_at), d.id)
                                          < (COALESCE(j.created_at, j.posted_at, j.fetched_at), j.id))""")
            if campione:
                for riga in stratifica((_riga_attiva(r, bench) for r in cur), campione):
                    scrivi(riga)
            else:
                for r in cur:
                    scrivi(_riga_attiva(r, bench))
    scrivi_indice(percorso + ".indice.json.gz", indice)
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
    if not campione:
        _vetrina_merge({"offerte": n, "paesi": len(vet_paesi),
                        "piattaforme": len(vet_piattaforme),
                        "nuove_24h": vet_nuove})
    return n


def aziende(dsn: str, campione: int | None = None) -> int:
    percorso, f = _apri("aziende-segnali" + ("-campione" if campione else ""))
    indice_path = percorso.replace("aziende-segnali", "offerte-attive") + ".indice.json.gz"
    archivio = carica_indice(indice_path)
    n = 0
    copertura: dict[str, int] = {}
    with psycopg.connect(dsn) as conn, conn.cursor(name="esp_az") as cur:
        cur.itersize = 2000
        cur.execute("""
            SELECT ac.platform_id, ac.slug, ac.company_name, ac.country,
                   ac.logo_domain, ac.logo_url, ac.job_count,
                   COALESCE(ac.employees_reg, ac.employees_wd,
                            ac.employees_site, ac.employees_self, cd.employees),
                   COALESCE(ac.industry_reg, ac.industry, ac.industry_site,
                            ac.pdl_industry, ac.industry_mix),
                   ac.employees_reg_band, ac.reg_source, ac.reg_source,
                   CASE WHEN ac.industry_reg IS NOT NULL THEN ac.reg_source
                        WHEN ac.industry IS NOT NULL THEN 'wikidata'
                        WHEN ac.industry_site IS NOT NULL THEN 'company_site'
                        WHEN ac.pdl_industry IS NOT NULL THEN 'pdl (free dataset)'
                        WHEN ac.industry_mix IS NOT NULL THEN 'job_mix' END,
                   NULL, NULL,
                   ac.site_domain, ac.lei, ac.company_name_source,
                   ad.size_range, ad.size_da, ad.locations, ad.n_locations,
                   ad.hq_country, ad.hq_state, ad.hq_city, ad.hq_street,
                   ad.hq_zipcode, ad.hq_full_address, ad.hq_da, ad.description,
                   ad.description_da, ad.external_urls, ad.keywords,
                   ad.employees_best, ad.employees_scope, ad.legal_name,
                   ad.legal_form, ad.legal_form_code, ad.registration_id,
                   ad.founded, ad.hq_latitude, ad.hq_longitude,
                   ac.employees_reg, ac.employees_wd, ac.employees_site,
                   ac.employees_self_n, ac.industry_mix_share, ad.founded_da,
                   ac.pdl_size
              FROM ats_companies ac
              LEFT JOIN company_domains cd ON cd.domain = ac.logo_domain
              LEFT JOIN aziende_dettagli ad ON ad.company_id = ac.id""")
        arricchite = {}
        for r in cur:
            key = f"{r[0]}\0{r[1]}"
            if key in archivio:
                arricchite[key] = r

    for key, canon in archivio.items():
        r = list(arricchite.get(key) or [None] * 49)
        r[0], r[1] = canon["ats"], canon["company_slug"]
        cime = canon["top_skills"]
        tec = canon["technologies"]
        (sito, lei, nome_da, size_range, size_da, sedi, n_sedi,
         hq_paese, hq_stato, hq_citta, hq_via, hq_cap, hq_indirizzo,
         hq_da, descr, descr_da, urls, keywords, dip_best, dip_scope,
         legal_name, legal_form, legal_form_code, reg_id, fondata,
         hq_lat, hq_lon, e_reg, e_wd, e_site, e_self) = r[15:46]
        if dip_best is None and size_range is None:
            dip_best, dip_da, dip_scope, size_range, size_da = dipendenti(
                e_reg, e_site, e_self, e_wd, r[9], None, r[48])
        else:
            dip_da = next((fonte for fonte, numero in (("wikidata", e_wd), ("sito", e_site),
                          ("registro", e_reg), ("dichiarato", e_self))
                          if numero and int(numero) == dip_best), None)
        hq = ({"country": hq_paese, "state": hq_stato, "city": hq_citta,
               "street": hq_via, "zipcode": hq_cap, "full_address": hq_indirizzo,
               "latitude": hq_lat, "longitude": hq_lon, "source": hq_da}
              if hq_da else None)
        dominio = ((sito or "").lstrip("'") or r[4] or canon.get("domain"))
        company = pulito(canon.get("company") or r[2])
        country = canon.get("country") or r[3]
        f.write(_riga(
            ats=canon["ats"], company_slug=canon["company_slug"], company=company,
            country=country, domain=dominio, logo=r[5],
            active_jobs=canon["active_jobs"], employees=dip_best,
            employees_scope=dip_scope, employees_source=dip_da, industry=r[8],
            employees_legal_entity=e_reg, employees_legal_entity_band=r[9],
            employees_legal_entity_source=r[11], industry_source=r[12],
            industry_mix_share=r[46], founded_source=r[47], legal_name=legal_name,
            legal_form=legal_form, legal_form_code=legal_form_code,
            registration_id=reg_id, founded=fondata,
            jobs_posted_30d=canon["jobs_posted_30d"],
            languages=canon["languages"], top_skills=cime, website=sito, lei=lei,
            company_name_source=nome_da, size_range=size_range,
            size_range_source=size_da, locations=list(sedi or []),
            n_locations=n_sedi, headquarters=hq, description=pulito(descr) or None,
            description_source=descr_da, external_urls=list(urls or []),
            keywords=keywords, technologies=tec))
        n += 1
        copertura["country"] = copertura.get("country", 0) + (country is not None)
        copertura["domain"] = copertura.get("domain", 0) + bool(dominio)
        copertura["website"] = copertura.get("website", 0) + bool(sito)
        copertura["industry"] = copertura.get("industry", 0) + bool(r[8])
        copertura["size_range"] = copertura.get("size_range", 0) + bool(size_range)
        copertura["employees"] = copertura.get("employees", 0) + (dip_best is not None)
        copertura["legal_name"] = copertura.get("legal_name", 0) + bool(legal_name)
        copertura["legal_form"] = copertura.get("legal_form", 0) + bool(legal_form)
        copertura["registration_id"] = copertura.get("registration_id", 0) + bool(reg_id)
        copertura["founded"] = copertura.get("founded", 0) + bool(fondata)
        copertura["headquarters"] = copertura.get("headquarters", 0) + bool(hq)
        copertura["description"] = copertura.get("description", 0) + bool(pulito(descr))
        copertura["technologies"] = copertura.get("technologies", 0) + bool(tec)
        copertura["top_skills"] = copertura.get("top_skills", 0) + bool(cime)
        copertura["locations"] = copertura.get("locations", 0) + bool(sedi)
        copertura["external_urls"] = copertura.get("external_urls", 0) + bool(urls)
        copertura["lei"] = copertura.get("lei", 0) + bool(lei)
        copertura["languages"] = copertura.get("languages", 0) + bool(canon["languages"])
    _chiudi(percorso, f, n)
    nome_manifest = f"{CARTELLA}/manifest-campione.json" if campione else f"{CARTELLA}/manifest-ultimo.json"
    try:
        with open(nome_manifest) as mf:
            manifest = json.load(mf)
    except (OSError, json.JSONDecodeError):
        manifest = {"date": dt.date.today().isoformat()}
    manifest["companies_rows"] = n
    if not campione:
        manifest["coverage_aziende"] = {k: round(100 * v / max(n, 1), 1)
                                        for k, v in copertura.items()}
        _vetrina_merge({"aziende": n})
    with open(nome_manifest + ".tmp", "w") as mf:
        json.dump(manifest, mf, indent=1, default=str)
    os.replace(nome_manifest + ".tmp", nome_manifest)
    return n


def esporta_coppia(dsn: str, campione: int | None = None) -> tuple[int, int]:
    global CARTELLA
    radice = CARTELLA
    staging = tempfile.mkdtemp(prefix=".staging-", dir=radice)
    try:
        CARTELLA = staging
        n_jobs = attive(dsn, campione)
        n_aziende = aziende(dsn, campione)
        nome_manifest = "manifest-campione.json" if campione else "manifest-ultimo.json"
        with open(os.path.join(staging, nome_manifest)) as f:
            manifest = json.load(f)
        jobs_path = _ultimo_file(staging, "offerte-attive-campione" if campione else "offerte-attive")
        companies_path = _ultimo_file(staging, "aziende-segnali-campione" if campione else "aziende-segnali")
        pubblica_generazione(staging, radice, manifest, os.path.basename(jobs_path),
                             os.path.basename(companies_path), nome_manifest)
        return n_jobs, n_aziende
    finally:
        CARTELLA = radice
        if os.path.isdir(staging):
            import shutil
            shutil.rmtree(staging)


def scadute(dsn: str, giorni: int | None = None) -> int:
    percorso, f = _apri("offerte-chiuse")
    filtro = ("AND expired_at > now() - make_interval(days => %s)"
              if giorni else "")
    n = 0
    chiuse_24h = 0
    soglia_24h = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    with psycopg.connect(dsn) as conn:
        # 09/10/2026: i contatori della vetrina NON possono venire dal
        # conteggio del file — il giro notturno scrive solo le chiuse
        # degli ultimi 7 giorni (--giorni), e lo storico si misura sul
        # database intero. Due conteggi SQL a parte: il file resta la
        # settimana, il manifest dice il totale vero.
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*),
                                  count(*) FILTER (WHERE expired_at >=
                                     now() - make_interval(days => 1))
                             FROM ats_jobs WHERE expired_at IS NOT NULL""")
            storico_tot, chiuse_oggi_db = cur.fetchone()
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
                    id=str(r[0]), title=pulito(r[1]), ats=r[2], company_slug=r[3],
                    url=r[4], country=r[5], city=r[6], language=r[7],
                    seniority=r[8], posted_at=r[9], closed_at=r[10],
                    first_seen=r[11]))
                n += 1
                if r[10] and r[10] >= soglia_24h:
                    chiuse_24h += 1
    _chiudi(percorso, f, n)
    _vetrina_merge({"chiuse_storico": storico_tot,
                    "chiuse_24h": chiuse_oggi_db})
    return n


class _NullOutput:
    def write(self, value: str) -> int:
        return len(value)


def _salva_hash(conn, batch: list[tuple]) -> None:
    if not batch:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO ats_delta_hash (job_id, content_hash, last_emitted_at)
            VALUES (%s, %s, CASE WHEN %s THEN now() END)
            ON CONFLICT (job_id) DO UPDATE SET
                content_hash = EXCLUDED.content_hash,
                updated_at = now(),
                last_emitted_at = CASE WHEN %s THEN now()
                                       ELSE ats_delta_hash.last_emitted_at END
            """,
            batch,
        )
    batch.clear()


def _chiusure_massive_delta(conn, da: str, adesso: dt.datetime) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH chiusi AS (
                SELECT platform_id, slug, count(*) AS n FROM ats_jobs
                 WHERE expired_at > %s::timestamptz
                   AND expired_at <= %s::timestamptz
                   AND COALESCE(closure_reason, '') NOT IN
                       ('zombie_365d', 'zombie_365d_backlog', 'last_seen_7d_backlog')
                 GROUP BY 1, 2),
            tot_tenant AS (
                SELECT platform_id, slug, count(*) AS tot FROM ats_jobs
                 WHERE expired_at IS NULL OR expired_at > %s::timestamptz
                 GROUP BY 1, 2),
            rifiuto_tenant AS (
                SELECT c.platform_id, c.slug, c.n, t.tot
                  FROM chiusi c JOIN tot_tenant t USING (platform_id, slug)
                 WHERE c.n >= %s AND c.n > t.tot * %s),
            chiusi_platform AS (
                SELECT platform_id, sum(n) AS n FROM chiusi GROUP BY 1),
            tot_platform AS (
                SELECT platform_id, count(*) AS tot FROM ats_jobs
                 WHERE expired_at IS NULL OR expired_at > %s::timestamptz
                 GROUP BY 1)
            SELECT platform_id || '/' || slug, n, tot FROM rifiuto_tenant
            UNION ALL
            SELECT c.platform_id, c.n, t.tot
              FROM chiusi_platform c JOIN tot_platform t USING (platform_id)
             WHERE c.n >= %s AND c.n > t.tot * %s
            """,
            (da, adesso, da, 100, 0.30, da, 500, 0.30),
        )
        return cur.fetchall()


def _leggi_cursore(conn, fallback: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT valore FROM ats_delta_state WHERE chiave = 'flusso'")
        row = cur.fetchone()
    return row[0] if row else fallback


def _salva_cursore(conn, adesso: dt.datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ats_delta_state (chiave, valore, updated_at)
            VALUES ('flusso', %s, now())
            ON CONFLICT (chiave) DO UPDATE SET
                valore = EXCLUDED.valore, updated_at = now()
            """,
            (adesso.isoformat(),),
        )


def flusso(dsn: str, *, dry_run: bool = False) -> dict:
    """Il delta feed: offerte nuove, aggiornate e chiuse dall'ultimo giro.

    Gli aggiornamenti portano la stessa fotografia completa dell'export
    attive e il loro `updated_at`, non soltanto il campo cambiato. Ogni
    query ha anche un limite superiore: il cursore non puo' saltare un
    evento arrivato mentre il file era in scrittura.
    """
    cartella = f"{CARTELLA}/flusso"
    if not dry_run:
        os.makedirs(cartella, exist_ok=True)
    cursore_file = f"{cartella}/cursore.json"
    try:
        with open(cursore_file) as f:
            da = json.load(f)["t"]
    except Exception:                                # noqa: BLE001
        da = (dt.datetime.now(dt.UTC)
              - dt.timedelta(minutes=15)).isoformat()
    adesso = dt.datetime.now(dt.UTC)
    marca = adesso.strftime("%Y%m%d-%H%M%S")
    percorso = f"{cartella}/novita-{marca}.jsonl.gz"
    stats = {
        "nuove": 0,
        "aggiornate": 0,
        "chiuse": 0,
        "hash_baseline": 0,
        "hash_immutati": 0,
    }
    hash_batch: list[tuple] = []
    conn = psycopg.connect(dsn)
    output = None
    try:
        output = (
            nullcontext(_NullOutput())
            if dry_run
            else gzip.open(percorso + ".tmp", "wt", encoding="utf-8")  # noqa: SIM115
        )
        f = output.__enter__()
        da = _leggi_cursore(conn, da)
        bench = _benchmark(conn)
        bloccate = _chiusure_massive_delta(conn, da, adesso)
        if bloccate:
            dettaglio = ", ".join(f"{p}: {n}/{tot}" for p, n, tot in bloccate)
            raise RuntimeError(f"Delta fermo: chiusure massive {dettaglio}")
        with conn.cursor(name="fl_nuove") as cur:
            cur.itersize = 2000
            cur.execute(f"""{_ATTIVE_SELECT}
                 WHERE j.created_at > %s::timestamptz
                   AND j.created_at <= %s::timestamptz
                   AND j.expired_at IS NULL""", (da, adesso))
            for r in cur:
                record = _riga_attiva(r, bench)
                f.write(_riga(event="new", **record))
                stats["nuove"] += 1
                if not dry_run:
                    digest = valuta_updated(None, record).digest
                    hash_batch.append((str(record["id"]), digest, True, True))
                if len(hash_batch) >= 1000:
                    _salva_hash(conn, hash_batch)
        with conn.cursor(name="fl_aggiornate") as cur:
            cur.itersize = 2000
            cur.execute(f"""{_ATTIVE_SELECT}
                 WHERE j.content_updated_at > %s::timestamptz
                   AND j.content_updated_at <= %s::timestamptz
                   AND COALESCE(j.created_at, j.posted_at, j.fetched_at)
                       <= %s::timestamptz
                   AND j.expired_at IS NULL
                   AND NOT EXISTS (SELECT 1 FROM ats_jobs d
                                    WHERE d.duplicate_key = j.duplicate_key
                                      AND j.duplicate_key IS NOT NULL
                                      AND d.expired_at IS NULL
                                      AND (COALESCE(d.created_at, d.posted_at,
                                                    d.fetched_at), d.id)
                                          < (COALESCE(j.created_at, j.posted_at,
                                                      j.fetched_at), j.id))""",
                        (da, adesso, da))
            for r in cur:
                record = _riga_attiva(r, bench)
                precedente = r[52] if len(r) > 52 else None
                decisione = valuta_updated(precedente, record)
                if decisione.motivo == "immutato":
                    stats["hash_immutati"] += 1
                    continue
                if not dry_run:
                    hash_batch.append((
                        str(record["id"]), decisione.digest,
                        decisione.emetti, decisione.emetti,
                    ))
                if decisione.motivo == "baseline":
                    stats["hash_baseline"] += 1
                else:
                    f.write(_riga(event="updated", **record))
                    stats["aggiornate"] += 1
                if len(hash_batch) >= 1000:
                    _salva_hash(conn, hash_batch)
            if not dry_run:
                _salva_hash(conn, hash_batch)
        with conn.cursor(name="fl_chiuse") as cur:
            cur.itersize = 2000
            cur.execute("""
                SELECT id, title, platform_id, slug, url, country,
                       posted_at, expired_at
                  FROM ats_jobs
                 WHERE expired_at > %s::timestamptz
                   AND expired_at <= %s::timestamptz""", (da, adesso))
            for r in cur:
                f.write(_riga(
                    event="closed", id=str(r[0]), title=pulito(r[1]), ats=r[2],
                    company_slug=r[3], url=r[4], country=r[5],
                    posted_at=r[6], closed_at=r[7]))
                stats["chiuse"] += 1
        output.__exit__(None, None, None)
        if not dry_run:
            os.replace(percorso + ".tmp", percorso)
            _salva_cursore(conn, adesso)
            conn.commit()
    except Exception as exc:
        if output is not None:
            output.__exit__(type(exc), exc, exc.__traceback__)
        if not dry_run:
            try:
                os.remove(percorso + ".tmp")
            except OSError:
                pass
        raise
    finally:
        conn.close()
    log.info("flusso %s%s: %s", marca, " dry-run" if dry_run else "", stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.esporta")
    ap.add_argument("--attive", action="store_true")
    ap.add_argument("--aziende", action="store_true")
    ap.add_argument("--scadute", action="store_true")
    ap.add_argument("--flusso", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="per --flusso: conta gli eventi senza file, cursore o hash")
    ap.add_argument("--giorni", type=int, default=None,
                    help="per --scadute: solo le chiuse negli ultimi N giorni")
    ap.add_argument("--campione", type=int, default=None,
                    help="per --attive/--aziende: solo N righe, in un file "
                         "«-campione» a parte (collaudo o assaggio)")
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    if args.attive and args.aziende:
        print("attive e aziende:", esporta_coppia(dsn, args.campione))
    else:
        if args.attive:
            print("attive:", attive(dsn, args.campione))
        if args.aziende:
            print("aziende:", aziende(dsn, args.campione))
    if args.scadute:
        print("scadute:", scadute(dsn, args.giorni))
    if args.flusso:
        print("flusso:", flusso(dsn, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
