"""Quante tecnologie del golden stanno OLTRE la finestra della testa?

Se una tecnologia sta dopo il token 1024, il modello non l'ha mai vista: darla
per «persa» e' colpa del taglio, non del modello. E il voto che ne esce non
dice quello che sembra dire.

Si usa lo stesso tokenizzatore della testa (mmBERT) e lo stesso taglio del
demone: titolo + testo, tagliato a `max_len` token.
"""
import glob, gzip, json, os, sys, importlib.util
from transformers import AutoTokenizer

sp = importlib.util.spec_from_file_location("a", "/opt/nivult/engine/scripts/ancoraggio.py")
A = importlib.util.module_from_spec(sp); sp.loader.exec_module(A)

MOD = os.environ.get("MODELLO_TEC", "/opt/nivult/gpu/tec-v1-ck00500")
MAXLEN = int(os.environ.get("MAXLEN", "1024"))
tok = AutoTokenizer.from_pretrained(MOD)

mano, fam = {}, {}
for p in sorted(glob.glob("/opt/nivult/golden-tec-famiglie/etichette-*.json")):
    j = json.load(open(p))
    for o in j["offerte"]:
        mano[o["id"]] = [t["nome"] for t in o["tecnologie"]]
        fam[o["id"]] = j["famiglia"]

dentro = fuori = 0
persi_fuori = []
tronc = tot = 0
for linea in gzip.open("/opt/nivult/banco-famiglie.jsonl.gz", "rt"):
    d = json.loads(linea)
    if d["id"] not in mano: continue
    tot += 1
    testo = f"{d.get('title') or ''}\n{A.pulito(d.get('text'))}"
    ids = tok(testo, add_special_tokens=False)["input_ids"]
    if len(ids) > MAXLEN: tronc += 1
    visto = tok.decode(ids[:MAXLEN], skip_special_tokens=True)
    for n in mano[d["id"]]:
        if A.filtra(visto, [n])[0]: dentro += 1
        else:
            fuori += 1
            persi_fuori.append((fam[d["id"]], n))

print(f"=== {tot} annunci del golden, finestra {MAXLEN} token")
print(f"  troncati dalla finestra: {tronc} ({tronc*100/max(tot,1):.0f}%)")
print(f"\n  tecnologie etichettate a mano: {dentro+fuori}")
print(f"     {dentro:>3}  il modello le PUO' vedere")
print(f"     {fuori:>3}  stanno OLTRE la finestra: darle per perse e' colpa del taglio")
if persi_fuori:
    print("\n  quali:")
    for f, n in sorted(persi_fuori): print(f"     {f:<22} {n}")
