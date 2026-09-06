"""nivult-v1: un solo codice per Colab (GPU) e per il collaudo a secco
sul N5 (CPU, poche righe, pochi passi). Base: mmBERT-base — NON XLM-R
(scelta del 2026-09-06, vedi docs/rubrica-classificazione.md e la
ricerca sui modelli: +2,5 punti XNLI e 2-4x piu' veloce di XLM-R).

Cinque teste su un encoder condiviso:
  famiglia (33), seniority (6), contratto (6), remoto (3)  -> softmax,
      perdita mascherata dove l'etichetta manca (ignore_index)
  lingue richieste (21 codici)                             -> multi-label,
      perdita solo sulle righe che hanno l'etichetta

Ricetta: AdamW 3e-5, warmup 6%, 3 epoche, label smoothing 0.1 (aiuta la
calibrazione), max_len 384 (titolo + luogo + inizio descrizione), bf16 su
GPU. Checkpoint per epoca in --out (su Colab: Drive) e ripresa
automatica: una disconnessione non butta via niente.

Esame: accuratezza per testa su golden (a mano e casuale), temperature
scaling stimato sul golden casuale, curva precisione/copertura sul golden
a mano e SOGLIA che da' il 95% di precisione sugli accettati. Cancello:
>= 90% sulla famiglia nel golden a mano, o non si deploya.

Uso:
  python addestra_v1.py --train dataset-train-v1.jsonl.gz --golden dataset-golden-v1.jsonl.gz --out ./v1
  python addestra_v1.py ... --max-righe 200 --max-passi 3 --cpu      # collaudo a secco
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import random
import time

import torch
import torch.nn as nn
import torch.nn.functional as F

BASE = "jhu-clsp/mmBERT-base"
FAMIGLIE = ['Administrative', 'Agriculture', 'Art & Design', 'Construction', 'Consulting',
            'Creative & Media', 'Customer Service & Support', 'Data & Analytics', 'Education',
            'Energy', 'Engineering', 'Environmental & Sustainability', 'Finance & Accounting',
            'Food & Beverage', 'Government & Public Sector', 'Healthcare', 'Hospitality',
            'Human Resources', 'Legal', 'Logistics', 'Management & Leadership', 'Manufacturing',
            'Marketing', 'Retail', 'Sales', 'Science & Research', 'Security & Safety',
            'Social Services', 'Software', 'Sports & Recreation', 'Technology', 'Trades',
            'Transportation']
SENIORITY = ["intern", "junior", "mid", "senior", "lead", "head"]
CONTRATTO = ["full_time", "part_time", "contract", "temporary", "internship", "apprenticeship"]
REMOTO = ["remote", "hybrid", "onsite"]
LINGUE = ["en", "fr", "de", "it", "es", "pt", "nl", "sv", "da", "no", "fi", "pl", "ru", "ar",
          "zh", "ja", "tr", "cs", "el", "ro", "hu"]
TESTE = {"family": FAMIGLIE, "seniority": SENIORITY, "employment_type": CONTRATTO, "remote": REMOTO}
IGN = -100


def leggi(percorso: str, max_righe: int | None = None) -> list[dict]:
    out = []
    with gzip.open(percorso, "rt") as f:
        for riga in f:
            out.append(json.loads(riga))
            if max_righe and len(out) >= max_righe:
                break
    return out


_TAG = None


def _pulito(t: str) -> str:
    """Difesa in profondita': anche se il dataset arrivasse sporco, il modello
    non vede mai entita' o tag HTML (il 06/09 il 54% delle righe li aveva)."""
    global _TAG
    import html, re
    if _TAG is None:
        _TAG = re.compile(r"<[^>]+>")
    t = html.unescape(html.unescape(t or ""))
    return re.sub(r"\s+", " ", _TAG.sub(" ", t).replace("\xa0", " ")).strip()


def testo(x: dict) -> str:
    return f"{x.get('title') or ''} | {x.get('location') or ''}\n{_pulito(x.get('text'))[:1200]}"


def etichette(x: dict) -> dict:
    y = {}
    for testa, voc in TESTE.items():
        v = x.get(testa)
        y[testa] = voc.index(v) if v in voc else IGN
    lr = x.get("languages_required")
    if lr:
        y["lingue"] = [1.0 if cod in lr else 0.0 for cod in LINGUE]
        y["lingue_mask"] = 1.0
    else:
        y["lingue"] = [0.0] * len(LINGUE)
        y["lingue_mask"] = 0.0
    return y


class Modello(nn.Module):
    def __init__(self, base: str = BASE):
        super().__init__()
        from transformers import AutoModel
        self.enc = AutoModel.from_pretrained(base)
        h = self.enc.config.hidden_size
        self.drop = nn.Dropout(0.1)
        self.teste = nn.ModuleDict({t: nn.Linear(h, len(v)) for t, v in TESTE.items()})
        self.lingue = nn.Linear(h, len(LINGUE))

    def forward(self, input_ids, attention_mask):
        out = self.enc(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        # media mascherata dei token: piu' stabile del solo [CLS] sui ModernBERT
        m = attention_mask.unsqueeze(-1).to(out.dtype)
        pooled = (out * m).sum(1) / m.sum(1).clamp(min=1)
        pooled = self.drop(pooled)
        return {t: head(pooled) for t, head in self.teste.items()} | {"lingue": self.lingue(pooled)}


PESI_CLASSI: dict = {}     # testa -> tensore dei pesi per classe (riempito in main)


def pesi_classi(train: list[dict], dev) -> dict:
    """Pesi inversi alla frequenza, tagliati a [0.5, 4]: nell'anteprima del
    06/09 il modello prediceva full_time al posto di temporary/part_time
    (16 errori su 25) e mid al posto di lead (7): le classi rare le
    ignorava. Il taglio evita di sovrapesare classi con pochi esempi."""
    out = {}
    for t, voc in TESTE.items():
        conta = torch.ones(len(voc))
        for x in train:
            v = x.get(t)
            if v in voc:
                conta[voc.index(v)] += 1
        w = (conta.sum() / (len(voc) * conta)).clamp(0.5, 4.0)
        out[t] = w.to(dev)
    return out


def perdita(logits: dict, y: dict, smoothing: float = 0.1) -> torch.Tensor:
    tot = 0.0
    for t in TESTE:
        tot = tot + F.cross_entropy(logits[t], y[t], ignore_index=IGN, label_smoothing=smoothing,
                                    weight=PESI_CLASSI.get(t))
    bce = F.binary_cross_entropy_with_logits(logits["lingue"], y["lingue"], reduction="none").mean(1)
    mask = y["lingue_mask"]
    if mask.sum() > 0:
        tot = tot + (bce * mask).sum() / mask.sum()
    return tot


def lotti(righe: list[dict], tok, bs: int, max_len: int, shuffle: bool):
    idx = list(range(len(righe)))
    if shuffle:
        random.shuffle(idx)
    for i in range(0, len(idx), bs):
        parte = [righe[j] for j in idx[i:i + bs]]
        enc = tok([testo(x) for x in parte], truncation=True, max_length=max_len,
                  padding=True, return_tensors="pt")
        ys = [etichette(x) for x in parte]
        y = {t: torch.tensor([e[t] for e in ys]) for t in TESTE}
        y["lingue"] = torch.tensor([e["lingue"] for e in ys])
        y["lingue_mask"] = torch.tensor([e["lingue_mask"] for e in ys])
        yield enc, y, parte


@torch.no_grad()
def predici(model, tok, righe, dev, bs=64, max_len=384):
    model.eval()
    out = []
    for enc, _, parte in lotti(righe, tok, bs, max_len, shuffle=False):
        lg = model(enc["input_ids"].to(dev), enc["attention_mask"].to(dev))
        for i, x in enumerate(parte):
            r = {"id": x["id"]}
            for t in TESTE:
                p = F.softmax(lg[t][i].float(), -1)
                r[t + "_logits"] = lg[t][i].float().cpu().tolist()
                r[t] = TESTE[t][int(p.argmax())]
                r[t + "_conf"] = float(p.max())
            r["lingue"] = [LINGUE[k] for k, v in enumerate(torch.sigmoid(lg["lingue"][i].float()).tolist()) if v > 0.5]
            out.append(r)
    return out


def temperatura(pred: list[dict], righe: list[dict], testa: str) -> float:
    """Temperature scaling per la testa: T che minimizza la NLL sul golden casuale."""
    voc = TESTE[testa]
    coppie = [(torch.tensor(p[testa + "_logits"]), voc.index(x[testa]))
              for p, x in zip(pred, righe) if x.get(testa) in voc]
    if len(coppie) < 30:
        return 1.0
    L = torch.stack([c[0] for c in coppie]); y = torch.tensor([c[1] for c in coppie])
    migliore, T_best = 1e9, 1.0
    for T in [0.5 + 0.05 * k for k in range(60)]:
        nll = F.cross_entropy(L / T, y).item()
        if nll < migliore:
            migliore, T_best = nll, T
    return T_best


def esame(pred, righe, testa, T=1.0, obiettivo=0.95):
    voc = TESTE[testa]
    coppie = [(torch.softmax(torch.tensor(p[testa + "_logits"]) / T, -1), x[testa])
              for p, x in zip(pred, righe) if x.get(testa) in voc]
    if not coppie:
        return {"n": 0}
    n = len(coppie)
    giuste = sum(1 for p, y in coppie if voc[int(p.argmax())] == y)
    curva = []
    for s in [0.5 + 0.05 * k for k in range(10)]:
        acc = [(voc[int(p.argmax())] == y) for p, y in coppie if float(p.max()) >= s]
        curva.append({"soglia": round(s, 2), "copertura": round(len(acc) / n, 3),
                      "precisione": round(sum(acc) / len(acc), 3) if acc else None})
    soglia95 = next((c["soglia"] for c in curva if c["precisione"] and c["precisione"] >= obiettivo), None)
    return {"n": n, "accuratezza": round(giuste / n, 4), "T": T, "curva": curva, "soglia_95": soglia95}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", required=True); ap.add_argument("--golden", required=True)
    ap.add_argument("--out", default="./v1"); ap.add_argument("--base", default=BASE)
    ap.add_argument("--epoche", type=int, default=3); ap.add_argument("--bs", type=int, default=32)
    ap.add_argument("--lr", type=float, default=3e-5); ap.add_argument("--max-len", type=int, default=384)
    ap.add_argument("--max-righe", type=int, default=None); ap.add_argument("--max-passi", type=int, default=None)
    ap.add_argument("--cpu", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    random.seed(42); torch.manual_seed(42)
    dev = torch.device("cpu" if a.cpu or not torch.cuda.is_available() else "cuda")
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(a.base)
    train = leggi(a.train, a.max_righe); golden = leggi(a.golden, a.max_righe)
    print(f"train {len(train)} | golden {len(golden)} | device {dev} | base {a.base}", flush=True)

    model = Modello(a.base).to(dev)
    PESI_CLASSI.update(pesi_classi(train, dev))
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=0.01)
    passi_epoca = math.ceil(len(train) / a.bs)
    tot_passi = a.max_passi or passi_epoca * a.epoche
    warm = max(1, int(0.06 * tot_passi))
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: min(1.0, (s + 1) / warm) * max(0.0, (tot_passi - s) / max(1, tot_passi - warm)))
    ckpt = os.path.join(a.out, "checkpoint.pt")
    epoca0, passo = 0, 0
    if os.path.exists(ckpt):
        st = torch.load(ckpt, map_location=dev)
        model.load_state_dict(st["model"]); opt.load_state_dict(st["opt"]); sched.load_state_dict(st["sched"])
        epoca0, passo = st["epoca"] + 1, st["passo"]
        print(f"ripresa dal checkpoint: epoca {epoca0}, passo {passo}", flush=True)
    usa_bf16 = dev.type == "cuda" and torch.cuda.is_bf16_supported()
    scaler = torch.amp.GradScaler("cuda", enabled=(dev.type == "cuda" and not usa_bf16))

    for ep in range(epoca0, a.epoche):
        model.train(); t0 = time.time(); somma = 0.0; n = 0
        for enc, y, _ in lotti(train, tok, a.bs, a.max_len, shuffle=True):
            if a.max_passi and passo >= a.max_passi:
                break
            enc = {k: v.to(dev) for k, v in enc.items()}; y = {k: v.to(dev) for k, v in y.items()}
            with torch.autocast(device_type=dev.type, dtype=torch.bfloat16 if usa_bf16 else torch.float16,
                                enabled=(dev.type == "cuda")):
                lg = model(enc["input_ids"], enc["attention_mask"])
                loss = perdita(lg, y)
            opt.zero_grad(set_to_none=True)
            if scaler.is_enabled():
                scaler.scale(loss).backward(); scaler.unscale_(opt)
                nn.utils.clip_grad_norm_(model.parameters(), 1.0); scaler.step(opt); scaler.update()
            else:
                loss.backward(); nn.utils.clip_grad_norm_(model.parameters(), 1.0); opt.step()
            sched.step(); passo += 1; somma += loss.item(); n += 1
            if n % 200 == 0:
                print(f"  ep {ep} passo {passo} loss {somma/n:.4f} {(time.time()-t0)/60:.1f} min", flush=True)
        print(f"epoca {ep}: loss media {somma/max(1,n):.4f} in {(time.time()-t0)/60:.1f} min", flush=True)
        torch.save({"model": model.state_dict(), "opt": opt.state_dict(), "sched": sched.state_dict(),
                    "epoca": ep, "passo": passo}, ckpt)
        if a.max_passi and passo >= a.max_passi:
            break

    # --- esame
    pred = predici(model, tok, golden, dev, bs=a.bs, max_len=a.max_len)
    mano = [(p, x) for p, x in zip(pred, golden) if x.get("fonte") == "mano"]
    casuale = [(p, x) for p, x in zip(pred, golden) if x.get("fonte") != "mano"]
    rapporto = {"base": a.base, "train": len(train), "golden": len(golden), "passi": passo}
    for t in TESTE:
        T = temperatura([p for p, _ in casuale], [x for _, x in casuale], t) if casuale else 1.0
        rapporto[t] = {"casuale": esame([p for p, _ in casuale], [x for _, x in casuale], t, T),
                       "mano": esame([p for p, _ in mano], [x for _, x in mano], t, T)}
    # lingue: precisione/copertura sull'insieme delle etichette
    tp = fp = fn = 0
    for p, x in zip(pred, golden):
        if x.get("languages_required"):
            vere, viste = set(x["languages_required"]), set(p["lingue"])
            tp += len(vere & viste); fp += len(viste - vere); fn += len(vere - viste)
    rapporto["lingue"] = {"precisione": round(tp / max(1, tp + fp), 3), "copertura": round(tp / max(1, tp + fn), 3)}
    fam_mano = rapporto["family"]["mano"].get("accuratezza")
    rapporto["cancello"] = {"famiglia_mano": fam_mano, "passa": bool(fam_mano is not None and fam_mano >= 0.90)}
    json.dump(rapporto, open(os.path.join(a.out, "esame-v1.json"), "w"), indent=1, ensure_ascii=False)
    print(json.dumps({k: (v if k in ("cancello", "lingue") else {kk: vv.get("accuratezza") for kk, vv in v.items()})
                      for k, v in rapporto.items() if k in TESTE or k in ("cancello", "lingue")},
                     indent=1, ensure_ascii=False), flush=True)
    # --- salvataggio del modello per il runtime
    torch.save(model.state_dict(), os.path.join(a.out, "pesi.pt"))
    tok.save_pretrained(a.out)
    json.dump({"base": a.base, "teste": TESTE, "lingue": LINGUE, "max_len": a.max_len,
               "temperature": {t: rapporto[t]["casuale"].get("T", 1.0) for t in TESTE},
               "soglie_95": {t: rapporto[t]["mano"].get("soglia_95") for t in TESTE}},
              open(os.path.join(a.out, "config-v1.json"), "w"), indent=1)
    print("salvato in", a.out, "| cancello:", rapporto["cancello"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
