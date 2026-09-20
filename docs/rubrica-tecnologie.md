# Rubrica delle tecnologie

Il confine, scritto una volta. Serve a tre cose insieme: alle etichette fatte a
mano (il metro), al messaggio di sistema del maestro che etichetta il grosso, e
alla lista di alias che trasforma un nome in una marcatura sul testo.

Scritta il 19/09/2026, prima di etichettare, perché una rubrica scritta dopo si
adatta alle risposte che si hanno già in mano.

## La domanda

Non «questa parola è tecnica?» ma: **l'annuncio nomina uno strumento con
un'identità propria, che chi legge potrebbe cercare per nome?**

Un datore che scrive «SAP» nomina una cosa precisa e riconoscibile. Uno che
scrive «gestione del magazzino» descrive un mestiere. Il primo è una tecnologia,
il secondo no, e la differenza non è quanto suona tecnico: è se esiste un oggetto
con quel nome.

## Sono tecnologie

- **Software e applicazioni con un nome proprio** — Microsoft Excel, SAP,
  Salesforce, AutoCAD, Opera PMS, Jira, Photoshop.
- **Linguaggi, framework, librerie** — Python, C++, React, TensorFlow, Ada.
- **Basi di dati, piattaforme, sistemi operativi** — PostgreSQL, MongoDB Atlas,
  Amazon Web Services, Kubernetes, Linux, Red Hat.
- **Protocolli, standard e norme tecniche con una sigla d'uso** — BACS, HVDC,
  ISO 9001, ISO 27001, SOC 2, HACCP, OWASP. Sono impianti che si costruiscono e
  su cui si viene certificati, non discipline.
- **Macchinari e apparati con un nome o un tipo riconoscibile** — pressa
  piegatrice, tornio CNC, fibra ottica, videosorveglianza, muletto, saldatrice
  TIG. Qui sta la parte del mercato che i modelli generativi sbagliano di piu':
  un elettricista e un magazziniere hanno tecnologie, si chiamano solo in un
  altro modo.
- **Metodi con un nome proprio e un corpo definito** — Scrum, Kanban, Six Sigma,
  ITIL, GAAP. Non «lavoro agile» come atteggiamento: Scrum come metodo.

## NON sono tecnologie

| categoria | esempi che abbiamo visto sbagliare |
|---|---|
| discipline e settori | ingegneria civile, project management, marketing, contabilita' |
| processi e attivita' | pianificazione, budgeting, reportistica, manutenzione, installazione, dressaggio dei piatti |
| qualita' personali | problem solving, leadership, precisione, gestione dello stress, pensiero analitico |
| benefit e condizioni | 401k, buoni pasto, ferie, assicurazione sanitaria, provvigione, smart working |
| materiali e prodotti | rame, acciaio, fibre, additivi, cemento, legno |
| titoli e abilitazioni della persona | laurea, diploma, bac pro, CAP, patente B, primo soccorso, BLS |
| lingue naturali | italiano, inglese, svedese |
| reparti, mansioni, aziende | ufficio acquisti, addetto vendite, Hitachi |

Tre casi di confine, decisi una volta per tutte:

- **Una certificazione su una tecnologia** vale la tecnologia, non la
  certificazione: «certificato AWS Solutions Architect» -> `Amazon Web Services`.
- **Una norma tecnica** e' una tecnologia (ISO 9001, HACCP); **un'abilitazione
  personale** non lo e' (patente per carrelli elevatori). La prima e' una cosa,
  la seconda e' un permesso.
- **Una norma che si applica lavorando** e' una tecnologia (ISO 27001, SOC 2,
  CyberEssentials, OWASP: si costruisce un impianto e ci si fa certificare);
  **una legge o un regime di approvazione** non lo e' (GDPR, SOX, CE/UKCA, FDA).
  Questa regola e' nata etichettando i lotti 2 e 3, e ha corretto la rubrica:
  GDPR era finito nell'elenco delle tecnologie per errore.

Le **sigle di categoria di software** stanno dentro — ERP, MES, APS, CRM, HCM,
PLM, GED: nominano un tipo di sistema che l'azienda ha davvero, ed e' cosi' che
il mercato le cerca. Le **sigle di processo** stanno fuori: P2P, DFM, EBOM, KPP.

## Come si scrive il nome

**Nella forma che l'annuncio usa, non nella forma commerciale completa.** E' il
contrario di quello che si chiede a un modello generativo, e per un motivo
preciso: la marcatura deve poter puntare il dito su un pezzo di testo. Se il
testo dice «Excel», l'etichetta e' `Excel`. Il collegamento fra `Excel` e
`Microsoft Excel` e' lavoro della lista di alias, che sta a valle e si puo'
correggere senza rietichettare niente.

Il 22% delle marcature perse nel primo addestramento nasceva proprio qui: il
maestro normalizzava i nomi, il costruttore del dataset non li ritrovava nel
testo, e quella tecnologia diventava un esempio negativo.

- Si prende la forma piu' estesa presente nel testo: se c'e' «Amazon Web Services
  (AWS)», l'etichetta e' `Amazon Web Services`; se c'e' solo «AWS», e' `AWS`.
- Si toglie la punteggiatura al bordo, si tiene la maiuscola del testo.
- Un nome che compare piu' volte si etichetta una volta sola.

## Il ruolo

- `usata` — il lavoro la usa.
- `servizio` — l'azienda la offre ai clienti, chi lavora non la tocca
  necessariamente.
- `gradita` — requisito preferenziale, «gradita conoscenza di».

Nel dubbio fra `usata` e `gradita`, vince `usata`: e' il caso piu' frequente e
l'errore costa meno.

## Quando la risposta e' «nessuna»

Un annuncio senza tecnologie e' un esito normale, non un fallimento. Un commesso,
un cuoco, un giardiniere possono non nominarne nessuna. **Un elenco vuoto e' piu'
utile di un elenco inventato**: le voci finte hanno avvelenato tutti i tentativi
precedenti, e una tecnologia mancata si recupera al giro dopo, una inventata
resta nel dato che vendiamo.
