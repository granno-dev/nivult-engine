"""Testa di v1 che MARCA le tecnologie nel testo: classificazione per token su mmBERT.

Non sceglie da un elenco chiuso come le altre teste di v1: decide, per ogni parola,
se fa parte di una tecnologia richiesta. E' l'unico modo per distinguere «Excel» usato
come requisito da «Excel» nominato di sfuggita — distinzione che il dizionario non sa
fare (37% contro il 75% del 2B, misurato il 16/09/2026).

Etichette BIO: 0=fuori, 1=inizio tecnologia, 2=dentro tecnologia.

  python addestra_tecnologie.py --passi 3000 --bs 8 --accumulo 2
"""
from __future__ import annotations
import argparse, gzip, json, os, random, sys, time
import torch
from torch.utils.data import Dataset, DataLoader
from transformers import AutoTokenizer, AutoModelForTokenClassification, get_cosine_schedule_with_warmup

BASE = os.environ.get("BASE_TEC", "jhu-clsp/mmBERT-base")
TRAIN = os.environ.get("TRAIN_TEC", "/opt/nivult/tecnologie/tecnologie-train.jsonl.gz")

class Tec(Dataset):
    def __init__(self, righe, tok, max_len):
        self.r, self.tok, self.max_len = righe, tok, max_len
    def __len__(self): return len(self.r)
    def __getitem__(self, i):
        r = self.r[i]
        testo = (r["titolo"] + "\n" + r["testo"])[: 20000]
        sposta = len(r["titolo"]) + 1        # gli intervalli sono sul solo testo
        enc = self.tok(testo, truncation=True, max_length=self.max_len, return_offsets_mapping=True)
        et = [0] * len(enc["input_ids"])
        for a, b, _ in r["intervalli"]:
            a += sposta; b += sposta
            primo = True
            for k, (oi, of) in enumerate(enc["offset_mapping"]):
                if of <= oi: continue          # token speciali
                if oi >= b: break
                if of > a and oi < b:
                    et[k] = 1 if primo else 2; primo = False
        for k, (oi, of) in enumerate(enc["offset_mapping"]):
            if of <= oi: et[k] = -100          # i token speciali non contano
        return {"input_ids": enc["input_ids"], "attention_mask": enc["attention_mask"], "labels": et}

def raggruppa(lotto, pad):
    n = max(len(x["input_ids"]) for x in lotto)
    ii = torch.full((len(lotto), n), pad, dtype=torch.long)
    am = torch.zeros((len(lotto), n), dtype=torch.long)
    ll = torch.full((len(lotto), n), -100, dtype=torch.long)
    for k, x in enumerate(lotto):
        m = len(x["input_ids"])
        ii[k, :m] = torch.tensor(x["input_ids"]); am[k, :m] = torch.tensor(x["attention_mask"])
        ll[k, :m] = torch.tensor(x["labels"])
    return {"input_ids": ii, "attention_mask": am, "labels": ll}

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--passi", type=int, default=15000,
                    help="5.228 passi = un'epoca su 83.649 righe a lotti 8x2")
    ap.add_argument("--bs", type=int, default=8)
    ap.add_argument("--accumulo", type=int, default=2)
    ap.add_argument("--max-len", type=int, default=1024)
    ap.add_argument("--lr", type=float, default=3e-5)
    ap.add_argument("--righe", type=int, default=0)
    ap.add_argument("--out", default="/workspace/tecnologie/modello")
    ap.add_argument("--salva-ogni", type=int, default=2500,
                    help="ogni checkpoint pesa 1,2 GB: 2.500 passi fa sei salvataggi su 15.000")
    ap.add_argument("--tieni-parziali", action="store_true",
                    help="tiene anche le righe con un nome non ancorato (etichette incomplete)")
    a = ap.parse_args()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(BASE)
    model = AutoModelForTokenClassification.from_pretrained(
        BASE, num_labels=3, id2label={0: "O", 1: "B-TEC", 2: "I-TEC"}).to(dev)
    model.train()
    righe = []
    scartate = 0
    with gzip.open(TRAIN, "rt") as f:
        for l in f:
            r = json.loads(l)
            # Riga parziale = almeno un nome del maestro non si e' ancorato al
            # testo, quindi le sue etichette sono incomplete. Usarla come pulita
            # insegna a tacere dove invece c'era qualcosa.
            if r.get("parziale") and not a.tieni_parziali:
                scartate += 1
                continue
            righe.append(r)
            if a.righe and len(righe) >= a.righe: break
    print(f"righe parziali scartate: {scartate:,}", flush=True)
    random.Random(1).shuffle(righe)
    con = sum(1 for r in righe if r["intervalli"])
    print(f"base {BASE} | device {dev} | righe {len(righe):,} (con marcature {con:,}) | "
          f"max_len {a.max_len} | lotto {a.bs}x{a.accumulo}", flush=True)
    dl = DataLoader(Tec(righe, tok, a.max_len), batch_size=a.bs, shuffle=True,
                    collate_fn=lambda b: raggruppa(b, tok.pad_token_id), num_workers=2)
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=0.01)
    sch = get_cosine_schedule_with_warmup(opt, int(0.05 * a.passi) + 1, a.passi)
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time(); passo = 0; perdite = []
    for epoca in range(100):
        for i, b in enumerate(dl):
            b = {k: v.to(dev) for k, v in b.items()}
            out = model(**b); perdita = out.loss / a.accumulo
            if not torch.isfinite(perdita): opt.zero_grad(set_to_none=True); continue
            perdita.backward(); perdite.append(out.loss.item())
            if (i + 1) % a.accumulo: continue
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step(); sch.step(); opt.zero_grad(set_to_none=True); passo += 1
            if passo % 25 == 0:
                dt = time.time() - t0
                m = sum(perdite[-400:]) / max(len(perdite[-400:]), 1)
                print(f"  passo {passo}/{a.passi} | perdita {m:.4f} | {dt/passo:.2f} s/passo | "
                      f"mancano {(a.passi-passo)*dt/passo/3600:.1f} h", flush=True)
            if passo % a.salva_ogni == 0 or passo >= a.passi:
                # Numerati, o si sovrascrivono e resta solo l'ultimo passo:
                # il migliore lo sceglie l'esame sul golden, dopo.
                dove = os.path.join(a.out, f"ck-{passo:05d}")
                os.makedirs(dove, exist_ok=True)
                model.save_pretrained(dove, safe_serialization=True); tok.save_pretrained(dove)
                print(f"  salvato {dove}", flush=True)
            if passo >= a.passi:
                print(f"FINE: {passo} passi in {(time.time()-t0)/3600:.2f} h", flush=True); return 0
    return 0

if __name__ == "__main__":
    sys.exit(main())
