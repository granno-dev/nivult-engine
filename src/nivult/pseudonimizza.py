"""Il CV bendato prima di uscire dallo Spazio economico europeo.

L'estrazione del profilo manda il CV a Z.ai, che elabora fuori dal SEE.
Il modello non ha bisogno di sapere COME SI CHIAMA il candidato, dove
abita o quale sia il suo numero: gli servono mestiere, competenze,
lingue, anni e la storia dei ruoli. Tutto il resto e' dato personale che
attraversa una frontiera senza servire a niente.

Questo modulo toglie quel «resto» e lo sostituisce con segnaposto
stabili. Stabili e' la parola importante: `[CANDIDATO]` ripetuto e'
leggibile per il modello, mentre una cancellazione secca spezza le frasi
e peggiora l'estrazione.

COSA NON SI TOCCA, e non e' una dimenticanza:

  - i nomi delle AZIENDE, perche' `roles[].employer` e' un campo che
    l'estrazione deve restituire e che il pannello mostra al candidato;
  - le scuole e le certificazioni, per la stessa ragione;
  - le citta', che servono a capire il mercato di riferimento.

Il dizionario di ritorno resta in memoria per la durata della chiamata e
non va MAI scritto: ne' a log, ne' a database, ne' dentro
`raw_extraction`. E' la chiave che annullerebbe tutto il lavoro.

Nessuna promessa di perfezione. Una regex non e' un riconoscitore di
entita' e un CV puo' nascondere un indirizzo in una riga che sembra
altro. E' una riduzione del rischio, non una garanzia: vale come misura
tecnica ai sensi dell'art. 32, non come anonimizzazione ai sensi del
considerando 26 — il testo resta un dato personale anche dopo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# I segnaposto. Parole intere e maiuscole: nessun CV vero le contiene, e
# il modello le riconosce come buchi invece di provare a interpretarle.
CANDIDATO = "[CANDIDATO]"
EMAIL = "[EMAIL]"
TELEFONO = "[TELEFONO]"
INDIRIZZO = "[INDIRIZZO]"
COLLEGAMENTO = "[LINK]"
IDENTIFICATIVO = "[ID]"
NASCITA = "[DATA_NASCITA]"

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")

# I numeri di telefono europei, scritti come li scrive la gente: prefisso
# internazionale o zero iniziale, poi da 8 a 13 cifre con spazi, punti,
# trattini o parentesi in mezzo. Il confine a sinistra esclude le cifre
# attaccate, cosi' un importo o un anno non diventano un telefono.
#
# SPAZI ORIZZONTALI, non `\s`: `\s` comprende il ritorno a capo, e con
# quello la regex saltava a fine riga per continuare a contare cifre.
# Su un CV vero «+39 333 1234567» seguito da «2020-2024 HR, Ferrero» si
# mangiava anche il «202» dell'anno, e la riga successiva arrivava al
# modello monca. Un numero di telefono sta su una riga sola.
_TELEFONO = re.compile(
    r"(?<![\d/])(?:\+|00)[ \t]?\d{1,3}[ \t.\-/]?(?:\(0\)[ \t.\-]?)?"
    r"(?:\d[ \t.\-]?){7,12}\d"
    r"|(?<![\d/+])0\d{1,3}[ \t.\-/]?(?:\d[ \t.\-]?){6,11}\d(?![\d/])")

_URL = re.compile(
    r"\b(?:https?://|www\.)\S+"
    r"|\b(?:linkedin\.com|github\.com|gitlab\.com|xing\.com|behance\.net"
    r"|dribbble\.com|stackoverflow\.com)/\S+", re.I)

# Gli identificativi nazionali che compaiono davvero nei CV europei.
# Ognuno e' ancorato a una parola chiave o a una forma inconfondibile:
# senza ancora, un codice fiscale italiano e' indistinguibile da una
# sigla di prodotto e si finirebbe per bendare mezzo CV.
_IDENTIFICATIVI = [
    # Codice fiscale italiano: 6 lettere, 2 cifre, lettera, 2 cifre,
    # lettera, 3 cifre, lettera. La forma e' gia' un'ancora.
    re.compile(r"\b[A-Z]{6}\d{2}[A-Z]\d{2}[A-Z]\d{3}[A-Z]\b"),
    # Partita IVA, NIF/DNI, NIE, PESEL, personnummer, BSN, NINO, AHV:
    # tutti dietro la loro etichetta.
    re.compile(
        r"\b(?:p\.?\s?iva|partita iva|vat|nif|n\.?i\.?f|dni|nie|cif"
        r"|pesel|personnummer|person\s?nr|bsn|nino|national insurance"
        r"|steuer-?id|steuernummer|num[eé]ro de s[eé]curit[eé] sociale"
        r"|nir|ssn|social security)[ \t]*[:.\-]?[ \t]*[A-Z0-9][A-Z0-9 \t.\-/]{5,20}",
        re.I),
    re.compile(r"\b[A-Z]{2}\d{2}[\sA-Z0-9]{11,30}\b"),   # IBAN
]

# La data di nascita, dietro l'etichetta. Senza etichetta non si tocca:
# un CV e' pieno di date, e bendarle tutte cancella la storia dei ruoli,
# che serve.
_NASCITA = re.compile(
    r"\b(?:nat[oa](?: il)?|data di nascita|date of birth|d\.?o\.?b|born"
    r"|geboren|geburtsdatum|n[eé]\(?e\)? le|fecha de nacimiento"
    r"|data de nascimento|geboortedatum|f[oö]dd|data urodzenia)"
    r"[ \t]*[:.\-]?[ \t]*\d{1,4}[ \t./\-]\w{1,9}[ \t./\-]\d{2,4}", re.I)

# L'indirizzo di casa: una via riconosciuta dalla sua parola, fino a fine
# riga. Le parole coprono le nove lingue del prodotto.
_VIA = re.compile(
    r"^.{0,40}\b(?:via|viale|piazza|corso|vicolo|strada|largo"
    r"|street|st\.|road|rd\.|avenue|ave\.|lane|drive"
    r"|stra(?:ss|ß)e|str\.|weg|platz|allee"
    r"|rue|avenue|boulevard|impasse|chemin"
    r"|calle|avenida|plaza|carrer|paseo"
    r"|rua|travessa|avenida"
    r"|straat|laan|plein|gracht"
    r"|gatan|v[aä]gen|gata"
    r"|ulica|ul\.|aleja)\b.{0,60}$",
    re.I | re.M)

# Il CAP con la citta', nelle forme europee piu' comuni.
_CAP = re.compile(
    # Il ramo olandese (`1012 AB`) escludeva gli anni: su «2018-2022 HR
    # Business Partner» leggeva «2022 HR» come CAP e «Business Partner»
    # come citta', e si portava via meta' della riga di un incarico. Un
    # CV e' fatto di anni seguiti da sigle in maiuscolo: qui il falso
    # positivo non e' un caso limite, e' il caso normale.
    r"\b(?:\d{5}|(?!19\d\d|20\d\d)\d{4}[ \t]?[A-Z]{2}"
    r"|[A-Z]{1,2}\d{1,2}[A-Z]?[ \t]?\d[A-Z]{2})"
    r"[ \t]+[A-ZÀ-Ý][\w'’\-]+(?:[ \t]+[A-ZÀ-Ý][\w'’\-]+)?\b")

# Le particelle nobiliari e patronimiche delle nove lingue del prodotto.
PARTICELLE = {"de", "del", "della", "dello", "dei", "degli", "di", "da",
              "dal", "dalla", "van", "von", "der", "den", "ter", "te",
              "du", "des", "le", "la", "y", "dos", "das", "do", "af", "av"}

_PAROLE_NON_NOME = {
    "curriculum", "vitae", "resume", "résumé", "cv", "lebenslauf",
    "profilo", "profile", "profil", "perfil", "contatti", "contact",
    "kontakt", "esperienza", "experience", "competenze", "skills",
    "formazione", "education", "sommario", "summary", "about",
}


@dataclass
class Bendato:
    """Il testo pronto a partire, e la chiave per rileggerlo.

    `chiave` esiste solo perche' il chiamante possa RIMETTERE il nome
    vero nei campi che tornano al candidato — non per archiviarla.
    """

    testo: str
    chiave: dict[str, str] = field(default_factory=dict)
    nome: str | None = None


def _nome_in_testa(testo: str) -> str | None:
    """Il nome del candidato, cercato dove sta sempre: in cima.

    Si guardano le prime righe piene fino al primo contatto (email o
    telefono): un CV mette il nome li' sopra in tutte le impaginazioni
    che esistono. Una riga e' un nome se ha da due a quattro parole che
    cominciano per maiuscola e nessuna parola da intestazione.

    Se il CV non ha quella forma, si torna None e il nome resta nel
    testo: meglio un nome che passa di un pezzo di mestiere cancellato
    perche' somigliava a un nome.
    """
    # «Jan de Vries», «Ludwig von Meyer», «Maria della Rovere»: la
    # particella e' minuscola per grammatica, non per distrazione, e
    # pretendere la maiuscola su OGNI parola lasciava passare il cognome
    # intero. Restano necessarie almeno due parole maiuscole, altrimenti
    # «responsabile di reparto» diventerebbe un nome.
    for riga in testo.splitlines()[:12]:
        riga = riga.strip(" \t|·•-–—:")
        if not riga or len(riga) > 60:
            continue
        if _EMAIL.search(riga) or _TELEFONO.search(riga):
            continue
        parole = riga.split()
        if not 2 <= len(parole) <= 4:
            continue
        if any(p.lower().strip(".,") in _PAROLE_NON_NOME for p in parole):
            continue
        maiuscole = [p for p in parole
                     if re.match(r"^[A-ZÀ-Ý][\w'’\-]*[.,]?$", p)]
        altre = [p for p in parole if p not in maiuscole]
        if len(maiuscole) >= 2 and all(p.lower() in PARTICELLE for p in altre):
            return riga
    return None


def benda(testo: str) -> Bendato:
    """Il CV senza i dati che il modello non deve vedere."""
    chiave: dict[str, str] = {}

    def _sostituisci(rx: re.Pattern[str], segno: str, s: str) -> str:
        def _f(m: re.Match[str]) -> str:
            chiave.setdefault(segno, m.group(0))
            return segno
        return rx.sub(_f, s)

    nome = _nome_in_testa(testo)
    fuori = testo

    # L'ordine conta. Prima le forme lunghe e ancorate (nascita,
    # identificativi, indirizzi), poi quelle brevi: bendare prima i
    # numeri sciolti spezzerebbe le ancore delle forme lunghe.
    fuori = _sostituisci(_NASCITA, NASCITA, fuori)
    for rx in _IDENTIFICATIVI:
        fuori = _sostituisci(rx, IDENTIFICATIVO, fuori)
    fuori = _sostituisci(_VIA, INDIRIZZO, fuori)
    fuori = _sostituisci(_CAP, INDIRIZZO, fuori)
    fuori = _sostituisci(_URL, COLLEGAMENTO, fuori)
    fuori = _sostituisci(_EMAIL, EMAIL, fuori)
    fuori = _sostituisci(_TELEFONO, TELEFONO, fuori)

    # Il nome per ultimo, e ovunque compaia: in testa, nel piede di
    # pagina, dentro «Referenze di ...». Anche i pezzi singoli, perche'
    # il nome proprio da solo ricompare spesso.
    if nome:
        chiave[CANDIDATO] = nome
        pezzi = sorted((p.strip(".,") for p in nome.split()), key=len,
                       reverse=True)
        for p in [nome, *pezzi]:
            if len(p) < 3:
                continue
            fuori = re.sub(rf"\b{re.escape(p)}\b", CANDIDATO, fuori)
        fuori = re.sub(rf"(?:{re.escape(CANDIDATO)}\s*){{2,}}",
                       CANDIDATO + " ", fuori)

    return Bendato(testo=fuori, chiave=chiave, nome=nome)
