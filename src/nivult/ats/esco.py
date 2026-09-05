"""Il riconoscitore ESCO: 13.485 competenze, 28 lingue, un solo passaggio.

Il salto di qualita' sul technographic: «saldatura», «Schweißen»,
«soudage» e «welding» diventano LA STESSA competenza, con l'etichetta
inglese canonica. TheirStack e' anglocentrico; noi diventiamo leggibili
in tutta Europa — che e' il nostro campo.

La tecnica: un automa Aho-Corasick su tutte le etichette (preferite +
alternative, tutte le lingue) — ~duecentomila stringhe cercate in UN
passaggio sul testo, non duecentomila regex. Guardie di qualita':
etichette da 4 a 60 caratteri, confini di parola verificati attorno a
ogni riscontro, e per ogni competenza esce UNA voce canonica (inglese)
per quante lingue la menzionino.

L'automa si costruisce pigramente al primo uso (qualche secondo) e vive
per tutto il processo. Se il file della tassonomia manca, il modulo si
spegne in silenzio: l'estrazione classica continua da sola.
"""
from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("nivult.ats.esco")

PERCORSO = "/opt/nivult/esco-competenze.jsonl"
# il cancello di qualita': ESCO entra in produzione SOLO dopo la
# calibrazione (esco_calibra), che produce anche la lista nera degli
# alias-trappola («absorb» e' un verbo comune prima che una piattaforma)
ATTIVO = "/opt/nivult/esco-attivo"
LISTA_NERA = "/opt/nivult/esco-lista-nera.json"

_automa = None          # ahocorasick.Automaton | False (assente)
_canonico: dict = {}    # uri -> etichetta inglese

_NON_PAROLA = re.compile(r"[a-z0-9à-ÿåäöøœæčřšžßẞ]", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def _costruisci():
    global _automa, _canonico
    if _automa is not None:
        return
    if not (os.path.exists(PERCORSO) and os.path.exists(ATTIVO)):
        _automa = False          # non calibrato: ci si comporta come
        return                   # se ESCO non esistesse
    nere: set = set()
    try:
        nere = set(json.load(open(LISTA_NERA)))
    except OSError:
        pass
    import ahocorasick
    a = ahocorasick.Automaton()
    n_etichette = 0
    with open(PERCORSO, encoding="utf-8") as f:
        for riga in f:
            v = json.loads(riga)
            uri = v["uri"]
            pref = v.get("preferred") or {}
            inglese = pref.get("en") or pref.get("en-us") \
                or next(iter(pref.values()), None)
            if not inglese:
                continue
            _canonico[uri] = inglese
            etichette = set()
            for lab in pref.values():
                etichette.add(lab)
            for labs in (v.get("alt") or {}).values():
                etichette.update(labs)
            for lab in etichette:
                chiave = _norm(lab)
                if not 4 <= len(chiave) <= 60 or chiave in nere:
                    continue
                if a.exists(chiave):
                    a.get(chiave)[1].add(uri)
                else:
                    # (lunghezza, competenze): la lunghezza serve al
                    # riconoscitore per trovare l'inizio del riscontro
                    a.add_word(chiave, (len(chiave), {uri}))
                    n_etichette += 1
    a.make_automaton()
    _automa = a
    log.info("ESCO: automa pronto — %d etichette, %d competenze",
             n_etichette, len(_canonico))


def estrai(testo: str, massimo: int = 25) -> list[str]:
    """Le competenze ESCO citate nel testo, come etichette canoniche EN."""
    _costruisci()
    if not _automa or not testo:
        return []
    t = _norm(testo)[:12000]
    trovate: set = set()
    for fine, (lung, uris) in _automa.iter(t):
        inizio = fine - lung + 1
        # confini di parola: «art» dentro «part-time» non conta
        if inizio > 0 and _NON_PAROLA.match(t[inizio - 1]):
            continue
        if fine + 1 < len(t) and _NON_PAROLA.match(t[fine + 1]):
            continue
        trovate.update(uris)
    return sorted({_canonico[u] for u in trovate
                   if u in _canonico})[:massimo]
