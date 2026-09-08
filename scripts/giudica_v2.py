#!/usr/bin/env python3
"""Il giudice sui casi dubbi, su GPU (Colab/RunPod) con vLLM.

Input: da_giudicare.jsonl (le righe dove GLM e v1 non concordano o v1 e'
incerto). Il giudice (Qwen3.6-35B-A3B, prompt con le definizioni delle 33
famiglie e le regole di confine: 79,6% sulle 280 a mano, misurato l'08/09)
da' il suo parere. La regola di decisione e' PRUDENTE:

  - giudice = GLM  oppure  giudice = v1   → la famiglia entra (due su tre)
  - tre pareri diversi                    → resta fuori: va in chat (per titolo)

    python scripts/giudica_v2.py --in da_giudicare.jsonl --out giudicati.jsonl [--modello Qwen/Qwen3.6-35B-A3B]

Sul N5 sarebbero tre giorni (8 s/annuncio); su una H100 con vLLM ~1 ora.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time

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
    ap.add_argument("--modello", default="Qwen/Qwen3.6-35B-A3B")
    ap.add_argument("--max", type=int, default=None)
    a = ap.parse_args()
    from vllm import LLM, SamplingParams
    from vllm.sampling_params import GuidedDecodingParams
    righe = [json.loads(l) for l in open(a.src)]
    if a.max:
        righe = righe[:a.max]
    print(f"{len(righe)} casi da giudicare con {a.modello}", flush=True)
    llm = LLM(model=a.modello, max_model_len=4096, gpu_memory_utilization=0.92, enable_prefix_caching=True)
    tok = llm.get_tokenizer()
    prompts = []
    for r in righe:
        u = f"Titolo: {r['title']}\nSede: {r.get('location') or ''}\nAzienda: {(r.get('azienda') or '').split('/')[-1]}\n\n{(r.get('text') or '')[:2500]}"
        prompts.append(tok.apply_chat_template([{"role": "system", "content": SYS}, {"role": "user", "content": u}],
                                               tokenize=False, add_generation_prompt=True, enable_thinking=False))
    sp = SamplingParams(temperature=0, max_tokens=40, guided_decoding=GuidedDecodingParams(json=SCHEMA))
    t0 = time.time()
    esiti = llm.generate(prompts, sp)
    st = collections.Counter()
    with open(a.out, "w") as f:
        for r, e in zip(righe, esiti):
            try:
                fam = json.loads(e.outputs[0].text)["family"]
            except Exception:  # noqa: BLE001
                fam = None
            if fam and fam == r.get("glm"):
                decisione, fonte = fam, "giudice+glm"
            elif fam and fam == r.get("v1"):
                decisione, fonte = fam, "giudice+v1"
            else:
                decisione, fonte = None, "tre_pareri"
            st[fonte] += 1
            f.write(json.dumps({**r, "giudice": fam, "family": decisione, "family_prov": fonte}, ensure_ascii=False) + "\n")
    print(f"FINE {dict(st)} in {int(time.time()-t0)}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
