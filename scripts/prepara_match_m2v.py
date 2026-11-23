#!/usr/bin/env python3
"""Prepara una tranche decontaminata di coppie per il training Model2Vec."""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from metriche_match_v2 import carica_banco, carica_banco_v3
from nivult.matching.llm import offerta_per_prompt, profilo_come_testo
from nivult.matching.micro_ruoli import RuoliMicro
from nivult.matching.retrieval import lingue_mancanti, tokenizza

PROFILI = [
    ("hr-generalist-mid", "HR Generalist", "2-5",
     ["employee relations", "HRIS", "performance management", "recruiting", "labour law", "payroll"],
     ["Italian", "English"], "Cerca un ruolo HR generalista in azienda, non in agenzia."),
    ("people-partner-senior", "People Partner", "5-10",
     ["stakeholder management", "employee relations", "change management", "HRIS", "performance management", "coaching"],
     ["German", "English"], "Cerca un ruolo di people partnership in azienda strutturata."),
    ("recruiter-senior-fr", "Talent Acquisition Partner", "5-10",
     ["full-cycle recruiting", "sourcing", "interviewing", "ATS", "employer branding", "stakeholder management"],
     ["French", "English"], "Cerca recruiting interno, non vendita o call center."),
    ("recruiter-junior", "Junior Recruiter", "0-2",
     ["recruiting", "CV screening", "interviewing", "ATS", "sourcing", "communication"],
     ["Italian", "English"], "Cerca un primo ruolo nel recruiting, anche con affiancamento."),
    ("backend-python-senior", "Senior Python Backend Engineer", "5-10",
     ["Python", "API design", "PostgreSQL", "Docker", "cloud", "distributed systems"],
     ["Italian", "English"], "Cerca sviluppo backend di prodotto, non supporto IT."),
    ("backend-java-mid", "Java Backend Developer", "2-5",
     ["Java", "Spring Boot", "SQL", "REST API", "microservices", "unit testing"],
     ["English", "German"], "Cerca sviluppo backend Java e microservizi."),
    ("frontend-mid-fr", "Frontend Developer", "2-5",
     ["JavaScript", "TypeScript", "React", "HTML", "CSS", "accessibility"],
     ["French", "English"], "Cerca sviluppo frontend web, non backend puro."),
    ("ml-scientist-senior", "Senior Machine Learning Scientist", "5-10",
     ["Python", "SQL", "machine learning", "statistics", "experiment design", "MLOps"],
     ["English"], "Cerca modelli ML applicati al prodotto, non solo BI."),
    ("data-analyst-junior", "Junior Data Analyst", "0-2",
     ["SQL", "Excel", "Python", "data visualization", "business intelligence", "statistics"],
     ["Italian", "English"], "Cerca analisi dati e BI, non ricerca ML avanzata."),
    ("data-engineer-senior", "Senior Data Engineer", "5-10",
     ["Python", "SQL", "ETL", "Apache Spark", "Airflow", "cloud", "data modelling"],
     ["German", "English"], "Cerca data engineering, non sviluppo backend applicativo."),
    ("payroll-senior", "HR Payroll Specialist", "5-10",
     ["payroll", "employee administration", "labour law", "social contributions", "HRIS", "Excel"],
     ["Italian", "English"], "Cerca payroll e HR operations, non contabilità generica."),
    ("sales-senior-fr", "Senior B2B Account Manager", "5-10",
     ["B2B sales", "account management", "CRM", "negotiation", "business development", "pipeline management"],
     ["French", "English"], "Cerca vendita B2B e gestione clienti, non recruiting."),
    ("support-mid-de", "Customer Support Specialist", "2-5",
     ["customer service", "ticketing", "CRM", "communication", "problem solving", "order management"],
     ["German", "English"], "Cerca assistenza clienti, non vendita outbound."),
    ("marketing-mid", "Digital Marketing Specialist", "2-5",
     ["SEO", "content marketing", "Google Analytics", "campaign management", "social media", "copywriting"],
     ["Italian", "English"], "Cerca marketing digitale, non vendita diretta."),
    ("product-senior", "Senior Product Manager", "5-10",
     ["product management", "roadmapping", "stakeholder management", "agile", "user research", "analytics"],
     ["English"], "Cerca product management in azienda di prodotto."),
    ("project-mid-fr", "IT Project Manager", "2-5",
     ["project management", "agile", "scrum", "stakeholder management", "budgeting", "risk management"],
     ["French", "English"], "Cerca gestione progetti IT, non sviluppo operativo."),
]
VALIDATION = {"recruiter-junior", "frontend-mid-fr", "payroll-senior", "project-mid-fr"}
PAESI = ("IT", "DE", "FR", "SE")


def _chiave(riga: dict) -> tuple[str, str]:
    def pulisci(valore) -> str:
        return " ".join(str(valore or "").lower().split())
    return pulisci(riga.get("title")), pulisci(riga.get("organization") or riga.get("company"))


def _offerte(radice: Path) -> tuple[list[dict], list[int], int]:
    vietati = set()
    chiavi = set()
    for banco in (carica_banco(radice), carica_banco_v3(radice)):
        for riga in banco:
            vietati.add(riga["job_id"])
            chiavi.add(_chiave(riga))
    uniche = {}
    corrotte = []
    for n, linea in enumerate((radice / "dati/match-v3/distrattori.jsonl").read_text().splitlines(), 1):
        try:
            riga = json.loads(linea)
        except json.JSONDecodeError:
            corrotte.append(n)
            continue
        if riga["job_id"] in vietati or _chiave(riga) in chiavi:
            continue
        if len(riga.get("ai_requirements_summary") or "") < 100:
            continue
        precedente = uniche.get(riga["job_id"])
        if precedente is None or len(riga["ai_requirements_summary"]) > len(
                precedente["ai_requirements_summary"]):
            uniche[riga["job_id"]] = riga
    return list(uniche.values()), corrotte, len(vietati)


def _profili() -> dict[str, dict]:
    out = {}
    for chiave, ruolo, seniority, competenze, lingue, note in PROFILI:
        profilo = {"ruolo": ruolo, "seniority": seniority, "competenze": competenze,
                   "lingue": lingue, "sedi": ["Italia", "Europa", "remoto"], "note": note}
        out[chiave] = {"profilo": profilo_come_testo(profilo),
                       "wants": f"Ruolo a cui punta: {ruolo}\n{note}",
                       "ruolo": ruolo, "seniority": seniority,
                       "competenze": competenze, "lingue": lingue}
    return out


def _testo(job: dict) -> str:
    return " ".join(str(job.get(k) or "") for k in
                    ("title", "ai_requirements_summary", "ai_core_responsibilities"))


def _split(job: dict) -> str:
    base = str(job.get("organization") or job.get("company") or job["job_id"]).lower()
    digest = hashlib.md5(("m2v|" + base).encode()).hexdigest()
    return "validation" if int(digest[:8], 16) % 10 < 3 else "train"


def _misure(profilo: dict, job: dict, ruoli: RuoliMicro) -> dict:
    testo = _testo(job)
    semantiche = ruoli.feature(profilo["ruolo"], str(job.get("title") or ""))
    skill = set(tokenizza(" ".join(profilo["competenze"])))
    token_job = set(tokenizza(testo))
    copertura = len(skill & token_job) / len(skill) if skill else 0.0
    mancanti = lingue_mancanti(testo, set(profilo["lingue"]))
    senior = profilo["seniority"] in {"5-10", "10+"}
    livello = str(job.get("ai_experience_level") or "").lower()
    return {"plausibile": semantiche[0] * .55 + semantiche[1] * .25 + copertura * .2,
            "lingua": bool(mancanti and semantiche[0] > .08),
            "seniority": bool((senior and livello in {"intern", "junior"})
                              or (not senior and livello in {"senior", "lead", "head"})),
            "vicino": .04 < semantiche[0] < .45 and copertura > 0,
            "score": semantiche[0] + copertura}


def _stratificato(candidati: list[dict], n: int, tipo: str, scelti: list[dict]) -> None:
    gruppi = defaultdict(list)
    for job, misure in candidati:
        gruppi[job.get("country") or "?"].append((job, misure))
    for gruppo in gruppi.values():
        gruppo.sort(key=lambda x: (-x[1]["score"], x[0]["job_id"]))
    while len(scelti) < n and any(gruppi.values()):
        for paese in (*PAESI, "?"):
            if gruppi[paese] and len(scelti) < n:
                job, misure = gruppi[paese].pop(0)
                scelti.append((job, misure, tipo))


def _coppie_profilo(chiave: str, profilo: dict, jobs: list[dict], ruoli: RuoliMicro,
                    per_profilo: int) -> list[dict]:
    candidati = [(job, _misure(profilo, job, ruoli)) for job in jobs]
    scelti = []
    plausibili = sorted(candidati, key=lambda x: (-x[1]["plausibile"], x[0]["job_id"]))
    _stratificato(plausibili, per_profilo * 2 // 5, "plausibile", scelti)
    usati = {job["job_id"] for job, _, _ in scelti}
    hard = [(job, misura) for job, misura in candidati if job["job_id"] not in usati]
    hard.sort(key=lambda x: (not x[1]["lingua"], not x[1]["seniority"],
                             not x[1]["vicino"], -x[1]["score"], x[0]["job_id"]))
    _stratificato(hard, per_profilo * 4 // 5, "hard-negative", scelti)
    usati = {job["job_id"] for job, _, _ in scelti}
    casuali = [(job, misura) for job, misura in candidati if job["job_id"] not in usati]
    random.Random(42).shuffle(casuali)
    _stratificato(casuali, per_profilo, "casuale", scelti)
    if len(scelti) != per_profilo:
        raise RuntimeError(f"coppie insufficienti per {chiave}: {len(scelti)}/{per_profilo}")
    split = "validation" if chiave in VALIDATION else "train"
    out = []
    for job, _, selezione in scelti:
        offerta = dict(job)
        out.append({"pair_id": f"{chiave}:{job['job_id']}", "profile_key": chiave,
                    "profilo": profilo["profilo"], "job": offerta,
                    "offerta": offerta_per_prompt(offerta, profilo["wants"]),
                    "wants": profilo["wants"],
                    "split": split, "selezione": selezione})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / ".release-staging/m2v-pool.jsonl")
    ap.add_argument("--per-profilo", type=int, default=250)
    args = ap.parse_args()
    offerte, corrotte, escluse = _offerte(ROOT)
    ruoli = RuoliMicro.da_esco(ROOT / "dati/esco/occupazioni.jsonl")
    per_split = defaultdict(list)
    for job in offerte:
        per_split[_split(job)].append(job)
    pool = []
    for chiave, profilo in _profili().items():
        split = "validation" if chiave in VALIDATION else "train"
        pool.extend(_coppie_profilo(chiave, profilo, per_split[split], ruoli,
                                    args.per_profilo))
    if len({r["pair_id"] for r in pool}) != len(pool):
        raise RuntimeError("pair_id duplicati")
    aziende = defaultdict(set)
    for r in pool:
        aziende[(r["job"].get("organization") or r["job"]["job_id"]).lower()].add(r["split"])
    if any(len(split) > 1 for split in aziende.values()):
        raise RuntimeError("azienda condivisa fra train e validation")
    for n, riga in enumerate(pool):
        riga["case_number"] = n
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in pool))
    meta = {"coppie": len(pool), "offerte_uniche": len(offerte), "righe_corrotte": corrotte,
            "offerte_escluse": escluse, "split": dict(Counter(r["split"] for r in pool)),
            "selezione": dict(Counter(r["selezione"] for r in pool)),
            "profili": dict(Counter(r["profile_key"] for r in pool)),
            "paesi": dict(Counter(r["job"].get("country") for r in pool))}
    args.out.with_name("m2v-pool-meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
