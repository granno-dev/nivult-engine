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
               "workable", "ashby", "personio", "lever",
               # aggiunte il 10/09/2026 per orario/durata: dichiarano il dato
               # e non lo leggevamo. iCIMS (309.422 offerte) NON e' qui: il suo
               # campo mescola anche la seniority («Experienced», «RN») e valori
               # sanitari americani (PRN, Per Diem), va mappato con calma.
               "bamboohr", "breezy", "zohorecruit", "pinpoint",
               "recruiterbox", "vincere", "jsonld")
RAW_CAMPI = ("typeContrat", "dureeTravailLibelle", "experienceLibelle", "employment_type",
             "working_hours_type", "workplace_model", "experience_required", "extent", "engagementtype",
             "experienceLevel", "typeOfEmployment", "location", "employment_type_code", "experience_code",
             "remote", "hybrid", "workplace", "experience", "workplaceType", "isRemote", "employmentType",
             "employmentStatusLabel", "type", "Job_Type", "positionType")


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
        # «Contract» negli ATS anglosassoni e' AMBIGUO: a volte un contractor
        # (autonomo), a volte un tempo determinato. Misurato il 09/09/2026 sul
        # dataset v2: negli annunci che dicono CDD/temporary il dato dichiarato
        # diceva temporary 193 volte e contract 197. Un'etichetta che non
        # distingue non insegna niente: «Contract» resta NULL e lo decide il
        # modello dal testo. `contract` vale solo dove la fonte dice autonomo
        # (France Travail LIB/FRA/CCE/REP, Recruitee e Personio «freelance»).
        return {"Full-time": "full_time", "Part-time": "part_time",
                "Temporary": "temporary", "Intern": "internship", "Internship": "internship",
                "Apprenticeship": "apprenticeship"}.get((r.get("typeOfEmployment") or {}).get("label"))
    if pid == "recruitee":
        c = r.get("employment_type_code") or ""
        return {"fulltime_permanent": "full_time", "fulltime": "full_time", "fulltime_fixed_term": "temporary",
                "parttime_permanent": "part_time", "parttime_fixed_term": "part_time", "parttime": "part_time",
                "freelance": "contract", "internship": "internship",
                "apprenticeship": "apprenticeship", "traineeship": "internship"}.get(c)
    if pid == "workable":
        return {"Full-time": "full_time", "Part-time": "part_time",
                "Temporary": "temporary", "Internship": "internship"}.get(r.get("employment_type"))
    if pid == "ashby":
        return {"FullTime": "full_time", "PartTime": "part_time",
                "Temporary": "temporary", "Intern": "internship"}.get(r.get("employmentType"))
    if pid == "personio":
        return {"permanent": "full_time", "intern": "internship", "temporary": "temporary",
                "trainee": "internship", "freelance": "contract", "working_student": "part_time",
                "fixed_term": "temporary"}.get(r.get("employmentType"))
    return None


# ── ORARIO e DURATA: due domande diverse, due colonne ───────────────
# `employment_type` ne mescolava due: quante ore si lavora (full/part time)
# e che natura ha il rapporto (indeterminato, determinato, stage...). Da
# `full_time` non si ricava se il posto e' stabile, e «permanent» non
# esisteva fra i valori: il filtro «e' a tempo indeterminato?», la prima
# domanda di chi cerca lavoro, non aveva risposta.
#
# Peggio: il dato ci ARRIVAVA e lo buttavamo. «CDI» (contrat a duree
# indeterminee) diventava `full_time`, che parla di ore e non di durata;
# Recruitee dichiara `fulltime_permanent` — ENTRAMBI gli assi, espliciti —
# e ne tenevamo meta'; Personio dichiara `permanent` e lo salvavamo come
# `full_time`, sbagliando su tutte e due.
#
# Regola: si scrive SOLO dove la fonte lo dice esplicitamente. Valori
# ambigui (l'inglese «Contract», lo svedese «Vanlig anställning» che vuol
# dire «impiego normale» e non dichiara la durata) restano NULL e li
# decidera' il lettore del testo. Valori misurati sul database il 10/09/2026.

def _jsonld_tipo(v) -> str | None:
    """schema.org mette `employmentType` a volte come stringa, a volte come
    lista (`["FULL_TIME"]`) e a volte con piu' valori insieme
    (`["FULL_TIME", "PART_TIME"]`): con due valori non si sceglie, si tace."""
    if isinstance(v, str):
        v = v.strip()
        if v.startswith("["):
            import json as _json
            try:
                v = _json.loads(v)
            except Exception:                        # noqa: BLE001
                return None
        else:
            return v.upper().replace(" ", "_").replace("-", "_")
    if isinstance(v, list):
        vals = {str(x).upper().replace(" ", "_").replace("-", "_") for x in v if x}
        return vals.pop() if len(vals) == 1 else None
    return None


def orario(pid: str, r: dict) -> str | None:
    """full_time / part_time: quante ore, e nient'altro."""
    if pid == "francetravail":
        d = (r.get("dureeTravailLibelle") or "").lower()
        if "temps partiel" in d:
            return "part_time"
        ore = re.search(r"(\d{1,2})h/semaine", d)
        # in Francia la settimana piena e' 35 ore: sotto le 30 e' parziale,
        # in mezzo non si indovina
        if ore:
            n = int(ore.group(1))
            return "full_time" if n >= 35 else ("part_time" if n < 30 else None)
        return None
    if pid == "arbetsformedlingen":
        return {"Heltid": "full_time", "Deltid": "part_time"}.get(
            (r.get("working_hours_type") or {}).get("label"))
    if pid == "nav":
        return {"Heltid": "full_time", "Deltid": "part_time"}.get(r.get("extent"))
    if pid == "smartrecruiters":
        return {"Full-time": "full_time", "Part-time": "part_time"}.get(
            (r.get("typeOfEmployment") or {}).get("label"))
    if pid == "recruitee":
        c = r.get("employment_type_code") or ""
        if c.startswith("fulltime"):
            return "full_time"
        if c.startswith("parttime"):
            return "part_time"
        return None
    if pid == "workable":
        return {"Full-time": "full_time", "Part-time": "part_time"}.get(r.get("employment_type"))
    if pid == "ashby":
        return {"FullTime": "full_time", "PartTime": "part_time"}.get(r.get("employmentType"))
    if pid == "personio":
        # «working_student» in Germania e' per definizione a ore ridotte
        return "part_time" if r.get("employmentType") == "working_student" else None
    if pid == "bamboohr":
        e = (r.get("employmentStatusLabel") or "").lower().replace("-", " ")
        if "full time" in e:
            return "full_time"
        if "part time" in e:
            return "part_time"
        return None
    if pid == "breezy":
        # il nome e' tradotto («Vollzeit», «Temps plein»): si usa l'id, che
        # non cambia lingua
        return {"fullTime": "full_time", "partTime": "part_time"}.get(
            (r.get("type") or {}).get("id") if isinstance(r.get("type"), dict) else None)
    if pid == "zohorecruit":
        j = (r.get("Job_Type") or "").lower()
        if j in ("full time", "tiempo completo", "vollzeit", "temps plein",
                 "voltijd", "tempo pieno", "heltid"):
            return "full_time"
        if j in ("part time", "tiempo parcial", "teilzeit", "temps partiel",
                 "deeltijd", "tempo parziale", "deltid"):
            return "part_time"
        return None
    if pid == "pinpoint":
        e = r.get("employment_type") or ""
        if e.endswith("full_time"):
            return "full_time"
        if e.endswith("part_time"):
            return "part_time"
        return None
    if pid == "recruiterbox":
        return {"full_time": "full_time", "part_time": "part_time"}.get(r.get("positionType"))
    if pid == "jsonld":
        v = _jsonld_tipo(r.get("employmentType"))
        return {"FULL_TIME": "full_time", "PART_TIME": "part_time"}.get(v)
    return None


def durata(pid: str, r: dict) -> str | None:
    """permanent / fixed_term / internship / apprenticeship / freelance:
    che natura ha il rapporto, e nient'altro."""
    if pid == "francetravail":
        return {"CDI": "permanent", "DIN": "permanent",
                "CDD": "fixed_term", "MIS": "fixed_term", "SAI": "fixed_term",
                "DDI": "fixed_term", "TTI": "fixed_term",
                "LIB": "freelance", "FRA": "freelance",
                "CCE": "freelance", "REP": "freelance",
                }.get((r.get("typeContrat") or "").upper())
    if pid == "arbetsformedlingen":
        lab = (r.get("employment_type") or {}).get("label") or ""
        if "Tillsvidare" in lab:
            return "permanent"
        # «Vanlig anställning» = «impiego normale»: NON dichiara la durata,
        # e sono 5.525 offerte. Restano senza, che e' la verita'.
        if any(k in lab for k in ("Tidsbegränsad", "Vikariat", "Säsong",
                                  "Behovs", "Sommarjobb", "feriejobb")):
            return "fixed_term"
        return None
    if pid == "nav":
        return {"Fast": "permanent",
                "Vikariat": "fixed_term", "Engasjement": "fixed_term",
                "Sesong": "fixed_term", "Prosjekt": "fixed_term",
                "Åremål": "fixed_term",
                "Lærling": "apprenticeship", "Trainee": "internship",
                "Frilanser": "freelance",
                "Selvstendig næringsdrivende": "freelance",
                }.get(r.get("engagementtype"))
    if pid == "smartrecruiters":
        # «Contract» resta NULL: negli ATS anglosassoni vale sia autonomo
        # sia tempo determinato (misurato: 197 contro 193 sul dataset v2)
        return {"Intern": "internship", "Internship": "internship",
                "Temporary": "fixed_term", "Apprenticeship": "apprenticeship",
                }.get((r.get("typeOfEmployment") or {}).get("label"))
    if pid == "recruitee":
        c = r.get("employment_type_code") or ""
        if c.endswith("_permanent"):
            return "permanent"
        if c.endswith("_fixed_term"):
            return "fixed_term"
        return {"freelance": "freelance", "internship": "internship",
                "traineeship": "internship", "apprenticeship": "apprenticeship",
                "temporary": "fixed_term"}.get(c)
    if pid == "workable":
        return {"Temporary": "fixed_term", "Internship": "internship"}.get(r.get("employment_type"))
    if pid == "ashby":
        return {"Temporary": "fixed_term", "Intern": "internship"}.get(r.get("employmentType"))
    if pid == "personio":
        return {"permanent": "permanent", "fixed_term": "fixed_term",
                "temporary": "fixed_term", "intern": "internship",
                "trainee": "internship", "freelance": "freelance",
                }.get(r.get("employmentType"))
    if pid == "bamboohr":
        e = (r.get("employmentStatusLabel") or "").lower()
        if "permanent" in e:
            return "permanent"
        if "seasonal" in e or "temporary" in e:
            return "fixed_term"
        if "intern" in e:
            return "internship"
        if "contractor" in e:
            return "freelance"
        return None
    if pid == "breezy":
        # «contract» resta NULL: stessa ambiguita' dell'inglese
        return {"temporary": "fixed_term"}.get(
            (r.get("type") or {}).get("id") if isinstance(r.get("type"), dict) else None)
    if pid == "zohorecruit":
        return {"permanent": "permanent", "festanstellung": "permanent",
                "temporary": "fixed_term", "internship": "internship",
                "stage": "internship", "apprentissage": "apprenticeship",
                "alternance": "apprenticeship", "freelance": "freelance",
                }.get((r.get("Job_Type") or "").lower())
    if pid == "pinpoint":
        e = r.get("employment_type") or ""
        if e.startswith("permanent"):
            return "permanent"
        if e.startswith("fixed_term") or e == "temporary":
            return "fixed_term"
        return {"internship": "internship", "apprenticeship": "apprenticeship",
                "freelance": "freelance"}.get(e)
    if pid == "vincere":
        # vocabolario delle agenzie: «Contract» e «Temp-To-Perm» restano NULL
        return {"permanent": "permanent", "festanstellung": "permanent",
                "permanent / fixed term (perm)": "permanent",
                "temporary": "fixed_term", "locum": "fixed_term",
                "interim / project consulting": "fixed_term",
                }.get((r.get("type") or "").lower() if isinstance(r.get("type"), str) else None)
    if pid == "jsonld":
        return {"TEMPORARY": "fixed_term", "INTERN": "internship",
                "CONTRACTOR": "freelance", "PER_DIEM": "fixed_term",
                }.get(_jsonld_tipo(r.get("employmentType")))
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
    st = {"viste": 0, "seniority": 0, "employment_type": 0, "remote": 0,
          "orario": 0, "durata": 0}
    t0 = time.time()
    with psycopg.connect(dsn) as conn:
        while st["viste"] < limite:
            righe = conn.execute("""
                SELECT j.id, j.platform_id,
                       (SELECT jsonb_object_agg(k, j.raw->k) FROM unnest(%s::text[]) k WHERE j.raw ? k),
                       j.seniority, j.employment_type, j.remote,
                       j.orario, j.durata
                  FROM ats_jobs j
                 WHERE j.expired_at IS NULL AND j.dichiarati_at IS NULL AND j.platform_id = ANY(%s::text[])
                 ORDER BY j.fetched_at DESC LIMIT 5000""", (list(RAW_CAMPI), list(PIATTAFORME))).fetchall()
            if not righe:
                break
            sen, con, rem, ids = [], [], [], []
            ora, dur = [], []
            for jid, pid, campi, s0, c0, r0, o0, d0 in righe:
                ids.append(jid)
                campi = campi or {}
                if s0 is None and (v := seniority(pid, campi)):
                    sen.append((jid, v))
                if c0 is None and (v := contratto(pid, campi)):
                    con.append((jid, v))
                if r0 is None and (v := remoto(pid, campi)):
                    rem.append((jid, v))
                if o0 is None and (v := orario(pid, campi)):
                    ora.append((jid, v))
                if d0 is None and (v := durata(pid, campi)):
                    dur.append((jid, v))
            for col, rows in (("seniority", sen), ("employment_type", con),
                              ("remote", rem), ("orario", ora), ("durata", dur)):
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
