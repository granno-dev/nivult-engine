"""Toglie dal dataset di mT5 le coppie che insegnano a inventare.

LA CAUSA, misurata il 20/09/2026 su 3.000 coppie. Il maestro (DeepSeek) ha
scritto la sintesi leggendo l'annuncio INTERO; mT5 in addestramento ne vede i
primi 1024 token, e il 34,3% degli annunci viene troncato. Delle cifre di paga
che il maestro scrive:

  69,3%  stanno anche nel troncato          -> coppia sana
  16,1%  stanno solo nella coda tagliata    -> il TAGLIO insegna a inventare
  14,5%  non stanno da nessuna parte        -> il MAESTRO insegna a inventare

Il 7,8% delle coppie contiene quindi una lezione precisa: «ingresso senza
cifra -> uscita con cifra». Ripetuta migliaia di volte, il modello impara che
la sintesi di un annuncio americano contiene un range di paga, e quando non lo
trova se lo inventa. In produzione: 13,2%. Il modello non e' rotto — sta
riproducendo fedelmente cio' che gli abbiamo mostrato.

LA CURA. Si applica al BERSAGLIO lo stesso filtro che gia' gira in produzione
sulle uscite (`sintesi_ancorata.ripulisci`), ma con per fonte **cio' che il
modello vedra' davvero**: l'annuncio troncato agli stessi 1024 token, con lo
stesso tokenizzatore. Cosi' una sola regola copre tutte e due le cause — non
importa se la cifra manca perche' l'abbiamo tagliata o perche' il maestro l'ha
inventata: se il modello non puo' vederla, non deve impararla.

E' il metodo pubblicato in «Entity-level Factual Consistency of Abstractive
Text Summarization» (EACL 2021, arXiv 2102.09130): filtrare le coppie il cui
riassunto di riferimento contiene entita' assenti dalla fonte riduce le
allucinazioni. Qui si filtra sui NUMERI, che nel nostro prodotto sono la cosa
che fa danno.

PERCHE' NON SI ALLARGA LA FINESTRA, ne' si taglia in testa e in coda. Misurato
sulle stesse righe: 640 testa + 384 coda porta le cifre di paga non viste dal
17,8% all'8,8%, ma la copertura delle parole della sintesi scende di 5,5 punti
— si recupera il salario pagando il centro dell'annuncio, dove stanno i
requisiti. Si comprerebbe meno invenzione di numeri rischiandone altre. Il
filtro sui bersagli risolve il problema senza pagare niente.

  BASE=/opt/nivult/mt5 python pulisci_dataset_sintesi.py \\
      --ingresso sintesi-train.jsonl.gz --uscita sintesi-train-pulito.jsonl.gz
"""
from __future__ import annotations
import argparse
import gzip
import json
import os
import sys

from transformers import AutoTokenizer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sintesi_ancorata import ripulisci, numeri_fuori, solo_cifre   # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ingresso", default="/opt/nivult/sintesi/sintesi-train.jsonl.gz")
    ap.add_argument("--uscita", default="/opt/nivult/sintesi/sintesi-train-pulito.jsonl.gz")
    ap.add_argument("--base", default=os.environ.get("BASE", "/opt/nivult/mt5"))
    ap.add_argument("--max-in", type=int, default=1024,
                    help="deve essere IDENTICO a --max-in dell'addestramento")
    ap.add_argument("--limite", type=int, default=0)
    a = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(a.base)
    st = dict.fromkeys(("lette", "intatte", "accorciate", "buttate", "troncate",
                        "residuo"), 0)

    with gzip.open(a.ingresso, "rt") as f, gzip.open(a.uscita, "wt") as g:
        for linea in f:
            if a.limite and st["lette"] >= a.limite:
                break
            try:
                d = json.loads(linea)
                m = d["messages"]
            except Exception:                                      # noqa: BLE001
                continue
            st["lette"] += 1
            intero, sintesi = m[1]["content"], m[2]["content"]

            ids = tok(intero, add_special_tokens=False)["input_ids"]
            if len(ids) > a.max_in:
                st["troncate"] += 1
                visto = tok.decode(ids[:a.max_in], skip_special_tokens=True)
            else:
                visto = intero

            pulita, tolte, _ = ripulisci(sintesi, visto)
            if pulita is None:
                st["buttate"] += 1
                continue
            if tolte:
                st["accorciate"] += 1
                m[2]["content"] = pulita
            else:
                st["intatte"] += 1
            # controprova sulla riga che sto per scrivere: se resta una cifra
            # non ancorata, il filtro non ha fatto il suo lavoro e voglio saperlo
            if numeri_fuori(m[2]["content"], solo_cifre(visto)):
                st["residuo"] += 1
            g.write(json.dumps(d, ensure_ascii=False) + "\n")

            if st["lette"] % 20000 == 0:
                print(f"  {st['lette']}...", flush=True)

    n = st["lette"] or 1
    tenute = st["intatte"] + st["accorciate"]
    print(f"\n=== {n} coppie lette  ({st['troncate'] * 100 / n:.1f}% troncate a {a.max_in} token)")
    print(f"  {st['intatte'] * 100 / n:5.1f}%  ({st['intatte']:6d})  intatte")
    print(f"  {st['accorciate'] * 100 / n:5.1f}%  ({st['accorciate']:6d})  perdono una frase")
    print(f"  {st['buttate'] * 100 / n:5.1f}%  ({st['buttate']:6d})  buttate: sotto le 20 parole")
    print(f"\n  scritte: {tenute} ({tenute * 100 / n:.1f}%)")
    print(f"  cifre non ancorate rimaste: {st['residuo']}  (deve essere 0)")


if __name__ == "__main__":
    main()
