"""Il riconoscitore ESCO: 13.466 competenze, 28 lingue, un solo passaggio.

Il salto di qualita' sul technographic: «saldatura», «Schweißen»,
«soudage» e «welding» diventano LA STESSA competenza, con l'etichetta
inglese canonica. TheirStack e' anglocentrico; noi diventiamo leggibili
in tutta Europa — che e' il nostro campo.

La tecnica: un automa Aho-Corasick su tutte le etichette (preferite +
alternative, tutte le lingue) — ~duecentomila stringhe cercate in UN
passaggio sul testo, non duecentomila regex.

Le guardie di qualita', tutte misurate sul corpus (08/09/2026, 400
annunci a caso: «avec» dava «mobile device management» nel 22% degli
annunci, «data» → «statistics» 15%, «lead» → «lead others» 12%,
«planning» → «design ventilation network» 11%, «paris» → «betting»):

1. etichette da 4 a 60 caratteri, confini di parola attorno al riscontro;
2. **un alias di UNA parola vale solo se preso dalle etichette
   preferite**: le alternative monoparola («term», «plan», «data»,
   «lead», «access») sono abbreviazioni e sinonimi larghi, e sono la
   quasi totalita' delle trappole;
3. **un'etichetta di una parola scatta solo nella SUA lingua**: «lassen»
   e' «saldare» in olandese ma un verbo comune in tedesco, «physique» e'
   la fisica in francese e il fisico in inglese. La lingua dell'annuncio
   la da' `lingua.rileva`; se non si sbilancia, le monoparola non
   scattano affatto — meglio una competenza in meno che una inventata;
4. la lista nera di `esco_calibra`, per alias o per etichetta canonica.

Per ogni competenza esce UNA voce canonica (inglese) per quante lingue
la menzionino. L'automa si costruisce pigramente al primo uso (qualche
secondo) e vive per tutto il processo. Se il file della tassonomia o il
marcatore di calibrazione mancano, il modulo si spegne in silenzio:
l'estrazione classica continua da sola.
"""
from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger("nivult.ats.esco")

PERCORSO = "/opt/nivult/esco-competenze.jsonl"
# il cancello di qualita': ESCO entra in produzione SOLO dopo la
# calibrazione (esco_calibra --apri), che produce anche la lista nera
# degli alias-trappola («absorb» e' un verbo comune prima che una
# piattaforma)
ATTIVO = "/opt/nivult/esco-attivo"
LISTA_NERA = "/opt/nivult/esco-lista-nera.json"

# Le lingue sono competenze che vale la pena tenere anche se compaiono
# in un annuncio su dieci: dicono a chi e' rivolto il posto. La
# calibrazione non le mette mai in lista nera per frequenza.
LINGUE = frozenset({
    "English", "German", "French", "Italian", "Spanish", "Portuguese",
    "Dutch", "Swedish", "Danish", "Norwegian", "Finnish", "Polish",
    "Czech", "Slovak", "Hungarian", "Romanian", "Bulgarian", "Greek",
    "Croatian", "Slovenian", "Serbian", "Estonian", "Latvian",
    "Lithuanian", "Irish", "Maltese", "Icelandic", "Russian",
    "Ukrainian", "Turkish", "Arabic", "Hebrew", "Chinese", "Japanese",
    "Korean", "Hindi", "Catalan", "Basque", "Galician", "Luxembourgish",
    "Flemish", "Latin",
})

_automa = None          # ahocorasick.Automaton | False (assente)
_canonico: dict = {}    # uri -> etichetta inglese

_NON_PAROLA = re.compile(r"[a-z0-9à-ÿåäöøœæčřšžßẞ]", re.I)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def _lingua_codice(lang: str) -> str:
    return lang.split("-")[0].lower()      # en-us -> en


def _carica_nera() -> set:
    try:
        return set(json.load(open(LISTA_NERA)))
    except (OSError, ValueError):
        return set()


def _costruisci(forza: bool = False):
    """Costruisce l'automa. `forza` salta il cancello (per la calibrazione)."""
    global _automa, _canonico
    if _automa is not None:
        return
    if not os.path.exists(PERCORSO) or not (forza or os.path.exists(ATTIVO)):
        _automa = False          # non calibrato: ci si comporta come
        return                   # se ESCO non esistesse
    nere = _carica_nera()
    nere_norm = {_norm(x) for x in nere}
    import ahocorasick
    a = ahocorasick.Automaton()
    n_etichette = 0
    n_scartate = 0

    def registra(lab: str, lang: str, uri: str, alternativa: bool) -> None:
        nonlocal n_etichette, n_scartate
        chiave = _norm(lab)
        if not 4 <= len(chiave) <= 60 or chiave in nere_norm:
            return
        monoparola = " " not in chiave
        if monoparola and alternativa:
            n_scartate += 1        # guardia 2
            return
        lingue = frozenset({_lingua_codice(lang)}) if monoparola else None
        if a.exists(chiave):
            lung, uris, vecchie = a.get(chiave)
            uris.add(uri)
            if lingue is not None and vecchie is not None:
                a.add_word(chiave, (lung, uris, vecchie | lingue))
            elif lingue is None:
                a.add_word(chiave, (lung, uris, None))
        else:
            # (lunghezza, competenze, lingue ammesse | None = tutte): la
            # lunghezza serve al riconoscitore per trovare l'inizio
            a.add_word(chiave, (len(chiave), {uri}, lingue))
            n_etichette += 1

    with open(PERCORSO, encoding="utf-8") as f:
        for riga in f:
            v = json.loads(riga)
            uri = v["uri"]
            pref = v.get("preferred") or {}
            inglese = pref.get("en") or pref.get("en-us") \
                or next(iter(pref.values()), None)
            if not inglese or inglese in nere or _norm(inglese) in nere_norm:
                continue
            _canonico[uri] = inglese
            for lang, lab in pref.items():
                registra(lab, lang, uri, alternativa=False)
            for lang, labs in (v.get("alt") or {}).items():
                for lab in labs:
                    registra(lab, lang, uri, alternativa=True)
    a.make_automaton()
    _automa = a
    log.info("ESCO: automa pronto — %d etichette, %d competenze, "
             "%d alias monoparola scartati, %d voci in lista nera",
             n_etichette, len(_canonico), n_scartate, len(nere))


def _riscontri(testo: str, lingua: str | None):
    """(alias, uris) per ogni riscontro ammesso nel testo gia' normalizzato."""
    for fine, (lung, uris, lingue) in _automa.iter(testo):
        inizio = fine - lung + 1
        # confini di parola: «art» dentro «part-time» non conta
        if inizio > 0 and _NON_PAROLA.match(testo[inizio - 1]):
            continue
        if fine + 1 < len(testo) and _NON_PAROLA.match(testo[fine + 1]):
            continue
        # guardia 3: una parola sola vale solo nella sua lingua
        if lingue is not None and lingua not in lingue:
            continue
        yield testo[inizio:fine + 1], uris


def estrai(testo: str, massimo: int = 25, lingua: str | None = None) -> list[str]:
    """Le competenze ESCO citate nel testo, come etichette canoniche EN.

    `lingua` e' il codice della lingua dell'annuncio; se manca la si
    rileva dal testo. Senza una lingua chiara le etichette di una sola
    parola non scattano."""
    _costruisci()
    if not _automa or not testo:
        return []
    if lingua is None:
        from nivult.ats import lingua as _lingua
        lingua = _lingua.rileva(testo)
    t = _norm(testo)[:12000]
    trovate: set = set()
    for _alias, uris in _riscontri(t, lingua):
        trovate.update(uris)
    return sorted({_canonico[u] for u in trovate
                   if u in _canonico})[:massimo]
