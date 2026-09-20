"""Un annuncio non si taglia: si legge a pezzi.

IL PROBLEMA. Un modello transformer ha una lunghezza massima d'ingresso, e non
e' una pigrizia: il costo dell'attenzione cresce col QUADRATO dei token.
Raddoppiare la finestra costa quattro volte. L'annuncio piu' lungo misurato in
produzione ha 11.885 token — darlo intero a un colpo solo riempie la memoria
della scheda e blocca la macchina, com'e' gia' successo due volte a settembre.

LA SOLUZIONE, per un modello che MARCA parole dentro il testo. Si spezza
l'annuncio in finestre che si sovrappongono, si passa il modello su ciascuna, e
si uniscono le marcature. Nessun carattere resta fuori, e non c'e' niente da
conciliare: una tecnologia trovata nel terzo pezzo vale quanto una trovata nel
primo.

Misurato il 20/09/2026 sulla testa tecnologie: a 1024 token il **22,5%** degli
annunci veniva letto a meta', e su quelli si perdeva in media il **27%** del
testo — la fine, dove i requisiti elencano gli strumenti.

PERCHE' LA SOVRAPPOSIZIONE. Senza, un nome a cavallo del confine verrebbe
spezzato in due e nessuno dei due pezzi lo riconoscerebbe. Con 64 token di
sovrapposizione qualunque nome ragionevole compare intero in almeno una
finestra.

PERCHE' SI TAGLIA SUI TOKEN E NON SUI CARATTERI. Quanti caratteri stiano in un
token cambia con la lingua: il tedesco ne mette meno dell'inglese, il polacco
ancora meno. Tagliando a caratteri si andrebbe lunghi su una lingua e corti su
un'altra. Qui si tokenizza una volta sola, si taglia sui token, e si
restituiscono SOTTOSTRINGHE del testo originale — cosi' chi legge le marcature
continua a lavorare sul testo vero, con i suoi offset.
"""
from __future__ import annotations

SOVRAPPOSIZIONE = 64


def finestre_span(tok, testo: str, max_len: int,
                  sovrapposizione: int = SOVRAPPOSIZIONE) -> list[tuple[int, int]]:
    """Gli ESTREMI delle finestre sul testo originale.

    Si restituiscono gli estremi e non le stringhe perche' cosi' il controllo
    della copertura e' esatto: cercare una sottostringa dentro un annuncio che
    ripete le stesse frasi riaggancia sempre la prima occorrenza, e il banco
    dichiara buchi che non esistono (visto il 20/09/2026).
    """
    testo = testo or ""
    if not testo:
        return []
    # due token li prendono i marcatori di inizio e fine che il tokenizzatore
    # aggiunge quando il testo viene davvero dato al modello
    utili = max(1, max_len - 2)
    enc = tok(testo, add_special_tokens=False, return_offsets_mapping=True)
    off = [o for o in enc["offset_mapping"] if o[1] > o[0]]
    if not off:
        return [(0, len(testo))]
    if len(off) <= utili:
        return [(0, len(testo))]

    passo = max(1, utili - sovrapposizione)
    fuori, i = [], 0
    while i < len(off):
        j = min(i + utili, len(off))
        # la PRIMA finestra parte da 0 e l'ULTIMA arriva in fondo: gli spazi e
        # la punteggiatura ai bordi non sono token, e senza questo restano fuori
        a = 0 if i == 0 else off[i][0]
        b = len(testo) if j >= len(off) else off[j - 1][1]
        fuori.append((a, b))
        if j >= len(off):
            break
        i += passo
    return fuori


def finestre(tok, testo: str, max_len: int,
             sovrapposizione: int = SOVRAPPOSIZIONE) -> list[str]:
    """Le finestre come sottostringhe del testo originale.

    Se il testo ci sta tutto, torna una lista di un elemento — cosi' il
    chiamante non ha due strade da tenere.
    """
    return [(testo or "")[a:b]
            for a, b in finestre_span(tok, testo, max_len, sovrapposizione)]
