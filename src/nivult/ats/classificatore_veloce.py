"""Il classificatore senza AI: dizionario multilingue + occupazioni API.

    python -m nivult.ats.classificatore_veloce --limite 50000
    python -m nivult.ats.classificatore_veloce --stats

Classifica le offerte in famiglie professionali SENZA chiamate GLM:
1. DIZIONARIO — le parole nei titoli dicono la famiglia ("nurse",
   "krankenschwester", "sviluppatore" → la risposta è nel nome)
2. OCCUPAZIONI API — molte API classificano già (Arbetsförmedlingen
   dà occupation.label, SmartRecruiters dà department)

Velocità: migliaia di titoli al secondo. Costo: zero.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import unicodedata

import psycopg

log = logging.getLogger("nivult.ats.classificatore_veloce")

ATS_DSN = os.environ.get(
    "ATS_DATABASE_URL",
    "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")

# Le 33 famiglie del vocabolario del motore
FAMIGLIE = [
    "Administrative", "Agriculture", "Art & Design", "Construction",
    "Consulting", "Creative & Media", "Customer Service & Support",
    "Data & Analytics", "Education", "Energy", "Engineering",
    "Environmental & Sustainability", "Finance & Accounting",
    "Food & Beverage", "Government & Public Sector", "Healthcare",
    "Hospitality", "Human Resources", "Legal", "Logistics",
    "Management & Leadership", "Manufacturing", "Marketing", "Retail",
    "Sales", "Science & Research", "Security & Safety", "Social Services",
    "Software", "Sports & Recreation", "Technology", "Trades",
    "Transportation",
]

# ── IL DIZIONARIO ─────────────────────────────────────────────────
# Ogni famiglia: le parole (in tutte le lingue dei nostri paesi) che
# nel titolo identificano quella famiglia. Ordinate per specificità:
# il sistema cerca PRIMA le parole più specifiche, poi le generiche.

DIZIONARIO: dict[str, list[str]] = {
    "Healthcare": [
        "behavior analyst", "behavior technician", "bcba", "rbt", "operatore socio sanitario", "service hospitalier",
        # EN
        "nurse", "doctor", "physician", "surgeon", "dentist", "pharmacist",
        "midwife", "therapist", "psychologist", "paramedic", "caregiver",
        "care assistant", "healthcare", "medical", "clinical", "hospital",
        "psychiatrist", "radiologist", "veterinary", "veterinarian",
        "optometrist", "chiropodist", "podiatrist", "sonographer",
        "physiotherapist", "occupational therapist", "speech therapist",
        # FR
        "infirmier", "infirmière", "médecin", "docteur", "chirurgien",
        "dentiste", "pharmacien", "sage-femme", "kinésithérapeute",
        "psychologue", "aide-soignant", "soignant", "santé", "médical",
        "hospitalier", "auxiliaire de puériculture",
        # DE
        "krankenschwester", "krankenpfleger", "pfleger", "arzt", "ärztin",
        "zahnarzt", "apotheker", "hebamme", "physiotherapeut",
        "psychologe", "pflege", "medizinisch", "rettungsassistent",
        "gesundheits", "pflegekraft", "altenpflege",
        # IT
        "infermiere", "infermiera", "medico", "chirurgo", "dentista",
        "farmacista", "ostetrica", "fisioterapista", "psicologo",
        "operatore socio-sanitario", "oss", "sanitario", "infermier",
        # SV
        "sjuksköterska", "undersköterska", "läkare", "tandläkare",
        "apotekare", "barnmorska", "fysioterapeut", "psykolog",
        "vårdbiträde", "vård", "omsorg",
        # NL
        "verpleegkundige", "arts", "tandarts", "apotheker",
        "verzorgende", "zorg",
        # ES / PT / PL
        "enfermero", "enfermera", "médico", "farmacéutico",
        "enfermeiro", "enfermeira", "pielęgniarka", "lekarz",
   
        "esthetician",
        "esthéticienne",
        "estheticienne",
        "esthéticien",
        "aide-soignante",
        "aide soignant",
        "infirmier diplômé",
        "médecin généraliste",
        "médecin du travail",
        "ergothérapeute",
        "diététicien",
        "diététicienne",
        "audio prothésiste",
        "orthoptiste",
        "psychomotricien",
   
        "betreuungskraft",
        "pflegehelfer",
        "ergotherapeut",
        "pflegedienstleitung",
        "medizinische",
        "rettungsdienst",
        "sanitäter",
        "notfallsanitäter",
        "radiologie",
        "laborant",
        "medizinisch-technische",
        "mtla",
        "auxiliaire de vie",
        "aide à domicile",
        "aide-ménagère",
        "orthophoniste",
        "audioprothésiste",
        "opticien",
        "pédicure",
        "assistant dentaire",
        "secrétaire médicale",
        "brancardier",
        "ambulancier",
        "puéricultrice",
        "röntgensjuksköterska",
        "omsorgspersonal",
        "tandsköterska",
        "arbetsterapeut",
        "kurator",
        "vårdadministratör",
        "sjukgymnast",
        "ambulanssjukvårdare",
        "ziekenverzorger",
        "verloskundige",
        "eerstelijnszorg",
        "técnico de saúde",
        "opiekun",
        "ratownik",
    ],
    "Software": [
        "software developer", "software engineer", "programmer",
        "programmatore", "entwickler", "utvecklare", "backend", "frontend",
        "full stack", "fullstack", "devops", "mobile developer",
        "ios developer", "android developer", "web developer",
        "systeemontwikkelaar", "informatyk", "programador",
        "développeur", "software", "coding", "javascript", "python", "informatik", "werkstudent informatik",
        "java developer", "php developer", "ruby", "golang", "scala",
    ],
    "Data & Analytics": [
        "data scientist", "data analyst", "data engineer", "data analyst",
        "analyste de données", "datenanalyst", "data-analist",
        "business intelligence", "bi analyst", "statistico", "statistician",
        "machine learning", "deep learning", "analista de datos",
        "analyst", "analytics",
    ],
    "Engineering": [
        # EN
        "engineer", "mechanical", "electrical engineer", "civil engineer",
        "structural engineer", "process engineer", "quality engineer",
        "design engineer", "project engineer", "manufacturing engineer",
        "industrial engineer", "chemical engineer", "aerospace",
        "automotive engineer", "robotics",
        # FR
        "ingénieur", "ingénieure",
        # DE
        "ingenieur", "ingenieurin", "maschinenbau", "elektrotechnik",
        # IT
        "ingegnere", "ingegner",
        # SV
        "ingenjör", "civilingenjör",
        # NL
        "werktuigbouwkundig", "elektrisch ingenieur",
        # altre
        "inżynier", "ingeniero",
    ],
    "Finance & Accounting": [
        "accountant", "comptable", "buchhalter", "contabile",
        "bookkeeper", "financial analyst", "analyste financier",
        "controller", "cfo", "auditor", "revisor", "tax advisor",
        "fiscaliste", "steuerberater", "commercialista",
        "finance", "financial", "comptabilité", "rechnungswesen",
        "amministrazione", "financieel", "payroll", "creditor", "debitor",
        "eigentelijke", "finanzas", "contabilidade",
    ],
    "Sales": [
        "agente immobiliare", "agente d affari", "consulente commerciale",
        "sales", "salesperson", "account executive", "account manager",
        "business development", "commercial", "vendite", "vendita",
        "verkäufer", "vertrieb", "säljare", "försäljning",
        "verkoper", "verkoop", "sprzedaż", "ventas", "vendas",
        "inside sales", "outside sales", "sales representative",
        "rappresentante", "représentant", "vertreter",
   
        "vertriebsmitarbeiter",
        "verkäufer im außendienst",
        "key account",
        "vertriebsleiter",
        "handelsvertreter",
        "gebietsverkaufsleiter",
        "telefonverkäufer",
        "vertriebsingenieur",
        "commercial terrain",
        "commercial sédentaire",
        "technico-commercial",
        "chargé de clientèle",
        "responsable commercial",
        "animateur commercial",
        "vendeur terrain",
        "téléconseiller commercial",
        "chargé d'affaires",
        "försäljningschef",
        "key account manager",
        "affärsutvecklare",
        "försäljningsansvarig",
    ],
    "Marketing": [
        "marketing", "marketeer", "marketing manager", "digital marketing",
        "content marketing", "seo", "sem", "social media", "brand manager",
        "growth hacker", "copywriter", "email marketing",
        "marketing specialist", "responsable marketing",
        "marketingverantwoordelijke",
    ],
    "Human Resources": [
        "hr", "human resources", "recruiter", "recruitment",
        "talent acquisition", "hr manager", "hr business partner",
        "personnel", "risorse umane", "recruitment consultant",
        "recruteur", "personalberater", "personalreferent",
        "rekryterare", "rekryteringskonsult", "hr-consultant",
        "personnels", "hr specialist", "hr generalist",
        "payroll specialist", "compensation", "benefits",
    ],
    "Retail": [
        "assistente di negozio", "commerciante al dettaglio", "einzelhandel", "addetto alle vendite", "addetto vendita",
        "retail", "shop assistant", "store manager", "cashier",
        "commesso", "venditore al dettaglio", "verkäufer",
        "butikssäljare", "butiksmedarbetare", "winkelmedewerker",
        "kassamedewerker", "sprzedawca", "dependiente", "vendedor",
        "sales assistant", "retail assistant", "shop worker",
        "visual merchandiser",
   
        "kassierer",
        "einzelhandelskaufmann",
        "filialleiter",
        "verkäuferin",
        "abteilungsleiter",
        "warenhaus",
        "vendeur",
        "vendeuse",
        "caissier",
        "caissière",
        "magasinier",
        "chef de rayon",
        "responsable de magasin",
        "hôte de caisse",
        "conseiller de vente",
        "assistant de vente",
        "kassör",
        "butikschef",
        "försäljningsmedarbetare",
        "varuhus",
    ],
    "Logistics": [
        "magazziniere", "carrellista", "mulettista", "addetto al magazzino", "facchino", "cariste",
        "logistics", "supply chain", "warehouse", "magazzino",
        "lager", "lagermitarbeiter", "warehouse operative",
        "spedition", "freight", "spedizioniere", "logistiek",
        "logistyka", "logística", "distribuzione", "distribution",
        "forklift", "carrello elevatore", "staplerfahrer",
    ],
    "Transportation": [
        "driver", "chauffeur", "autista", "camionista", "fahrer",
        "lastbilförare", "truck driver", "bus driver", "taxi driver",
        "chauffeur", "vrachtwagenchauffeur", "kierowca", "conductor",
        "pilota", "pilot", "conducător", "marittimo", "ship",
        "delivery driver", "consegne", "livraison", "zustellung",
   
        "postbote",
        "zusteller",
        "kraftfahrer",
        "busfahrer",
        "lkw-fahrer",
        "lieferfahrer",
        "speditionskaufmann",
        "disponent",
        "berufskraftfahrer",
        "fahrzeugführer",
        "zugfahrer",
        "conducteur",
        "chauffeur livreur",
        "livreur",
        "conducteur de bus",
        "conducteur poids lourd",
        "convoyeur",
        "facteur",
        "conducteur de train",
        "agent de quai",
        "bussförare",
        "lastbilsförare",
        "budbil",
        "distributör",
        "järnvägstjänsteman",
        "tågmästare",
        " taxi",
        "bezorger",
        "postbezorger",
        "buschauffeur",
        "treinmachinist",
    ],
    "Construction": [
        "construction", "construction worker", "edile", "muratore",
        "bauarbeiter", "byggnadsarbetare", "bouwvakker", "budowlany",
        "construcción", "construção", "site manager", "capocantiere",
        "bauleiter", "plumber", "idraulico", "installateur",
        "electrician", "elektriker", "électricien", "elektricien",
        "carpenter", "carpentiere", "tischler", "snickare",
        "painter", "pittore", "maler", "målare",
        "bricklayer", "maçon", "maurer", "murare",
    ],
    "Trades": [
        "elettricista", "chauffagiste", "manutentore", "termoidraulico", "installatore", "macon",
        "electrician", "elektriker", "électricien", "elektricien",
        "plumber", "idraulico", "installateur", "klempner",
        "mechanic", "meccanico", "mechaniker", "mekaniker",
        "welder", "saldatore", "schweißer", "svetsare",
        "carpenter", "falegname", "tischler", "snickare",
        "hairdresser", "parrucchiere", "friseur", "frisör",
        "tailor", "sarto", "schneider", "skräddare",
        "butcher", "macellaio", "metzger", "slaktare",
        "baker", "panettiere", "bäcker", "bagare",
   
        "workshop technician",
        "bench technician",
        "technicien",
        "technicien de maintenance",
        "maintenance technician",
   
        "elektroniker",
        "sanitärinstallateur",
        "mechatroniker",
        "anlagenmechaniker",
        "dachdecker",
        "maurer",
        "betonbauer",
        "gerüstbauer",
        "straßenbauer",
        "tiefbauer",
        "fleischer",
        "konditor",
        "thermobâcheur",
        "couvreur",
        "maçon",
        "plombier",
        "menuisier",
        "charpentier",
        "peintre en bâtiment",
        "carreleur",
        "coiffeur",
        "boulanger",
        "pâtissier",
        "boucher",
        "plåtslagare",
        "målare",
        "golvläggare",
        "betongarbetare",
        "grundläggare",
    ],
    "Food & Beverage": [
        "lavapiatti", "plongeur", "aiuto cuoco", "pizzaiolo",
        "chef", "cook", "cuoco", "koch", "kock", "kok",
        "waiter", "waitress", "cameriere", "cameriera", "kellner",
        "servitör", "servitrice", "ober", "kelner", "kelnerka",
        "barista", "barman", "bartender", "sommelier",
        "kitchen", "cucina", "küche", "kök", "keuken",
        "restaurant", "ristorante", "gaststätte", "restaurang",
        "pizzeria", "catering", "gastronomia", "gastronomie",
   
        "commis de cuisine",
        "cuisinier",
        "cuisinière",
        "pizzaiolo",
        "barback",
        "commis",
        "chef de partie",
        "chef de rang",
        "maître d'",
        "maitre d'",
        "plongeur",
        "plongeuse",
        "économe",
        "econome",
        "sous-chef",
        "sous chef",
   
        "köchin",
        "kellnerin",
        "servicekraft",
        "restaurantfachmann",
        "hotelfachmann",
        "barkeeper",
        "barmeister",
        "serveur",
        "serveuse",
        "traiteur",
        "servitris",
        "kallskänka",
        "konditori",
    ],
    "Hospitality": [
        "hotel", "receptionist", "reception", "front desk",
        "receptioniste", "receptionist", "receptionistin",
        "housekeeping", "cameriera ai piani", "hotel manager",
        "hospitality", "hospitality manager", "turismo", "tourism",
        "tourisme", "tourismus", "tourism",
    ],
    "Customer Service & Support": [
        "customer service", "customer support", "helpdesk",
        "call center", "callcentre", "servizio clienti",
        "kundendienst", "kundtjänst", "klantenservice",
        "service client", "customer care", "customer success",
        "technical support", "supporto tecnico", "technischer support",
    ],
    "Administrative": [
        "administrative", "admin assistant", "secretary", "sekretär",
        "secretaire", "segretaria", "assistente amministrativo",
        "office manager", "receptionist", "data entry",
        "amministrazione", "bürokaufmann", "kontorist",
        "office clerk", "impiegato", "impiegata", "employé",
   
        "assistante de direction",
    ],
    "Education": [
        "teacher", "insegnante", "lehrer", "lehrerin", "lärare",
        "professor", "professore", "professeur", "docente",
        "educator", "educatore", "erzieher", "förskollärare",
        "trainer", "formatore", "formateur", "trainer",
        "school", "scuola", "schule", "skola",
        "university", "università", "universität", "universitet",
        "nursery", "asilo", "kita", "förskola",
    ],
    "Legal": [
        "lawyer", "avvocato", "anwalt", "anwältin", "advokat",
        "jurist", "giurista", "juriste", "legal", "legale",
        "notary", "notaio", "notar", "notarie",
        "paralegal", "legal assistant", "avvocatessa",
        "solicitor", "barrister", "counsel",
    ],
    "Security & Safety": [
        "security", "sicurezza", "sicherheit", "säkerhet",
        "security guard", "guardia giurata", "security officer",
        "surveillance", "sorveglianza", "überwachung",
        "bodyguard", "vigilant", "beveiliging",
    ],
    "Social Services": [
        "babysitting", "baby sitter", "garde d enfant", "assistente all infanzia", "educatore",
        "social worker", "assistente sociale", "sozialarbeiter", "auxiliaire de crèche", "auxiliaire de creche",
        "socionom", "maatschappelijk werker",
        "youth worker", "educatore professionale",
        "personal assistant", "personlig assistent",
        "disability", "disabilità", "behinderung", "funktionshinder",
        "elderly care", "anziani", "älterenpflege", "äldreomsorg",
   
        "sozialpädagoge",
        "erzieher",
        "heilerziehungspflege",
        "sozialdienst",
        "jugendhilfe",
        "seniorenbetreuung",
        "behindertenhilfe",
        "sozialassistent",
        "betreuer",
        "travailleur social",
        "éducateur spécialisé",
        "assistant social",
        "éducatrice de jeunes enfants",
        "auxiliaire de vie sociale",
        "aide familiale",
        "conseiller en économie sociale",
        "accompagnant éducatif",
        "moniteur éducateur",
        "stödassistent",
        "behandlingsassistent",
        "boendestödjare",
        "arbetsledare",
        "daglig verksamhet",
        "lss-handläggare",
        "sociaal werker",
        "begeleider",
        "persoonlijk begeleider",
        "woonbegeleider",
        "educador social",
        "pracownik socjalny",
        "opiekun osoby starszej",
    ],
    "Manufacturing": [
        "operatore cnc", "collaudatore", "conduttore di macchine",
        "manufacturing", "produzione", "produktion", "productie",
        "production", "production worker", "operaio", "arbeiter",
        "production operator", "machine operator", "operatore macchina",
        "maschinenführer", "industriearbeiter",
        "assembly", "montaggio", "montage", "montör",
   
        "prozesstechniker",
        "produktionsmitarbeiter",
        "werker",
        "fertigung",
        "spritzguss",
        "werkzeugbau",
        "cnc",
        "fräser",
        "dreher",
        "schweißer",
        "industriemechaniker",
        "elektroniker",
        "anlagenführer",
        "produktionshelfer",
        "opérateur de production",
        "agent de production",
        "conducteur d'installations",
        "technicien de production",
        "opérateur sur machine",
        "cariste",
        "agent de fabrication",
        "conducteur de ligne",
        "maskinoperatör",
        "produktionsoperatör",
        "cnc-operatör",
        "industriarbetare",
        "monterare",
        "processoperatör",
        "productiemedewerker",
        "machinebediener",
        "monteur",
    ],
    "Management & Leadership": [
        "manager", "director", "head of", "vp", "chief",
        "leiter", "leitung", "chef", "responsable",
        "responsabile", "manager", "coordinator",
        "team leader", "capo reparto", "teamleiter",
        "general manager", "direttore generale", "geschäftsführer",
        "project manager", "projectleider", "projektledare",
   
    ],
    "Consulting": [
        "consultant", "consulente", "berater", "beraterin",
        "konsult", "adviseur", "konsultant",
        "management consulting", "strategy consultant",
        "business consultant", "consultancy",
    ],
    "Science & Research": [
        "geologo", "geologa", "biologo", "chimico",
        "scientist", "researcher", "ricercatore", "forscher",
        "forskare", "wetenschapper", "naukowiec",
        "laboratory", "laboratorio", "labor", "laboratorium",
        "research", "ricerca", "forschung", "forskning",
        "phd", "postdoc", "chemist", "biologist", "physicist",
    ],
    "Technology": [
        "tecnico informatico", "informatico", "sistemista", "help desk",
        "it specialist", "system administrator", "sysadmin",
        "network engineer", "amministratore di sistema",
        "systemadministrator", "it support", "it tecnico",
        "infrastructure", "cloud engineer", "architect",
        "it consultant", "database administrator", "dba",
        "devops engineer", "site reliability",
    ],
    "Art & Design": [
        "designer", "graphic designer", "ux designer", "ui designer",
        "designer grafico", "grafikdesigner", "grafisk designer",
        "art director", "creative director", "illustrator",
        "illustratore", "illustrator", "photographer", "fotografo",
        "fotograf", "fotograf", "fotógrafo", "artist", "artista",
    ],
    "Creative & Media": [
        "journalist", "giornalista", "journalist", "journalist",
        "editor", "redattore", "redakteur", "redaktör",
        "scrittore", "autor", "författare",
        "content creator", "video editor", "videomaker",
        "producer", "produttore", "produzent", "producent",
        "copywriter", "social media manager",
    ],
    "Government & Public Sector": [
        "government", "public sector", "pubblica amministrazione",
        "öffentliches", "offentlig sektor", "overheid",
        "civil servant", "funzionario", "beamter",
        "municipality", "comune", "kommun", "gemeente",
        "ministero", "ministerium", "ministry",
    ],
    "Energy": [
        "energy", "energia", "energie", "energi",
        "renewable", "rinnovabili", "erneuerbare", "fornybar",
        "solar", "wind", "eolico", "solare", "windkraft",
        "oil", "gas", "petrolio", "erdöl", "erdgas",
        "power plant", "centrale", "kraftwerk",
    ],
    "Environmental & Sustainability": [
        "environmental", "ambientale", "umwelt", "miljö",
        "sustainability", "sostenibilità", "nachhaltigkeit",
        "hållbarhet", "duurzaamheid",
        "ecology", "ecologia", "ökologie", "ekologi",
        "recycling", "riciclo", "recycling", "återvinning",
    ],
    "Agriculture": [
        "agriculture", "agricoltura", "landwirtschaft", "jordbruk",
        "farmer", "contadino", "bauer", "bonde",
        "agronomist", "agronomo", "agronom",
        "horticulture", "orticoltura", "gartenbau",
        "livestock", "allevamento", "viehzucht",
    ],
    "Sports & Recreation": [
        "sport", "sportivo", "sport", "idrott",
        "fitness", "personal trainer", "istruttore fitness",
        "fitness trainer", "coach", "allenatore", "trainer",
        "gym", "palestra", "fitnessstudio", "gym",
        "swimming", "nuoto", "schwimmen", "simning",
    ],
}

# Prepara il lookup: parola → famiglia, con le parole più lunghe prima
# (per evitare che "nurse" matchi prima di "registered nurse")
_LOOKUP: list[tuple[str, str]] = []
for famiglia, parole in DIZIONARIO.items():
    for parola in parole:
        _LOOKUP.append((parola.lower(), famiglia))
_LOOKUP.sort(key=lambda x: -len(x[0]))


def _pulisci_titolo(titolo: str) -> str:
    """Minuscolo, senza accenti, per il matching."""
    t = unicodedata.normalize("NFKD", titolo or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    # la punteggiatura che incolla le parole diventa spazio: cosi'
    # "operaio/a", "garde d'enfant", "impiegato-contabile" si spezzano
    # e le parole del dizionario le ritrovano
    t = re.sub(r"[/'’‘\-.,;:()\[\]]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def classifica_titolo(titolo: str) -> tuple[str | None, float]:
    """(famiglia, confidenza) dal solo titolo. None se non matcha."""
    t = _pulisci_titolo(titolo)
    if not t or len(t) < 3:
        return None, 0.0
    import unicodedata
    for parola, famiglia in _LOOKUP:
        # WORD BOUNDARY: 'sem' non deve matchare dentro 'Assembler'.
        # Una parola matcha solo se è una parola intera nel titolo.
        # Anche le parole del dizionario vengono pulite (senza accenti)
        # così 'röntgensjuksköterska' matcha 'rontgensjukskoterska'
        p = "".join(c for c in unicodedata.normalize("NFKD", parola)
                     if not unicodedata.combining(c))
        if re.search(r"\b" + re.escape(p) + r"\b", t):
            # confidenza più alta se la parola è lunga (più specifica)
            conf = min(len(parola) / len(t) + 0.3, 0.95)
            return famiglia, round(conf, 2)
    return None, 0.0


# ── LE OCCUPAZIONI GIA' CLASSIFICATE NELLE API ────────────────────

# Arbetsförmedlingen: occupation.label → famiglia nostra
_OCCUPAZIONI_AF = {
    "sjuksköterska": "Healthcare", "undersköterska": "Healthcare",
    "läkare": "Healthcare", "tandläkare": "Healthcare",
    "personlig assistent": "Social Services",
    "städare": "Administrative", "butikssäljare": "Retail",
    "dataingenjör": "Engineering", "systemutvecklare": "Software",
    "lärare": "Education", "förskollärare": "Education",
    "socionom": "Social Services", "psykolog": "Healthcare",
    "ekonom": "Finance & Accounting", "jurist": "Legal",
    "säljare": "Sales", "kock": "Food & Beverage",
    "servitör": "Food & Beverage", "snickare": "Trades",
    "elektriker": "Trades", "mekaniker": "Trades",
    "lastbilsförare": "Transportation", "busförare": "Transportation",
    "vårdbiträde": "Healthcare", "omsorg": "Social Services",
    "säkerhetsrådgivare": "Security & Safety",
    "bilskadereparatör": "Trades",
}



# ── I CODICI OCCUPAZIONE UFFICIALI: la classificazione multilingua vera ──
# I servizi pubblici europei etichettano ogni offerta con un codice
# occupazione standard, indipendente dalla lingua. Mapparlo alle nostre
# famiglie classifica in QUALSIASI lingua UE senza dizionario — e' cosi'
# che si copre l'Europa, non aggiungendo parole a mano lingua per lingua.

# ISCO-08, sub-major group (le due cifre dopo "C"): lo standard mondiale,
# usato da EURES per tutti i 27 paesi UE. ~40 gruppi -> le nostre famiglie.
ISCO_SUBMAJOR = {
    "11": "Management & Leadership", "12": "Management & Leadership",
    "13": "Management & Leadership", "14": "Management & Leadership",
    "21": "Engineering", "22": "Healthcare", "23": "Social Services",
    "24": "Finance & Accounting", "25": "Software", "26": "Legal",
    "31": "Engineering", "32": "Healthcare", "33": "Sales",
    "34": "Social Services", "35": "Technology",
    "41": "Administrative", "42": "Administrative",
    "43": "Administrative", "44": "Administrative",
    "51": "Hospitality", "52": "Sales", "53": "Healthcare",
    "54": "Security & Safety",
    "71": "Trades", "72": "Trades", "73": "Trades", "74": "Trades",
    "75": "Manufacturing",
    "81": "Manufacturing", "82": "Manufacturing", "83": "Transportation",
    "93": "Manufacturing", "94": "Food & Beverage", "95": "Sales",
    "96": "Environmental & Sustainability",
}

# ROME (France Travail), lettera iniziale = grande dominio.
ROME_DOMINIO = {
    "C": "Finance & Accounting", "D": "Sales", "E": "Marketing",
    "F": "Trades", "G": "Hospitality", "H": "Manufacturing",
    "I": "Trades", "J": "Healthcare", "K": "Social Services",
    "M": "Administrative", "N": "Logistics",
}

# Arbetsförmedlingen: occupation_field, i grandi gruppi svedesi (ESCO).
AF_FIELD = {
    "hälso- och sjukvård": "Healthcare",
    "administration, ekonomi, juridik": "Administrative",
    "försäljning, inköp, marknadsföring": "Sales",
    "yrken med social inriktning": "Social Services",
    "pedagogik": "Social Services", "data/it": "Software",
    "transport, distribution, lager": "Logistics",
    "industriell tillverkning": "Manufacturing",
    "hotell, restaurang, storhushåll": "Hospitality",
    "bygg och anläggning": "Trades",
    "chefer och verksamhetsledare": "Management & Leadership",
    "yrken med teknisk inriktning": "Engineering",
    "naturvetenskapligt arbete": "Science & Research",
    "kropps- och skönhetsvård": "Trades",
    "säkerhetsarbete": "Security & Safety",
    "militärt arbete": "Security & Safety",
    "naturbruk": "Environmental & Sustainability",
}


def _isco_da_eures(raw: dict):
    for code in (raw.get("jobCategoriesCodes") or []):
        if isinstance(code, str) and "/isco/C" in code:
            due = code.rsplit("/isco/C", 1)[-1][:2]
            fam = ISCO_SUBMAJOR.get(due)
            if fam:
                return fam
    return None


def classifica_da_raw(raw: dict, piattaforma: str) -> tuple[str | None, float]:
    """La classificazione dal codice occupazione ufficiale, in ogni lingua.

    I servizi pubblici portano un codice standard per occupazione: si
    mappa quello, non il titolo. Cosi' un'offerta polacca, greca o
    portoghese si classifica bene quanto una inglese, perche' il codice
    non dipende dalla lingua.
    """
    if not isinstance(raw, dict):
        return None, 0.0

    if piattaforma == "eures":
        fam = _isco_da_eures(raw)
        if fam:
            return fam, 0.97

    if piattaforma == "francetravail":
        # prima il libelle' pulito (il dizionario lo riconosce spesso),
        # poi la lettera del codice ROME come rete
        lib = raw.get("romeLibelle")
        if lib:
            fam, conf = classifica_titolo(lib)
            if fam:
                return fam, max(conf, 0.9)
        code = raw.get("romeCode") or ""
        fam = ROME_DOMINIO.get(code[:1].upper()) if code else None
        if fam:
            return fam, 0.85

    if piattaforma == "bundesanstellung":
        beruf = raw.get("hauptberuf")
        if beruf:
            fam, conf = classifica_titolo(beruf)
            if fam:
                return fam, max(conf, 0.9)

    if piattaforma == "arbetsformedlingen":
        campo = (raw.get("occupation_field") or {}).get("label", "").lower()
        fam = AF_FIELD.get(campo.strip())
        if fam:
            return fam, 0.97
        occ = (raw.get("occupation") or {}).get("label", "")
        occ_l = occ.lower()
        for parola, famiglia in _OCCUPAZIONI_AF.items():
            if parola in occ_l:
                return famiglia, 0.98

    return None, 0.0


# ── IL CLASSIFICATORE COMPLETO ────────────────────────────────────

def classifica(dsn: str, limite: int = 50000) -> dict:
    """Classifica senza AI: dizionario + occupazioni API."""
    stats = {"viste": 0, "classificate": 0, "dizionario": 0,
             "da_api": 0, "non_matchate": 0}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT j.id, j.title, j.platform_id, j.raw
                  FROM ats_jobs j
             LEFT JOIN job_classifications c ON c.job_id = j.id
                 WHERE c.job_id IS NULL AND j.expired_at IS NULL
                 LIMIT %s
            """, (limite,))
            offerte = cur.fetchall()

        da_scrivere: list[tuple] = []
        for jid, titolo, pid, raw in offerte:
            stats["viste"] += 1
            # 1: l'API l'ha già classificata?
            famiglia, conf = classifica_da_raw(raw, pid)
            if famiglia:
                stats["da_api"] += 1
            else:
                # 2: il dizionario
                famiglia, conf = classifica_titolo(titolo)
                if famiglia:
                    stats["dizionario"] += 1
            if famiglia:
                da_scrivere.append((jid, famiglia, conf, "dizionario"))
                stats["classificate"] += 1
            else:
                stats["non_matchate"] += 1

        # scrittura in batch
        if da_scrivere:
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO job_classifications
                      (job_id, family, confidence, model)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (job_id) DO NOTHING
                """, da_scrivere)
            conn.commit()

    return stats


def stats(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL")
            vive = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM job_classifications")
            tot = cur.fetchone()[0]
            cur.execute("""
                SELECT model, count(*) FROM job_classifications
                GROUP BY 1 ORDER BY 2 DESC
            """)
            per_modello = cur.fetchall()
            cur.execute("""
                SELECT family, count(*) FROM job_classifications
                GROUP BY 1 ORDER BY 2 DESC LIMIT 12
            """)
            top = cur.fetchall()
    print(f"\nofferte vive: {vive}")
    print(f"classificate: {tot} ({tot / max(vive, 1) * 100:.0f}%)")
    print("per metodo:")
    for m, n in per_modello:
        print(f"  {m:15s} {n}")
    print("top famiglie:")
    for f, n in top:
        print(f"  {f:35s} {n}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-8s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.classificatore_veloce",
                                 description=__doc__)
    ap.add_argument("--limite", type=int, default=50000)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)

    if args.stats:
        stats(ATS_DSN)
    else:
        s = classifica(ATS_DSN, args.limite)
        print(f"\nClassificatore veloce: {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
