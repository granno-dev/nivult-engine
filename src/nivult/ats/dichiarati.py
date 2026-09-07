"""I campi che il recruiter ha gia' compilato nell'ATS: contratto, seniority, remoto.

Lever e Ashby dichiarano `workplaceType`, SmartRecruiters `experienceLevel` e
`typeOfEmployment`, Recruitee contratto/esperienza/remoto, Workable, Personio,
France Travail il tipo di contratto e gli anni di esperienza, Arbetsförmedlingen
e NAV orario e tipo. Fino al 07/09/2026 questi campi restavano nel JSON grezzo
e le colonne del prodotto aspettavano un LLM. Qui diventano etichette DIRETTE:
esatte (le ha scelte chi ha scritto l'annuncio), gratis, per ogni offerta.

`applica(dsn)` riempie seniority / employment_type / remote dove sono NULL e
segna `dichiarati_at`; le stesse funzioni le usa `scripts/estrai_dataset_v2.py`
per il dataset (dove la menzione nel testo distingue estrazione da stima).
"""
from __future__ import annotations

import logging
import os
import re
import time

import psycopg

log = logging.getLogger("nivult.ats.dichiarati")

PIATTAFORME = ("francetravail", "arbetsformedlingen", "nav", "smartrecruiters", "recruitee",
               "workable", "ashby", "personio", "lever")
RAW_CAMPI = ("typeContrat", "dureeTravailLibelle", "experienceLibelle", "employment_type",
             "working_hours_type", "workplace_model", "experience_required", "extent", "engagementtype",
             "experienceLevel", "typeOfEmployment", "location", "employment_type_code", "experience_code",
             "remote", "hybrid", "workplace", "experience", "workplaceType", "isRemote", "employmentType")


# ── contratto: dai campi dichiarati al vocabolario di Nivult ─────────
def contratto(pid: str, r: dict) -> str | None:
    if pid == "francetravail":
        tc = (r.get("typeContrat") or "").upper()
        durata = (r.get("dureeTravailLibelle") or "").lower()
        ore = re.search(r"(\d{1,2})h", durata)
        if ore and int(ore.group(1)) < 30 or "temps partiel" in durata:
            return "part_time"
        return {"CDI": "full_time", "CDD": "temporary", "MIS": "temporary", "SAI": "temporary",
                "LIB": "contract", "FRA": "contract", "DIN": "full_time", "DDI": "temporary",
                "CCE": "contract", "REP": "contract", "TTI": "temporary"}.get(tc)
    if pid == "arbetsformedlingen":
        ore = (r.get("working_hours_type") or {}).get("label", "")
        tipo = (r.get("employment_type") or {}).get("label", "")
        if ore == "Deltid":
            return "part_time"
        if any(k in tipo for k in ("Tidsbegränsad", "Vikariat", "Säsong", "Behovs")):
            return "temporary"
        if ore == "Heltid" or "Vanlig" in tipo or "Tillsvidare" in tipo:
            return "full_time"
        return None
    if pid == "nav":
        if r.get("extent") == "Deltid":
            return "part_time"
        t = r.get("engagementtype") or ""
        if t in ("Vikariat", "Engasjement", "Sesong", "Prosjekt"):
            return "temporary"
        if t == "Fast" or r.get("extent") == "Heltid":
            return "full_time"
        return None
    if pid == "smartrecruiters":
        return {"Full-time": "full_time", "Part-time": "part_time", "Contract": "contract",
                "Temporary": "temporary", "Intern": "internship", "Internship": "internship",
                "Apprenticeship": "apprenticeship"}.get((r.get("typeOfEmployment") or {}).get("label"))
    if pid == "recruitee":
        c = r.get("employment_type_code") or ""
        return {"fulltime_permanent": "full_time", "fulltime": "full_time", "fulltime_fixed_term": "temporary",
                "parttime_permanent": "part_time", "parttime_fixed_term": "part_time", "parttime": "part_time",
                "freelance": "contract", "contract": "contract", "internship": "internship",
                "apprenticeship": "apprenticeship", "traineeship": "internship"}.get(c)
    if pid == "workable":
        return {"Full-time": "full_time", "Part-time": "part_time", "Contract": "contract",
                "Temporary": "temporary", "Internship": "internship"}.get(r.get("employment_type"))
    if pid == "ashby":
        return {"FullTime": "full_time", "PartTime": "part_time", "Contract": "contract",
                "Temporary": "temporary", "Intern": "internship"}.get(r.get("employmentType"))
    if pid == "personio":
        return {"permanent": "full_time", "intern": "internship", "temporary": "temporary",
                "trainee": "internship", "freelance": "contract", "working_student": "part_time",
                "fixed_term": "temporary"}.get(r.get("employmentType"))
    return None


# ── seniority dichiarata ────────────────────────────────────────────
def seniority(pid: str, r: dict) -> str | None:
    if pid == "francetravail":
        e = (r.get("experienceLibelle") or "").lower()
        if "débutant" in e or "debutant" in e:
            return "junior"
        m = re.search(r"(\d+)\s*(an|mois)", e)
        if m:
            n = int(m.group(1)) / (12 if m.group(2) == "mois" else 1)
            return "junior" if n < 2 else ("mid" if n < 5 else "senior")
        return None
    if pid == "arbetsformedlingen":
        return "junior" if str(r.get("experience_required")).lower() == "false" else None
    if pid in ("smartrecruiters", "workable"):
        lab = (r.get("experienceLevel") or {}).get("label") if pid == "smartrecruiters" else r.get("experience")
        return {"Entry level": "junior", "Entry Level": "junior", "Internship": "intern", "Associate": "mid",
                "Mid-Senior level": "senior", "Mid-Senior Level": "senior", "Director": "head",
                "Executive": "head"}.get(lab or "")
    if pid == "recruitee":
        return {"entry_level": "junior", "mid_level": "mid", "experienced": "senior", "student_school": "intern",
                "student_college": "intern", "manager": "lead", "senior_manager": "head"}.get(r.get("experience_code") or "")
    return None


# ── remoto dichiarato ───────────────────────────────────────────────
def remoto(pid: str, r: dict) -> str | None:
    if pid == "lever":
        return {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}.get(r.get("workplaceType") or "")
    if pid == "ashby":
        return {"Remote": "remote", "Hybrid": "hybrid", "OnSite": "onsite"}.get(r.get("workplaceType") or "")
    if pid == "recruitee":
        rem, hyb = str(r.get("remote")).lower() == "true", str(r.get("hybrid")).lower() == "true"
        return "hybrid" if hyb else ("remote" if rem else "onsite")
    if pid == "workable":
        return {"remote": "remote", "hybrid": "hybrid", "on_site": "onsite", "onsite": "onsite"}.get(r.get("workplace") or "")
    if pid == "smartrecruiters":
        return "remote" if str((r.get("location") or {}).get("remote")).lower() == "true" else None
    if pid == "arbetsformedlingen":
        lab = (r.get("workplace_model") or {}).get("label", "")
        return "onsite" if "på plats" in lab else ("remote" if "distans" in lab.lower() else None)
    return None




def applica(dsn: str, limite: int = 200_000) -> dict:
    """Riempie le colonne vuote dai campi dichiarati, a lotti, con unnest."""
    st = {"viste": 0, "seniority": 0, "employment_type": 0, "remote": 0}
    t0 = time.time()
    with psycopg.connect(dsn) as conn:
        while st["viste"] < limite:
            righe = conn.execute("""
                SELECT j.id, j.platform_id,
                       (SELECT jsonb_object_agg(k, j.raw->k) FROM unnest(%s::text[]) k WHERE j.raw ? k),
                       j.seniority, j.employment_type, j.remote
                  FROM ats_jobs j
                 WHERE j.expired_at IS NULL AND j.dichiarati_at IS NULL AND j.platform_id = ANY(%s::text[])
                 ORDER BY j.fetched_at DESC LIMIT 5000""", (list(RAW_CAMPI), list(PIATTAFORME))).fetchall()
            if not righe:
                break
            sen, con, rem, ids = [], [], [], []
            for jid, pid, campi, s0, c0, r0 in righe:
                ids.append(jid)
                campi = campi or {}
                if s0 is None and (v := seniority(pid, campi)):
                    sen.append((jid, v))
                if c0 is None and (v := contratto(pid, campi)):
                    con.append((jid, v))
                if r0 is None and (v := remoto(pid, campi)):
                    rem.append((jid, v))
            for col, rows in (("seniority", sen), ("employment_type", con), ("remote", rem)):
                if rows:
                    conn.execute(f"UPDATE ats_jobs j SET {col} = coalesce(j.{col}, v.val) "
                                 f"FROM unnest(%s::uuid[], %s::text[]) AS v(id, val) WHERE j.id = v.id",
                                 ([r[0] for r in rows], [r[1] for r in rows]))
                    st[col] += len(rows)
            conn.execute("UPDATE ats_jobs SET dichiarati_at = now() WHERE id = ANY(%s::uuid[])", (ids,))
            conn.commit()
            st["viste"] += len(righe)
    log.info("dichiarati: %s in %ds", st, time.time() - t0)
    return st


def main(argv=None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.dichiarati")
    ap.add_argument("--limite", type=int, default=200_000)
    a = ap.parse_args(argv)
    dsn = os.environ.get("ATS_DATABASE_URL", "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    print("Dichiarati:", applica(dsn, a.limite))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
