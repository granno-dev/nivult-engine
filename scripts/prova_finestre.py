"""Il banco della lettura a pezzi: si prova prima di metterla in produzione.

Tre cose da dimostrare, e nessuna si puo' dare per scontata:
  1. nessun carattere del testo resta fuori da tutte le finestre;
  2. un nome a cavallo di un confine compare INTERO in almeno una finestra;
  3. un testo che ci sta tutto torna una finestra sola, non due.
"""
from __future__ import annotations
import os
import sys

from transformers import AutoTokenizer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from finestre import finestre, finestre_span                        # noqa: E402

MOD = os.environ.get("MODELLO_TEC", "/opt/nivult/gpu/tec-v1-ck00500")
tok = AutoTokenizer.from_pretrained(MOD)


def copre(lunghezza: int, span: list[tuple[int, int]]) -> bool:
    """Ogni carattere sta in almeno una finestra? Si guardano gli ESTREMI.

    Cercare le sottostringhe non funziona su un annuncio che ripete le stesse
    frasi: `find` riaggancia la prima occorrenza e il banco dichiara buchi che
    non ci sono.
    """
    coperto = [False] * lunghezza
    for a, b in span:
        for k in range(a, min(b, lunghezza)):
            coperto[k] = True
    return all(coperto)


def main() -> int:
    guai = 0

    # 1. testo corto: una finestra sola
    corto = "Cercasi saldatore con esperienza di saldatura MIG e uso della smerigliatrice."
    p = finestre(tok, corto, 1024)
    print(f"corto ({len(corto)} car): {len(p)} finestra/e  "
          f"{'ok' if len(p) == 1 and p[0] == corto else 'SBAGLIATO'}")
    guai += not (len(p) == 1 and p[0] == corto)

    # 2. testo lungo: copertura totale
    lungo = ("Requisiti e mansioni del candidato per la posizione aperta. " * 400
             + " Si richiede la conoscenza di Brandmeldeanlagen e di NFPA 72.")
    for maxlen in (128, 256, 1024):
        sp = finestre_span(tok, lungo, maxlen)
        p = finestre(tok, lungo, maxlen)
        ok_cop = copre(len(lungo), sp)
        # 3. il nome in fondo compare intero in almeno un pezzo?
        ok_nome = any("Brandmeldeanlagen" in x for x in p) and any("NFPA 72" in x for x in p)
        lung_max = max((len(tok(x, add_special_tokens=False)["input_ids"]) for x in p),
                       default=0)
        print(f"lungo, finestra {maxlen:>4}: {len(p):>3} pezzi   "
              f"copertura {'ok' if ok_cop else 'BUCHI'}   "
              f"nomi in fondo {'ok' if ok_nome else 'PERSI'}   "
              f"pezzo piu' lungo {lung_max} token "
              f"{'ok' if lung_max <= maxlen else 'SFORA'}")
        guai += (not ok_cop) + (not ok_nome) + (lung_max > maxlen)

    # 4. un nome esattamente a cavallo del confine
    a_meta = "x " * 500 + "Pick-by-Voice " + "y " * 500
    p = finestre(tok, a_meta, 256)
    intero = any("Pick-by-Voice" in x for x in p)
    print(f"nome a cavallo del confine: {len(p)} pezzi   "
          f"{'ok, compare intero' if intero else 'SPEZZATO'}")
    guai += not intero

    print("\n" + ("tutto a posto" if not guai else f"{guai} controlli falliti"))
    return 1 if guai else 0


if __name__ == "__main__":
    sys.exit(main())
