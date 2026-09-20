"""La differenza fra i checkpoint e' reale o e' rumore delle 200 righe?

La curva oscilla fra 68,4% e 76,4% di F1. Con 200 annunci, una manciata di righe
che vanno in un verso o nell'altro sposta il numero di parecchio — e scegliere il
massimo di una curva rumorosa vuol dire scegliere il checkpoint piu' fortunato,
non il migliore.

Si misura col ricampionamento: si riestraggono 200 righe A CASO dalle 200 (con
ripetizioni), si ricalcola l'F1, mille volte. La larghezza di quella distribuzione
e' l'incertezza vera della misura. Se gli intervalli di due checkpoint si
sovrappongono, fra quei due non si puo' scegliere coi dati che abbiamo.

Non serve rieseguire i modelli: l'esame salva gia' `dettaglio` con i nomi trovati
e i nomi veri riga per riga.
"""
from __future__ import annotations
import glob
import json
import os
import random
import sys


def combacia(a: list[str], b: list[str]) -> int:
    bb = [y.lower() for y in b]
    return sum(1 for x in (s.lower() for s in a) if any(x in y or y in x for y in bb))


def f1_da(righe) -> float:
    P = nP = R = nR = 0
    for c, c2, nt, nv in righe:
        P += c; nP += nt; R += c2; nR += nv
    prec = 100 * P / max(nP, 1)
    ric = 100 * R / max(nR, 1)
    return 2 * prec * ric / max(prec + ric, 1e-9)


GIRI = 1000
rnd = random.Random(1)
print(f"{'checkpoint':<16}{'F1':>8}{'intervallo 90%':>22}{'larghezza':>12}")
risultati = {}
for p in sorted(glob.glob(sys.argv[1] if len(sys.argv) > 1 else "/root/esame-corto-*.json")):
    d = json.load(open(p))
    # precalcolo per riga: (combaciati trovati, combaciati veri, n trovati, n veri)
    righe = []
    for r in d["dettaglio"]:
        t, v = r["trovati"], r["veri"]
        if not t and not v:
            continue                       # d'accordo sul silenzio: non entra nell'F1
        righe.append((combacia(t, v), combacia(v, t), len(t), len(v)))
    vero = f1_da(righe)
    campioni = sorted(f1_da([righe[rnd.randrange(len(righe))] for _ in righe])
                      for _ in range(GIRI))
    lo, hi = campioni[int(GIRI * 0.05)], campioni[int(GIRI * 0.95)]
    nome = os.path.basename(p).replace("esame-", "").replace(".json", "")
    risultati[nome] = (vero, lo, hi)
    print(f"{nome:<16}{vero:>7.1f}%{f'{lo:.1f}% - {hi:.1f}%':>22}{hi-lo:>11.1f}")

if len(risultati) >= 2:
    ordinati = sorted(risultati.items(), key=lambda x: -x[1][0])
    primo, secondo = ordinati[0], ordinati[1]
    print(f"\nil migliore e' {primo[0]} ({primo[1][0]:.1f}%), il secondo {secondo[0]} "
          f"({secondo[1][0]:.1f}%)")
    if secondo[1][0] >= primo[1][1]:
        print("gli intervalli SI SOVRAPPONGONO: con 200 righe non si puo' dire "
              "quale dei due sia migliore.")
    else:
        print("il primo e' fuori dall'intervallo del secondo: la differenza regge.")
