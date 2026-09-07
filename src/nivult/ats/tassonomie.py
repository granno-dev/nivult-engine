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
