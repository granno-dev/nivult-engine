"""Addestra un modello ENCODER-DECODER a scrivere le nostre sintesi degli annunci.

Perche' questa architettura e non un altro Gemma/Qwen: misurato il 17/09/2026 sul N5 con
uscita forzata a 120 token, cioe' la lunghezza vera delle nostre sintesi —
  Qwen 2B (decoder-only, 1,88 mld):    37.000 annunci/giorno
  mT5 XLSum (enc-dec, 582 mln):       140.000
  mt5-small (enc-dec, 300 mln):       326.000
Un decoder-only riattraversa le 790 parole dell'annuncio a ogni parola che scrive; qui
l'encoder legge una volta sola e il decoder scrive guardando quella lettura.

Si parte da XLSum, che sa gia' riassumere in 45 lingue: deve solo imparare COME
riassumiamo noi gli annunci, non cosa sia un riassunto.

  BASE=csebuetnlp/mT5_multilingual_XLSum python addestra_mt5.py --passi 2500
"""
from __future__ import annotations
import argparse, contextlib, gzip, json, os, random, sys, time
import torch
from torch.utils.data import Dataset, DataLoader
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM, get_cosine_schedule_with_warmup

TRAIN = os.environ.get("TRAIN_MT5", "/workspace/sintesi/sintesi-train.jsonl.gz")
BASE = os.environ.get("BASE", "csebuetnlp/mT5_multilingual_XLSum")

class Sintesi(Dataset):
    """Legge il dataset in formato messaggi e ne ricava le due parti: annuncio -> sintesi."""
    def __init__(self, righe, tok, max_in, max_out):
        self.r, self.tok, self.mi, self.mo = righe, tok, max_in, max_out
    def __len__(self): return len(self.r)
    def __getitem__(self, i):
        m = self.r[i]["messages"]
        ingresso = m[1]["content"]          # titolo, sede, lingua e testo dell'annuncio
        uscita = m[2]["content"]            # la sintesi di DeepSeek
        a = self.tok(ingresso, truncation=True, max_length=self.mi)
        b = self.tok(text_target=uscita, truncation=True, max_length=self.mo)
        return {"input_ids": a["input_ids"], "attention_mask": a["attention_mask"],
                "labels": b["input_ids"]}

def raggruppa(lotto, pad):
    ni = max(len(x["input_ids"]) for x in lotto); nl = max(len(x["labels"]) for x in lotto)
    ii = torch.full((len(lotto), ni), pad, dtype=torch.long)
    am = torch.zeros((len(lotto), ni), dtype=torch.long)
    ll = torch.full((len(lotto), nl), -100, dtype=torch.long)
    for k, x in enumerate(lotto):
        a, b = len(x["input_ids"]), len(x["labels"])
        ii[k, :a] = torch.tensor(x["input_ids"]); am[k, :a] = torch.tensor(x["attention_mask"])
        ll[k, :b] = torch.tensor(x["labels"])
    return {"input_ids": ii, "attention_mask": am, "labels": ll}

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--passi", type=int, default=2500)
    ap.add_argument("--bs", type=int, default=8)
    ap.add_argument("--accumulo", type=int, default=2)
    ap.add_argument("--max-in", type=int, default=1536)   # copre il 97% degli annunci
    ap.add_argument("--max-out", type=int, default=256)   # copre il 99,9% delle sintesi
    ap.add_argument("--lr", type=float, default=5e-5)
    ap.add_argument("--righe", type=int, default=0)
    ap.add_argument("--out", default="/workspace/mt5/modello")
    ap.add_argument("--salva-ogni", type=int, default=500)
    # Su una scheda a noleggio la caduta e' un evento normale, non un'eccezione: si
    # riparte dall'ultimo salvataggio invece di rifare tutto (17/09/2026).
    ap.add_argument("--riprendi", action="store_true")
    a = ap.parse_args()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    stato_f = os.path.join(a.out, "stato.json")
    fatti = 0; da = BASE
    if a.riprendi and os.path.exists(stato_f):
        fatti = int(json.load(open(stato_f)).get("passo", 0)); da = a.out
        print(f"riprendo da {da}: {fatti} passi gia' fatti", flush=True)
    tok = AutoTokenizer.from_pretrained(da)
    # I PESI IN FLOAT32, il CALCOLO in bfloat16. Tenere i pesi in bfloat16 e metterci
    # sopra AdamW sembra funzionare e non funziona: bfloat16 ha tre cifre decimali
    # scarse, e appena i gradienti si fanno piccoli l'aggiornamento viene arrotondato
    # a zero. Il 17/09/2026 la perdita e' scesa a 2,60 entro il passo 1000 e non si e'
    # piu' mossa per cinquemila passi: non era convergenza, era il pavimento della
    # precisione. Il modello scriveva parole a caso.
    peso = torch.float32 if os.environ.get("PESI", "f32") == "f32" else torch.bfloat16
    model = AutoModelForSeq2SeqLM.from_pretrained(da, dtype=peso).to(dev)
    autocast = (torch.autocast("cuda", dtype=torch.bfloat16) if peso is torch.float32 and dev == "cuda"
                else contextlib.nullcontext())
    # Il checkpointing ricalcola le attivazioni per risparmiare memoria: serve su una
    # scheda piccola, su una da 48 GB costa solo tempo. CHECKPOINTING=0 lo spegne.
    if os.environ.get('CHECKPOINTING', '1') != '0':
        model.gradient_checkpointing_enable()
    model.config.use_cache = False
    model.train()
    righe = []
    with gzip.open(TRAIN, "rt") as f:
        for l in f:
            righe.append(json.loads(l))
            if a.righe and len(righe) >= a.righe: break
    random.Random(1).shuffle(righe)
    print(f"base {BASE} | {sum(p.numel() for p in model.parameters())/1e6:.0f}M parametri | "
          f"device {dev} | righe {len(righe):,} | ingresso {a.max_in} uscita {a.max_out} | "
          f"lotto {a.bs}x{a.accumulo}", flush=True)
    ds = Sintesi(righe, tok, a.max_in, a.max_out)
    lung = [len(r["messages"][1]["content"]) for r in righe]   # caratteri: basta per ordinare
    def lotti_omogenei():
        r = random.Random(7)
        idx = list(range(len(righe))); r.shuffle(idx)
        blocco = a.bs * 50
        fuori = []
        for i in range(0, len(idx), blocco):
            pezzo = sorted(idx[i:i+blocco], key=lambda k: lung[k])
            fuori += [pezzo[j:j+a.bs] for j in range(0, len(pezzo), a.bs)]
        r.shuffle(fuori)
        return fuori
    dl = DataLoader(ds, batch_sampler=lotti_omogenei(),
                    collate_fn=lambda b: raggruppa(b, tok.pad_token_id), num_workers=4)
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=0.01)
    sch = get_cosine_schedule_with_warmup(opt, int(0.03 * a.passi) + 1, a.passi)
    for _ in range(fatti): sch.step()        # la curva riprende dove si era interrotta
    os.makedirs(a.out, exist_ok=True)
    t0 = time.time(); passo = fatti; perdite = []; scartati = 0
    for epoca in range(100):
        for i, b in enumerate(dl):
            if i < fatti * a.accumulo:
                continue          # lotti gia' visti: l'ordine ha un seme, sono gli stessi
            nv = model.config.vocab_size
            ii_max = int(b["input_ids"].max()); ii_min = int(b["input_ids"].min())
            lv = b["labels"][b["labels"] != -100]
            lb_max = int(lv.max()) if lv.numel() else 0
            lb_min = int(lv.min()) if lv.numel() else 0
            if ii_max >= nv or ii_min < 0 or lb_max >= nv or lb_min < 0 or lv.numel() == 0:
                print(f"  LOTTO MALATO al passo {passo}: vocab {nv}, ingresso [{ii_min},{ii_max}], "
                      f"etichette [{lb_min},{lb_max}], etichette vive {int(lv.numel())}, "
                      f"forma {tuple(b['input_ids'].shape)}/{tuple(b['labels'].shape)}", flush=True)
                scartati += 1; continue
            b = {k: v.to(dev) for k, v in b.items()}
            with autocast:
                out = model(**b)
            perdita = out.loss.float() / a.accumulo
            if not torch.isfinite(perdita): scartati += 1; opt.zero_grad(set_to_none=True); continue
            perdita.backward(); perdite.append(out.loss.item())
            if (i + 1) % a.accumulo: continue
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step(); sch.step(); opt.zero_grad(set_to_none=True); passo += 1
            # LA PERDITA SI MUOVE ANCORA? Il 17/09/2026 e' rimasta a 2,59 per cinquemila
            # passi e l'ho letta come convergenza: era il pavimento di bfloat16, e sono
            # state due ore e mezza di scheda pagate per un modello che scriveva parole
            # a caso. Un addestramento che non migliora da mille passi ha qualcosa che
            # non va, e deve dirlo da solo invece di aspettare che qualcuno lo noti.
            if passo % 250 == 0 and len(perdite) > 2200:
                recente = sum(perdite[-800:]) / 800
                prima = sum(perdite[-2200:-1400]) / 800
                if prima - recente < 0.01:
                    guasto = (f"PERDITA FERMA: {prima:.3f} -> {recente:.3f} in 1400 lotti "
                              f"(passo {passo}). Non e' convergenza finche' non si e' escluso: "
                              f"pesi in bfloat16 (PESI=f32), lr troppo bassa, dati ripetuti.")
                    print(guasto, flush=True)
                    if os.environ.get("FERMATI_SE_FERMA", "1") == "1":
                        print("mi fermo: meglio perdere un passo che pagare un giro intero", flush=True)
                        return 4
            if passo % 25 == 0:
                dt = time.time() - t0
                m = sum(perdite[-400:]) / max(len(perdite[-400:]), 1)
                sp = dt / max(passo - fatti, 1)
                print(f"  passo {passo}/{a.passi} | perdita {m:.3f} | {sp:.2f} s/passo | "
                      f"mancano {(a.passi-passo)*sp/3600:.1f} h | scartati {scartati}", flush=True)
            if passo % a.salva_ogni == 0 or passo >= a.passi:
                model.to(torch.bfloat16).save_pretrained(a.out, safe_serialization=True)
                model.to(peso); tok.save_pretrained(a.out)
                json.dump({"passo": passo, "perdita": sum(perdite[-400:])/max(len(perdite[-400:]),1)},
                          open(stato_f, "w"))
                print(f"  salvato al passo {passo}", flush=True)
            if passo >= a.passi:
                print(f"FINE: {passo} passi in {(time.time()-t0)/3600:.2f} h, perdita "
                      f"{sum(perdite[-400:])/max(len(perdite[-400:]),1):.3f}", flush=True)
                return 0
    return 0

if __name__ == "__main__":
    sys.exit(main())
