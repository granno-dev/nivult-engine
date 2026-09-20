"""Tagliare in testa butta le condizioni. Tagliare in testa E in coda le salva?

Un annuncio mette prima il ruolo e alla fine le condizioni — orario, contratto,
paga. Prendendo i primi 1024 token si perde proprio la coda, ed e' li' che sta
il salario: il 16,1% delle cifre nelle sintesi del maestro e' in quella coda.

L'alternativa non costa un token in piu': invece di «i primi 1024» si prende
«i primi N + gli ultimi 1024-N», con i puntini in mezzo. Stessa memoria, stessa
velocita', ma la coda torna visibile.

Qui si misura quanto si recupera, per tre tagli diversi, prima di cambiare
qualcosa. Il numero che conta e' B: le cifre che il maestro ha scritto e che il
modello NON puo' vedere. Piu' B scende, meno il dataset insegna a inventare —
e meno informazione vera si butta via.
"""
from __future__ import annotations
import gzip
import json
import os
import re
import sys

from transformers import AutoTokenizer

TRAIN = os.environ.get("TRAIN_MT5", "/opt/nivult/sintesi/sintesi-train.jsonl.gz")
BASE = os.environ.get("BASE", "/opt/nivult/mt5")
MAX_IN = int(os.environ.get("MAX_IN", "1024"))
QUANTE = int(sys.argv[1]) if len(sys.argv) > 1 else 2000

NUMERO = re.compile(r"\d[\d.,  ]*\d|\d")
PAGA = re.compile(r"(?i)[$€£¥]|\b(salary|salaire|stipendio|sueldo|wage|wages|pay|paid|"
                  r"compensation|hourly|per hour|annual|annually|yearly|range|usd|eur|gbp|"
                  r"sek|retribuzione|verg[uü]tung|gehalt|lohn|l[oö]n)")
PUNTINI = " […] "


def solo_cifre(t: str) -> str:
    return re.sub(r"\D", "", t)


def cifre_paga(s: str) -> list[str]:
    fuori = []
    for m in NUMERO.finditer(s):
        c = solo_cifre(m.group(0))
        if len(c) < 3 or (c.isdigit() and 2019 <= int(c) <= 2030):
            continue
        if PAGA.search(s[max(0, m.start() - 40):m.end() + 40]):
            fuori.append(c)
    return fuori


def taglia(tok, testo: str, testa: int, coda: int) -> str:
    """Primi `testa` token + ultimi `coda` token, con i puntini in mezzo."""
    ids = tok(testo, add_special_tokens=False)["input_ids"]
    if len(ids) <= testa + coda:
        return testo
    a = tok.decode(ids[:testa], skip_special_tokens=True)
    # `ids[-0:]` in Python e' TUTTA la lista, non la lista vuota: senza questo
    # controllo la riga di riferimento («solo testa») leggeva l'annuncio intero
    # e dichiarava zero cifre perse. Sbagliato il 20/09/2026, visto rileggendo.
    if coda == 0:
        return a
    b = tok.decode(ids[-coda:], skip_special_tokens=True)
    return a + PUNTINI + b


def main() -> None:
    tok = AutoTokenizer.from_pretrained(BASE)
    # (nome, testa, coda) — tutti costano gli stessi MAX_IN token
    forme = [("solo testa 1024", MAX_IN, 0),
             ("896 + 128", MAX_IN - 128, 128),
             ("768 + 256", MAX_IN - 256, 256),
             ("640 + 384", MAX_IN - 384, 384)]
    st = {nome: {"A": 0, "B": 0} for nome, _, _ in forme}
    tot_c = righe = 0

    with gzip.open(TRAIN, "rt") as f:
        for linea in f:
            if righe >= QUANTE:
                break
            try:
                m = json.loads(linea)["messages"]
            except Exception:                                      # noqa: BLE001
                continue
            righe += 1
            intero, sintesi = m[1]["content"], m[2]["content"]
            c_intero = solo_cifre(intero)
            voci = cifre_paga(sintesi)
            if not voci:
                continue
            viste = {nome: solo_cifre(taglia(tok, intero, t, c)) for nome, t, c in forme}
            for n in voci:
                if n not in c_intero:
                    tot_c += 1                     # il maestro l'ha inventata: nessun taglio aiuta
                    continue
                for nome in viste:
                    st[nome]["A" if n in viste[nome] else "B"] += 1

    print(f"=== {righe} coppie\n")
    print(f"  cifre inventate dal MAESTRO (nessun taglio le recupera): {tot_c}\n")
    print(f"  {'forma del taglio':<20} {'viste':>8} {'non viste':>10} {'non viste':>11}")
    for nome, _, _ in forme:
        a, b = st[nome]["A"], st[nome]["B"]
        t = a + b or 1
        print(f"  {nome:<20} {a:>8} {b:>10} {b * 100 / t:>10.1f}%")


if __name__ == "__main__":
    main()
