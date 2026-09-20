"""Il taglio testa+coda salva i salari, ma cosa toglie?

Spostare token dalla testa alla coda recupera le condizioni e perde il centro
dell'annuncio, dove stanno i requisiti. Il guadagno l'ho gia' misurato (le
cifre di paga viste passano dall'82% al 91%). Qui misuro la perdita, sulla
stessa scala e sulle stesse righe, cosi' le due cose si possono confrontare.

LA MISURA: per ogni coppia si prendono le parole di contenuto della sintesi
del maestro (dalla quinta lettera in su, cosi' si saltano articoli e
preposizioni in tutte le lingue senza una lista per lingua) e si guarda quante
di quelle parole compaiono nella finestra. E' una copertura, non una qualita':
dice quanta parte della sintesi il modello puo' ancora giustificare guardando
cio' che vede. Se scende, la finestra sta togliendo sostanza.

Si guardano SOLO gli annunci davvero troncati: sugli altri le finestre sono
identiche e includerli annacquerebbe la differenza fino a farla sparire.
"""
from __future__ import annotations
import gzip
import json
import os
import re
import sys
import unicodedata

from transformers import AutoTokenizer

TRAIN = os.environ.get("TRAIN_MT5", "/opt/nivult/sintesi/sintesi-train.jsonl.gz")
BASE = os.environ.get("BASE", "/opt/nivult/mt5")
MAX_IN = int(os.environ.get("MAX_IN", "1024"))
QUANTE = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
PUNTINI = " […] "


def parole(t: str) -> set[str]:
    t = unicodedata.normalize("NFKD", (t or "").lower())
    return {p for p in re.findall(r"[a-zà-öø-ÿ]{5,}", t)}


def taglia(tok, ids, testa: int, coda: int) -> str:
    if len(ids) <= testa + coda:
        return tok.decode(ids, skip_special_tokens=True)
    a = tok.decode(ids[:testa], skip_special_tokens=True)
    if coda == 0:
        return a
    return a + PUNTINI + tok.decode(ids[-coda:], skip_special_tokens=True)


def main() -> None:
    tok = AutoTokenizer.from_pretrained(BASE)
    forme = [("solo testa 1024", MAX_IN, 0),
             ("896 + 128", MAX_IN - 128, 128),
             ("768 + 256", MAX_IN - 256, 256),
             ("640 + 384", MAX_IN - 384, 384)]
    somma = {n: 0.0 for n, _, _ in forme}
    n_tronc = letti = 0

    with gzip.open(TRAIN, "rt") as f:
        for linea in f:
            if letti >= QUANTE:
                break
            try:
                m = json.loads(linea)["messages"]
            except Exception:                                      # noqa: BLE001
                continue
            letti += 1
            ids = tok(m[1]["content"], add_special_tokens=False)["input_ids"]
            if len(ids) <= MAX_IN:
                continue                       # non troncato: le finestre coincidono
            pv = parole(m[2]["content"])
            if not pv:
                continue
            n_tronc += 1
            for nome, t, c in forme:
                somma[nome] += len(pv & parole(taglia(tok, ids, t, c))) / len(pv)

    print(f"=== {letti} coppie lette, {n_tronc} davvero troncate\n")
    print(f"  {'forma del taglio':<20} {'parole della sintesi coperte':>30}")
    base = somma[forme[0][0]] / max(n_tronc, 1)
    for nome, _, _ in forme:
        v = somma[nome] / max(n_tronc, 1)
        d = (v - base) * 100
        print(f"  {nome:<20} {v * 100:>24.1f}%   {d:+.1f} punti")


if __name__ == "__main__":
    main()
