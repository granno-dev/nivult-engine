"""Un'invenzione vera, o la mia estrazione che ha guardato il campo sbagliato?

Il primo giro ha trovato numeri e nomi fuori dal testo. Ma cercavo in UN campo
solo — quello che il demone da' da leggere al modello — e se il salario sta in
`payRange` invece che in `description`, il mio conto grida all'invenzione per
colpa mia.

Qui si distinguono tre casi, e il terzo e' l'unico grave:

  A  sta nel campo che il modello ha letto            → nessun difetto
  B  sta altrove nel raw (payRange, jobLocation...)   → colpa mia che misuro,
                                                        ma il modello NON l'ha
                                                        letto: lo ha indovinato
  C  non sta da nessuna parte nel raw                 → inventato, punto

B merita una parola: il modello non ha visto quel campo, quindi anche
azzeccandolo ha tirato a indovinare. Ma un salario azzeccato non fa danno al
cliente, un salario sbagliato si': per questo B e C si contano separati.
"""
from __future__ import annotations
import json
import re
import sys
import unicodedata

PERCORSO = sys.argv[1] if len(sys.argv) > 1 else "/tmp/campione-raw.jsonl"
_TAG = re.compile(r"<[^>]+>")
MAIUSCOLE_OVUNQUE = {"de", "sv", "da", "no", "nb", "nn", "lb"}
NUMERO = re.compile(r"\d[\d.,]*")
PAROLA = re.compile(r"\b[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ&.'-]{2,}")


def schiaccia(t: str) -> str:
    t = unicodedata.normalize("NFKD", t.lower())
    return "".join(c for c in t if c.isalnum())


def pezzi(sintesi: str) -> tuple[list[str], list[str]]:
    num = []
    for m in NUMERO.finditer(sintesi):
        g = m.group(0).rstrip(".,")
        secco = g.replace(".", "").replace(",", "")
        if not secco:
            continue
        n = int(secco) if secco.isdigit() else None
        if n is not None and (n < 13 or 2019 <= n <= 2030):
            continue
        num.append(g)
    inizi = {0} | {m.end() for m in re.finditer(r"[.!?:;]\s+", sintesi)}
    nom = [m.group(0).rstrip(".,") for m in PAROLA.finditer(sintesi) if m.start() not in inizi]
    return num, nom


def main() -> None:
    st = dict.fromkeys(("righe", "num_A", "num_B", "num_C", "nom_A", "nom_B", "nom_C",
                        "righe_num_C", "righe_nom_C", "ripassate"), 0)
    esempi: list[str] = []
    for r in open(PERCORSO, encoding="utf-8"):
        r = r.strip()
        if not r:
            continue
        d = json.loads(r)
        st["righe"] += 1
        st["ripassate"] += bool(d.get("ripassata"))
        letto = schiaccia(" ".join((d.get("titolo") or "", d.get("luogo") or "",
                                    d.get("piattaforma") or "", _TAG.sub(" ", d.get("corpo") or ""))))
        # il raw intero: tutti i campi, anche quelli che il modello non vede
        tutto = schiaccia(_TAG.sub(" ", d.get("raw_intero") or ""))

        num, nom = pezzi(d["sintesi"])
        gravi_num, gravi_nom = [], []
        for g in num:
            secco = g.replace(".", "").replace(",", "")
            dove = ("A" if (schiaccia(g) in letto or schiaccia(secco) in letto)
                    else "B" if (schiaccia(g) in tutto or schiaccia(secco) in tutto) else "C")
            st["num_" + dove] += 1
            if dove == "C":
                gravi_num.append(g)
        if d.get("lang") not in MAIUSCOLE_OVUNQUE:
            for p in nom:
                dove = "A" if schiaccia(p) in letto else "B" if schiaccia(p) in tutto else "C"
                st["nom_" + dove] += 1
                if dove == "C":
                    gravi_nom.append(p)
        st["righe_num_C"] += bool(gravi_num)
        st["righe_nom_C"] += bool(gravi_nom)
        if (gravi_num or gravi_nom) and len(esempi) < 14:
            esempi.append(f"[{d['id'][:8]}] numeri={gravi_num[:3]} nomi={gravi_nom[:3]}\n"
                          f"      {d['sintesi'][:140]}")

    n = st["righe"] or 1
    print(f"=== {n} sintesi, confrontate col raw INTERO ({st['ripassate']} ripassate)\n")
    for eti, ch in (("numeri", "num"), ("nomi propri", "nom")):
        tot = st[ch + "_A"] + st[ch + "_B"] + st[ch + "_C"] or 1
        print(f"  {eti}: {tot} in tutto")
        print(f"     {st[ch+'_A']*100/tot:5.1f}%  nel testo che il modello ha letto")
        print(f"     {st[ch+'_B']*100/tot:5.1f}%  altrove nel raw (indovinati, ma veri)")
        print(f"     {st[ch+'_C']*100/tot:5.1f}%  DA NESSUNA PARTE: inventati")
    print(f"\n  {st['righe_num_C']*100/n:5.1f}% delle sintesi ha almeno un numero inventato")
    print(f"  {st['righe_nom_C']*100/n:5.1f}% delle sintesi ha almeno un nome inventato")
    print("\n--- esempi da leggere a mano:")
    for e in esempi:
        print("   " + e)


if __name__ == "__main__":
    main()
