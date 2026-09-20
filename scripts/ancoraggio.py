"""Trovare nel testo dell'annuncio il nome di una tecnologia, o dire che non c'e'.

Serve a due cose che devono dare la STESSA risposta, e per questo stanno qui e non
duplicate: il demone del 2B, che butta i nomi inventati prima di scriverli, e il
costruttore del dataset, che non puo' marcare cio' che non trova.

IL PROBLEMA CHE RISOLVE (misurato il 19/09/2026)

Su 122.592 nomi prodotti dal 2B, il 19% non si trovava nel testo cercandolo alla
lettera. Due cause opposte, e vanno distinte perche' hanno cure opposte:

  - il modello NORMALIZZA: scrive «Microsoft Excel» dove il testo dice «Excel»,
    «PowerBI» dove dice «Power BI». Il nome e' giusto, la forma no. Butta via 15
    punti su 19, ed e' quello che le varianti qui sotto recuperano.
  - il modello INVENTA: scrive «Microsoft Dynamics 365» su un annuncio che parla
    di SAP e Sage e non nomina Microsoft in nessun punto. Sono i 4 punti che
    restano, e vanno buttati: sono righe false nel dato che vendiamo.

Nella modalita' «sole tecnologie» accesa il 19/09 le invenzioni sono il 10,4%
contro l'1,9% di quando il 2B scriveva anche la sintesi: scrivere la sintesi lo
obbligava a leggere il testo.
"""
from __future__ import annotations

import html
import re
import unicodedata

# I fornitori il cui nome il modello aggiunge davanti e il testo spesso omette.
FORNITORI = ("microsoft", "ms", "google", "amazon", "adobe", "oracle", "ibm", "apple",
             "atlassian", "autodesk", "sap", "salesforce", "aws", "meta", "jetbrains",
             "red hat", "vmware", "cisco", "siemens", "dassault", "altium", "hashicorp")
# Parole di coda che il modello aggiunge per chiarire e il testo non ha.
CODE = ("software", "platform", "platforms", "systems", "system", "tools", "tool", "suite",
        "cloud", "online", "server", "framework", "database", "databases", "erp", "crm",
        "technologies", "technology", "applications", "application", "programming language")

_TAG = re.compile(r"<[^>]+>")
_SPAZI = re.compile(r"\s+")

# Nomi che il mercato usa in due forme, e nessuna regola generale le lega: il
# modello scrive «PostgreSQL» dove l'annuncio scrive «Postgres», «Node.js» dove
# scrive «Node», «Golang» dove scrive «Go». Sono coppie, quindi la tabella vale
# nei due sensi e si costruisce una volta sola qui sotto.
# Misurate sul golden del 19-20/09: erano gli ultimi nomi veri che il filtro
# buttava. La lista si allunga a mano, ed e' il posto giusto per farlo — la
# rubrica lo prevede: la forma si etichetta come sta nel testo, e il legame fra
# le due forme sta a valle.
_COPPIE = (
    ("postgresql", "postgres"), ("node.js", "node"), ("nodejs", "node"),
    ("golang", "go"), ("javascript", "js"), ("typescript", "ts"),
    ("kubernetes", "k8s"), ("powershell", "power shell"),
    ("power bi", "powerbi"), ("github actions", "gh actions"),
    ("visual studio code", "vs code"), ("vscode", "vs code"),
    ("microsoft 365", "m365"), ("office 365", "o365"),
    ("google cloud platform", "gcp"), ("amazon web services", "aws"),
    ("six sigma black belt", "ssbb"), ("six sigma", "6 sigma"),
)
ALIAS: dict[str, tuple[str, ...]] = {}
for _a, _b in _COPPIE:
    ALIAS.setdefault(_a, ()); ALIAS.setdefault(_b, ())
    ALIAS[_a] += (_b,); ALIAS[_b] += (_a,)


def pulito(t: str | None) -> str:
    """Il testo senza marcato ne' spazi doppi. Identica al demone: i due devono
    lavorare sulla stessa stringa, carattere per carattere."""
    t = html.unescape(html.unescape(t or ""))
    return _SPAZI.sub(" ", _TAG.sub(" ", t).replace("\xa0", " ")).strip()


def _piatto(s: str) -> str:
    """Solo lettere e cifre, minuscole, accenti sciolti."""
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(c for c in s if c.isalnum())


def varianti(nome: str) -> list[str]:
    """Le forme in cui quello stesso nome puo' comparire nel testo, dalla piu' fedele."""
    n = nome.strip()
    fuori = [n]

    # le coppie di nomi che il mercato usa entrambe
    fuori += list(ALIAS.get(n.lower(), ()))

    # «Amazon Web Services (AWS)» -> anche «Amazon Web Services» e «AWS»
    m = re.match(r"^(.*?)\s*\(([^)]{2,20})\)\s*$", n)
    if m:
        fuori += [m.group(1).strip(), m.group(2).strip()]

    basso = n.lower()
    # prefisso del fornitore via: «Microsoft Excel» -> «Excel»
    for f in FORNITORI:
        if basso.startswith(f + " ") and len(n) > len(f) + 1:
            fuori.append(n[len(f) + 1:].strip())
            break
    # coda di chiarimento via: «Jira Software» -> «Jira»
    for c in CODE:
        if basso.endswith(" " + c) and len(n) > len(c) + 1:
            fuori.append(n[: -(len(c) + 1)].strip())
            break
    # sigla dalle iniziali: «Amazon Web Services» -> «AWS»
    parole = [p for p in re.split(r"[\s/-]+", n) if p and p[0].isalpha()]
    if 2 <= len(parole) <= 4:
        fuori.append("".join(p[0] for p in parole).upper())
    # punteggiatura variabile: «Node.js» -> «Nodejs», «Node js»
    if re.search(r"[.\-_]", n):
        fuori += [re.sub(r"[.\-_]", "", n), re.sub(r"[.\-_]", " ", n)]

    visti, unici = set(), []
    for v in fuori:
        v = v.strip(" ,;:.()[]")
        # Si tengono anche le forme di UN carattere: «R» e «C» sono linguaggi veri,
        # e scartarle voleva dire cancellarle tutte dal dato (collaudo del 19/09).
        # La sicurezza su quelle la da' il confronto sensibile alle maiuscole.
        if v and v.lower() not in visti:
            visti.add(v.lower()); unici.append(v)
    return unici


def _cerca(testo: str, forma: str, sensibile: bool = False) -> tuple[int, int] | None:
    """Prima occorrenza, con confini di parola dove la forma e' alfanumerica.

    Il confine di coda ammette un plurale: «Project» combacia con «projects»,
    perche' l'annuncio declina i nomi e il modello no.

    `sensibile` pretende che il pezzo di TESTO trovato sia in maiuscolo o con
    l'iniziale maiuscola. Serve per le forme corte e per le sigle costruite dalle
    iniziali, dove il confronto indifferente aggancia parole qualunque: «Adobe
    Creative Cloud» finiva su un «acc» minuscolo, e «R» su qualsiasi «r» isolata.

    Le maiuscole si pretendono dal testo, NON dal nome che arriva dal modello: il
    modello scrive minuscolo («c#», «go», «sap mm») e pretendere le maiuscole da
    lui buttava tecnologie vere — C#, Go, X, 8D e sei moduli SAP, misurato sul
    golden il 19/09/2026. Il testo invece le maiuscole le ha, ed e' li' che
    distinguono una sigla da una parola comune.
    """
    f = forma.lower()
    basso = testo.lower()
    if f[0].isalnum() and f[-1].isalnum():
        patt = r"(?<![0-9A-Za-z])" + re.escape(f) + r"(?:e?s)?(?![0-9A-Za-z])"
    else:                                  # «C++», «.NET»: i confini li rompono
        patt = re.escape(f)
    for m in re.finditer(patt, basso):
        if not sensibile:
            return (m.start(), m.end())
        pezzo = testo[m.start():m.end()]
        lettere = [c for c in pezzo if c.isalpha()]
        if not lettere or pezzo == pezzo.upper() or lettere[0].isupper():
            return (m.start(), m.end())
    return None


def _pretende_maiuscole(nome: str, forma: str) -> bool:
    """Questa forma va cercata rispettando le maiuscole?

    Due casi: le forme cortissime (una o due lettere), e le sigle costruite da noi
    dalle iniziali — riconoscibili perche' sono tutte maiuscole e non comparivano
    nel nome di partenza.
    """
    if len(forma) <= 2:
        return True
    return forma.isupper() and forma not in nome


def _mappa_piatta(testo: str) -> tuple[str, list[int]]:
    """Il testo ridotto a lettere e cifre, piu' l'indice per tornare all'originale.

    Per i nomi che nel testo esistono ma con altra punteggiatura o spaziatura:
    «SAP S/4HANA» contro «SAP S/4 HANA», «Node.js» contro «Node JS». Il tentativo
    precedente cercava finestre con una regex avida e trovava sempre un pezzo piu'
    lungo del nome, quindi non combaciava mai: era il 18% dei nomi persi.
    """
    fuori, indici = [], []
    for i, ch in enumerate(testo):
        for d in unicodedata.normalize("NFKD", ch.lower()):
            if d.isalnum():
                fuori.append(d); indici.append(i)
    return "".join(fuori), indici


def _prefissi(nome: str) -> list[str]:
    """«AutoCAD Inventor Professional» -> anche «AutoCAD Inventor», «AutoCAD».

    Il modello allarga i nomi con la versione o l'edizione («LexisNexis 360»): se
    la forma intera non c'e', marcare il nome piu' corto e' meglio di niente. Non
    si scende mai al solo nome del fornitore: «Google» da solo non e' una
    tecnologia, e marcarlo sarebbe peggio che lasciar perdere.
    """
    parole = nome.split()
    fuori = []
    for taglio in range(len(parole) - 1, 0, -1):
        corto = " ".join(parole[:taglio])
        if len(corto) >= 4 and corto.lower() not in FORNITORI:
            fuori.append(corto)
    return fuori


def ancora(testo: str, nome: str) -> tuple[int, int, str] | None:
    """Dove sta questo nome nel testo? None se non c'e' in nessuna delle sue forme.

    Ritorna (inizio, fine, il pezzo di testo trovato): gli scostamenti servono al
    dataset di marcature, il pezzo di testo serve a chi vuole sapere in che forma
    l'annuncio lo scriveva.
    """
    if not testo or not nome:
        return None
    forme = varianti(nome)

    for forma in forme:
        p = _cerca(testo, forma, _pretende_maiuscole(nome, forma))
        if p:
            return (p[0], p[1], testo[p[0]:p[1]])

    # Scritto con altra punteggiatura: si cerca nel testo appiattito. Qui NON ci
    # sono confini di parola — appiattire toglie gli spazi, che sono i confini —
    # quindi le forme corte restano fuori: «acc» combaciava dentro «Accoglienza».
    # Da cinque caratteri in su il rischio di finire dentro un'altra parola per
    # caso e' trascurabile, e questo percorso serve ai nomi composti («SAP S/4
    # HANA», «Sales Force»), che sono tutti piu' lunghi.
    piatto_testo = indici = None
    for forma in forme:
        piatto_nome = _piatto(forma)
        if not 5 <= len(piatto_nome) <= 32 or _pretende_maiuscole(nome, forma):
            continue
        if piatto_testo is None:
            piatto_testo, indici = _mappa_piatta(testo)
        j = piatto_testo.find(piatto_nome)
        if j >= 0:
            a, b = indici[j], indici[j + len(piatto_nome) - 1] + 1
            return (a, b, testo[a:b])

    # nome allargato dal modello: si ripiega sul prefisso piu' lungo che esiste
    for corto in _prefissi(nome):
        p = _cerca(testo, corto, _pretende_maiuscole(nome, corto))
        if p:
            return (p[0], p[1], testo[p[0]:p[1]])
    return None


def filtra(testo: str, tecnologie) -> tuple[list, int]:
    """Butta dalle tecnologie quelle che l'annuncio non nomina.

    Ritorna (quelle che restano, quante buttate). Chi chiama deve contare le
    buttate in una statistica: se quel numero sale, il modello sta inventando
    di piu' e lo si vede prima di venderlo a qualcuno.
    """
    if not tecnologie:
        return [], 0
    tenute, buttate = [], 0
    for t in tecnologie:
        nome = t.get("nome") if isinstance(t, dict) else t
        nome = str(nome or "")
        if nome and ancora(testo, nome):
            tenute.append(t)
        else:
            buttate += 1
    return tenute, buttate
