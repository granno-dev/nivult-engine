"""Il paese da qualsiasi localita' del mondo, offline.

Un'offerta senza paese e' un'offerta che non si puo' contare ne'
filtrare. Le fonti europee il paese ce l'hanno; gli ATS globali
(Workday, Ashby, Breezy...) no — la loro localita' e' un testo libero,
«Studio City, CA» o «Delhi» o «Montreal, QC». Questo modulo lo mappa a
un codice ISO2, per QUALSIASI paese, non solo l'Europa.

Livelli, dal piu' sicuro al piu' largo:
  1. gli STATI di USA («City, CA» -> US): un pattern inequivocabile;
  2. un codice o nome di PAESE scritto nel testo («US», «Uruguay»);
  3. la CITTA' nel database GeoNames (via geonamescache, offline): 4,8
     milioni di localita' col loro paese, la piu' popolosa vince
     l'omonimia (Paris -> FR, non Paris/Texas).

Due accorgimenti fanno la differenza sui dati veri:
  - si tolgono gli accenti prima di confrontare, perche' le offerte
    scrivono «Montreal» e «Sao Paulo» dove GeoNames ha «Montréal» e
    «São Paulo»;
  - si ripulisce il rumore («Bay Area», «Office», «HQ», «Remote»)
    che incolla parole alla citta'.

Resta apposta senza risposta cio' che un paese non ce l'ha davvero:
«2 Locations», «Remote», «Worldwide», «Europe». Forzarli sarebbe
inventare.
"""

from __future__ import annotations

import re
import unicodedata

import geonamescache


def _piatto(s: str) -> str:
    """Minuscolo, senza accenti: «Montréal» e «Montreal» diventano uguali."""
    n = unicodedata.normalize("NFKD", s)
    return "".join(c for c in n if not unicodedata.combining(c)).lower()


_gc = geonamescache.GeonamesCache()

# citta' (nome appiattito) -> ISO2, tenendo per gli omonimi la piu' popolosa
_CITTA: dict[str, str] = {}
_POP: dict[str, int] = {}
for _c in _gc.get_cities().values():
    _n = _piatto(_c["name"])
    _pop = _c.get("population", 0) or 0
    if _n not in _CITTA or _pop > _POP.get(_n, 0):
        _CITTA[_n] = _c["countrycode"]
        _POP[_n] = _pop

# stati USA a due lettere: dopo una virgola inchiodano il paese
_STATI_US = set(_gc.get_us_states().keys())
_STATI_US_NOMI = {_piatto(s["name"]) for s in _gc.get_us_states().values()}

# codici ISO2 dei paesi (per «US», «FR» scritti nudi) e nomi per esteso
_CODICI = set(_gc.get_countries().keys())
_PAESI = {_piatto(c["name"]): cc for cc, c in _gc.get_countries().items()}

# abbreviazioni e nomi locali che GeoNames non indicizza cosi'
_ALIAS = {
    "usa": "US", "u.s.": "US", "u.s.a.": "US", "us": "US",
    "uk": "GB", "u.k.": "GB", "uae": "AE",
    "nyc": "US", "goteborg": "SE", "bangalore": "IN", "kobenhavn": "DK",
    "milano": "IT", "roma": "IT", "torino": "IT", "napoli": "IT",
    "firenze": "IT", "venezia": "IT", "genova": "IT", "padova": "IT",
    "wien": "AT", "praha": "CZ", "warszawa": "PL", "lisboa": "PT",
    "bruxelles": "BE", "antwerpen": "BE", "koln": "DE", "munchen": "DE",
}

# rumore da togliere prima di cercare la citta'
_RUMORE = re.compile(
    r"\b(area|region|greater|metropolitan|metro|province|county|"
    r"office|hq|headquarters|campus|downtown|remote|hybrid|on[- ]?site|"
    r"city of|zona|regione|provincia di|sede|dintorni|bay)\b", re.I)

# indizi di paese scritti in chiaro dentro la stringa
_INDIZIO = re.compile(r"\b(U\.?S\.?A?|USA|UK|UAE)\b")


def paese_da_localita(loc: str | None) -> str | None:
    """ISO2 del paese, o None se davvero non si capisce."""
    if not loc or not loc.strip():
        return None
    t = loc.strip()

    piatto = _RUMORE.sub(" ", _piatto(t)).strip()
    pezzi = [p.strip() for p in re.split(r"[,\-/()|:]", piatto) if p.strip()]

    # 0. un nome di paese scritto per esteso e NON ambiguo (cioe' che non
    # sia anche il nome di uno stato USA): «Australia», «Canada», «Mexico»
    # vincono su tutto. «Georgia» no: e' stato USA e paese insieme, e nei
    # dati di lavoro e' quasi sempre lo stato — la si lascia ai passi dopo.
    for p in pezzi:
        if p in _PAESI and p not in _STATI_US_NOMI:
            return _PAESI[p]

    # 1. «City, CA» — stato USA a due lettere dopo virgola. Ma alcune
    # sigle di stato sono anche codici-paese ISO: «IN» e' Indiana ma pure
    # India, «DE» Delaware ma pure Germania, «SC» South Carolina ma pure
    # le Seychelles. In quel caso la citta' decide: se cio' che precede
    # la sigla e' una citta' di QUEL paese («Bengaluru, IN» -> India),
    # vince il paese; altrimenti e' lo stato USA («Florence, SC» -> US).
    m = re.search(r",\s*([A-Z]{2})\b", t)
    if m and m.group(1) in _STATI_US:
        code = m.group(1)
        if code in _CODICI and paese_da_localita(t[:m.start()]) == code:
            return code
        return "US"

    # 2. un codice paese scritto in chiaro («US Remote», «Remote - US»)
    m = _INDIZIO.search(t)
    if m:
        chiave = m.group(1).upper().replace(".", "")
        if chiave in ("US", "USA"):
            return "US"
        if chiave == "UK":
            return "GB"
        if chiave == "UAE":
            return "AE"

    # la stringa intera come citta': i nomi col trattino («Aix-en-Provence»)
    # non vanno sbriciolati dallo split
    if piatto in _CITTA:
        return _CITTA[piatto]

    # 3. uno stato USA per esteso o un alias. NIENTE codici di due lettere
    # nudi: «MH» e' Maharashtra (Mumbai) ma anche le Isole Marshall — le
    # sigle regionali collidono coi codici-paese ISO e darebbero paesi
    # assurdi. La citta' (passo 4) e' un segnale piu' sicuro della sigla.
    for p in pezzi:
        if p in _STATI_US_NOMI:
            return "US"
        if p in _ALIAS:
            return _ALIAS[p]

    # 4. la citta' piu' popolosa fra i pezzi: cosi' «Toronto» batte «Can»
    # (un paesino turco omonimo) in «CAN, Ontario, Toronto». Si ignorano i
    # pezzi di due lettere e le preposizioni (_STOP): collidono con
    # cittadine omonime e falserebbero il paese.
    migliore = None
    for p in pezzi:
        if len(p) >= 3 and p not in _STOP and p in _CITTA and (
                migliore is None or _POP.get(p, 0) > migliore[1]):
            migliore = (_CITTA[p], _POP.get(p, 0))
    return migliore[0] if migliore else None


# soglia di popolazione per lo scan aggressivo: sotto, e' un villaggio
# omonimo che darebbe falsi positivi (ci sono «Berlin» minuscole ovunque)
_MIN_POP = 1000

# preposizioni e particelle che NON sono citta', anche se GeoNames ha un
# paesino omonimo: «sur» (Nogent-SUR-Seine) e' anche una citta' in Oman,
# «van» (VAN Nuys) una in Turchia. Senza questa lista il geocoder
# scambiava mezza Francia per l'Oman. Le citta' vere di tre lettere
# (Ulm, Zug, Pau, Gap, Hof, Fes) restano: si filtra il senso, non la
# lunghezza.
_STOP = {"sur", "sous", "les", "le", "la", "aux", "au", "en", "des", "du",
         "de", "et", "st", "ste", "van", "von", "der", "den", "of", "the",
         "and", "on", "in", "at", "el", "di", "del", "da", "lo", "lès"}


def paese_da_testo_libero(loc: str | None) -> str | None:
    """Come `paese_da_localita`, ma spezza anche sugli spazi.

    Da usare SOLO quando la stringa e' gia' una localita' strutturata —
    per esempio la sede estratta dall'URL di Workday, «Canada BC
    Vancouver» o «Bucharest Romania» — dove citta', stato e paese sono
    parole staccate senza virgola. Cerca prima un nome di paese, poi la
    citta' piu' popolosa fra tutte le parole e coppie di parole; la
    soglia di popolazione tiene fuori i villaggi omonimi. Su testo
    libero qualsiasi darebbe falsi positivi: non e' per quello.
    """
    diretto = paese_da_localita(loc)
    if diretto:
        return diretto
    piatto = _RUMORE.sub(" ", _piatto(loc or "")).strip()
    parole = [w for w in re.split(r"[\s,\-/()|:]+", piatto) if w]

    # un nome di paese fra le parole o le coppie di parole vince per primo:
    # un paese scritto in chiaro batte l'euristica dello stato USA.
    for i in range(len(parole)):
        if parole[i] in _PAESI and parole[i] not in _STATI_US_NOMI:
            return _PAESI[parole[i]]
        if i + 1 < len(parole):
            due = parole[i] + " " + parole[i + 1]
            if due in _PAESI:
                return _PAESI[due]

    # uno stato USA nominato per esteso («Missouri», «Iowa») o col codice
    # («NJ», «WI»): lo stato inchioda il paese. Si escludono i codici che
    # sono anche codici-paese ISO — «DE» e' Delaware ma pure Germania,
    # «IN» Indiana ma pure India: quelli li risolve la citta'.
    for w in parole:
        if w in _STATI_US_NOMI or (
                len(w) == 2 and w.upper() in _STATI_US
                and w.upper() not in _CODICI):
            return "US"

    # la citta' piu' popolosa fra parole e coppie: la popolazione decide,
    # cosi' «York» non batte «New York». Parole cortissime (≤3) ignorate:
    # collidono con sigle e cittadine omonime.
    migliore = None
    for i in range(len(parole)):
        for cand in (parole[i],
                     parole[i] + " " + parole[i + 1] if i + 1 < len(parole)
                     else None):
            if cand and len(cand) >= 3 and cand not in _STOP \
                    and cand in _CITTA and _POP.get(cand, 0) >= _MIN_POP:
                if migliore is None or _POP[cand] > migliore[1]:
                    migliore = (_CITTA[cand], _POP[cand])
    return migliore[0] if migliore else None
