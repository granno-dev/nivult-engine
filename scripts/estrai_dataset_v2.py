#!/usr/bin/env python3
"""Il dataset v2: etichette umane e dichiarate, non solo GLM.

Ogni riga porta, per ogni campo, l'etichetta E la sua provenienza:

  family          codice ufficiale (ROME / SSYK / ISCO, mappato in
                  nivult.ats.tassonomie) > accordo GLM+v1 (dall'audit,
                  confidenza >= 0.75) > «none» per i titoli che non sono
                  annunci; le righe in disaccordo o incerte NON entrano:
                  vanno in da_giudicare.jsonl per il giudice esterno.
  employment_type dichiarato dal datore nei campi strutturati dell'ATS
                  (typeContrat, employment_type_code, typeOfEmployment…)
  seniority       dichiarato (experienceLibelle in anni, experienceLevel,
                  experience_code, experience_required)
  remote          dichiarato (workplaceType, isRemote, remote/hybrid,
                  workplace, workplace_model)

Per i campi dichiarati si annota anche `menzione`: se il testo dell'annuncio
nomina il valore (es. «CDI», «senior», «remote»). Dove NON lo nomina, la riga
e' per costruzione un esempio di STIMA: il modello deve ricavare il campo da
titolo, ruolo, azienda e contesto, e siccome la verita' e' dichiarata dal
datore, la precisione della stima si misura esattamente.

    ATS_DATABASE_URL=... python scripts/estrai_dataset_v2.py --out /opt/nivult/v2 [--audit audit-v1.jsonl] [--limite N]

Escluse come in v1: iCIMS per intero (testo di un altro annuncio) e le
chimere delle piattaforme con id per tenant. Deduplica per titolo+azienda+
luogo e per titolo+testo. Le aziende dell'esame a mano restano fuori dal
train, per intero.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import os
import re
import sys

import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from nivult.ats.tassonomie import famiglia_da_isco, famiglia_da_rome, famiglia_da_ssyk  # noqa: E402
from nivult.ats.dichiarati import contratto, remoto, seniority  # noqa: E402
from prompt_v2 import menziona  # noqa: E402

QUOTA_ESAME_AZIENDE = 0.06
_TAG = re.compile(r"<[^>]+>")


def pulisci(t: str | None, n: int = 1200) -> str:
    import html
    t = t or ""
    s = t.lstrip()
    if s[:1] in "{[":
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                t = str(d.get("text") or d.get("description") or d.get("descriptionPlain") or "")
                if not t:
                    t = " ".join(str(v) for v in d.values() if isinstance(v, str))
            elif isinstance(d, list):
                t = " ".join(str(x) for x in d if isinstance(x, str))
        except ValueError:
            pass
    t = html.unescape(html.unescape(t))
    t = _TAG.sub(" ", t).replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()[:n]


# ── titoli che non sono annunci: la classe «none» ───────────────────
RX_NONE = re.compile(
    r"^\s*(?:general|spontaneous|open|unsolicited|internal|speculative)\s+application|candidatura\s+spontanea|"
    r"candidature\s+spontan|initiativbewerbung|autocandidatura|talent\s+(?:pool|community|network)|"
    r"join\s+our\s+talent|future\s+opportunit|keep\s+in\s+touch|expression\s+of\s+interest|"
    r"^\s*(?:test|prova|dummy|sample)\b|^\s*application\s*$|^\s*apply\s*$|^\s*careers?\s*$", re.I)


def chiave_dup(titolo: str, azienda: str, luogo: str) -> str:
    return hashlib.sha1(f"{(titolo or '').lower().strip()}|{azienda}|{(luogo or '').lower().strip()}".encode()).hexdigest()[:16]


def chiave_testo(titolo: str, testo: str) -> str:
    return hashlib.sha1(f"{(titolo or '').lower().strip()}|{(testo or '')[:300].lower()}".encode()).hexdigest()[:16]


def lato_azienda(azienda: str) -> str:
    h = int(hashlib.sha1(azienda.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
    return "esame" if h < QUOTA_ESAME_AZIENDE else "train"


RAW_CAMPI = ("romeCode", "typeContrat", "dureeTravailLibelle", "experienceLibelle", "occupation_group",
             "employment_type", "working_hours_type", "workplace_model", "experience_required", "extent",
             "engagementtype", "jobCategoriesCodes", "experienceLevel", "typeOfEmployment", "location",
             "employment_type_code", "experience_code", "remote", "hybrid", "workplace", "experience",
             "workplaceType", "isRemote", "employmentType")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/opt/nivult/v2")
    ap.add_argument("--audit", default=None, help="audit-v1.jsonl: accordo GLM+v1 per la famiglia")
    ap.add_argument("--golden-mano", default=None, help="dataset-golden-v1.jsonl.gz: le sue aziende restano fuori dal train")
    ap.add_argument("--limite", type=int, default=None)
    ap.add_argument("--escludi-piattaforme", default="icims")
    ap.add_argument("--escludi-chimere", default="workday,cornerstone,eploy,traffit,pinpoint,vincere")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    accordo: dict[str, dict] = {}
    if a.audit:
        for l in open(a.audit):
            d = json.loads(l)
            accordo[d["id"]] = d
        print(f"audit: {len(accordo)} righe", flush=True)
    aziende_esame: set[str] = set()
    id_esame: set[str] = set()
    if a.golden_mano:
        for l in gzip.open(a.golden_mano, "rt"):
            g = json.loads(l)
            id_esame.add(g["id"])
            if g.get("azienda"):
                aziende_esame.add(g["azienda"])

    escluse = [p.strip() for p in a.escludi_piattaforme.split(",") if p.strip()]
    chimere = [p.strip() for p in a.escludi_chimere.split(",") if p.strip() and p.strip() not in escluse]
    sql = """
        SELECT j.id::text, j.platform_id, j.slug, j.title, coalesce(j.location, j.city, ''), j.country,
               coalesce((SELECT v FROM unnest(ARRAY[j.raw->>'description', j.raw->>'content', j.raw->>'descriptionHtml', j.raw->>'descriptionPlain', j.raw->>'externalDescription', j.raw->>'jobDescription', j.raw->>'job_description', j.raw->>'Job_Description', j.raw->>'body', j.raw->>'content_html', j.raw->>'description_html', j.raw->>'descriptionBody', j.raw->>'text', j.raw->'_jobposting'->>'description', j.raw->>'ShortDescriptionStr']) v WHERE length(v) >= 80 LIMIT 1), ''),
               x.family, x.model, j.lang, j.languages_required,
               (SELECT jsonb_object_agg(k, j.raw->k) FROM unnest(%s::text[]) k WHERE j.raw ? k) AS campi
          FROM ats_jobs j LEFT JOIN job_classifications x ON x.job_id = j.id
         WHERE j.title IS NOT NULL AND length(j.title) > 2
           -- anche le SCADUTE, se portano un codice ufficiale o un campo dichiarato:
           -- per il dataset l'etichetta umana vale uguale (la prima versione, 08/09,
           -- prendeva solo le attive: 5.278 ROME invece di 66.000)
           AND (j.expired_at IS NULL OR j.platform_id = ANY(%s::text[]))
           AND NOT (j.platform_id = ANY(%s::text[]))
           AND NOT (j.platform_id = ANY(%s::text[]) AND j.url NOT ILIKE '%%' || j.slug || '%%')
    """
    if a.limite:
        sql += f" ORDER BY j.fetched_at DESC LIMIT {int(a.limite)}"
    conn = psycopg.connect(os.environ["ATS_DATABASE_URL"])
    cur = conn.cursor(name="v2")
    cur.itersize = 5000
    con_etichette_umane = ["francetravail", "arbetsformedlingen", "eures", "nav", "smartrecruiters", "recruitee",
                           "workable", "ashby", "personio", "lever"]
    cur.execute(sql, (list(RAW_CAMPI), con_etichette_umane, escluse, chimere))

    st = collections.Counter()
    prov = collections.Counter()
    visti_dup: set[str] = set()
    visti_testo: set[str] = set()
    f_train = gzip.open(os.path.join(a.out, "dataset-train-v2.jsonl.gz"), "wt")
    f_esame = gzip.open(os.path.join(a.out, "dataset-esame-v2.jsonl.gz"), "wt")
    f_giud = open(os.path.join(a.out, "da_giudicare.jsonl"), "w")
    for (jid, pid, slug, tit, loc, ctry, desc, fam_glm, mod_glm, lang, lingue, campi) in cur:
        st["lette"] += 1
        campi = campi or {}
        testo = pulisci(desc)
        if len(testo) < 80 and not campi:
            st["senza_testo"] += 1
            continue
        azienda = f"{pid}/{slug}"
        k1, k2 = chiave_dup(tit, azienda, loc), chiave_testo(tit, testo)
        if k1 in visti_dup or (testo and k2 in visti_testo):
            st["duplicate"] += 1
            continue
        visti_dup.add(k1)
        visti_testo.add(k2)

        # ── famiglia ──
        fam, fam_prov = None, None
        if pid == "francetravail":
            fam, fam_prov = famiglia_da_rome(campi.get("romeCode")), "rome"
        elif pid == "arbetsformedlingen":
            fam, fam_prov = famiglia_da_ssyk((campi.get("occupation_group") or {}).get("legacy_ams_taxonomy_id")), "ssyk"
        elif pid == "eures":
            codici = campi.get("jobCategoriesCodes")
            if isinstance(codici, str):
                try:
                    codici = json.loads(codici)
                except ValueError:
                    codici = []
            isco = next((c for c in (codici or []) if "/isco/" in str(c)), None)
            fam, fam_prov = famiglia_da_isco(isco), "isco"
        if fam is None and RX_NONE.search(tit or ""):
            fam, fam_prov = "none", "regola"
        if fam is None and fam_glm and mod_glm and (mod_glm.startswith("glm") or mod_glm == "nivult-v1"):
            au = accordo.get(jid)
            if au is None:
                fam, fam_prov = (fam_glm, "glm") if mod_glm.startswith("glm") else (None, None)
                if fam_prov == "glm":
                    fam_prov = "glm_senza_audit"
            elif au.get("accordo"):
                fam, fam_prov = fam_glm, "glm+v1"
            else:
                f_giud.write(json.dumps({"id": jid, "title": tit, "location": loc, "azienda": azienda,
                                         "text": testo[:600], "glm": fam_glm, "v1": au.get("v1"),
                                         "conf_v1": au.get("conf")}, ensure_ascii=False) + "\n")
                st["da_giudicare"] += 1
        if fam_prov:
            prov[f"family:{fam_prov}"] += 1

        # ── campi dichiarati ──
        riga = {"id": jid, "title": tit, "location": loc, "country": ctry, "text": testo, "lang": lang,
                "azienda": azienda, "family": fam, "family_prov": fam_prov,
                "languages_required": list(lingue) if lingue else None}
        for campo, fn in (("employment_type", contratto), ("seniority", seniority), ("remote", remoto)):
            v = fn(pid, campi) if campi else None
            riga[campo] = v
            riga[f"{campo}_prov"] = "dichiarato" if v else None
            riga[f"{campo}_menzione"] = menziona(campo, v, f"{tit} {testo}") if v else None
            if v:
                prov[f"{campo}:dichiarato"] += 1
                prov[f"{campo}:stima" if not riga[f"{campo}_menzione"] else f"{campo}:estrazione"] += 1
        if not fam and not any(riga[c] for c in ("employment_type", "seniority", "remote")):
            st["senza_etichette"] += 1
            continue
        lato = "esame" if (jid in id_esame or azienda in aziende_esame or lato_azienda(azienda) == "esame") else "train"
        (f_esame if lato == "esame" else f_train).write(json.dumps(riga, ensure_ascii=False) + "\n")
        st[lato] += 1
        if st["lette"] % 50000 == 0:
            print(f"  {st['lette']} lette: {dict(st)}", flush=True)
    for f in (f_train, f_esame, f_giud):
        f.close()
    print("\nRIGHE:", dict(st))
    print("PROVENIENZE:", json.dumps(dict(sorted(prov.items())), indent=1))
    json.dump({"righe": dict(st), "provenienze": dict(prov)}, open(os.path.join(a.out, "rapporto-v2.json"), "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
