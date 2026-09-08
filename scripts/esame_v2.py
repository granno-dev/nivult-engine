#!/usr/bin/env python3
"""L'esame di nivult-v2: campo per campo, estrazione e stima separate, con i cancelli.

Tre banchi, tutti con verita' che non viene da un modello:
  1. le 280 a mano (dataset-golden-v1): famiglia, seniority, contratto, remoto;
  2. i codici ufficiali del lato esame (family_prov in rome/ssyk/isco): famiglia;
  3. i campi DICHIARATI del lato esame: seniority, contratto, remoto, separati
     per menzione (estrazione) e non menzione (stima), 2.000 righe per campo.

    python scripts/esame_v2.py --modello <cartella o repo> --adapter <cartella lora> \\
        --golden dataset-golden-v1.jsonl.gz --esame dataset-esame-v2.jsonl.gz --out esame-v2.json

Cancelli (decisi il 07/09/2026): famiglia >= 0.92 sulle 280 a mano e sui codici;
seniority e contratto >= 0.88 in estrazione; remoto >= 0.90; la stima si
riporta a parte e NON e' un cancello per andare in produzione: e' il numero
che dice se pubblicarla.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import json
import random
import re
import time

import torch



def _carica(modello: str, adapter: str | None):
    from transformers import AutoModelForCausalLM, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(modello)
    tok.padding_side = "left"
    m = AutoModelForCausalLM.from_pretrained(modello, torch_dtype=torch.bfloat16, device_map="auto")
    if adapter:
        from peft import PeftModel
        m = PeftModel.from_pretrained(m, adapter)
        m = m.merge_and_unload()
    m.eval()
    return tok, m


@torch.no_grad()
def predici(tok, m, righe: list[dict], campi: list[str], bs: int = 16) -> list[dict]:
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from prompt_v2 import SISTEMA, utente
    out = []
    for i in range(0, len(righe), bs):
        b = righe[i:i + bs]
        msgs = [[{"role": "system", "content": SISTEMA},
                 {"role": "user", "content": utente(r, campi, (r.get("text") or "")[:1000])}] for r in b]
        try:
            enc = tok.apply_chat_template(msgs, add_generation_prompt=True, return_tensors="pt", padding=True,
                                          return_dict=True, enable_thinking=False).to(m.device)
        except TypeError:   # template senza enable_thinking
            enc = tok.apply_chat_template(msgs, add_generation_prompt=True, return_tensors="pt", padding=True,
                                          return_dict=True).to(m.device)
        gen = m.generate(**enc, max_new_tokens=120, do_sample=False)
        for k in range(len(b)):
            testo = tok.decode(gen[k][enc["input_ids"].shape[1]:], skip_special_tokens=True)
            mm = re.search(r"\{.*\}", testo, re.S)
            try:
                out.append(json.loads(mm.group(0)) if mm else {})
            except json.JSONDecodeError:
                out.append({})
    return out


def acc(pred: list[dict], righe: list[dict], campo: str) -> dict:
    n = ok = 0
    conf = collections.Counter()
    for p, r in zip(pred, righe):
        v = r.get(campo)
        if v in (None, "None", ""):
            continue
        n += 1
        if p.get(campo) == v:
            ok += 1
        else:
            conf[(v, p.get(campo))] += 1
    return {"n": n, "accuratezza": round(ok / n, 4) if n else None, "errori": conf.most_common(6)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modello", required=True)
    ap.add_argument("--adapter", default=None)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--esame", required=True)
    ap.add_argument("--out", default="esame-v2.json")
    ap.add_argument("--per-campo", type=int, default=2000)
    ap.add_argument("--bs", type=int, default=16)
    a = ap.parse_args()
    rnd = random.Random(3)
    tok, m = _carica(a.modello, a.adapter)
    rapporto: dict = {}
    t0 = time.time()

    # 1. le 280 a mano
    mano = [json.loads(l) for l in gzip.open(a.golden, "rt")]
    mano = [r for r in mano if r.get("fonte") == "mano"]
    for r in mano:
        r.setdefault("azienda", "")
    pred = predici(tok, m, mano, ["family", "seniority", "employment_type", "remote"], a.bs)
    rapporto["mano"] = {c: acc(pred, mano, c) for c in ("family", "seniority", "employment_type", "remote")}
    print("mano:", {c: v["accuratezza"] for c, v in rapporto["mano"].items()}, flush=True)

    # 2 e 3. il lato esame del dataset v2
    esame = [json.loads(l) for l in gzip.open(a.esame, "rt")]
    codici = [r for r in esame if r.get("family_prov") in ("rome", "ssyk", "isco")]
    rnd.shuffle(codici)
    codici = codici[:a.per_campo]
    pred = predici(tok, m, codici, ["family"], a.bs)
    rapporto["codici"] = {"family": acc(pred, codici, "family")}
    print("codici:", rapporto["codici"]["family"]["accuratezza"], flush=True)

    rapporto["dichiarati"] = {}
    for campo in ("seniority", "employment_type", "remote"):
        for modo, menz in (("estrazione", True), ("stima", False)):
            sub = [r for r in esame if r.get(campo) and r.get(f"{campo}_prov") == "dichiarato"
                   and bool(r.get(f"{campo}_menzione")) == menz]
            rnd.shuffle(sub)
            sub = sub[:a.per_campo]
            if not sub:
                continue
            pred = predici(tok, m, sub, [campo], a.bs)
            r_ = acc(pred, sub, campo)
            # quante volte il modello dichiara di stimare, quando stima davvero
            r_["dice_stimato"] = round(sum(1 for p in pred if p.get(f"{campo}_stimato") is True) / max(len(pred), 1), 3)
            rapporto["dichiarati"][f"{campo}:{modo}"] = r_
            print(f"{campo} {modo}: {r_['accuratezza']} (n={r_['n']}, dice stimato {r_['dice_stimato']})", flush=True)

    fam_mano = rapporto["mano"]["family"]["accuratezza"] or 0
    fam_cod = rapporto["codici"]["family"]["accuratezza"] or 0
    d = rapporto["dichiarati"]
    cancello = {
        "famiglia_mano": fam_mano, "famiglia_codici": fam_cod,
        "seniority_estrazione": (d.get("seniority:estrazione") or {}).get("accuratezza"),
        "contratto_estrazione": (d.get("employment_type:estrazione") or {}).get("accuratezza"),
        "remoto_estrazione": (d.get("remote:estrazione") or {}).get("accuratezza"),
        "passa": fam_mano >= 0.92 and fam_cod >= 0.92
                 and ((d.get("seniority:estrazione") or {}).get("accuratezza") or 0) >= 0.88
                 and ((d.get("employment_type:estrazione") or {}).get("accuratezza") or 0) >= 0.88
                 and ((d.get("remote:estrazione") or {}).get("accuratezza") or 0) >= 0.90,
    }
    rapporto["cancello"] = cancello
    rapporto["secondi"] = int(time.time() - t0)
    json.dump(rapporto, open(a.out, "w"), indent=1, ensure_ascii=False)
    print("CANCELLO:", json.dumps(cancello), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
