"""Perche' mT5 inventa i salari: la colpa e' nelle coppie di addestramento?

L'IPOTESI. Il maestro (DeepSeek) ha scritto la sintesi leggendo l'annuncio
INTERO. mT5 in addestramento ne vede solo i primi 1024 token. Quando il salario
sta nella coda tagliata, la coppia insegna alla lettera: «ingresso senza cifra
-> uscita con cifra». Ripetuto migliaia di volte, il modello impara che una
sintesi di annuncio americano contiene un range di paga, e quando non lo trova
se lo inventa.

Se l'ipotesi e' giusta il modello non e' rotto: sta riproducendo fedelmente
cio' che gli e' stato mostrato, e la cura sta nei dati, non nei pesi.

COME SI MISURA, senza ambiguita':
  per ogni coppia (ingresso, sintesi del maestro)
    - si taglia l'ingresso a 1024 token con lo STESSO tokenizzatore
      dell'addestramento (non a caratteri: il taglio vero e' in token);
    - si guarda se la sintesi contiene una cifra che nel troncato non c'e';
    - e si distingue il caso in cui quella cifra stia nell'annuncio INTERO
      (allora e' il taglio ad averla tolta: colpa nostra) dal caso in cui non
      ci sia da nessuna parte (allora era il maestro a inventare).

Tre numeri alla fine, e ciascuno indica una cura diversa:
  A  la cifra c'e' nel troncato                -> coppia sana
  B  c'e' solo nella coda tagliata             -> il TAGLIO insegna a inventare
  C  non c'e' da nessuna parte                 -> il MAESTRO insegna a inventare
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
QUANTE = int(sys.argv[1]) if len(sys.argv) > 1 else 3000

NUMERO = re.compile(r"\d[\d.,  ]*\d|\d")
PAGA = re.compile(r"(?i)[$€£¥]|\b(salary|salaire|stipendio|sueldo|wage|wages|pay|paid|"
                  r"compensation|hourly|per hour|annual|annually|yearly|range|usd|eur|gbp|"
                  r"sek|retribuzione|verg[uü]tung|gehalt|lohn|l[oö]n)")


def solo_cifre(t: str) -> str:
    return re.sub(r"\D", "", t)


def numeri(s: str, solo_paga: bool) -> list[str]:
    fuori = []
    for m in NUMERO.finditer(s):
        c = solo_cifre(m.group(0))
        if len(c) < 3 or (c.isdigit() and 2019 <= int(c) <= 2030):
            continue
        if solo_paga and not PAGA.search(s[max(0, m.start() - 40):m.end() + 40]):
            continue
        fuori.append(c)
    return fuori


def main() -> None:
    tok = AutoTokenizer.from_pretrained(BASE)
    st = {k: 0 for k in ("righe", "A", "B", "C", "righe_B", "righe_C", "troncati")}
    esempi = []
    with gzip.open(TRAIN, "rt") as f:
        for linea in f:
            if st["righe"] >= QUANTE:
                break
            try:
                m = json.loads(linea)["messages"]
            except Exception:                                      # noqa: BLE001
                continue
            intero = m[1]["content"]
            sintesi = m[2]["content"]
            st["righe"] += 1

            ids = tok(intero, truncation=True, max_length=MAX_IN)["input_ids"]
            troncato = tok.decode(ids, skip_special_tokens=True)
            st["troncati"] += len(tok(intero)["input_ids"]) > MAX_IN

            c_tronc = solo_cifre(troncato)
            c_intero = solo_cifre(intero)
            b = c = 0
            for n in numeri(sintesi, solo_paga=True):
                if n in c_tronc:
                    st["A"] += 1
                elif n in c_intero:
                    st["B"] += 1
                    b += 1
                else:
                    st["C"] += 1
                    c += 1
            st["righe_B"] += bool(b)
            st["righe_C"] += bool(c)
            if (b or c) and len(esempi) < 6:
                esempi.append((("TAGLIO" if b else "MAESTRO"), sintesi[:150]))

    n = st["righe"] or 1
    tot = st["A"] + st["B"] + st["C"] or 1
    print(f"=== {n} coppie di addestramento  (troncate a {MAX_IN} token: "
          f"{st['troncati'] * 100 / n:.1f}%)\n")
    print(f"  cifre di paga nelle sintesi del maestro: {tot}")
    print(f"     {st['A'] * 100 / tot:5.1f}%  A  ci sono anche nel troncato — coppia sana")
    print(f"     {st['B'] * 100 / tot:5.1f}%  B  solo nella coda tagliata — E' IL TAGLIO")
    print(f"     {st['C'] * 100 / tot:5.1f}%  C  in nessun posto — e' il MAESTRO")
    print(f"\n  {st['righe_B'] * 100 / n:5.1f}% delle coppie insegna a inventare per colpa del taglio")
    print(f"  {st['righe_C'] * 100 / n:5.1f}% delle coppie insegna a inventare per colpa del maestro")
    print("\n--- esempi:")
    for k, s in esempi:
        print(f"   [{k}] {s}")


if __name__ == "__main__":
    main()
