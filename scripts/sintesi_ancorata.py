"""Una cifra che nell'annuncio non c'e' non esce dalla sintesi.

mT5 inventa salari: nel 13,2% delle sue sintesi c'e' una cifra di paga che in
tutto il raw non esiste. A volte e' un numero vero con le cifre scambiate
(l'annuncio dice $116.975, la sintesi $116.775), a volte e' un range tondo
tirato fuori dal nulla perche' gli annunci americani di solito ne hanno uno.
Il 2B, sullo stesso metro e sullo stesso tipo di campione, sta allo 0,3%: non
e' un difetto dei generativi, e' di questo modello.

La regola e' quella gia' usata per le tecnologie: **si tiene solo cio' che nel
testo si puo' puntare col dito.** Qui l'unita' non e' la parola ma la FRASE:
un numero sbagliato avvelena la frase che lo contiene, non tutta la sintesi.

  1. si spezza la sintesi in frasi;
  2. una frase con un numero non ancorato si butta;
  3. se quel che resta e' sotto le 20 parole, la sintesi non vale piu' niente
     e si butta tutta (stessa soglia che il demone usa per accettarla).

Quali numeri si controllano, e perche' non tutti:
  - meno di tre cifre: «3 anni di esperienza», «2 turni». Troppo comuni, e
    l'annuncio li scrive spesso in lettere: darebbero solo falsi allarmi.
  - gli anni fra 2019 e 2030: idem, e non sono un fatto che il cliente compra.
  - tutto il resto si' — salari in testa, ma anche codici, metrature, distanze.

Il confronto avviene a sole cifre («90,000», «90.000» e «90 000» sono lo stesso
numero) e contro il RAW INTERO, non solo contro il campo che il modello legge:
se la cifra sta in un campo che il modello non ha visto, ha tirato a indovinare
ma non ha mentito, e non c'e' motivo di buttare la frase.
"""
from __future__ import annotations
import re

# I numeri si confrontano a sole cifre: i separatori cambiano da un paese all'altro
_NON_CIFRA = re.compile(r"\D")
# un numero: gruppi di cifre eventualmente separati da . , o spazio stretto
_NUMERO = re.compile(r"\d[\d.,   ]*\d|\d")
# fine frase: punto/!/? seguito da spazio e maiuscola, o fine testo
_FRASE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-ÖØ-Þ0-9])")

MIN_PAROLE = 20


def solo_cifre(t: str) -> str:
    return _NON_CIFRA.sub("", t)


def _da_controllare(cifre: str) -> bool:
    if len(cifre) < 3:
        return False
    if cifre.isdigit() and 2019 <= int(cifre) <= 2030:
        return False
    return True


def numeri_fuori(frase: str, fonte_cifre: str) -> list[str]:
    """I numeri della frase che nella fonte non compaiono."""
    fuori = []
    for m in _NUMERO.finditer(frase):
        c = solo_cifre(m.group(0))
        if _da_controllare(c) and c not in fonte_cifre:
            fuori.append(m.group(0).strip())
    return fuori


def ripulisci(sintesi: str, fonte: str) -> tuple[str | None, int, list[str]]:
    """Torna (sintesi ripulita o None, frasi tolte, numeri buttati).

    None vuol dire: quel che resta non e' piu' una sintesi. Meglio nessuna
    sintesi che una sintesi con dentro un salario inventato — il cliente su
    quella cifra ci costruisce una decisione.
    """
    sintesi = (sintesi or "").strip()
    if not sintesi:
        return None, 0, []
    fonte_cifre = solo_cifre(fonte or "")

    tenute, tolte, buttati = [], 0, []
    for frase in _FRASE.split(sintesi):
        fuori = numeri_fuori(frase, fonte_cifre)
        if fuori:
            tolte += 1
            buttati.extend(fuori)
        else:
            tenute.append(frase)

    if not tolte:
        return sintesi, 0, []
    pulita = " ".join(tenute).strip()
    if len(pulita.split()) < MIN_PAROLE:
        return None, tolte, buttati
    return pulita, tolte, buttati
