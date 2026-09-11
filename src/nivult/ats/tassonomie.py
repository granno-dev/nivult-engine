"""Dai codici ufficiali di occupazione alle 33 famiglie di Nivult.

Le etichette umane costano zero e valgono piu' di un LLM: chi pubblica
un annuncio su France Travail sceglie un codice ROME, su Arbetsförmedlingen
un gruppo SSYK, su EURES un'occupazione ESCO con il suo gruppo ISCO. Qui
quelle scelte diventano la famiglia con cui addestriamo i modelli.

Tre corrispondenze, tutte a livello di GRUPPO (non di singolo codice):

  - ROME (Francia): le prime tre lettere del codice (es. «M18» = informatica)
    sono il dominio piu' il sottodominio; 110 gruppi bastano, con qualche
    eccezione sul codice intero dove il gruppo e' misto;
  - ISCO-08 (ESCO/EURES, e via ISCO anche SSYK, STYRK, KldB): il gruppo
    minore a 3 cifre (130 gruppi);
  - SSYK 2012 (Svezia): e' ISCO-08 con qualche divergenza; le prime 3
    cifre si trattano come ISCO, con le eccezioni svedesi note.

Le convenzioni sono quelle di Nivult, non di ISCO: commesso → Retail,
operaio → Manufacturing, autista → Transportation, elettricista/idraulico
→ Trades, pulizie → Trades, dirigente d'azienda → Management & Leadership.
`None` dove il gruppo e' davvero misto: meglio niente che un timbro.
"""
from __future__ import annotations

import html
import re

# ── ROME: gruppo a 3 caratteri → famiglia ───────────────────────────
ROME_GRUPPI: dict[str, str | None] = {
    # A — agricoltura, pesca, spazi verdi
    "A11": "Agriculture", "A12": "Agriculture", "A13": "Agriculture", "A14": "Agriculture", "A15": "Agriculture",
    # B — arti e artigianato d'arte
    "B11": "Art & Design", "B12": "Art & Design", "B13": "Art & Design", "B14": "Art & Design",
    "B15": "Art & Design", "B16": "Trades", "B17": "Art & Design", "B18": "Art & Design",
    # C — banca, assicurazioni, immobiliare
    "C11": "Finance & Accounting", "C12": "Finance & Accounting", "C13": "Finance & Accounting",
    "C14": "Finance & Accounting", "C15": "Sales",
    # D — commercio: D11 mestieri alimentari, D12 vendita in negozio, D13 telemarketing, D14 commerciale, D15 direzione negozio
    "D11": "Trades", "D12": "Retail", "D13": "Sales", "D14": "Sales", "D15": "Retail",
    # E — comunicazione, media, multimedia
    "E11": "Marketing", "E12": "Art & Design", "E13": "Creative & Media", "E14": "Creative & Media",
    # F — edilizia: F11 architettura/studi, F12 ingegneria civile e conduzione lavori, F13-F17 cantiere
    "F11": "Art & Design", "F12": "Construction", "F13": "Construction", "F14": "Construction",
    "F15": "Construction", "F16": "Construction", "F17": "Construction",
    # G — alberghi, ristorazione, turismo, animazione
    "G11": "Hospitality", "G12": "Hospitality", "G13": "Hospitality", "G14": "Hospitality",
    "G15": "Hospitality", "G16": "Food & Beverage", "G17": "Food & Beverage", "G18": "Food & Beverage",
    # H — industria: H11 R&D/ingegneria, H12-H14 metodi/qualita'/manutenzione industriale, H2x-H3x produzione
    "H11": "Engineering", "H12": "Engineering", "H13": "Engineering", "H14": "Engineering", "H15": "Manufacturing",
    "H21": "Manufacturing", "H22": "Manufacturing", "H23": "Manufacturing", "H24": "Manufacturing", "H25": "Manufacturing",
    "H26": "Manufacturing", "H27": "Manufacturing", "H28": "Manufacturing", "H29": "Manufacturing",
    "H31": "Manufacturing", "H32": "Manufacturing", "H33": "Manufacturing", "H34": "Manufacturing",
    # I — installazione e manutenzione: mestieri
    "I11": "Trades", "I12": "Trades", "I13": "Trades", "I14": "Trades", "I15": "Trades", "I16": "Trades",
    # J — sanita'
    "J11": "Healthcare", "J12": "Healthcare", "J13": "Healthcare", "J14": "Healthcare", "J15": "Healthcare",
    # K — servizi alla persona e alla collettivita'
    "K11": "Social Services", "K12": "Social Services", "K13": "Social Services", "K14": "Government & Public Sector",
    "K15": "Government & Public Sector", "K16": "Education", "K17": "Security & Safety", "K18": "Security & Safety",
    "K19": "Legal", "K21": "Education", "K22": "Trades", "K23": "Environmental & Sustainability",
    "K24": "Science & Research", "K25": "Security & Safety", "K26": "Social Services",
    # L — spettacolo
    "L11": "Creative & Media", "L12": "Creative & Media", "L13": "Creative & Media", "L14": "Creative & Media", "L15": "Sports & Recreation",
    # M — supporto all'impresa: M11 direzione, M12 finanza, M13 studi/consulenza, M14 analisi/gestione, M15 HR,
    #     M16 segreteria/amministrazione, M17 marketing/commerciale, M18 informatica
    "M11": "Management & Leadership", "M12": "Finance & Accounting", "M13": "Consulting", "M14": "Consulting",
    "M15": "Human Resources", "M16": "Administrative", "M17": "Sales", "M18": "Software",
    # N — trasporti e logistica
    "N11": "Logistics", "N12": "Logistics", "N13": "Logistics", "N21": "Transportation", "N22": "Transportation",
    "N31": "Transportation", "N32": "Transportation", "N41": "Transportation", "N42": "Transportation",
    "N43": "Transportation", "N44": "Transportation",
}
# eccezioni sul codice intero: dove il gruppo mescola famiglie diverse
ROME_CODICI: dict[str, str | None] = {
    "M1603": "Transportation",      # facteur (distribuzione documenti), non HR
    "M1601": "Customer Service & Support",   # chargé d'accueil
    "M1704": "Customer Service & Support",   # responsable relation client
    "M1705": "Marketing",           # marketing
    "M1701": "Marketing", "M1702": "Marketing", "M1703": "Sales", "M1706": "Marketing", "M1707": "Sales",
    "M1801": "Technology", "M1802": "Technology", "M1803": "Technology", "M1804": "Data & Analytics",
    "M1805": "Software", "M1806": "Software", "M1810": "Technology",
    "M1402": "Consulting", "M1403": "Data & Analytics", "M1404": "Data & Analytics", "M1405": "Data & Analytics",
    "M1101": "Management & Leadership", "M1102": "Management & Leadership",
    "M1301": "Finance & Accounting", "M1302": "Management & Leadership",
    "K1303": "Social Services", "K2111": "Education", "K2105": "Education",
    "H2102": "Manufacturing", "H1502": "Manufacturing",
    "N4103": "Transportation", "N4101": "Transportation", "N4102": "Transportation",
    "D1101": "Trades", "D1102": "Trades", "D1103": "Trades", "D1104": "Trades",   # boucher, boulanger, charcutier, pâtissier
    "D1401": "Sales", "D1402": "Sales", "D1403": "Sales", "D1404": "Sales", "D1405": "Sales", "D1406": "Sales", "D1407": "Sales", "D1408": "Sales",
    "D1501": "Retail", "D1502": "Retail", "D1503": "Retail", "D1504": "Retail", "D1505": "Retail", "D1506": "Retail", "D1507": "Retail", "D1508": "Retail", "D1509": "Retail",
    "G1801": "Food & Beverage", "G1802": "Food & Beverage", "G1803": "Food & Beverage", "G1804": "Food & Beverage",
    "K2204": "Trades",   # agent de propreté (pulizie): convenzione Nivult = Trades, come «cleaning» negli ATS
    "F1102": "Art & Design", "F1101": "Art & Design",
}


def famiglia_da_rome(codice: str | None) -> str | None:
    if not codice:
        return None
    c = codice.strip().upper()
    if c in ROME_CODICI:
        return ROME_CODICI[c]
    return ROME_GRUPPI.get(c[:3])


# ── ISCO-08: gruppo minore a 3 cifre → famiglia ─────────────────────
ISCO_MINORI: dict[str, str | None] = {
    # 1 dirigenti
    "111": "Government & Public Sector", "112": "Management & Leadership", "121": "Management & Leadership",
    "122": "Management & Leadership", "131": "Management & Leadership", "132": "Management & Leadership",
    "133": "Technology", "134": "Management & Leadership", "141": "Hospitality", "142": "Retail", "143": "Management & Leadership",
    # 2 professioni intellettuali
    "211": "Science & Research", "212": "Data & Analytics", "213": "Science & Research", "214": "Engineering",
    "215": "Engineering", "216": "Art & Design", "221": "Healthcare", "222": "Healthcare", "223": "Healthcare",
    "224": "Healthcare", "225": "Healthcare", "226": "Healthcare", "231": "Education", "232": "Education",
    "233": "Education", "234": "Education", "235": "Education", "241": "Finance & Accounting", "242": "Consulting",
    "243": "Marketing", "251": "Software", "252": "Technology", "261": "Legal", "262": "Creative & Media",
    "263": "Social Services", "264": "Creative & Media", "265": "Creative & Media",
    # 3 tecnici
    "311": "Engineering", "312": "Manufacturing", "313": "Manufacturing", "314": "Science & Research",
    "315": "Transportation", "321": "Healthcare", "322": "Healthcare", "323": "Healthcare", "324": "Healthcare",
    "325": "Healthcare", "331": "Finance & Accounting", "332": "Sales", "333": "Logistics", "334": "Administrative",
    "335": "Government & Public Sector", "341": "Social Services", "342": "Sports & Recreation",
    "343": "Creative & Media", "351": "Technology", "352": "Technology",
    # 4 impiegati
    "411": "Administrative", "412": "Administrative", "413": "Administrative", "421": "Finance & Accounting",
    "422": "Customer Service & Support", "431": "Finance & Accounting", "432": "Logistics", "441": "Administrative",
    # 5 servizi e vendita
    "511": "Transportation", "512": "Food & Beverage", "513": "Food & Beverage", "514": "Trades",
    "515": "Hospitality", "516": "Social Services", "521": "Retail", "522": "Retail", "523": "Retail",
    "524": "Sales", "531": "Social Services", "532": "Healthcare", "541": "Security & Safety",
    # 6 agricoltura
    "611": "Agriculture", "612": "Agriculture", "613": "Agriculture", "621": "Agriculture", "622": "Agriculture",
    "631": "Agriculture", "632": "Agriculture", "633": "Agriculture", "634": "Agriculture",
    # 7 artigiani e operai specializzati
    "711": "Construction", "712": "Construction", "713": "Construction", "721": "Trades", "722": "Trades",
    "723": "Trades", "731": "Art & Design", "732": "Manufacturing", "741": "Trades", "742": "Trades",
    "751": "Trades", "752": "Trades", "753": "Manufacturing", "754": "Trades",
    # 8 conduttori di impianti e macchinari
    "811": "Manufacturing", "812": "Manufacturing", "813": "Manufacturing", "814": "Manufacturing",
    "815": "Manufacturing", "816": "Manufacturing", "817": "Manufacturing", "818": "Manufacturing",
    "821": "Manufacturing", "831": "Transportation", "832": "Transportation", "833": "Transportation",
    "834": "Logistics", "835": "Transportation",
    # 9 professioni elementari
    "911": "Trades", "912": "Trades", "921": "Agriculture", "931": "Construction", "932": "Manufacturing",
    "933": "Logistics", "941": "Food & Beverage", "951": "Sales", "952": "Retail", "961": "Trades",
    "962": "Trades",
    # 0 forze armate
    "011": "Security & Safety", "021": "Security & Safety", "031": "Security & Safety",
}


def famiglia_da_isco(codice: str | None) -> str | None:
    """Accetta «C432», «432», «4321», «http://data.europa.eu/esco/isco/C432»."""
    if not codice:
        return None
    m = re.search(r"(?:isco/)?C?(\d{3,4})\s*$", codice.strip())
    if not m:
        return None
    return ISCO_MINORI.get(m.group(1)[:3])


# SSYK 2012 = ISCO-08 con divergenze; le prime 3 cifre coincidono quasi sempre.
# Eccezioni svedesi note (gruppi che SSYK numera diversamente).
SSYK_ECCEZIONI: dict[str, str] = {
    "159": "Management & Leadership", "179": "Management & Leadership",
    "911": "Trades",   # städare
}


def famiglia_da_ssyk(codice: str | None) -> str | None:
    if not codice:
        return None
    c = re.sub(r"\D", "", codice)[:3]
    if c in SSYK_ECCEZIONI:
        return SSYK_ECCEZIONI[c]
    return ISCO_MINORI.get(c)


# ── La categoria che sceglie CHI PUBBLICA ───────────────────────────
#
# ATTENZIONE, misurato l'11/09/2026: questa categoria NON basta a timbrare
# la famiglia di un'offerta, e non e' un difetto delle mappe qui sotto.
#
#   accordo col verdetto del modello, 27.696 righe   55%
#   idem usando il titolo invece della categoria     69%
#   famiglie che reggono l'80%: 4 su 32, l'8% del volume
#   e il numero che decide: dove GLM e v1 CONCORDANO fra loro — il
#   segnale piu' forte che abbiamo — la categoria dichiarata dice lo
#   stesso solo nel 59% dei casi (29.074 righe). Un segnale informativo
#   starebbe sopra l'85%.
#
# Il motivo e' strutturale, non di grafia: la categoria dell'ATS e' un
# secchio piu' grosso delle nostre 33 famiglie. «information_technology»
# non sa dire se e' Technology, Software o Data & Analytics; «sales» non
# distingue Sales da Retail; «hospitality» non distingue Hospitality da
# Food & Beverage. Dove noi tagliamo fine, lei sceglie la meta' sbagliata
# circa una volta su tre.
#
# Percio': si usa come INDIZIO nel fascicolo del giudice esterno (v2), mai
# per scrivere in job_classifications. Scriverla li' sarebbe peggio che
# lasciarla vuota — i classificatori usano ON CONFLICT (job_id) DO NOTHING,
# quindi una famiglia dichiarata sbagliata resterebbe li' per sempre e
# impedirebbe al modello di rimediare.

# Non e' un codice ufficiale, ma e' pur sempre un'etichetta umana, e
# arriva gratis nel raw. Due forme molto diverse:
#
#  1. un VOCABOLARIO chiuso, quando l'ATS impone un elenco a tendina.
#     smartrecruiters («information_technology») e workable («Information
#     Technology») usano lo stesso elenco — le job function di LinkedIn —
#     scritto in due modi; recruitee ha il suo, 44 voci. Qui la mappa e'
#     esatta e si puo' fidare.
#  2. TESTO LIBERO, quando l'ATS lascia scrivere al tenant: iCIMS
#     («Heart Of House», «Kirkland's Home»), oracle, inrecruiting, hirehive.
#     1.773 valori distinti su 20.000 righe iCIMS. Qui servono regole per
#     parole, in ordine dal piu' specifico al piu' generico.
#
# In entrambi i casi: `None` dove la voce e' davvero mista («other»,
# «general_business», «technical»). Una famiglia sbagliata e' peggio di
# una mancante — finisce nei filtri del digest.


def _norm(v: str | None) -> str:
    """«Accounting &amp; Finance» -> «accounting & finance»."""
    if not isinstance(v, str):
        return ""
    v = html.unescape(html.unescape(v))
    return re.sub(r"\s+", " ", v.replace("\xa0", " ")).strip().lower()


# le job function di LinkedIn, come le espongono smartrecruiters e workable
FUNZIONI: dict[str, str | None] = {
    "information technology": "Technology",
    "sales": "Sales",
    "engineering": "Engineering",
    "health care provider": "Healthcare",
    "customer service": "Customer Service & Support",
    "consulting": "Consulting",
    "production": "Manufacturing",
    "manufacturing": "Manufacturing",
    "administrative": "Administrative",
    "finance": "Finance & Accounting",
    "marketing": "Marketing",
    "management": "Management & Leadership",
    "education": "Education",
    "business development": "Sales",
    "human resources": "Human Resources",
    "project management": "Management & Leadership",
    "supply chain": "Logistics",
    "accounting auditing": "Finance & Accounting",
    "distribution": "Logistics",
    "legal": "Legal",
    "design": "Art & Design",
    "research": "Science & Research",
    "product management": "Management & Leadership",
    "science": "Science & Research",
    "art creative": "Art & Design",
    "purchasing": "Logistics",
    "strategy planning": "Management & Leadership",
    "training": "Education",
    "writing editing": "Creative & Media",
    "advertising": "Marketing",
    "public relations": "Marketing",
    "data analyst": "Data & Analytics",
    # miste per costruzione: si lasciano al modello
    "other": None, "general business": None, "analyst": None,
    "business analyst": None, "quality assurance": None,
}

# recruitee, 44 voci
RECRUITEE: dict[str, str | None] = {
    "information_technology": "Technology", "internet": "Technology",
    "telecommunication": "Technology",
    "healthcare": "Healthcare", "retail": "Retail", "engineering": "Engineering",
    "hospitality": "Hospitality", "tourism": "Hospitality",
    "sales": "Sales", "logistics": "Logistics", "procurement": "Logistics",
    "consulting": "Consulting", "accountancy": "Finance & Accounting",
    "finance": "Finance & Accounting", "banking": "Finance & Accounting",
    "insurance": "Finance & Accounting",
    "manufacturing": "Manufacturing", "construction": "Construction",
    "administrative": "Administrative", "marketing_pr": "Marketing",
    "advertising": "Marketing", "education": "Education",
    "security": "Security & Safety", "recruitment_hr": "Human Resources",
    "government_nonprofit": "Government & Public Sector",
    "customer_service": "Customer Service & Support",
    "legal_services": "Legal", "management": "Management & Leadership",
    "design": "Art & Design", "architectural_services": "Art & Design",
    "energy": "Energy", "publishing": "Creative & Media",
    "arts_entertainment": "Creative & Media", "cleaning": "Trades",
    "biotech_pharma": "Science & Research", "science": "Science & Research",
    "agriculture": "Agriculture", "leisure": "Sports & Recreation",
    # miste: «technical» sta tanto per mestieri quanto per ingegneria,
    # «automotive» e «property» sono settori, non mestieri
    "other": None, "technical": None, "automotive": None, "property": None,
    "translation_services": None,
}


# Le regole per il testo libero. L'ORDINE E' LA REGOLA: la prima che
# aggancia vince, quindi si va dal piu' specifico al piu' generico.
# «Retail Banking Center» e' banca, non negozio; «Engineering-Software»
# e' software, non ingegneria; «Retail Tax Leadership» e' fisco, non
# negozio; «Food & Beverage Management» e' cucina, non direzione.
_REGOLE: list[tuple[str, str]] = [
    (r"\bbank|\bcredit union", "Finance & Accounting"),
    (r"\btax\b|accounting|\bauditor|bookkeep", "Finance & Accounting"),
    (r"software|developer|\bsap\b|\berp\b|programm|full.?stack|devops|"
     r"web develop|mobile develop|\bqa engineer", "Software"),
    # «IT:» e «Information Technology» prima dell'ingegneria, o
    # «IT: … / Engineer» finirebbe fra gli ingegneri
    (r"^it\b|\bit:|information technology|\binformation systems\b", "Technology"),
    # e lo sport prima della scuola, o «Athletics & Coaching (School
    # Based)» diventerebbe insegnamento
    (r"athletic|\bsport|coaching|\bfitness|recreation|\bleisure|\bgym\b",
     "Sports & Recreation"),
    (r"\bengineer(ing|s)?\b|\bengineer\b", "Engineering"),
    (r"merchandis", "Retail"),
    (r"arborist|landscap|horticultur|\bfarm|agricultur|\bgrower", "Agriculture"),
    (r"caregiv|\bcare ?giver", "Healthcare"),
    (r"direct support|developmental disabilit|transitional housing|"
     r"social work|social servic|case manage|\bshelter\b", "Social Services"),
    (r"\bnurs|\brn\b|\blpn\b|\bcna\b|physician|\bdoctor|\bmedical\b|clinic|"
     r"patient|\btherap|radiolog|pharmac|\bimaging\b|respiratory|\bdental|"
     r"veterinar|allied health|health ?care|\bhealth\b|\bmidwif|hospice|"
     r"surgic|\bicu\b|phlebotom|sonograph|optometr|chiropract|"
     r"resident care|direct care|rehab|speech language|\bdialysis", "Healthcare"),
    (r"culinar|kitchen|restaurant|\bcook\b|\bchef\b|food (service|&|and)|"
     r"dining|barista|catering|banquet|dietary|\bbaker|beverage|"
     r"front of house|heart of house|back of house|concession|"
     r"\bwait(er|ress|staff)|\bbartend|\bdeli\b|\bbistro", "Food & Beverage"),
    (r"hospitality|\bhotel|housekeep|\bguest\b|casino|resort|front desk|"
     r"concierge|tourism|\bspa\b|\blodging", "Hospitality"),
    (r"retail|\bstore(s)?\b|cashier|\bcass(a|iere)|sales associate|"
     r"shop sales|\bboutique|store associate", "Retail"),
    (r"teacher|\bschool|education|academic|\bfacult|\btutor|\bprofessor|"
     r"instructor|childcare|\bdaycare|curriculum", "Education"),
    (r"\blegal\b|attorney|\blawyer|paralegal|compliance counsel|"
     r"paralegale|\bavvocat", "Legal"),
    (r"security|\bguard\b|\bpolice|firefight|loss prevention|surveillance|"
     r"\bsafety\b|\bpubblica sicurezza", "Security & Safety"),
    (r"\bhvac\b|plumb|electrician|\belettricist|carpent|welder|weld\b|"
     r"\bmason|painter|locksmith|janitor|custodial|cleaning|\bpulizi|"
     r"maintenance|service technician|automotive|body technician|"
     r"\bmechanic|field service|\bcraft\b|skilled trade|\belectrical\b|"
     r"\brepair\b|groundskeep|\bfitter\b|\binstaller", "Trades"),
    (r"construction|\bbuilding site|\bcantier|\broofing|\bscaffold|"
     r"\bsurveyor|civil works", "Construction"),
    (r"transport|\bdriver(s)?\b|\btrucking|\bchauffeur|\baviation|"
     r"\bpilot\b|\bfleet\b|\bautist|\bcourier|\bdelivery driver", "Transportation"),
    (r"logistic|warehouse|\bdistribution\b|supply chain|procurement|"
     r"\bpurchasing|\bmagazzin|\binventory|\bforklift", "Logistics"),
    (r"\bfinance\b|\bfinanci|treasur|\bpayroll|\bbilling|\bcredit\b|"
     r"\binsurance|\bactuar", "Finance & Accounting"),
    (r"human resources|\bhr\b|recruit|talent acquisition|\brisorse umane",
     "Human Resources"),
    (r"marketing|advertis|\bbrand\b|public relations|\bcommunications?\b|"
     r"\bsocial media", "Marketing"),
    (r"\bsales\b|business development|account (executive|manager)|"
     r"\bcommercial(e|i)\b|\bvendit", "Sales"),
    (r"customer (service|support|care|advisor|experience)|client service|"
     r"call cent|contact cent|help ?desk|\bassistenza client", "Customer Service & Support"),
    (r"information technology|\bit\b|\bit:|infrastructure|cyber|"
     r"\bnetwork|systems admin|telecommunicat|\btechnology\b|\bhelpdesk|"
     r"\bcloud\b|\binformatic", "Technology"),
    (r"\bdata\b|analytics|business intelligence|data scien|\bstatistic",
     "Data & Analytics"),
    (r"manufactur|\bproduction\b|\bplant\b|assembly|machinist|\bfabricat|"
     r"\bproduzione|\bmetalmeccanic|\bfoundry|\bmilling", "Manufacturing"),
    (r"\bscience|research|laborator|\blab\b|\bchemist|\bbiolog|\bclinical trial",
     "Science & Research"),
    (r"\bdesign|graphic|\bcreative\b|\bart\b|architect|\bphotograph|"
     r"\bvideo\b|\bux\b|\bui\b", "Art & Design"),
    (r"athletic|\bsport|coaching|\bfitness|recreation|\bleisure|\bgym\b",
     "Sports & Recreation"),
    (r"\benergy|\boil (and|&) gas|\butilit|renewable|\bsolar\b|\bwind farm",
     "Energy"),
    (r"environment|sustainab|\brecycling|\bwaste manage", "Environmental & Sustainability"),
    (r"government|public sector|\bmunicipal|\bcivil servic|\bpubblica amministr",
     "Government & Public Sector"),
    (r"consulting|consultant|\bconsulen", "Consulting"),
    (r"journalis|\beditorial|\bwriting\b|\bcontent\b|\bpublish|\bbroadcast|"
     r"\bmedia\b", "Creative & Media"),
    (r"management|leadership|\bdirector\b|\bexecutive\b|general manager|"
     r"\bdirezione\b|\bteam lead", "Management & Leadership"),
    (r"administrat|clerical|receptionist|\boffice support|back office|"
     r"\bsecretar|\bsegretari|\bdata entry\b|\badmin\b", "Administrative"),
]
REGOLE: list[tuple[re.Pattern[str], str]] = [
    (re.compile(rx, re.I), fam) for rx, fam in _REGOLE]


def famiglia_da_funzione(v: str | None) -> str | None:
    """Dal vocabolario chiuso di smartrecruiters/workable/recruitee.

    La stessa voce arriva in tre grafie: «accounting_auditing» da
    smartrecruiters, «Accounting/Auditing» da workable, «accountancy» da
    recruitee. Underscore e barra diventano spazio prima del confronto.
    """
    k = _norm(v)
    if not k:
        return None
    if k in RECRUITEE:                       # il vocabolario proprio
        return RECRUITEE[k]
    k = re.sub(r"[_/]+", " ", k).strip()
    if k in RECRUITEE:
        return RECRUITEE[k]
    return FUNZIONI.get(k)


def famiglia_da_categoria(v: str | None) -> str | None:
    """Da una categoria scritta a mano dal tenant. None dove e' mista."""
    t = _norm(v)
    if not t or len(t) > 120:
        return None
    for rx, fam in REGOLE:
        if rx.search(t):
            return fam
    return None
