"""Il confronto appaiato: due modelli sulle STESSE righe, riga per riga.

Gli intervalli separati sono troppo deboli: due modelli possono avere intervalli
larghi e sovrapposti e nondimeno uno essere sempre migliore dell'altro, riga per
riga. Il ricampionamento appaiato guarda la DIFFERENZA sulla stessa riga, e la
parte di rumore che entrambi condividono (una riga difficile e' difficile per
tutti) si cancella.

Se l'intervallo della differenza non contiene lo zero, la differenza e' reale.

Lezione gia' pagata: [[giudizio-confronto-appaiato]] — «due medie su campioni
diversi mentono».
"""
from __future__ import annotations
import glob
import json
import os
import random
import sys


def combacia(a, b) -> int:
    bb = [y.lower() for y in b]
    return sum(1 for x in (s.lower() for s in a) if any(x in y or y in x for y in bb))


def per_riga(trovati, veri):
    return (combacia(trovati, veri), combacia(veri, trovati), len(trovati), len(veri))


def f1_da(righe) -> float:
    P = nP = R = nR = 0
    for c, c2, nt, nv in righe:
        P += c; nP += nt; R += c2; nR += nv
    prec = 100 * P / max(nP, 1)
    ric = 100 * R / max(nR, 1)
    return 2 * prec * ric / max(prec + ric, 1e-9)


# il vero, per id
mano = {}
for p in sorted(glob.glob("/root/golden-tec/etichette-*.json")):
    for o in json.load(open(p))["offerte"]:
        mano[o["id"]] = [t["nome"] for t in o["tecnologie"]]

modelli: dict[str, dict[str, list[str]]] = {}
for p in sorted(glob.glob("/root/esame-corto-*.json")):
    d = json.load(open(p))
    nome = os.path.basename(p).replace("esame-", "").replace(".json", "")
    modelli[nome] = {r["id"]: r["trovati"] for r in d["dettaglio"]}

GIRI = 2000
rnd = random.Random(7)
ids = sorted(mano)

# l'F1 di ciascuno, e poi le differenze appaiate contro il migliore
punteggi = {}
for nome, u in modelli.items():
    righe = [per_riga(u.get(i, []), mano[i]) for i in ids
             if u.get(i) or mano[i]]
    punteggi[nome] = f1_da(righe)

ordinati = sorted(punteggi.items(), key=lambda x: -x[1])
campione = ordinati[0][0]
print(f"migliore: {campione} con F1 {punteggi[campione]:.1f}%\n")
print(f"{'sfidante':<16}{'F1':>7}{'differenza':>12}{'intervallo 90% della differenza':>34}{'':>3}verdetto")

for nome, f1 in ordinati[1:]:
    diffs = []
    for _ in range(GIRI):
        scelti = [ids[rnd.randrange(len(ids))] for _ in ids]
        ra = [per_riga(modelli[campione].get(i, []), mano[i]) for i in scelti
              if modelli[campione].get(i) or mano[i]]
        rb = [per_riga(modelli[nome].get(i, []), mano[i]) for i in scelti
              if modelli[nome].get(i) or mano[i]]
        diffs.append(f1_da(ra) - f1_da(rb))
    diffs.sort()
    lo, hi = diffs[int(GIRI * 0.05)], diffs[int(GIRI * 0.95)]
    reale = "DIVERSI" if lo > 0 else "indistinguibili"
    print(f"{nome:<16}{f1:>6.1f}%{punteggi[campione]-f1:>11.1f}"
          f"{f'{lo:+.1f} .. {hi:+.1f}':>34}   {reale}")
