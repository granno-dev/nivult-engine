"""I salari inventati: la classe che fa danno.

Un nome storpiato («Imaginin», «JaneUS») e' brutto. Un salario inventato e'
un'altra cosa: il cliente ci costruisce sopra una decisione, e il numero non
sta nell'annuncio. Qui si guarda solo quella classe, e si stampa l'annuncio
accanto alla sintesi perche' il numero vada letto, non creduto.

Un numero e' «da salario» se nella sintesi gli sta vicino un simbolo di valuta,
una parola di paga, o e' scritto come un range (X–Y). Il criterio e' largo
apposta: meglio leggere qualche falso allarme che perdere un caso vero.

Due correzioni al conto di prima, tutte e due a mio sfavore:
  - il genitivo sassone: «Marie Curie's» nella sintesi contro «Marie Curie»
    nell'annuncio non e' un'invenzione, e' una 's;
  - i separatori: «90,000» e «90.000» e «90 000» sono lo stesso numero, e
    l'annuncio puo' scriverlo in uno qualunque dei tre modi.
"""
from __future__ import annotations
import json
import re
import sys
import unicodedata

PERCORSO = sys.argv[1] if len(sys.argv) > 1 else "/tmp/campione-raw.jsonl"
_TAG = re.compile(r"<[^>]+>")
NUMERO = re.compile(r"\d[\d.,  ]*\d|\d")
PAGA = re.compile(r"(?i)[$€£¥]|\b(salary|salaris|salaire|stipendio|sueldo|wage|wages|pay|paid|"
                  r"compensation|hourly|per hour|annual|annually|yearly|range|usd|eur|gbp|sek|"
                  r"retribuzione|verg[uü]tung|gehalt|lohn|l[oö]n|k\b)")


def schiaccia(t: str) -> str:
    t = unicodedata.normalize("NFKD", t.lower())
    return "".join(c for c in t if c.isalnum())


def solo_cifre(t: str) -> str:
    return re.sub(r"\D", "", t)


def da_salario(sintesi: str, inizio: int, fine: int) -> bool:
    """C'e' un segno di paga entro 40 caratteri, prima o dopo?"""
    return bool(PAGA.search(sintesi[max(0, inizio - 40):fine + 40]))


def main() -> None:
    righe = 0
    con_salario_inventato = 0
    casi = []
    for r in open(PERCORSO, encoding="utf-8"):
        r = r.strip()
        if not r:
            continue
        d = json.loads(r)
        righe += 1
        s = d["sintesi"]
        corpo = _TAG.sub(" ", d.get("corpo") or "")
        letto_cifre = solo_cifre(" ".join((d.get("titolo") or "", corpo)))
        raw_cifre = solo_cifre(d.get("raw_intero") or "")

        inventati = []
        for m in NUMERO.finditer(s):
            g = m.group(0).strip()
            c = solo_cifre(g)
            if len(c) < 3:                      # 1-2 cifre: troppo comuni, rumore
                continue
            if c.isdigit() and 2019 <= int(c) <= 2030:
                continue
            if c in letto_cifre or c in raw_cifre:
                continue
            if da_salario(s, m.start(), m.end()):
                inventati.append(g)
        if inventati:
            con_salario_inventato += 1
            if len(casi) < 10:
                # la riga dell'annuncio dove si parla di paga, per confronto
                pezzi = [x.strip() for x in re.split(r"[\n.;]", corpo) if PAGA.search(x)][:3]
                casi.append((d["id"], inventati, s[:300], pezzi))

    print(f"=== {righe} sintesi\n")
    print(f"  {con_salario_inventato} ({con_salario_inventato*100/max(righe,1):.1f}%) "
          f"hanno una cifra di paga che in TUTTO il raw non c'e'\n")
    for i, (jid, inv, s, pezzi) in enumerate(casi, 1):
        print(f"--- {i}. {jid}   inventati: {inv}")
        print(f"    sintesi: {s}")
        if pezzi:
            for p in pezzi:
                print(f"    annuncio dice: {p[:150]}")
        else:
            print("    annuncio: nessuna riga che parli di paga")
        print()


if __name__ == "__main__":
    main()
