"""Le due correzioni strutturali, prima di pagare la GPU.

1. CONTAMINAZIONE. 48 dei 1.000 annunci del dev stanno anche nel train. Un
   esame su righe viste in addestramento misura la memoria, non la capacita' —
   ed e' l'errore che ha reso inutile piu' di una misura in questo progetto.
   Si tolgono dal TRAIN, non dal dev: il dev e' il metro e non si accorcia.

2. DOPPIONI. 7.312 righe (3,8%) sono lo stesso annuncio gia' presente. Non e'
   un guasto, ma quelle righe pesano il doppio delle altre senza che nessuno
   l'abbia deciso. Si tiene la prima e si buttano le copie.

Non si tocca nient'altro: i bersagli sono gia' passati dal filtro sulle cifre,
e i nomi propri NON si filtrano — misurato, il 95,4% e' ancorato e il residuo
e' quasi tutto traduzione di nomi di luogo, non invenzione.
"""
import gzip, hashlib, json, re, sys

PREF = re.compile(r"^Titolo: (.*)\nSede: (.*)\nLingua dell'annuncio: (.*?)\n\n", re.S)

def corpo(i):
    m = PREF.match(i or "")
    return (i or "")[m.end():] if m else (i or "")

def imp(t):
    return hashlib.sha1(re.sub(r"\s+", " ", (t or "").strip()).encode()).hexdigest()

train, dev, uscita = sys.argv[1], sys.argv[2], sys.argv[3]

nel_dev = set()
for r in gzip.open(dev, "rt"):
    r = r.strip()
    if not r: continue
    try: m = json.loads(r)["messages"]
    except Exception: continue
    nel_dev.add(imp(corpo(m[1].get("content"))))

viste = set()
lette = tolte_dev = tolte_doppie = scritte = 0
with gzip.open(train, "rt") as f, gzip.open(uscita, "wt") as g:
    for r in f:
        r = r.strip()
        if not r: continue
        try: d = json.loads(r); m = d["messages"]
        except Exception: continue
        lette += 1
        h = imp(corpo(m[1].get("content")))
        if h in nel_dev:
            tolte_dev += 1; continue
        if h in viste:
            tolte_doppie += 1; continue
        viste.add(h)
        g.write(json.dumps(d, ensure_ascii=False) + "\n")
        scritte += 1

print(f"lette          {lette}")
print(f"tolte perche' nel dev   {tolte_dev}")
print(f"tolte perche' doppie    {tolte_doppie}")
print(f"scritte        {scritte}  ({scritte*100/max(lette,1):.1f}%)")
