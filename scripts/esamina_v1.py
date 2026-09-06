"""Dove sbaglia v1? Il volano parte da qui: si legge ogni errore sul set
d'esame a mano, si trovano le coppie confuse, e la rubrica cresce.

Carica pesi.pt + config-v1.json (prodotti da addestra_v1.py), predice sul
golden e stampa: accuratezza per famiglia, le coppie (vera -> predetta)
piu' frequenti, e gli errori uno per uno (titolo, vera, predetta,
confidenza). Gira sulla iGPU del N5 se c'e' (HSA_OVERRIDE_GFX_VERSION),
altrimenti a CPU.

Uso: python esamina_v1.py --modello /opt/nivult/modelli/v1-anteprima --golden dataset-golden-v1.jsonl.gz
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict

import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from addestra_v1 import Modello, TESTE, leggi, predici  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modello", required=True)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--fonte", default="mano")
    ap.add_argument("--bs", type=int, default=32)
    a = ap.parse_args()
    cfg = json.load(open(os.path.join(a.modello, "config-v1.json")))
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.modello)
    m = Modello(cfg["base"])
    m.load_state_dict(torch.load(os.path.join(a.modello, "pesi.pt"), map_location="cpu"))
    m = m.to(dev)
    if dev.type == "cuda":
        m = m.half()
    righe = [x for x in leggi(a.golden) if x.get("fonte") == a.fonte]
    t0 = time.time()
    pred = predici(m, tok, righe, dev, bs=a.bs, max_len=cfg.get("max_len", 384))
    dt = time.time() - t0
    print(f"{len(righe)} righe ({a.fonte}) in {dt:.1f}s su {dev} = {len(righe)/dt:.1f} offerte/s\n")
    for testa, voc in TESTE.items():
        coppie = [(p, x) for p, x in zip(pred, righe) if x.get(testa) in voc]
        if not coppie:
            continue
        giuste = sum(1 for p, x in coppie if p[testa] == x[testa])
        print(f"== {testa}: {giuste}/{len(coppie)} = {100*giuste/len(coppie):.1f}%")
        per_cl: dict = defaultdict(lambda: [0, 0])
        conf = Counter()
        for p, x in coppie:
            per_cl[x[testa]][1] += 1
            if p[testa] == x[testa]:
                per_cl[x[testa]][0] += 1
            else:
                conf[(x[testa], p[testa])] += 1
        if testa == "family":
            print("  per famiglia (giuste/totale), peggiori prima:")
            for k, (g, n) in sorted(per_cl.items(), key=lambda kv: kv[1][0] / kv[1][1])[:12]:
                print(f"    {k:32s} {g}/{n}")
        print("  coppie confuse (vera -> predetta):")
        for (v, p_), n in conf.most_common(12):
            print(f"    {n:2d}  {v} -> {p_}")
        if testa == "family":
            print("  errori uno per uno:")
            for p, x in coppie:
                if p[testa] != x[testa]:
                    print(f"    [{x[testa]} -> {p[testa]} @{p[testa+'_conf']:.2f}] {x['title'][:70]}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
