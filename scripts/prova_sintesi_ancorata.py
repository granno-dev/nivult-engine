"""Quanto costa il filtro, prima di applicarlo.

Un filtro che toglie le bugie togliendo meta' del prodotto non e' una cura.
Qui si misura, sullo stesso campione, cosa resta: quante sintesi passano
intatte, quante perdono una frase, quante muoiono — e soprattutto se dopo il
filtro le cifre inventate sono davvero zero.
"""
from __future__ import annotations
import json
import re
import sys

sys.path.insert(0, "/opt/nivult/engine/scripts")
from sintesi_ancorata import ripulisci, numeri_fuori, solo_cifre   # noqa: E402

PERCORSO = sys.argv[1] if len(sys.argv) > 1 else "/tmp/campione-raw.jsonl"
_TAG = re.compile(r"<[^>]+>")

st = dict.fromkeys(("righe", "intatte", "accorciate", "morte", "residuo", "parole_prima", "parole_dopo"), 0)
esempi = []
for r in open(PERCORSO, encoding="utf-8"):
    r = r.strip()
    if not r:
        continue
    d = json.loads(r)
    st["righe"] += 1
    fonte = " ".join((d.get("titolo") or "", d.get("luogo") or "",
                      _TAG.sub(" ", d.get("raw_intero") or "")))
    s = d["sintesi"]
    pulita, tolte, buttati = ripulisci(s, fonte)
    st["parole_prima"] += len(s.split())
    if pulita is None:
        st["morte"] += 1
        if len(esempi) < 5:
            esempi.append(f"MORTA  buttati={buttati[:3]}\n       {s[:150]}")
    else:
        st["parole_dopo"] += len(pulita.split())
        if tolte:
            st["accorciate"] += 1
            if len(esempi) < 10:
                esempi.append(f"ACCORCIATA -{tolte} frase  buttati={buttati[:3]}\n"
                              f"       prima: {s[:120]}\n       dopo:  {pulita[:120]}")
        else:
            st["intatte"] += 1
        if numeri_fuori(pulita, solo_cifre(fonte)):
            st["residuo"] += 1

n = st["righe"] or 1
print(f"=== {n} sintesi mT5 passate al filtro\n")
print(f"  {st['intatte']*100/n:5.1f}%  ({st['intatte']:5d})  intatte, nessun numero da togliere")
print(f"  {st['accorciate']*100/n:5.1f}%  ({st['accorciate']:5d})  perdono una frase e restano valide")
print(f"  {st['morte']*100/n:5.1f}%  ({st['morte']:5d})  non restano sopra le 20 parole: buttate")
vive = st["intatte"] + st["accorciate"]
print(f"\n  restano {vive} sintesi su {n} ({vive*100/n:.1f}%)")
if vive:
    print(f"  lunghezza media: {st['parole_prima']/n:.0f} parole prima, {st['parole_dopo']/vive:.0f} dopo")
print(f"  cifre inventate rimaste: {st['residuo']}  (deve essere 0)")
print("\n--- esempi:")
for e in esempi:
    print("   " + e)
