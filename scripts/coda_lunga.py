"""Il numero che vale per il prodotto: la testa prende la CODA LUNGA?

Sapere che un'azienda usa Excel non si vende: lo usano tutte. Il valore di un
dato tecnografico sta nei nomi rari — Tigsvetsning, ORSY, Omnissa Horizon,
Import Sets — perche' sono quelli che identificano davvero cosa fa un'azienda.

Un modello puo' avere F1 76% prendendo solo i nomi comuni e perdendo tutta la
coda: l'F1 e' micro-medio sulle menzioni, e le menzioni sono dominate da Excel,
Python, SAP. Per il prodotto sarebbe un modello inutile con un bel voto.

Qui il richiamo si guarda diviso: nomi COMUNI (visti spesso nel dataset) e nomi
RARI. Il cancello dichiarato a settembre e mai verificato: sopra il 40% sulla
coda lunga il prodotto e' credibile, sotto il 20% la strada e' chiusa.
"""
from __future__ import annotations
import collections
import glob
import gzip
import json
import os
import sys


def combacia_uno(x: str, b) -> bool:
    xl = x.lower()
    return any(xl in y.lower() or y.lower() in xl for y in b)


# quanto e' comune ogni nome, secondo il dataset di addestramento
freq: collections.Counter[str] = collections.Counter()
with gzip.open("/root/tecnologie/tecnologie-train.jsonl.gz", "rt") as f:
    for linea in f:
        for _, _, nome in json.loads(linea)["intervalli"]:
            freq[nome.lower()] += 1

mano = {}
for p in sorted(glob.glob("/root/golden-tec/etichette-*.json")):
    for o in json.load(open(p))["offerte"]:
        mano[o["id"]] = [t["nome"] for t in o["tecnologie"]]

SOGLIA_RARO = int(sys.argv[1]) if len(sys.argv) > 1 else 50
comuni = {n for n, v in freq.items() if v >= SOGLIA_RARO}

tot_c = sum(1 for v in mano.values() for n in v if n.lower() in comuni)
tot_r = sum(1 for v in mano.values() for n in v if n.lower() not in comuni)
print(f"i {sum(len(v) for v in mano.values())} nomi del golden, divisi a «visto "
      f"{SOGLIA_RARO}+ volte nel dataset»:")
print(f"  comuni: {tot_c}   rari (la coda lunga): {tot_r}\n")

print(f"{'checkpoint':<16}{'richiamo comuni':>17}{'richiamo CODA LUNGA':>22}{'nomi coda presi':>18}")
for p in sorted(glob.glob("/root/esame-corto-*.json")):
    u = {r["id"]: r["trovati"] for r in json.load(open(p))["dettaglio"]}
    pc = pr = 0
    for i, veri in mano.items():
        trovati = u.get(i, [])
        for n in veri:
            if not combacia_uno(n, trovati):
                continue
            if n.lower() in comuni:
                pc += 1
            else:
                pr += 1
    nome = os.path.basename(p).replace("esame-", "").replace(".json", "")
    print(f"{nome:<16}{100*pc/max(tot_c,1):>16.1f}%{100*pr/max(tot_r,1):>21.1f}%{pr:>14} / {tot_r}")

print("\ncancello dichiarato il 17/09: sopra il 40% sulla coda lunga il prodotto "
      "e' credibile, sotto il 20% la strada e' chiusa.")
