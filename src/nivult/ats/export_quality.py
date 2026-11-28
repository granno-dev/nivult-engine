from __future__ import annotations

import collections
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .pii import contains_pii as contiene_pii

PLACEHOLDER = {"n/a", "null", "unknown", "confidential", "undefined", "none", "-", "--", "[redacted]"}
HTML_RX = re.compile(r"<(?:div|p|br|span)\b|&(?:nbsp|amp|#)[A-Za-z0-9#]*;?", re.I)
MOJIBAKE_RX = re.compile("Ã©|Ã¨|Ãª|Ã«|Ã¢|Ã§|Ã±|Ã³|Ã¶|Ã¼|Ãº|Ãš|Ã‰|Ãˆ|Ã‡|â€|�")
EU = frozenset("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split())


class InvariantError(ValueError):
    pass


def _data(valore):
    if not valore:
        return None
    try:
        d = dt.datetime.fromisoformat(str(valore).replace("Z", "+00:00"))
        return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)
    except ValueError:
        return None


def indice_nuovo() -> dict:
    return {}


def aggiorna_indice(indice: dict, riga: dict) -> None:
    ats, slug = riga.get("ats"), riga.get("company_slug")
    if not ats or not slug:
        raise InvariantError("job senza chiave ats/company_slug")
    key = f"{ats}\0{slug}"
    a = indice.setdefault(key, {
        "ats": ats, "company_slug": slug, "active_jobs": 0, "jobs_posted_30d": 0,
        "company": None, "countries": {}, "domains": {}, "languages": set(),
        "skills": {}, "technologies": {}, "first_seen": None, "last_seen": None,
    })
    a["active_jobs"] += 1
    if riga.get("company") and not a["company"]:
        a["company"] = riga["company"]
    for field, target in (("country", "countries"), ("company_domain", "domains")):
        if riga.get(field):
            a[target][riga[field]] = a[target].get(riga[field], 0) + 1
    if riga.get("language"):
        a["languages"].add(riga["language"])
    posted = _data(riga.get("posted_at"))
    if posted and posted >= dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30):
        a["jobs_posted_30d"] += 1
    for name, value in (("first_seen", min), ("last_seen", max)):
        if posted and (a[name] is None or value(a[name], posted) == posted):
            a[name] = posted
    for skill in riga.get("skills") or []:
        a["skills"][skill] = a["skills"].get(skill, 0) + 1
    for tech in set(riga.get("technologies") or []):
        if not isinstance(tech, str) or not tech.strip():
            continue
        tech = tech.strip()
        t = a["technologies"].setdefault(tech, {"jobs": 0, "first": None, "last": None})
        t["jobs"] += 1
        if posted:
            if t["first"] is None or posted < t["first"]:
                t["first"] = posted
            if t["last"] is None or posted > t["last"]:
                t["last"] = posted


def _maggiore(contatori: dict, default=None):
    if not contatori:
        return default
    return sorted(contatori, key=lambda x: (-contatori[x], x))[0]


def azienda_da_indice(a: dict) -> dict:
    tecnologie = []
    for tech, t in a["technologies"].items():
        tecnologie.append({
            "technology": tech, "active_jobs": t["jobs"], "jobs": t["jobs"],
            "first_verified_at": t["first"], "last_verified_at": t["last"],
        })
    tecnologie.sort(key=lambda x: (-x["active_jobs"], x["technology"]))
    skills = sorted(a["skills"].items(), key=lambda x: (-x[1], x[0]))[:15]
    return {
        "ats": a["ats"], "company_slug": a["company_slug"], "company": a["company"],
        "country": _maggiore(a["countries"]), "domain": _maggiore(a["domains"]),
        "active_jobs": a["active_jobs"], "jobs_posted_30d": a["jobs_posted_30d"],
        "languages": sorted(a["languages"]),
        "top_skills": [{"skill": s, "jobs": n} for s, n in skills],
        "technologies": tecnologie,
    }


def scrivi_indice(percorso: str, indice: dict) -> None:
    serializzato = {}
    for key, a in indice.items():
        b = azienda_da_indice(a)
        b["first_seen"] = a["first_seen"]
        b["last_seen"] = a["last_seen"]
        serializzato[key] = b
    with gzip.open(percorso, "wt", encoding="utf-8") as f:
        json.dump(serializzato, f, ensure_ascii=False, default=str)


def carica_indice(percorso: str) -> dict:
    with gzip.open(percorso, "rt", encoding="utf-8") as f:
        return json.load(f)


def _righe_jsonl(percorso: str):
    with gzip.open(percorso, "rt", encoding="utf-8") as f:
        for n, linea in enumerate(f, 1):
            try:
                yield n, json.loads(linea)
            except json.JSONDecodeError as exc:
                raise InvariantError(f"JSONL non valido {percorso}:{n}") from exc


def verifica_export(jobs_path: str, companies_path: str, *, campione: bool = False) -> dict[str, int]:
    import duckdb
    if campione:
        for n, r in _righe_jsonl(jobs_path):
            valida_job_sample(r, n)
        for n, c in _righe_jsonl(companies_path):
            valida_company_sample(c, n)
    with tempfile.TemporaryDirectory(prefix="export-invariants-") as temp_dir:
        con = duckdb.connect(":memory:")
        try:
            con.execute("SET threads=1")
            con.execute("SET preserve_insertion_order=false")
            con.execute("SET memory_limit='2GB'")
            con.execute(f"SET temp_directory='{temp_dir}'")
            con.execute("""CREATE TEMP TABLE jobs AS
                SELECT id,ats,company_slug,technologies FROM read_json(
                    ?,format='newline_delimited',columns={
                        'id':'VARCHAR','ats':'VARCHAR','company_slug':'VARCHAR',
                        'technologies':'VARCHAR[]'},union_by_name=true)""", [jobs_path])
            con.execute("""CREATE TEMP TABLE companies AS
                SELECT ats,company_slug,active_jobs,technologies FROM read_json(
                    ?,format='newline_delimited',columns={
                        'ats':'VARCHAR','company_slug':'VARCHAR','active_jobs':'BIGINT',
                        'technologies':'STRUCT(technology VARCHAR,active_jobs BIGINT,jobs BIGINT,first_verified_at VARCHAR,last_verified_at VARCHAR)[]'
                    },union_by_name=true)""", [companies_path])
            conteggi = {}
            conteggi["orphan_jobs"] = con.execute(
                "SELECT count(*) FROM jobs j LEFT JOIN companies c "
                "ON c.ats=j.ats AND c.company_slug=j.company_slug "
                "WHERE c.company_slug IS NULL").fetchone()[0]
            conteggi["companies_without_jobs"] = con.execute(
                "SELECT count(*) FROM companies c LEFT JOIN jobs j "
                "ON c.ats=j.ats AND c.company_slug=j.company_slug WHERE j.id IS NULL").fetchone()[0]
            job_tec = ("SELECT DISTINCT ats,company_slug,trim(t) technology "
                       "FROM jobs,UNNEST(technologies) u(t) WHERE trim(t)<>''")
            co_tec = ("SELECT DISTINCT ats,company_slug,trim(t.technology) technology "
                      "FROM companies,UNNEST(technologies) u(t) WHERE trim(t.technology)<>''")
            conteggi["missing_tech_pairs"] = con.execute(
                f"WITH j AS ({job_tec}), c AS ({co_tec}) "
                "SELECT count(*) FROM j LEFT JOIN c USING(ats,company_slug,technology) "
                "WHERE c.technology IS NULL").fetchone()[0]
            conteggi["ghost_tech_pairs"] = con.execute(
                f"WITH j AS ({job_tec}), c AS ({co_tec}) "
                "SELECT count(*) FROM c LEFT JOIN j USING(ats,company_slug,technology) "
                "WHERE j.technology IS NULL").fetchone()[0]
            conteggi["company_count_mismatch"] = con.execute(
                "WITH n AS (SELECT ats,company_slug,count(*) n FROM jobs GROUP BY 1,2) "
                "SELECT count(*) FROM companies c LEFT JOIN n "
                "ON c.ats=n.ats AND c.company_slug=n.company_slug "
                "WHERE coalesce(n.n,0)<>c.active_jobs").fetchone()[0]
            actual = ("SELECT ats,company_slug,trim(t) technology,count(distinct id) n "
                      "FROM jobs,UNNEST(technologies) u(t) WHERE trim(t)<>'' GROUP BY 1,2,3")
            declared = ("SELECT ats,company_slug,trim(t.technology) technology,t.active_jobs n "
                        "FROM companies,UNNEST(technologies) u(t) WHERE trim(t.technology)<>''")
            conteggi["tech_count_mismatch"] = con.execute(
                f"WITH a AS ({actual}), d AS ({declared}) "
                "SELECT count(*) FROM d LEFT JOIN a USING(ats,company_slug,technology) "
                "WHERE coalesce(a.n,0)<>d.n").fetchone()[0]
            if campione:
                conteggi["sample_cardinality_mismatch"] = con.execute(
                    "SELECT abs((SELECT count(distinct(ats,company_slug)) FROM jobs) "
                    "- (SELECT count(*) FROM companies))").fetchone()[0]
            if any(conteggi.values()):
                raise InvariantError(dict(conteggi))
            return {k: v for k, v in conteggi.items() if v}
        finally:
            con.close()


def valida_job_sample(r: dict, n: int = 0) -> None:
    for field in ("id", "title", "company", "company_slug", "country", "description", "url", "posted_at"):
        value = r.get(field)
        if value is None or not str(value).strip() or str(value).strip().lower() in PLACEHOLDER:
            raise InvariantError(f"sample job {n} campo {field} mancante/placeholder")
    for field in ("title", "company", "description"):
        if HTML_RX.search(r[field]) or MOJIBAKE_RX.search(r[field]) or contiene_pii(r[field]):
            raise InvariantError(f"sample job {n} campo {field} non pulito")


def valida_company_sample(c: dict, n: int = 0) -> None:
    for field in ("company", "company_slug", "country"):
        value = c.get(field)
        if value is None or not str(value).strip() or str(value).strip().lower() in PLACEHOLDER:
            raise InvariantError(f"sample company {n} campo {field} mancante/placeholder")
    if not isinstance(c.get("active_jobs"), int) or not 0 < c["active_jobs"] <= 500:
        raise InvariantError(f"sample company {n} active_jobs non realistico")
    if HTML_RX.search(c["company"]) or contiene_pii(c["company"]):
        raise InvariantError(f"sample company {n} non pulita")
    if c.get("description") and (HTML_RX.search(c["description"]) or contiene_pii(c["description"])):
        raise InvariantError(f"sample company {n} description non pulita")


def _digest(r: dict) -> str:
    return hashlib.md5(str(r.get("id", "")).encode()).hexdigest()


def _regione(country: str) -> str:
    if country in EU:
        return "EU"
    if country == "GB":
        return "UK"
    if country == "US":
        return "US"
    return "GLOBAL"


def stratifica(righe, totale: int = 500) -> list[dict]:
    candidati = {}
    conteggi = collections.Counter()
    per_ats = collections.Counter()
    aziende_viste = set()
    for r in righe:
        try:
            valida_job_sample(r)
        except InvariantError:
            continue
        chiave_azienda = (r["ats"], r["company_slug"])
        if chiave_azienda in aziende_viste:
            continue
        aziende_viste.add(chiave_azienda)
        strato = (r["ats"], r["country"])
        conteggi[strato] += 1
        per_ats[r["ats"]] += 1
        digest = _digest(r)
        bucket = candidati.setdefault(strato, [])
        if len(bucket) < 10:
            bucket.append((digest, r))
            bucket.sort(key=lambda x: x[0])
        elif digest < bucket[-1][0]:
            bucket[-1] = (digest, r)
            bucket.sort(key=lambda x: x[0])
    if len(conteggi) < 2:
        raise InvariantError("sample: strati insufficienti")
    alloc = collections.Counter()
    ats_n = collections.Counter()
    country_n = collections.Counter()
    region_n = collections.Counter()
    scelti = []
    ats_cap = max(1, int(totale * 0.15))
    country_cap = max(1, int(totale * 0.20))
    region_cap = max(1, int(totale * 0.40))
    for regione in ("EU", "UK", "US", "GLOBAL"):
        opzioni = [s for s in conteggi if _regione(s[1]) == regione]
        if not opzioni:
            continue
        strato = sorted(opzioni, key=lambda s: (-conteggi[s], s))[0]
        alloc[strato] += 1
        ats_n[strato[0]] += 1
        country_n[strato[1]] += 1
        region_n[regione] += 1
        scelti.append(strato)
    while len(scelti) < min(totale, sum(conteggi.values())):
        ammissibili = [s for s in conteggi if alloc[s] < conteggi[s]
                       and ats_n[s[0]] < ats_cap and country_n[s[1]] < country_cap
                       and region_n[_regione(s[1])] < region_cap]
        if not ammissibili:
            break
        strato = sorted(ammissibili, key=lambda s: (conteggi[s] / (alloc[s] + 1), s), reverse=True)[0]
        alloc[strato] += 1
        ats_n[strato[0]] += 1
        country_n[strato[1]] += 1
        region_n[_regione(strato[1])] += 1
        scelti.append(strato)
    risultato = []
    for strato, n in alloc.items():
        risultato.extend(r for _, r in candidati[strato][:n])
    if len(risultato) < totale:
        raise InvariantError(f"sample: solo {len(risultato)} righe valide su {totale}")
    risultato.sort(key=lambda r: (r["ats"], r["country"], str(r["id"])))
    return risultato[:totale]


def pubblica_generazione(staging: str, destinazione: str, manifest: dict, jobs_name: str,
                         companies_name: str, manifest_name: str) -> dict:
    jobs_path = os.path.join(staging, jobs_name)
    companies_path = os.path.join(staging, companies_name)
    invarianti = verifica_export(jobs_path, companies_path,
                                 campione=manifest_name == "manifest-campione.json")
    os.makedirs(destinazione, exist_ok=True)
    spostati = []
    try:
        for nome in (jobs_name, companies_name):
            sorgente = os.path.join(staging, nome)
            if not os.path.isfile(sorgente) or os.path.getsize(sorgente) == 0:
                raise InvariantError(f"file non pubblicabile: {nome}")
            destinazione_file = os.path.join(destinazione, nome)
            os.replace(sorgente, destinazione_file)
            spostati.append(destinazione_file)
        pronto = dict(manifest)
        pronto.update({"state": "ready", "files": {"jobs": jobs_name, "companies": companies_name},
                       "invariants": invarianti})
        tmp_manifest = os.path.join(destinazione, manifest_name + ".tmp")
        with open(tmp_manifest, "w", encoding="utf-8") as f:
            json.dump(pronto, f, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp_manifest, os.path.join(destinazione, manifest_name))
    except Exception:
        for percorso in spostati:
            try:
                os.remove(percorso)
            except OSError:
                pass
        raise
    for residuo in Path(staging).glob("*"):
        residuo.unlink()
    Path(staging).rmdir()
    return pronto
