"""Il dev e' dentro il train? Se si', l'esame misura la memoria, non la capacita'.

Niente tokenizzatore qui: solo impronte del testo dell'annuncio, cosi' il
controllo gira sull'intero file in un minuto invece che in un'ora.
"""
import gzip, hashlib, json, re, sys

PREF = re.compile(r"^Titolo: (.*)\nSede: (.*)\nLingua dell'annuncio: (.*?)\n\n", re.S)

def corpo(ing):
    m = PREF.match(ing or "")
    return (ing or "")[m.end():] if m else (ing or "")

def imp(t):
    return hashlib.sha1(re.sub(r"\s+", " ", (t or "").strip()).encode()).hexdigest()

def carica(p):
    fuori = []
    for r in gzip.open(p, "rt"):
        r = r.strip()
        if not r: continue
        try: m = json.loads(r)["messages"]
        except Exception: continue
        fuori.append(imp(corpo(m[1].get("content"))))
    return fuori

train = carica(sys.argv[1]); dev = carica(sys.argv[2])
st, sd = set(train), set(dev)
comuni = st & sd
print(f"train: {len(train)} righe, {len(st)} testi distinti")
print(f"dev:   {len(dev)} righe, {len(sd)} testi distinti")
print(f"\nannunci del dev presenti ANCHE nel train: {len(comuni)}  "
      f"({len(comuni)*100/max(len(sd),1):.1f}% del dev)")
doppi = len(train) - len(st)
print(f"copie in piu' dentro il train: {doppi} ({doppi*100/max(len(train),1):.1f}%)")
