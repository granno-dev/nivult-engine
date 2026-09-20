"""Quanto costa allargare la finestra da 1024 a 2048, misurato sulla scheda vera.

L'attenzione cresce col quadrato dei token, ma gli annunci corti non pagano:
con il riempimento dinamico un lotto di annunci brevi costa uguale. Quindi la
stima teorica (quattro volte) non dice niente — serve la misura su annunci
veri, presi dal dataset com'e'.

Si misura il tempo per annuncio a parita' di tutto il resto: stesso modello,
stesso lotto, stessi testi, uscita forzata alla lunghezza vera delle sintesi.
"""
import gzip, json, os, re, sys, time
import torch
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

P = os.environ.get("MT5", "/opt/nivult/mt5")
LOTTO = int(os.environ.get("LOTTO", "4"))
N = int(os.environ.get("N", "32"))
tok = AutoTokenizer.from_pretrained(P)
mod = AutoModelForSeq2SeqLM.from_pretrained(P, dtype=torch.float32).to("cuda").eval()

testi = []
for i, r in enumerate(gzip.open("/opt/nivult/sintesi/sintesi-train-finale.jsonl.gz", "rt")):
    if len(testi) >= N: break
    try: m = json.loads(r)["messages"]
    except Exception: continue
    testi.append(re.sub(r"\s+", " ", m[1]["content"]))

def giro(maxin):
    torch.cuda.synchronize(); t0 = time.time(); fatti = 0
    for i in range(0, len(testi), LOTTO):
        g = testi[i:i + LOTTO]
        enc = tok(g, return_tensors="pt", padding=True, truncation=True,
                  max_length=maxin).to("cuda")
        with torch.no_grad():
            mod.generate(**enc, max_new_tokens=120, num_beams=1, do_sample=False)
        fatti += len(g)
        del enc
    torch.cuda.synchronize()
    return (time.time() - t0) / fatti

giro(1024)                      # riscaldamento, non si conta
for m in (1024, 1536, 2048):
    s = giro(m)
    print(f"  finestra {m:>5}: {s*1000:6.0f} ms/annuncio   "
          f"{86400/s/1000:6.1f}k annunci al giorno")
