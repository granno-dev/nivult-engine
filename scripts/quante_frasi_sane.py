"""Il filtro ha tagliato frasi SANE per colpa della mia regex?

La regex dei numeri e' `\\d[\\d.,  ]*\\d`: accetta punti e spazi in mezzo, e
quindi puo' scavalcare la fine di una frase incollando «11.00» e «401» in un
finto numero «11.00. 401» che nel testo non esiste. Se e' successo mentre
filtravo, ho buttato frasi buone.

Si confronta il bersaglio ORIGINALE con quello ripulito: per ogni frase tolta
si guarda perche' e' stata tolta, e se l'unico motivo era un numero che
scavalca un punto, quella frase non andava tolta.
"""
import gzip, json, re, sys
sys.path.insert(0, "/opt/nivult")
from sintesi_ancorata import numeri_fuori, solo_cifre, _FRASE

ORIG = "/opt/nivult/sintesi/sintesi-train.jsonl.gz"
PULITO = "/opt/nivult/sintesi/sintesi-train-pulito.jsonl.gz"
QUANTE = int(sys.argv[1]) if len(sys.argv) > 1 else 40000
SCAVALCA = re.compile(r"\d[\d.,]*[.]\s+\d")

def bersagli(p, n):
    fuori = {}
    for i, r in enumerate(gzip.open(p, "rt")):
        if i >= n: break
        try: m = json.loads(r)["messages"]
        except Exception: continue
        fuori[i] = (m[1]["content"], m[2]["content"])
    return fuori

a, b = bersagli(ORIG, QUANTE), bersagli(PULITO, QUANTE)
# i due file sono allineati riga per riga tranne le 9 buttate: si confronta
# per ingresso, non per posizione
per_ing = {v[0]: v[1] for v in b.values()}
accorciate = solo_artefatto = 0
esempi = []
for i, (ing, orig) in a.items():
    pul = per_ing.get(ing)
    if pul is None or pul == orig: continue
    accorciate += 1
    tolte = [f for f in _FRASE.split(orig) if f not in pul]
    if tolte and all(SCAVALCA.search(f) and not numeri_fuori(
            SCAVALCA.sub(" ", f), solo_cifre(ing)) for f in tolte):
        solo_artefatto += 1
        if len(esempi) < 5: esempi.append(tolte[0][:130])

print(f"su {len(a)} coppie guardate: {accorciate} accorciate")
if accorciate:
    print(f"  di cui tolte SOLO per un numero che scavalca un punto: "
          f"{solo_artefatto} ({solo_artefatto*100/accorciate:.1f}%)")
for e in esempi: print("   ", e)
