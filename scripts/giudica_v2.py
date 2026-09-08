#!/usr/bin/env python3
"""Il giudice sui casi dubbi, via Ollama (Colab o qualunque GPU).

Input: da_giudicare.jsonl (le righe dove GLM e v1 non concordano o v1 e'
incerto). Il giudice (Qwen3.6-35B-A3B, prompt con le definizioni delle 33
famiglie e le regole di confine: 79,6% sulle 280 a mano, misurato l'08/09)
da' il suo parere. La regola di decisione e' PRUDENTE:

  - giudice = GLM  oppure  giudice = v1   → la famiglia entra (due su tre)
  - tre pareri diversi                    → resta fuori: va in chat (per titolo)

    python scripts/giudica_v2.py --in da_giudicare.jsonl --out giudicati.jsonl \\
        [--url http://127.0.0.1:11434] [--modello qwen3.6:35b-a3b] [--parallele 8]

Ollama si installa con uno script e porta con se' la CUDA giusta: e' la via
che su Colab funziona al primo colpo (vLLM l'08/09 ha rotto l'ambiente
con i conflitti di cuda-python). E' riprendibile: le righe gia' in --out
non si rifanno.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
import os
import sys
import time

import httpx

FAMIGLIE = ['Administrative', 'Agriculture', 'Art & Design', 'Construction', 'Consulting', 'Creative & Media',
            'Customer Service & Support', 'Data & Analytics', 'Education', 'Energy', 'Engineering',
            'Environmental & Sustainability', 'Finance & Accounting', 'Food & Beverage', 'Government & Public Sector',
            'Healthcare', 'Hospitality', 'Human Resources', 'Legal', 'Logistics', 'Management & Leadership',
            'Manufacturing', 'Marketing', 'Retail', 'Sales', 'Science & Research', 'Security & Safety',
            'Social Services', 'Software', 'Sports & Recreation', 'Technology', 'Trades', 'Transportation', 'none']
DEF = {
    "Retail": "vendita al dettaglio in negozio: commesso, addetto vendita, store manager, cassiere",
    "Sales": "vendita B2B/commerciale: account manager, sales representative, business developer, agente",
    "Trades": "mestieri manuali qualificati: elettricista, idraulico, saldatore, meccanico, falegname, tecnico manutentore, pulizie",
    "Manufacturing": "produzione industriale: operaio di linea, addetto macchine, montatore, controllo qualita' di fabbrica",
    "Transportation": "guida e trasporto: autista, corriere, conducente, macchinista, addetto consegne",
    "Logistics": "magazzino e supply chain: magazziniere, carrellista, pianificatore, spedizioni",
    "Construction": "cantiere edile: capocantiere, operaio edile, project manager edile, direttore lavori",
    "Engineering": "ingegneria di progettazione non software: meccanica, elettrica, civile, di processo, R&D hardware",
    "Administrative": "lavoro d'ufficio: segreteria, assistente, impiegato amministrativo, receptionist, office manager",
    "Management & Leadership": "SOLO direzione generale: CEO, direttore generale, country manager, head of division; NON store manager, team lead, project manager",
    "Technology": "IT e infrastruttura: sistemista, help desk, network, cloud/devops, cybersecurity",
    "Software": "sviluppo software: developer, QA/test automation, architetto software, mobile",
    "Data & Analytics": "dati: data analyst, data scientist, BI, data engineer, ML engineer",
    "Healthcare": "sanita': medico, infermiere, OSS, fisioterapista, farmacista, tecnico sanitario",
    "Social Services": "assistenza alla persona: educatore, assistente domiciliare, badante, operatore sociale",
    "Hospitality": "alberghi e ricevimento: receptionist d'hotel, housekeeping, concierge",
    "Food & Beverage": "ristorazione: cuoco, chef, cameriere, barista, responsabile ristorante",
    "Customer Service & Support": "assistenza clienti: call center, customer care, back office clienti",
    "Finance & Accounting": "contabilita' e finanza: contabile, controller, tesoreria, analista finanziario",
    "Human Resources": "risorse umane: recruiter, HR business partner, payroll, formazione",
    "Marketing": "marketing e comunicazione: marketing manager, digital, SEO, brand, social media, PR",
    "Legal": "legale: avvocato, giurista d'impresa, paralegal, compliance",
    "Education": "istruzione e formazione: insegnante, docente, formatore, tutor",
    "Science & Research": "ricerca scientifica: ricercatore, biologo, chimico di laboratorio",
    "Security & Safety": "sicurezza: guardia, vigilanza, HSE/RSPP, safety officer",
    "Consulting": "consulenza verso clienti: consultant, advisory, consulente di direzione/IT",
    "Creative & Media": "media e contenuti: giornalista, videomaker, copywriter, redattore",
    "Art & Design": "design e arti: graphic designer, UX/UI, interior designer, architetto",
    "Energy": "energia: impianti energetici, oil&gas, rinnovabili",
    "Environmental & Sustainability": "ambiente: sostenibilita', rifiuti, ecologia, ESG",
    "Agriculture": "agricoltura, allevamento, agronomo, giardiniere",
    "Government & Public Sector": "pubblica amministrazione e ruoli istituzionali",
    "Sports & Recreation": "sport e tempo libero: istruttore, personal trainer, animatore",
    "none": "NON e' un annuncio: candidatura spontanea, talent pool, pagina di prova",
}
SYS = ("Sei un classificatore di annunci di lavoro. Rispondi SOLO con un JSON {\"family\": ...}.\n"
       "family = la famiglia del RUOLO svolto, non del settore dell'azienda. Definizioni:\n"
       + "\n".join(f"- {k}: {v}" for k, v in DEF.items()) +
       "\nRegole di confine: negozio → Retail (non Sales); operaio di produzione → Manufacturing (non Trades); autista → "
       "Transportation; magazziniere → Logistics; impiegato/assistente → Administrative; un senior o un team lead resta "
       "nella sua famiglia, Management & Leadership e' solo direzione generale.")
SCHEMA = {"type": "object", "properties": {"family": {"type": "string", "enum": FAMIGLIE}}, "required": ["family"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--url", default=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"))
    ap.add_argument("--modello", default="qwen3.6:35b-a3b")
    ap.add_argument("--parallele", type=int, default=8)
    ap.add_argument("--max", type=int, default=None)
    a = ap.parse_args()
    righe = [json.loads(l) for l in open(a.src)]
    if a.max:
        righe = righe[:a.max]
    fatti: dict[str, dict] = {}
    if os.path.exists(a.out):
        for l in open(a.out):
            d = json.loads(l)
            fatti[d["id"]] = d
    da_fare = [r for r in righe if r["id"] not in fatti]
    print(f"{len(righe)} casi, {len(fatti)} gia' fatti, {len(da_fare)} da giudicare con {a.modello}", flush=True)
    cli = httpx.Client(timeout=600)

    def uno(r: dict) -> dict:
        u = (f"Titolo: {r['title']}\nSede: {r.get('location') or ''}\nAzienda: {(r.get('azienda') or '').split('/')[-1]}"
             f"\n\n{(r.get('text') or '')[:2500]}")
        body = {"model": a.modello, "stream": False, "think": False, "format": SCHEMA,
                "options": {"temperature": 0, "num_predict": 40, "num_ctx": 4096},
                "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": u}]}
        fam = None
        for tentativo in range(3):
            try:
                x = cli.post(a.url + "/api/chat", json=body)
                if x.status_code == 200:
                    fam = json.loads(x.json()["message"]["content"]).get("family")
                    break
            except Exception:  # noqa: BLE001
                time.sleep(2 * (tentativo + 1))
        if fam and fam == r.get("glm"):
            dec, fonte = fam, "giudice+glm"
        elif fam and fam == r.get("v1"):
            dec, fonte = fam, "giudice+v1"
        else:
            dec, fonte = None, "tre_pareri" if fam else "senza_risposta"
        return {**r, "giudice": fam, "family": dec, "family_prov": fonte}

    st = collections.Counter(d["family_prov"] for d in fatti.values())
    t0 = time.time()
    with open(a.out, "a") as f, cf.ThreadPoolExecutor(max_workers=a.parallele) as pool:
        for i, d in enumerate(pool.map(uno, da_fare)):
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
            st[d["family_prov"]] += 1
            if (i + 1) % 500 == 0:
                f.flush()
                v = (i + 1) / (time.time() - t0)
                print(f"  {i+1}/{len(da_fare)} {dict(st)} {v:.1f}/s, restano ~{int((len(da_fare)-i-1)/max(v,0.01)/60)} min", flush=True)
    print(f"FINE {dict(st)} in {int(time.time()-t0)}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
