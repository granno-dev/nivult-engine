# Il motore di arricchimento — come funziona oggi

Aggiornato al **22 settembre 2026**.

Questo documento spiega cosa gira, su quale macchina, e perché. La raccolta delle
offerte è raccontata in [raccolta-ats.md](raccolta-ats.md); qui si parla di cosa
succede *dopo*: come un annuncio grezzo diventa un dato che si vende.

## La riga che orienta tutto

**Il mercato non compra annunci, compra aziende.** TheirStack fa pagare un
credito per ogni azienda rivelata, tre per una azienda restituita dall'API o per
una ricerca di technographics; e non genera sintesi — la descrizione la converte
in Markdown e la passa così com'è.

Quindi l'annuncio è la materia prima. Il prodotto è **l'azienda**, con un dominio
a cui agganciarla e le tecnologie che usa.

## Chi fa cosa, e su quale macchina

| macchina | cosa ci gira | resa misurata |
|---|---|---|
| **N5** (casa, Radeon 890M) | `v1`: famiglia, seniority, contratto, remoto, lingue | 530.000 offerte/giorno |
| **Mac mini** (M2, 8 GB) | testa tecnologie `v2` | 400.000 offerte/giorno |
| **Hetzner** | cacciatore di domini, registri, dettagli, schede azienda, database | ~1.800 aziende/ora · 80 offerte/s sui dettagli |

Dal 21/09 **niente generativi in produzione**: mT5 è spento (nessuno legge le
sintesi) e il 2B lo era già dal 20/09, sostituito dalla testa tecnologie `v2`
che gira sullo stesso mmBERT di `v1`. Tutto ciò che resta è estrattivo o
deterministico, e questo è il motivo per cui il motore oggi regge il flusso
senza arretrato.

Ogni macchina esegue la tecnologia che le è propria: torch sulla Radeon,
llama.cpp su Metal. Niente conversioni, e soprattutto **mai due motori
generativi sulla stessa scheda** — la Radeon del N5 non ha memoria propria, la
prende dalla RAM di sistema, e due motori insieme l'hanno incastrata per undici
ore il 16/09.

### I tre modelli, e perché sono divisi così

| | cosa fa | qualità | stato |
|---|---|---|---|
| **v1** (mmBERT-base, 5 teste) | famiglia, seniority, contratto, remoto, lingue | famiglia 92,3% · contratto 94,2% · remoto 99,1% | in produzione sul N5 |
| **tec v2** (testa sullo stesso mmBERT) | le tecnologie citate nell'annuncio | IT F1 80,4% · otto famiglie non informatiche F1 70,3% | in produzione sul Mac mini dal 21/09 |
| **mT5** (582M) | la sintesi di ogni offerta | 4,44/5 | **spento dal 21/09**: nessun consumatore |
| **2B** (Qwen GGUF) | tecnologie, prima di `tec v2` | F1 65,4% sulle tecnologie | **spento dal 20/09**: la testa lo batte |

La testa `v2` batte il generativo da 2 miliardi di parametri (F1 74,5% contro
65,4% al primo esame, 80,4% oggi): **il guadagno viene dai dati etichettati, non
dalla dimensione del modello**. Ed è venti volte più veloce.

mT5 scriveva 95.000 sintesi al giorno per nessuno: il digest B2C usa GLM sulle
poche offerte già scremate, non le nostre sintesi, e i concorrenti che vendono
dati (Coresignal, TheirStack) le sintesi non le offrono. Le 172.735 già scritte
restano in tabella, il modello e il codice sono pronti a riaccendersi.

### Il ripasso, e perché oggi è spento

mT5 sbaglia più del 2B. Ma la sua **fiducia** — la media del logaritmo della
probabilità delle parole che ha scelto — predice dove sbaglia: correlazione
**+0,472** col voto del giudice, misurata su 250 sintesi giudicate una per una.
Ripassando col 2B il 10% meno convinto si intercetta il **50%** degli errori
gravi; il 20%, il 72%.

Il meccanismo è montato e funziona (`--ripasso` in `estrai_2b.py`), ma **è
spento di proposito**: le sintesi alimentano il digest B2C, che oggi ha zero
utenti, mentre le tecnologie si vendono adesso. Con 8 GB di RAM sul Mac le due
cose non ci stanno insieme. Si riaccende il giorno del primo abbonato: la coda
lo aspetta, ordinata dalla sintesi meno convinta in avanti.

### Il buttafuori

Non ha senso cercare tecnologie in un annuncio per camerieri. `v1`
classifica la famiglia professionale al 92,3%, e si usa come cancello: otto
famiglie — Agriculture, Social Services, Retail, Sports & Recreation, Food &
Beverage, Transportation, Trades, Healthcare — saltano il modello e ricevono
direttamente una lista vuota.

**Misurato** su 71.747 offerte già estratte: taglia il **28,2%** del flusso e
costa il **3,0%** delle tecnologie.

Attenzione a due famiglie che *sembrano* da escludere e non lo sono: *Logistics*
rende 0,65 tecnologie per offerta (SAP, gestionali di magazzino) e
*Manufacturing* 0,54.

Le righe escluse si scrivono con `tecnologie = []` — una risposta, non un buco —
ma con un `modello` che dice «famiglia esclusa»: fra il 5% e il 21% di quelle
offerte una tecnologia ce l'ha davvero, e se cambiamo idea sappiamo quali righe
tornare a leggere.

## Il cacciatore di domini

Senza dominio un'azienda non si aggancia a niente: né settore, né dipendenti, né
bilanci. È **il** collo di bottiglia del prodotto.

Quattro fonti in cascata, dalla più ricca alla più incerta:

1. **logo nella bacheca ATS** — quando un'azienda configura il tenant, l'ATS le
   chiede il sito per il link in testata: leggerlo non è indovinare;
2. **email dentro gli annunci** — trova quello che nessun generatore trova
   (`eliseai` → `meetelise.com`);
3. **nome generato** (`nomeazienda.com`);
4. **SearXNG** — ricerca su Google e Yahoo.

### Un solo giudice, e due livelli dichiarati

Nessuna fonte scrive di suo. Il livello dice **quanto è solida la prova**, ed è
esposto fino alla tabella che si vende:

| livello | prova | quando serve |
|---|---|---|
| **1** | il sito rimanda al NOSTRO identico tenant ATS | prende anche ciò che il nome non direbbe mai: `Orbotech → kla.com` (acquisita) |
| **2** | il dominio **corrisponde al nome**, esiste nel DNS, e il suffisso è plausibile | CVS Health, Broadcom e Colliers un dominio ovvio ce l'hanno ma non mettono da nessuna parte un link crawlabile al tenant |

Chi compra sceglie: livello 1 per incrociare col CRM, 1+2 per coprire di più.
Il livello 2 sbaglia circa **una volta su sette** — è dichiarato, non nascosto.

### L'ordine di pesca

Le piattaforme rendono in modo molto diverso: Vincere 67%, Zoho 52%, Catsone
38%… Ashby 3%, Greenhouse 4%. A parità di tempo, provare prima Zoho che Ashby
vale diciassette volte tanto.

La resa si **ricalcola dai dati a ogni giro** (tabella `caccia_resa`), non si
scrive a mano: così non invecchia e si aggiusta da sola.

### Perché gira su Hetzner e non in casa

Girava sul N5, e HomeShield del router bloccava le sue verifiche segnalandole
come malware — **a ragione, in qualche caso**: i siti di piccole aziende vengono
compromessi di continuo.

Il problema non era la rete, erano i dati: un blocco del router, per noi, è
indistinguibile da un sito irraggiungibile. L'azienda finiva archiviata come
«provata e fallita», e per regola non si riprovava **per 30 giorni**.

Il trasloco non è un compromesso — misurato motore per motore:

| motore | N5 (casa) | Hetzner |
|---|---|---|
| google | 10 | 10 |
| yahoo | 7 | 7 |
| brave | **0** | **16** |
| mojeek | **0** | **39** |
| startpage | **0** | **39** |

L'IP di casa era bruciato dal nostro stesso traffico: Google lo aveva sospeso
(«unusual traffic from your network»).

## La tabella che si vende

`aziende_vendibili` è materializzata (aggregare le tecnologie di 2,6 milioni di
offerte a ogni interrogazione costerebbe minuti) e si ricostruisce con
`scripts/costruisci_aziende.py`. L'aggancio fra offerta e azienda è
`(platform_id, slug)`: `ats_jobs` non ha un `company_id`, e la coppia copre il
95,1% delle offerte vive.

Sopra c'è la vista **`aziende_pronte`**: un'azienda per riga, niente bacheche,
un nome.

### I tre filtri, e perché ognuno esiste

- **bacheca o datore** — `mindpal.co` dichiara **769 nomi di datori diversi**
  nelle sue offerte. La prova che li distingue è il numero di nomi distinti:
  un'azienda ne dichiara uno, una bacheca centinaia. *Limite noto:* si possono
  giudicare solo le piattaforme che dichiarano il datore (pagine carriere
  JSON-LD, Greenhouse, SmartRecruiters); una bacheca altrove passa.
- **il nome** — si battezza solo il tenant che dichiara **un solo** nome.
  Prendere il primo `hiringOrganization` di una bacheca darebbe a
  `remotenurseconnection.com` il nome «BlueCross BlueShield of Tennessee». Lo
  slug NON si usa: darebbe «Tgocorp» spacciato per ragione sociale.
- **i doppioni** — più tenant della stessa azienda («1PACT» ne ha 8 regionali)
  si raggruppano sotto un capofila. Non si cancella niente. Due trappole: il
  capofila dev'essere **vendibile**, o sparisce tutto il gruppo; e le catene
  A→B→C vanno sciolte.

## I campi che si vendono (21–22 settembre 2026)

Il confronto con Coresignal — 104 campi contro i nostri 33 — ha prodotto una
regola semplice: **tutto ciò che si può leggere senza una fonte chiusa, si
legge.** Nessun modello e nessuna GPU: SQL, espressioni regolari multilingua e
registri pubblici, su Hetzner, mentre N5 e Mac mini restano liberi.

| tabella | chi la riempie | cosa contiene |
|---|---|---|
| `offerte_dettagli` | `nivult.ats.dettagli`, cron 07:00, ~80 righe/s | livello manageriale, turni, orario, urgenza, benefit (16 tag), testo del salario, scadenza dichiarata, regione (GeoNames admin1), CAP, coordinate, recruiter |
| `offerte_fonti` (vista) | `duplicate_key` | le altre copie della stessa offerta, con piattaforma, url e stato |
| `azienda_tecnologie` (materializzata) | `deploy/rinfresca-viste.sh`, 07:20 | le tecnologie **dell'azienda**, con prima e ultima comparsa |
| `aziende_dettagli` | `nivult.ats.aziende_dettagli`, cron 07:40 | fascia dimensionale, sedi viste nelle offerte, sede principale, descrizione, keywords, forma giuridica, fondazione |
| `aziende_registro` | `nivult.ats.registri_imprese`, `nivult.ats.gleif` | sede legale, forma giuridica, identificativo, data di costituzione, **con la fonte** |

`nivult.ats.esporta` porta tutto nei due JSONL giornalieri: 50 chiavi per
offerta, 36 per azienda, e un manifest che dichiara la copertura di ogni campo.
`--campione N` scrive N righe in un file a parte per collaudare una modifica
senza toccare il file del giorno (4 GB compressi).

**Fuori per scelta:** la traduzione degli annunci e i dati di finanziamento
(nessuna fonte libera). `applicants_count` e `is_easy_apply` esistono come
colonne ma restano vuoti: nessuna fonte li dichiara, e un campo vuoto dichiarato
è meglio di un campo inventato.

### Le tre regole che tengono onesto il dato

**Il testo si vende piano, non in HTML.** Tutte le 200.000 offerte Greenhouse
uscivano con le entità HTML codificate (35.000 due volte), le altre piattaforme
con i tag dentro. `testo.pulito()` decodifica fino a tre volte, toglie tag e
script, conserva gli a capo degli elenchi — e **non taglia mai**.

**Una etichetta rara vuole una prova nel testo.** Con le soglie a 0,5 `v1`
scriveva «internship» su 72.000 offerte attive, 30.000 delle quali senza una sola
parola da stage nel titolo: fra queste «Head of Global Product Quality» e
«Senior Alliance Manager». Ora `testo.evidenza_stage()` cerca in venti lingue
(una parola nel titolo, due nel testo) e, se la prova manca, `v1` scrive la sua
**seconda scelta** oppure niente. La riparazione ha esaminato 137.981 offerte:
15.323 dichiarate dalla piattaforma, 75.445 con la prova, 47.213 ripulite e
rimesse in coda.

**Un numero porta la sua portata.** I registri contano l'**unità legale**:
SIRENE dà Veolia Environnement a 1.499 dipendenti, Renault a 4, Eurofins a 374,
mentre il gruppo ne ha rispettivamente 220.000, 179.000 e 62.000. Una sola
funzione decide (`aziende_dettagli.dipendenti`): Wikidata, poi il sito, poi il
registro, poi ciò che l'annuncio dichiara; ogni numero esce con
`employees_scope` (`group` / `legal_entity` / `self_declared`) e il registro
resta visibile a parte come `employees_legal_entity`. La categoria INSEE
(PME/ETI/GE), definita a livello d'impresa, corregge la fascia.

## I registri delle imprese

Sede legale, forma giuridica, data di costituzione e identificativo: i campi che
un compratore di dati aziendali si aspetta, presi dalle fonti ufficiali. Il
match è per nome col nocciolo normalizzato (`_norm` toglie le forme societarie),
e **un nome che non combacia non entra**: meglio un buco che la sede di
un'altra azienda.

| fonte | paese | accesso | dà anche |
|---|---|---|---|
| Companies House | Regno Unito | chiave gratuita (`COMPANIES_HOUSE_KEY`) | SIC 2007 → settore |
| SIRENE | Francia | aperto | categoria INSEE, coordinate, fascia dipendenti |
| Corporations Canada | Canada | CSV aperti, ricaricati con `scripts/ca_carica.py` | solo società federali |
| KBO/BCE | Belgio | zip mensile scaricato a mano, `scripts/kbo_carica.py` | NACE principale |
| Brønnøysund | Norvegia | aperto | dipendenti esatti, sito |
| PRH | Finlandia | aperto | forma giuridica |
| CVR | Danimarca | aperto | dipendenti, settore |
| ARES | Cechia | aperto | NACE, forma |
| RPO | Slovacchia | aperto | ricerca per nome debole |
| SEC EDGAR | USA | aperto | solo società quotate |
| GLEIF | ovunque | aperto | sede operativa e forma per chi ha un LEI |

**Senza via d'uscita gratuita:** Germania (4.271 tenant, 257.000 offerte),
Paesi Bassi, Spagna, Italia, Svezia, Austria, India. Per loro restano GLEIF e
Wikidata. In attesa di credenziali: Zefix (Svizzera, richiesta via
`zefix@bj.admin.ch`), ABN Lookup (Australia), CRO (Irlanda).

## Come si opera

```bash
# stato dei dati, otto controlli con esito esplicito
python scripts/verifica_finale.py

# lo schema riproduce ancora la produzione?
python scripts/confronta_schema.py

# ricostruire la tabella che si vende (--bacheche e' lento: ~5 min, non serve ogni giorno)
python scripts/costruisci_aziende.py [--bacheche]

# i domini di fornitore (rippling.com & co.) — gira anche da cron alle 9
python scripts/pulisci_domini.py [--dry-run]

# i campi che si vendono (tutti e tre girano anche da cron, la mattina)
python -m nivult.ats.dettagli --limite 400000        # dettagli dell'offerta
python -m nivult.ats.aziende_dettagli [--tutte]      # la scheda azienda
python -m nivult.ats.registri_imprese --paesi GB --limite 4000
python -m nivult.ats.gleif --limite 300
python scripts/ca_carica.py --scarica                # registro canadese, 15 s
python scripts/kbo_carica.py --carica kbo.tsv.gz     # registro belga, mensile

# un assaggio dell'export senza toccare il file del giorno
python -m nivult.ats.esporta --attive --aziende --campione 300

# dare un nome alle aziende che non ce l'hanno, solo dove c'e' una prova
python scripts/battezza_aziende.py [--dry-run]
```

I supervisori stanno in `deploy/`: `nivult-n5.sh` (v1), `nivult-mini-tec.sh`
(testa tecnologie sul Mac mini), `nivult-caccia-hetzner.sh` (cacciatore). Ognuno rilancia il proprio demone
se muore — **prima non era così**, e il 19/09 l'estrattore del N5 è rimasto morto
per ore senza che nessuno se ne accorgesse.

## Le trappole, perché non si ripetano

**Un dominio rivendicato da 10+ aziende non è di nessuna di loro.** 457 aziende
avevano `rippling.com`: era il loro ATS. Elencare i fornitori uno per uno è una
corsa che si perde — la regola che non dipende dall'elenco sta in
`pulisci_domini.py`. Ma attenzione: **un fornitore è anche un datore**,
`workday.com` per l'azienda «Workday» è giusto.

**Una sorgente che risponde 200 può essere morta.** SearXNG ha prodotto 1 dominio
su 691 rispondendo sempre 200 — con zero risultati, perché i motori ci avevano
bloccati. Si misura una fonte su cosa **produce**, non su cosa risponde.

**Una soglia si calibra sui candidati veri, non contro il rumore.** La
somiglianza nome/dominio sembrava sbagliare 1 volta su 500 perché era stata
misurata contro coppie accoppiate **a caso**. In produzione sbagliava 1 volta su
7: i candidati di un motore di ricerca sono tutti plausibili.

**Un guasto di trasporto non è un esito.** Con `llama-server` giù, 109 offerte
sono state marcate come «illeggibili» e non torneranno mai in coda. Se il
servizio non ha risposto, non si scrive niente.

**Il checkpoint mT5 ha `use_cache: false`** ereditato da XLSum: senza cache il
decoder ricalcola tutto a ogni parola e a lotti da 8 riempie la memoria grafica
del N5 (47.050 MB su 47.083). Accenderla non è un'ottimizzazione, è sicurezza.

**`pgrep -f` trova sé stesso.** Più di una volta un controllo «il demone è vivo?»
ha risposto di sì trovando il proprio comando. Si verifica dal database — righe
scritte negli ultimi minuti — non dalla lista dei processi.
