"""La sintesi di un annuncio piu' lungo della finestra: a pezzi, senza tagliare.

IL PROBLEMA, misurato il 21/09/2026 su 63 annunci oltre i 2048 token: nel 24%
la coda che il modello non legge porta un fatto che il candidato vuole — paga
(7 annunci), orario (5), contratto (4), requisiti obbligatori (8). Il 16% delle
offerte attive supera la finestra: e' circa un annuncio su venticinque che
perde qualcosa.

LA SOLUZIONE. Il testo si spezza in finestre di token che si sovrappongono
(`finestre.py`, le stesse della testa tecnologie); ogni finestra, col suo
prefisso (titolo, sede, lingua), viene riassunta da sola. La prima sintesi e'
la spina dorsale: ruolo, azienda, responsabilita' stanno in testa. Dalle
sintesi delle finestre seguenti si tengono solo le frasi NUOVE — quelle che
non ripetono cio' che la spina dorsale dice gia' — perche' ogni pezzo riapre
con «L'azienda cerca un…» e quello lo abbiamo.

PERCHE' NON «sintesi delle sintesi». Un secondo passaggio del modello su un
testo fatto di sintesi sarebbe un ingresso che in addestramento non ha mai
visto; qui ogni frase e' uscita dal modello leggendo testo vero, e il filtro
d'ancoraggio (`sintesi_ancorata.ripulisci`) resta a valle, sull'annuncio
intero, come per tutte le altre.

L'unita' di misura e' la parola: `valida()` del demone accetta da 20 a 200
parole, e la sintesi a pezzi non deve superare il tetto.
"""
from __future__ import annotations
import re
import unicodedata
from collections.abc import Callable

_FRASE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÀ-ÖØ-Þ0-9«\"'])")
_PAROLA = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
TETTO_PAROLE = 190          # sotto il 200 di valida(), con margine
SOMIGLIANZA = 0.5           # sopra questa quota di parole in comune la frase e' un doppione


def _normalizza(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\n+", " ", (t or "").strip()))


def _parole(frase: str) -> set[str]:
    piatto = unicodedata.normalize("NFKD", frase.lower())
    return set(_PAROLA.findall(piatto))


def frasi(s: str) -> list[str]:
    return [f.strip() for f in _FRASE.split((s or "").strip()) if f.strip()]


def unisci(spina: str, altre: list[str]) -> tuple[str, int]:
    """La spina dorsale piu' le frasi nuove delle altre sintesi. Torna (testo, frasi aggiunte)."""
    tenute = frasi(spina)
    viste = [_parole(f) for f in tenute]
    parole = sum(len(f.split()) for f in tenute)
    aggiunte = 0
    for s in altre:
        for f in frasi(s):
            p = _parole(f)
            if len(p) < 3:
                continue
            doppione = any(len(p & v) / max(len(p), 1) >= SOMIGLIANZA for v in viste)
            if doppione:
                continue
            n = len(f.split())
            if parole + n > TETTO_PAROLE:
                return " ".join(tenute), aggiunte
            tenute.append(f)
            viste.append(p)
            parole += n
            aggiunte += 1
    return " ".join(tenute), aggiunte


def sintesi_a_pezzi(tok, prefisso: str, testo: str, max_in: int,
                    genera: Callable[[list[str]], list[str]],
                    finestre_span: Callable) -> tuple[str, int]:
    """Torna (sintesi, numero di pezzi). Un pezzo solo = la strada di sempre.

    `genera` prende i prompt gia' normalizzati e torna le sintesi: e' il chiamante
    (il demone, con la sua fiducia e il suo lotto) a decidere come farlo.
    """
    pieno = _normalizza(f"{prefisso}\n\n{testo}")
    n_pre = len(tok(_normalizza(prefisso), add_special_tokens=False)["input_ids"])
    utili = max_in - n_pre - 4                       # marcatori e i due a capo
    n = len(tok(pieno, add_special_tokens=False)["input_ids"])
    if n <= max_in - 2:
        return genera([pieno])[0], 1
    pezzi = [testo[a:b] for a, b in finestre_span(tok, testo, utili + 2)]
    prompts = [_normalizza(f"{prefisso}\n\n{p}") for p in pezzi]
    uscite = genera(prompts)
    unita, _ = unisci(uscite[0], uscite[1:])
    return unita, len(pezzi)
