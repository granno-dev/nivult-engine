"""Scrive i file di etichette per le otto famiglie del buttafuori.

Le voci qui sotto vengono dalla lettura a mano dei 104 annunci, alla cieca:
nessuna uscita di modello e' stata guardata prima o durante. La forma dei file
e' quella dei primi dieci lotti, cosi' `esame_golden.py` li legge senza
modifiche.
"""
from __future__ import annotations
import json
import os

INDICE = os.environ.get("INDICE", "golden-tec/indice-famiglie.json")
USCITA = os.environ.get("USCITA", "golden-tec-famiglie")

# n -> [(nome, ruolo), ...]
TEC: dict[int, list[tuple[str, str]]] = {
    3: [("setters", "usata"), ("hatchers", "usata")],
    7: [("Lely Horizon", "usata"), ("Herdenmanagementprogrammen", "gradita")],
    9: [("hjorteviltregisteret", "usata"), ("jegerregisteret", "usata"),
        ("ettersøkshundregisteret", "usata")],
    13: [("Reverse Osmosis", "usata")],
    17: [("POS", "usata")],
    33: [("Prompt EMR", "usata")],
    35: [("PEG J", "usata"), ("Pompa infusionale", "usata")],
    37: [("Talkspace platform", "usata")],
    38: [("EMR", "usata")],
    39: [("X-rays", "usata")],
    47: [("Terminal Radio", "usata")],
    49: [("StoreForce", "usata")],
    51: [("Smart Cart", "usata")],
    53: [("PACE", "usata")],
    60: [("Cera Technology app", "usata")],
    65: [("Ouihelp Pro", "usata")],
    66: [("MS Office", "usata"), ("Word", "usata"), ("Excel", "usata"), ("Outlook", "usata")],
    68: [("Microsoft Word", "usata"), ("Excel", "usata"), ("PowerPoint", "usata"),
         ("Windows Operating System", "usata")],
    70: [("Svetness Fitness App", "usata")],
    71: [("AED", "usata")],
    72: [("CGM", "usata"), ("Twin platform", "usata")],
    75: [("Svetness Fitness App", "usata")],
    76: [("Svetness Fitness App", "usata")],
    78: [("snowmobile", "usata")],
    79: [("Techem IMD-system", "usata"), ("IMD", "usata")],
    81: [("NFPA 72", "usata"), ("NEC", "usata"), ("Notifier", "usata"), ("Siemens", "usata"),
         ("Edwards", "usata"), ("Simplex", "usata"), ("Fire Lite", "usata"),
         ("Gamewell FCI", "usata"), ("Honeywell", "usata")],
    83: [("groupes électropompes", "usata"), ("armoires électriques", "usata"),
         ("pompes de relevage", "usata")],
    85: [("zoom boom", "usata")],
    86: [("Videosysteme", "usata"), ("Zutrittskontrolle", "usata"), ("Intercom", "usata"),
         ("Einbruchmeldeanlagen", "usata"), ("Brandmeldeanlagen", "usata"),
         ("Leitstellensysteme", "usata")],
    87: [("tracteurs", "usata"), ("moissonneuses", "usata")],
    90: [("HEXALIS", "gradita")],
    91: [("MIG Welding", "usata"), ("D1.1 GMAW", "gradita"), ("D1.3 GMAW", "gradita"),
         ("grinders", "usata")],
    94: [("High Speed Dentistry", "servizio"), ("Digital full body and dental radiology", "servizio"),
         ("Ultrasonography", "servizio")],
    95: [("Global Positioning System", "usata")],
    99: [("Cascadia Freightliners", "usata")],
}

# n -> perche' una voce che sembrava una tecnologia non lo e'
ESCLUSE: dict[int, list[str]] = {
    1: ["techniques d'entretien paysager (mestiere)", "règles de sécurité (processo)"],
    2: ["biologicals, adjuvants, seed treatments (prodotti)", "401k (benefit)"],
    4: ["Golden Harvest, Syngenta (aziende e marchi di prodotto)", "Digital tools (generico)"],
    6: ["digital banking, web-based meetings, mobile applications (categorie generiche)"],
    9: ["Naturmangfoldloven, Viltloven, Hundeloven (leggi)",
        "skyteprøve, førerkort klasse B (abilitazioni personali)",
        "sambandsutstyr (apparato generico)"],
    16: ["Wagestream (benefit, non strumento di lavoro)"],
    20: ["restaurant reservation systems (descrittivo, non un nome)",
         "Forbes standards (regime di valutazione, non tecnologia)"],
    21: ["Wagestream (benefit)"],
    22: ["myWORKDAY (modulistica per candidarsi, non strumento del lavoro)"],
    24: ["New York City Food Standards, Executive Order 122, Directive 6 (regolamenti)"],
    27: ["Basic Life Support, CAPTE, APTA (abilitazioni e accrediti personali)"],
    31: ["BLS, ACLS, PALS, NRP (abilitazioni personali)", "NLC/eNLC (compatto di licenze)"],
    32: ["Noom, SkinIO (piattaforme offerte come benefit)"],
    34: ["OBRA (legge)"],
    38: ["CLIA, HIPAA, NCQA (leggi e accrediti)", "AAMA, NCCT, NHA, BLS, CPT (certificazioni)"],
    40: ["theScore Bet Sportsbook (prodotto dell'azienda, fuori dal mestiere)",
         "soda dispensers, coffee machines (attrezzatura generica)"],
    42: ["plan-o-gram (documento di reparto, non strumento con identita')",
         "smartphone, app (generici)"],
    44: ["Benefits@work (benefit)"],
    46: ["Pokémon TCG (merce venduta, non strumento)", "e-handel (generico)",
         "il campo text contiene un JSON serializzato: guasto di dato"],
    48: ["IT come reparto, non come strumento"],
    50: ["GDPR (legge)"],
    54: ["DailyPay (benefit)", "Narcan (farmaco)"],
    55: ["DBS check, Blue Light Card (controlli e benefit)"],
    56: ["permobil, rullstol (ausili della persona assistita, non strumenti del lavoro)",
         "il campo text contiene un JSON serializzato: guasto di dato"],
    58: ["Google Chrome, Mozilla Firefox (modulistica per candidarsi, non strumenti del lavoro)"],
    59: ["ACT, SAT (test)"],
    60: ["BHN rewards platform (benefit)"],
    63: ["case management database (descrittivo)"],
    64: ["Care Act 2014, Mental Capacity Act 2005, Section 42 (leggi)"],
    66: ["member management system (descrittivo)", "CPR/AED/First Aid (certificazioni)"],
    69: ["H2F (programma), ACFT (test)", "CAATE, BOC, NPI (accrediti)"],
    71: ["LifeMart, Aflac (benefit)"],
    72: ["AI Digital Twin technology (tecnologia propria dell'azienda, non del mestiere)"],
    74: ["Pilates (disciplina, come 'yoga': il mestiere stesso, non uno strumento)"],
    80: ["Wagestream (benefit)"],
    81: ["OSHA (ente e norma di legge)"],
    83: ["capteurs (troppo generico)"],
    84: ["BMW (marchio del datore e dei veicoli, non sistema su cui si lavora)",
         "ASE (certificazione)"],
    85: ["CSTS 2020, H2S Alive, Fall Protection (tessere di sicurezza)"],
    89: ["TOYOTA (marchio del datore)"],
    90: ["logiciel de gestion de laboratoire (descrittivo)",
         "RPPS, AFGSU, CCEPS (abilitazioni personali)"],
    92: ["navigation applications (generico)", "CDL, DOT (patenti e visite)"],
    93: ["fax machine, ten key (attrezzatura d'ufficio comune)"],
    97: ["FIMO/FCO, permis CE, carte conducteur (abilitazioni)"],
    98: ["touch screen, calculator, hand truck (attrezzatura generica)"],
    99: ["KW's (troppo corto e ambiguo per ancorarlo)"],
    104: ["LGV (categoria di patente)"],
}

DUBBI = [
    "MACCHINARI CHIAMATI PER FUNZIONE. La rubrica tiene dentro «muletto» e "
    "«saldatrice TIG», cioe' tipi di macchina riconoscibili. Ho tenuto sulla "
    "stessa riga setters/hatchers (#3), zoom boom (#85), snowmobile (#78), "
    "tracteurs/moissonneuses (#87); ho lasciato fuori «industrial dishwashing "
    "equipment» (#14) e «fax machine» (#93), che sono descrizioni o oggetti "
    "d'ufficio comuni. Il confine e': un compratore di tecnografia ci filtrerebbe "
    "sopra?",
    "IL MARCHIO E' IL SISTEMA, O E' IL DATORE? In #81 «fire alarm systems from "
    "major manufacturers such as Notifier, Siemens, Edwards, Simplex...» nomina "
    "i sistemi su cui si mette le mani: dentro. In #84 e #89 «BMW» e «TOYOTA» "
    "sono il marchio del concessionario e delle auto: fuori. E' la differenza "
    "fra «lavoro su X» e «lavoro da X».",
    "MODULISTICA PER CANDIDARSI. myWORKDAY (#22), Google Chrome e Mozilla "
    "Firefox (#58) sono software veri e il modello quasi certamente li marchera'. "
    "Sono fuori perche' riguardano il candidarsi, non il lavoro. E' una trappola "
    "di precisione messa apposta nel metro.",
    "BENEFIT CON UN NOME PROPRIO. Wagestream, DailyPay, Noom, SkinIO, "
    "Benefits@work, LifeMart, Blue Light Card: piattaforme vere, ma sono paga e "
    "welfare. La rubrica le mette fuori con 401k e buoni pasto.",
    "REGISTRI PUBBLICI (#9). hjorteviltregisteret, jegerregisteret e "
    "ettersøkshundregisteret sono banche dati nazionali con un nome, e chi fa "
    "quel lavoro ci scrive dentro: tenuti. Ma sono il caso piu' fragile di tutto "
    "il lotto.",
    "DISCIPLINE CHE SEMBRANO METODI. Pilates (#74) ha un nome proprio e un corpo "
    "definito come Scrum, ma e' il mestiere stesso, non uno strumento che il "
    "mestiere usa: fuori, come «ingegneria civile».",
    "DUE ANNUNCI (#46, #56) hanno nel campo text un JSON serializzato invece del "
    "testo: stesso guasto di dato visto al #11 del primo giro. Restano nel "
    "campione perche' il modello in produzione li riceve identici.",
]

ORDINE = ["Agriculture", "Food & Beverage", "Healthcare", "Retail",
          "Social Services", "Sports & Recreation", "Trades", "Transportation"]


def main() -> None:
    indice = json.load(open(INDICE, encoding="utf-8"))
    os.makedirs(USCITA, exist_ok=True)
    per_fam: dict[str, list] = {}
    for r in indice:
        per_fam.setdefault(r["family"], []).append(r)

    tot_tec = tot_vuote = 0
    for k, fam in enumerate(ORDINE, 1):
        righe = per_fam.get(fam, [])
        offerte = []
        for r in righe:
            n = r["n"]
            voci = [{"nome": nome, "ruolo": ruolo} for nome, ruolo in TEC.get(n, [])]
            tot_tec += len(voci)
            tot_vuote += not voci
            o = {"n": n, "id": r["id"], "titolo": r["title"], "tecnologie": voci}
            if n in ESCLUSE:
                o["escluse"] = ESCLUSE[n]
            offerte.append(o)
        d = {"lotto": f"famiglie-{k:02d}",
             "famiglia": fam,
             "rubrica": "docs/rubrica-tecnologie.md",
             "etichettate_da": "claude-opus-5, lettura cieca (senza vedere le uscite dei modelli)",
             "data": "2026-09-20",
             "note_di_metodo": [
                 "Le otto famiglie che il buttafuori del 2B escludeva: qui non c'era "
                 "nessun metro, e la testa di marcatura le lavora tutte.",
                 "Stessa rubrica dei primi dieci lotti, stessa forma dei file: i due "
                 "voti si possono mettere accanto.",
                 "Il testo letto e' quello tagliato a 4.000 caratteri, la stessa "
                 "finestra che la testa vede in produzione (1.024 token): segnare una "
                 "tecnologia oltre quel punto vorrebbe dire contarla come persa quando "
                 "il modello non l'ha mai vista.",
             ],
             "dubbi": DUBBI,
             "offerte": offerte}
        p = os.path.join(USCITA, f"etichette-f{k:02d}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        print(f"{p}: {len(offerte)} offerte, "
              f"{sum(len(o['tecnologie']) for o in offerte)} tecnologie")

    n = len(indice)
    print(f"\ntotale: {n} offerte, {tot_tec} tecnologie, "
          f"{tot_vuote} senza nessuna ({tot_vuote * 100 / n:.0f}%)")


if __name__ == "__main__":
    main()
