# Rubrica di classificazione — la verità di riferimento

Questa rubrica è la **legge** per chiunque etichetti un'offerta: i modelli
maestri (GLM/Qwen), il set d'esame, e il modellino distillato che impara da
loro. È nata il 2026-09-06 dall'audit di 60 etichette GLM (3 errori netti,
tutti su titoli ambigui SENZA descrizione) e dal collaudo di nivult-v0 sul
residuo difficile (72% di accordo: i disaccordi erano tutti coppie ambigue
della tassonomia). Il problema non era l'intelligenza dei modelli ma la loro
**incoerenza** sugli stessi casi: la rubrica esiste per decidere una volta.

## Principio 1 — Il mestiere, non il settore dell'azienda

La famiglia descrive **cosa fa la persona**, non dove lavora. Un contabile in
un ospedale è *Finance & Accounting*, non *Healthcare*. Uno sviluppatore in
banca è *Software*. Un addetto alle pulizie in un albergo è *Trades*.

## Principio 2 — Senza testo, niente indovinelli

Se la descrizione manca e il titolo è ambiguo → **unknown**. Un titolo come
«Bar Back», «Technician», «Specialist», «Coordinator» senza contesto non si
etichetta a caso. Nel dataset di addestramento queste righe **non entrano**.
Un titolo *inequivocabile* («Registered Nurse», «Senior Java Developer»,
«Bäcker») si etichetta anche senza descrizione.

## Le coppie ambigue — decise una volta per tutte

| Se il ruolo è… | Famiglia | NON |
|---|---|---|
| lavoro manuale qualificato: falegname, elettricista, idraulico, pittore, saldatore, meccanico, tecnico installatore, addetto pulizie | **Trades** | Construction, Technology |
| gestione o esecuzione di cantieri edili: capocantiere, project manager edile, ingegnere civile di cantiere, muratore, carpentiere edile | **Construction** | Trades |
| vendita al banco/in negozio, cassiere, commesso, magazzino di negozio, visual merchandiser | **Retail** | Sales |
| vendita a clienti/aziende: account manager, sales negotiator, business developer, agente immobiliare, key account | **Sales** | Retail, Management |
| consulenza fiscale, contabile, revisione, credito, banca, assicurazioni, tesoreria, analista finanziario | **Finance & Accounting** | Consulting |
| consulenza di processo/organizzazione/strategia/IT per clienti esterni | **Consulting** | Finance |
| cucina, sala, bar, pasticceria, panificio, gestione ristorante | **Food & Beverage** | Hospitality, Healthcare |
| accoglienza, reception, eventi, hotel front-office, host/hostess | **Hospitality** | Food & Beverage |
| guida di veicoli (camion, bus, consegne), logistica di trasporto, autisti | **Transportation** | Trades, Logistics |
| magazzino, supply chain, spedizioni, pianificazione scorte | **Logistics** | Transportation |
| sviluppo software, dev, product owner tecnico, QA software | **Software** | Technology |
| infrastruttura IT, sistemi, reti, supporto tecnico, cloud ops, AV/IT installer di sistemi | **Technology** | Software |
| assistenza clienti, call center, help desk di primo livello | **Customer Service & Support** | Sales, Technology |
| chi guida un'area/divisione con responsabilità di P&L o di persone come contenuto principale del ruolo (VP, direttore, head of) | **Management & Leadership** | la famiglia del reparto |
| ma: un «Senior Manager Sales» *fa vendita* | **Sales** | Management |
| animatore, educatore, tutor, formatore, mentorship | **Education** | Sports, Engineering |
| istruttore sportivo, recreation leader, fitness | **Sports & Recreation** | Education |
| veterinario e assistenti veterinari | **Healthcare** | Agriculture |
| segreteria, assistente di direzione, facility supervisor d'ufficio | **Administrative** | Management |

Regola di scioglimento residuo: **vince il verbo del titolo**. «Manager» da
solo non è Management: serve che *gestire persone/P&L* sia il contenuto.

### Aggiunte dal set d'esame a mano (2026-09-06, 280 casi con testo)

Misurato: sui casi difficili GLM pre-rubrica concordava col giudizio a mano
al 76,6%, il dizionario al 62,7%. Quasi tutti i disaccordi di GLM erano
coppie che la tabella non nominava. Ora le nomina:

| Se il ruolo è… | Famiglia | NON |
|---|---|---|
| servizio in sala/bar/cucina **anche dentro un hotel** (Servicemitarbeiter, bartender, Restaurantleiter, minibar) | **Food & Beverage** | Hospitality |
| front office, reception d'albergo, housekeeping, rooms division | **Hospitality** | Food & Beverage |
| reception/segreteria in un ufficio o studio medico (non albergo) | **Administrative** | Hospitality |
| accettazione pazienti, «patient access», fatturazione sanitaria | **Administrative** | Healthcare |
| saldatore, tubista, elettricista **anche in cantiere** | **Trades** | Construction |
| manovale, muratore, aiuto muratore, plaquiste, uitvoerder, conducteur de travaux | **Construction** | Trades |
| il titolo dice «Consultant» e il contenuto è contabilità/fisco/tesoreria | **Finance & Accounting** | Consulting |
| il titolo dice «Consultant» su SAP/ERP/processi per clienti | **Consulting** | Technology |
| il titolo dice Engineer/Architect/Developer, **anche se il datore è una società di consulenza** | Software / Technology | Consulting |
| help desk IT interno, supporto tecnico ai dipendenti | **Technology** | Customer Service |
| assistenza clienti di un'azienda, call center, customer care | **Customer Service & Support** | Technology |
| commesso di supermercato chiamato «Customer Service Representative» | **Retail** | Customer Service |
| formazione aziendale, learning & development, «learning design» | **Human Resources** | Education |
| insegnante, tutor, istruttore (anche di cucina), formatore per esterni | **Education** | Food & Beverage, Technology |
| job coach, support worker, educatore per disabilità, soziale Betreuung | **Social Services** | Customer Service, Sports |
| regulatory affairs, compliance | **Legal** | Healthcare |
| consulente/venditore di mutui, prodotti bancari, immobili | **Sales** | Finance |
| analista credito, contabile, tesoreria, operations bancarie | **Finance & Accounting** | Sales |
| product manager di prodotti fisici (strategia, portafoglio) | **Marketing** | Engineering |
| product owner tecnico, data warehouse | **Data & Analytics** / **Software** | Logistics |
| programmatore CNC, zerspanungsmechaniker, operaio di linea | **Manufacturing** | Software, Trades |
| tecnico di riparazione (endoscopi, veicoli, macchinari) | **Trades** | Healthcare, Engineering |
| QA/QC inspector con laurea tecnica | **Engineering** | Trades |

### Dall'esame di v1-anteprima (06/09 sera: 86,4% sui 273 casi a mano, 37 errori)

Le coppie che il modello confonde, con la decisione:

| Se il ruolo è… | Famiglia | NON |
|---|---|---|
| impiegato commerciale, back-office commerciale/post-vendita, Sachbearbeiter Auftragsabwicklung, impiegato tecnico d'ufficio | **Administrative** | Sales, Customer Service, Logistics, Engineering |
| exploitant transport, pianificatore trasporti, autista-allestitore (Aufbaufahrer) | **Transportation** | Logistics |
| magazziniere, picking, addetto al magazzino | **Logistics** | Retail |
| solutions consultant / presales, property consultant | **Sales** | Consulting |
| inhouse consultant SAP/ERP (dipendente, non per clienti), delivery manager IT | **Technology** | Consulting |
| chef instructor, formatore di cucina | **Education** | Food & Beverage |
| assistente architetto, BIM coordinator | **Construction** / **Engineering** | Art & Design |

Tre regole trasversali:

- **Se titolo e testo parlano di due mestieri diversi, vince il testo.**
  «R&D Manager» con testo da tecnico irrigazione è Trades: il testo è
  l'annuncio, il titolo un errore di chi l'ha pubblicato.
- **Candidatura spontanea, «not hiring», «Test» → unknown.** Non è un
  mestiere.
- **PRN, per diem, bank staff, zero hours → `temporary`** anche se il
  campo dice part_time.

## Seniority — si DEDUCE, non si aspetta la parola

Si legge da responsabilità, anni richiesti, autonomia, ampiezza:

| Segnali | Seniority |
|---|---|
| stage, tirocinio, praktikant, apprendistato, «student» | intern |
| entry-level, ≤2 anni, «training provided», «no experience required», ruoli operativi semplici | junior |
| 3-6 anni, autonomia su un ambito, nessuna guida di persone | mid |
| «senior», 5+ anni con ownership, expert, «confirmé», principal (non di reparto) | senior |
| guida un team o un progetto trasversale (team lead, tech lead, supervisor con riporti) | lead |
| direzione: head of, director, VP, C-level, president, «full P&L» | head |

`unknown` **solo** se il testo non dà nessun segnale (né titolo né descrizione).

## Tipo di contratto — si legge, non si presume

- Solo se **scritto o fortemente implicato**: full_time, part_time, contract,
  temporary, internship, apprenticeship.
- «Per diem», «on call», «Aushilfe», «CDD», «befristet», «interim» → temporary.
- «Ausbildung», «apprentissage», «apprendistato» → apprenticeship.
- **Mai** presumere full_time perché non c'è scritto niente.

## Remoto

remote solo se il lavoro *si svolge* a distanza; hybrid se misto dichiarato;
onsite se sede fissa dichiarata. «Remote» nel campo *località* senza altro →
remote.

## Paese

ISO2 dalla località. «Remote», «Europe», «Worldwide», «2 Locations» → XX.

## Come si usa

1. Nel **prompt** dei maestri: la tabella delle coppie ambigue va incollata
   nel system prompt (è breve). Principio 2 va scritto esplicitamente.
2. Nel **dataset di addestramento**: si escludono le righe senza descrizione
   con titolo ambiguo; le righe già etichettate su una coppia ambigua si
   ri-etichettano seguendo la tabella.
3. Nel **set d'esame**: solo casi giudicati a mano con questa rubrica. Il
   modellino va in produzione solo se supera ≥90% **sul residuo difficile**.
4. La rubrica **cresce**: ogni nuova coppia ambigua scoperta dai disaccordi
   maestro/allievo entra qui, con la decisione.
