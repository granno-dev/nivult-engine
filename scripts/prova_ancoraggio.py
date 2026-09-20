"""I casi difficili dell'ancoraggio, scritti a mano.

Ognuno viene da un errore vero visto il 19/09/2026, non da fantasia: il filtro
tocca il dato che vendiamo, e i modi in cui puo' sbagliare sono due opposti —
buttare una tecnologia vera, o tenerne una inventata.
"""
import importlib.util
import sys

sp = importlib.util.spec_from_file_location("a", sys.argv[1] if len(sys.argv) > 1 else "ancoraggio.py")
A = importlib.util.module_from_spec(sp)
sp.loader.exec_module(A)

# (testo, nome, deve ancorare?, perche')
CASI = [
    # deve TENERE: il modello normalizza, l'annuncio no
    ("Ottima conoscenza di Excel e PowerPoint.", "Microsoft Excel", True, "prefisso fornitore via"),
    ("Esperienza con AWS e Kubernetes.", "Amazon Web Services", True, "sigla dalle iniziali"),
    ("Si lavora con Power BI ogni giorno.", "Microsoft Power BI", True, "prefisso + nome composto"),
    ("Sviluppo in Node.js e TypeScript.", "Node.js", True, "punteggiatura nel nome"),
    ("Gestione del gestionale SAP S/4 HANA.", "SAP S/4HANA", True, "spaziatura diversa"),
    ("Conoscenza di Jira e Confluence.", "Jira Software", True, "coda di chiarimento via"),
    ("Utilizzo di AutoCAD per i disegni.", "AutoCAD Inventor Professional", True, "nome allargato dal modello"),
    ("Si programma in Project e si riporta.", "Microsoft Project", True, "prefisso via, nome comune"),
    ("Esperienza in progetti R e Python.", "R", True, "linguaggio di UNA lettera, maiuscolo nel testo"),
    ("Sviluppo embedded in C e assembly.", "C", True, "linguaggio di una lettera"),
    ("Conoscenza del linguaggio Go.", "Go", True, "linguaggio di due lettere"),
    ("Sviluppo in C# e .NET.", "C#", True, "due caratteri con simbolo"),
    ("Esperienza su piattaforma Salesforce.", "Salesforce", True, "nome identico"),
    ("Si usano i tool Microsoft Office.", "Microsoft Office", True, "nome identico col fornitore"),

    # deve BUTTARE: il modello ha inventato
    ("Esperienza con SAP e Sage per la contabilita'.", "Microsoft Dynamics 365", False,
     "invenzione vera del 19/09: Microsoft non compare"),
    ("Si richiede esperienza in cucina e HACCP.", "Microsoft Excel", False, "niente a che vedere"),
    ("Accoglienza clienti, gestione acconti e casse.", "Adobe Creative Cloud", False,
     "la sigla ACC agganciava «acconti»/«acc» minuscolo"),
    ("Lavoro di reparto, turni e responsabilita'.", "R", False,
     "una «r» minuscola non e' il linguaggio R"),
    ("Attivita' di ricerca e sviluppo (R&S) sul prodotto.", "Python", False, "niente Python"),
    ("Si usa Sage per la fatturazione.", "Google Workspace", False, "invenzione: nessun Google"),
    # deve TENERE anche quando il modello scrive minuscolo: e' il difetto
    # trovato sul golden il 19/09, che buttava C#, Go, X, 8D e i moduli SAP
    ("Sviluppo in C# e .NET.", "c#", True, "il modello scrive minuscolo, il testo maiuscolo"),
    ("Conoscenza del linguaggio Go.", "go", True, "due lettere, modello minuscolo"),
    ("Esperienza in progetti R e Python.", "r", True, "una lettera, modello minuscolo"),
    ("SAP S/4HANA Procurement (MM, Ariba, Fieldglass).", "sap mm", True, "prefisso via, sigla nel testo maiuscola"),
    ("Partecipazione alle revues 8D e AMDEC.", "8d", True, "sigla con cifra, modello minuscolo"),
    ("Presenza su Instagram, TikTok e X.", "x", True, "una lettera maiuscola nel testo"),

    # e deve continuare a BUTTARE dove il testo ha solo minuscole
    ("Lavoro di reparto, turni e responsabilita'.", "r", False, "«r» solo dentro parole minuscole"),
    ("Accoglienza clienti, gestione acconti e casse.", "acc", False, "«acc» minuscolo dentro «acconti»"),
    ("Si va a piedi o in bici, come si vuole.", "go", False, "nessun «Go» maiuscolo nel testo"),

    # coppie di nomi di mercato: il modello ne scrive una, l'annuncio l'altra
    ("Esperienza con Postgres o MySQL.", "PostgreSQL", True, "coppia postgres/postgresql"),
    ("Sviluppo in Node e TypeScript.", "Node.js", True, "coppia node/node.js"),
    ("Conoscenza di Golang e Rust.", "Go", True, "coppia go/golang, al contrario"),
    ("Orchestrazione con k8s in produzione.", "Kubernetes", True, "coppia kubernetes/k8s"),
    ("Si lavora con AWS ogni giorno.", "Amazon Web Services", True, "sigla gia' coperta, non deve rompersi"),
    ("Pulizia dei locali e gestione del magazzino.", "PostgreSQL", False, "nessun database qui"),
]

fatti = falliti = 0
for testo, nome, atteso, perche in CASI:
    p = A.ancora(testo, nome)
    ok = (p is not None) == atteso
    fatti += 1
    if not ok:
        falliti += 1
        esito = f"ancorato a «{p[2]}»" if p else "non ancorato"
        print(f"FALLITO  {nome!r}  ({perche})")
        print(f"         atteso: {'ancora' if atteso else 'butta'}, ottenuto: {esito}")
        print(f"         testo: {testo!r}")

print(f"\n{fatti - falliti}/{fatti} casi passati")
sys.exit(1 if falliti else 0)
