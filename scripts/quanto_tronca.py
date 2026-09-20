"""La domanda di Giuseppe: addestrare su un dataset troncato al 35% ha senso?

Il troncamento in se' non e' un difetto — in produzione il modello vede la
stessa finestra. Il difetto e' se il BERSAGLIO parla di cose che nella finestra
non ci sono: allora la coppia insegna a inventare, e per i numeri l'abbiamo
curata ma per il resto del testo no.

Si misurano tre cose per ogni finestra possibile:
  - quanti annunci vengono troncati
  - quanta parte della sintesi resta giustificabile da cio' che il modello vede
    (parole di contenuto della sintesi presenti nella finestra)
  - quanto costa: i token in ingresso decidono memoria e velocita'

La copertura si misura SOLO sugli annunci troncati: sugli altri e' uguale per
definizione e includerli annacqua la differenza fino a nasconderla.
"""
import gzip, json, os, re, sys, unicodedata
from transformers import AutoTokenizer

TRAIN = "/opt/nivult/sintesi/sintesi-train-finale.jsonl.gz"
QUANTE = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
tok = AutoTokenizer.from_pretrained(os.environ.get("BASE", "/opt/nivult/mt5"))
FINESTRE = [768, 1024, 1536, 2048, 3072]

def par(t):
    t = unicodedata.normalize("NFKD", (t or "").lower())
    return {w for w in re.findall(r"[a-zà-öø-ÿ]{5,}", t)}

tronc = {f: 0 for f in FINESTRE}
cop_somma = {f: 0.0 for f in FINESTRE}
cop_n = {f: 0 for f in FINESTRE}
cop_tutti = {f: 0.0 for f in FINESTRE}
righe = 0
lung = []

for linea in gzip.open(TRAIN, "rt"):
    if righe >= QUANTE: break
    try: m = json.loads(linea)["messages"]
    except Exception: continue
    righe += 1
    ing, ber = m[1]["content"], m[2]["content"]
    ids = tok(ing, add_special_tokens=False)["input_ids"]
    lung.append(len(ids))
    pv = par(ber)
    if not pv: continue
    for f in FINESTRE:
        if len(ids) > f:
            tronc[f] += 1
            visto = tok.decode(ids[:f], skip_special_tokens=True)
            c = len(pv & par(visto)) / len(pv)
            cop_somma[f] += c; cop_n[f] += 1
            cop_tutti[f] += c
        else:
            cop_tutti[f] += 1.0

lung.sort()
print(f"=== {righe} annunci   mediana {lung[len(lung)//2]} token, "
      f"90° percentile {lung[int(len(lung)*0.9)]}, massimo {lung[-1]}\n")
print(f"  {'finestra':>9} {'troncati':>10} {'copertura sui troncati':>24} {'copertura su tutti':>20}")
for f in FINESTRE:
    t = tronc[f] * 100 / righe
    ct = cop_somma[f] / cop_n[f] * 100 if cop_n[f] else 100.0
    ca = cop_tutti[f] / righe * 100
    print(f"  {f:>9} {t:>9.1f}% {ct:>23.1f}% {ca:>19.1f}%")

print(f"\n  se si BUTTANO gli annunci troncati:")
for f in FINESTRE:
    print(f"     finestra {f:>5}: resterebbero {righe - tronc[f]} coppie su {righe} "
          f"({(righe-tronc[f])*100/righe:.0f}%)")
