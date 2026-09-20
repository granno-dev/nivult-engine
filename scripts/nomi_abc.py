"""Il 22,7% di nomi non ancorati e' una malattia o un difetto del mio metro?

Stessa divisione usata per i numeri, piu' una categoria in piu' che guardando
gli esempi si e' rivelata necessaria:

  A  il nome sta nel troncato                     -> a posto
  B  sta nell'annuncio intero, non nel troncato   -> e' il TAGLIO
  C  e' un composto col trattino le cui PARTI      -> difetto del mio metro,
     stanno nel testo («LANXESS-site», «RPO-teams»)   non del dataset
  D  non c'e' da nessuna parte                    -> invenzione vera

Senza la C avrei riportato come invenzioni delle parole che il maestro ha
semplicemente unito con un trattino, ed e' il tipo di errore che fa buttare
via un dataset sano.
"""
import gzip, json, os, re, sys, unicodedata
from transformers import AutoTokenizer

TRAIN = sys.argv[1] if len(sys.argv) > 1 else "/opt/nivult/sintesi/sintesi-train-pulito.jsonl.gz"
QUANTE = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
tok = AutoTokenizer.from_pretrained(os.environ.get("BASE", "/opt/nivult/mt5"))
PAROLA = re.compile(r"\b[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ&.'-]{2,}")
PREF = re.compile(r"^Titolo: (.*)\nSede: (.*)\nLingua dell'annuncio: (.*?)\n\n", re.S)
SU = {"de","sv","da","no","nb","nn","lb"}

def sch(t):
    t = unicodedata.normalize("NFKD", (t or "").lower())
    return "".join(c for c in t if c.isalnum())

st = {k: 0 for k in "ABCD"}
righe = 0
esempi = {"B": [], "D": []}
for linea in gzip.open(TRAIN, "rt"):
    if righe >= QUANTE: break
    try: m = json.loads(linea)["messages"]
    except Exception: continue
    ing, ber = m[1]["content"], m[2]["content"]
    mm = PREF.match(ing); lang = (mm.group(3).strip() if mm else "")
    if lang in SU: continue
    righe += 1
    ids = tok(ing, add_special_tokens=False)["input_ids"]
    visto = tok.decode(ids[:1024], skip_special_tokens=True) if len(ids) > 1024 else ing
    p_visto, p_intero = sch(visto), sch(ing)
    inizi = {0} | {x.end() for x in re.finditer(r"[.!?:;]\s+", ber)}
    for x in PAROLA.finditer(ber):
        if x.start() in inizi: continue
        n = x.group(0).rstrip(".,"); s = sch(n)
        if s in p_visto: st["A"] += 1
        elif s in p_intero:
            st["B"] += 1
            if len(esempi["B"]) < 5: esempi["B"].append(n)
        elif "-" in n and all(sch(p) in p_visto for p in n.split("-") if len(p) > 2):
            st["C"] += 1
        else:
            st["D"] += 1
            if len(esempi["D"]) < 12: esempi["D"].append(n)

tot = sum(st.values()) or 1
print(f"=== {righe} coppie, {tot} nomi propri nei bersagli\n")
for k, che in (("A","nel troncato — a posto"),
               ("B","solo nell'annuncio intero — e' il TAGLIO"),
               ("C","composto col trattino, parti presenti — difetto del METRO"),
               ("D","da nessuna parte — invenzione vera")):
    print(f"  {st[k]*100/tot:5.1f}%  ({st[k]:5d})  {k}  {che}")
print("\n  esempi B:", esempi["B"])
print("  esempi D:", esempi["D"])
