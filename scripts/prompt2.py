"""Il prompt corretto: il confine, e la normalizzazione dei nomi.

Dalla prova del 19/09 l'8B generico ha due difetti, e nessuno dei due e' una
questione di capacita' del modello:

  1. NON SA DOVE FINISCE UNA TECNOLOGIA. Ha scritto «401k retirement savings»,
     «civil engineering», «budgeting», «copper» — l'11% delle voci aggiunte.
     Nessuno gliel'ha mai detto, mentre il nostro 2B l'ha imparato addestrandosi.

  2. NON NORMALIZZA. Scrive «excel» dove il 2B scrive «Microsoft Excel», e cosi'
     le 89 voci che «si perde» in parte sono le stesse scritte diversamente.

Entrambi si correggono nel messaggio di sistema. Questo file tiene i due prompt
accanto, cosi' il confronto e' leggibile e ripetibile.
"""

# quello usato in produzione dal 2B, e nella prima prova dell'8B
VECCHIO = ("Sei l'estrattore di Nivult. Leggi l'annuncio di lavoro e rispondi SOLO con un JSON "
           "con le chiavi: tecnologie. tecnologie = lista di {nome, ruolo} con ruolo in "
           "usata|servizio|gradita. Solo le tecnologie scritte nell'annuncio, niente altro.")

NUOVO = (
    "Sei l'estrattore di Nivult. Leggi l'annuncio di lavoro ed elenca le TECNOLOGIE che nomina.\n"
    "\n"
    "Sono tecnologie: software e applicazioni (Microsoft Excel, SAP, Salesforce), linguaggi di "
    "programmazione, framework e librerie, database, piattaforme cloud, sistemi operativi, "
    "strumenti e macchinari con un nome proprio (AutoCAD, Opera PMS, Kubernetes), protocolli e "
    "standard tecnici (BACS, HVDC, ISO 9001), apparati specifici (videosorveglianza, fibra ottica).\n"
    "\n"
    "NON sono tecnologie, e non vanno elencate:\n"
    "- discipline e settori: ingegneria civile, project management, marketing, contabilita'\n"
    "- processi e attivita': pianificazione, budgeting, reportistica, formazione, manutenzione\n"
    "- qualita' personali: problem solving, leadership, lavoro di squadra, precisione\n"
    "- benefit e condizioni: 401k, buoni pasto, ferie, assicurazione sanitaria, smart working\n"
    "- materiali e prodotti: rame, acciaio, fibre, additivi, cemento\n"
    "- titoli e certificazioni personali: laurea, patente, primo soccorso, abilitazioni\n"
    "- nomi di reparti, mansioni o aziende\n"
    "\n"
    "Scrivi ogni nome nella sua forma commerciale completa e piu' comune: «Microsoft Excel» non "
    "«excel», «Amazon Web Services» non «aws», «Microsoft Power BI» non «power bi». Se la "
    "tecnologia e' nominata solo per sigla e la sigla e' il nome d'uso, lascia la sigla (SAP, SEO).\n"
    "\n"
    "Non dedurre nulla: elenca solo cio' che l'annuncio nomina davvero. Se non nomina tecnologie, "
    "rispondi con una lista vuota.\n"
    "\n"
    "ruolo: «usata» se il lavoro la usa, «servizio» se l'azienda la offre ai clienti, «gradita» "
    "se e' un requisito preferenziale.\n"
    "\n"
    "Rispondi SOLO con il JSON."
)

if __name__ == "__main__":
    print(f"vecchio: {len(VECCHIO)} caratteri")
    print(f"nuovo:   {len(NUOVO)} caratteri  (~{len(NUOVO)//4} token in piu' per chiamata)")
    print(f"\ncosto del prompt piu' lungo su 82.320 chiamate al giorno:")
    print(f"  ~{82320 * (len(NUOVO)-len(VECCHIO)) / 4 / 1e6:.1f} M token in piu' al giorno")
