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
    """Carica base + LoRA, e SI RIFIUTA di proseguire se il LoRA non e' entrato.

    Il buco che ci e' costato il primo tentativo (scoperto l'11/09/2026):
    Qwen3.5 si dichiara `Qwen3_5ForConditionalGeneration` e tiene lo stack di
    testo sotto `model.language_model.layers.*`. Unsloth ci aggancia il LoRA
    li' e salva chiavi come

        base_model.model.model.language_model.layers.0.mlp.gate_proj.lora_A

    ma `AutoModelForCausalLM` costruisce un albero SENZA quel segmento, e peft
    cerca `base_model.model.model.layers.0...`. Nessuna delle 256 chiavi
    combacia: finiscono tutte in un UserWarning («Found missing adapter keys»)
    che nessuno legge, il merge non fa niente, e l'esame misura il modello
    base credendo di misurare il nostro. L'esame del 09/09 che ha «bocciato»
    il primo tentativo con 67,5/76,6/65,2/83,3 e' esattamente questo.

    Due difese, e la seconda vale piu' della prima perche' non si fida:
     1. si istanzia la classe che il config DICHIARA, non quella comoda;
     2. si guarda dentro i pesi. `lora_B` nasce a zero e solo l'addestramento
        lo muove: se dopo il caricamento e' ancora tutto zero il LoRA non e'
        entrato, e si muore invece di produrre un voto falso.
    """
    import transformers
    from transformers import AutoConfig, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(modello)
    tok.padding_side = "left"
    cfg = AutoConfig.from_pretrained(modello)
    cls = getattr(transformers, (cfg.architectures or [""])[0],
                  transformers.AutoModelForCausalLM)
    print(f"base: {cls.__name__}", flush=True)
    m = cls.from_pretrained(modello, torch_dtype=torch.bfloat16, device_map="auto")
    if adapter:
        from peft import PeftModel
        m = PeftModel.from_pretrained(m, adapter)
        quanti = sum(1 for n, _ in m.named_parameters() if "lora_B" in n)
        somma = sum(float(p.abs().sum()) for n, p in m.named_parameters() if "lora_B" in n)
        print(f"LoRA: {quanti} tensori lora_B, somma |B| = {somma:.4f}", flush=True)
        if quanti == 0 or somma == 0.0:
            raise SystemExit(
                "LoRA NON caricato (lora_B tutto a zero): l'esame misurerebbe il "
                "modello base. Serve la stessa classe base con cui e' stato "
                "addestrato.")
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
        # si ferma a <|im_end|> (fine del turno) oltre che all'eos del modello base:
        # il tokenizer del base ha eos <|endoftext|>, e dopo <|im_end|> il modello
        # puo' continuare a scrivere finche' non lo raggiunge (visto il 09/09)
        fine = [i for i in (tok.convert_tokens_to_ids("<|im_end|>"), tok.eos_token_id) if i is not None]
        gen = m.generate(**enc, max_new_tokens=160, do_sample=False, eos_token_id=fine)
        for k in range(len(b)):
            testo = tok.decode(gen[k][enc["input_ids"].shape[1]:], skip_special_tokens=True)
            mm = re.search(r"\{.*?\}", testo, re.S)
            try:
                out.append(json.loads(mm.group(0)) if mm else {})
            except json.JSONDecodeError:
                out.append({})
    return out


def _norma(v):
    """`unknown` e `none` sono lo stesso concetto con due nomi: il golden a mano
    scrive «unknown», il dataset (regola RX_NONE) e quindi il modello scrivono
    «none». Il 11/09 questa differenza da sola valeva 6 errori su 280."""
    return "none" if v in ("unknown", "none", "None") else v


def acc(pred: list[dict], righe: list[dict], campo: str) -> dict:
    n = ok = 0
    conf = collections.Counter()
    for p, r in zip(pred, righe):
        v = r.get(campo)
        if v in (None, "None", ""):
            continue
        n += 1
        got = p.get(campo)
        if campo == "family":
            v, got = _norma(v), _norma(got) if isinstance(got, str) else got
        if got == v:
            ok += 1
        else:
            # la chiave della confusione deve essere hashabile: il 09/09 il modello
            # ha risposto con un dict per un campo e l'esame e' morto a meta'
            conf[(v, got if isinstance(got, (str, int, bool, type(None))) else json.dumps(got, ensure_ascii=False)[:40])] += 1
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
    # Il banco dei codici, SOLO dove codice e consenso GLM+v1 coincidono.
    # Misurato l'11/09: la famiglia da codice ufficiale coincide col consenso
    # solo nel 67% (Construction->Trades 413, Manufacturing->Trades 334...);
    # il nostro modello faceva 72,5%, cioe' SOPRA il consenso. Un cancello al
    # 92% su un banco dove due etichettatori concordano al 67% misura il
    # confine della tassonomia, non il modello. Le righe ambigue restano nella
    # verita' del dataset: spariscono solo dal banco. Se il file esame e'
    # vecchio e non ha `family_consenso`, si torna al banco intero e lo si dice.
    codici_tutti = [r for r in esame if r.get("family_prov") in ("rome", "ssyk", "isco")]
    con_consenso = [r for r in codici_tutti if "family_consenso" in r]
    if con_consenso:
        codici = [r for r in codici_tutti if r.get("family_consenso") and _norma(r["family_consenso"]) == _norma(r["family"])]
        banco = f"codice+consenso ({len(codici)} su {len(codici_tutti)})"
    else:
        codici = codici_tutti
        banco = f"SOLO codice, file esame senza family_consenso ({len(codici)})"
    print("banco codici:", banco, flush=True)
    rnd.shuffle(codici)
    codici = codici[:a.per_campo]
    pred = predici(tok, m, codici, ["family"], a.bs)
    rapporto["codici"] = {"family": acc(pred, codici, "family"), "banco": banco}
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
