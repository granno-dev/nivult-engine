# Le tecnologie le estrae v1, non più un generativo

*19-20 settembre 2026*

Fino a ieri le tecnologie le estraeva il 2B (Qwen 2B, generativo) sul Mac mini:
36.000 annunci al giorno, mentre ne entrano ~130.000. Era il nostro unico collo
di bottiglia di calcolo. Da oggi le estrae una **testa di marcatura su nivult-v1**
— lo stesso encoder che già classifica famiglia e seniority — e il collo di
bottiglia non c'è più.

## Perché un modello sei volte più piccolo fa meglio

Le tecnologie **stanno scritte nell'annuncio**. Un generativo deve riscriverne i
nomi da zero: per questo ne inventa il 10% e ne trova metà. La testa deve solo
**puntarle col dito** (classificazione per token, etichette BIO). Puntare è più
facile che scrivere.

## I numeri, sulle 200 righe etichettate a mano

| | precisione | richiamo | F1 | **coda lunga** |
|---|---|---|---|---|
| testa v1 di settembre | 90,2% | 48,5% | 63,1% | — |
| 2B in produzione | 93,6% | 50,3% | 65,4% | **30,1%** |
| 8B a noleggio + filtro | 58,2% | 69,5% | 63,4% | 54,9% |
| **testa v1 ck-00500** | **93,0%** | **64,9%** | **76,4%** | **50,1%** |

La **coda lunga** è il numero che decide, e nessuno l'aveva mai misurato: i nomi
comuni non si vendono (sapere che un'azienda usa Excel non dice niente), il
valore sta nei nomi rari — `Tigsvetsning`, `ORSY`, `Omnissa Horizon`,
`Pick-by-Voice`. Cancello dichiarato a settembre: sopra il 40% il prodotto è
credibile. **Il 2B era al 30,1%: sotto il cancello, e non lo sapevamo.**

## Il guadagno viene dai dati, non dal modello

Stessa architettura di settembre, stesse 307 milioni di parametri, e perfino
*meno* passi di addestramento (500 contro 3.000). Quattro correzioni al dataset:

1. **Ancoraggio con alias.** Il maestro scrive «Microsoft Excel», l'annuncio
   «Excel»: prima quella riga diventava un esempio *negativo*, insegnando a
   tacere. Recuperato il 15% delle etichette (perdita dal 19% al 4,2%).
2. **Via le righe parziali** (3.732): un nome non ancorato = etichette incomplete.
3. **Via le marcature sovrapposte** (802): in BIO un token non può appartenere a
   due voci.
4. **Il golden escluso per id** dalla query del dataset, o l'esame misura la
   memoria invece della capacità.

## Il picco è precoce, e la perdita non avvisa

| passi | F1 |
|---|---|
| 250 | 74,1% |
| **500** | **76,4%** |
| 2.500 | 74,5% |
| 5.000 | 69,7% |
| 15.000 | 69,7% |

Più si addestra, più l'allievo copia il maestro **compresi i suoi silenzi**: il
richiamo scende verso quello del 2B. A 500 passi era migliore proprio perché non
aveva ancora finito di copiarlo. La perdita intanto scendeva sempre (0,0038 →
0,0020): una perdita quasi a zero è il segnale che sta memorizzando.

Regola: **checkpoint ogni 250 passi nei primi 3.000, e si tiene il migliore sul
golden, mai l'ultimo.**

## Dove gira

Sul **Mac mini M2**, che è la macchina più veloce che abbiamo per questo modello:

| | annunci/giorno |
|---|---|
| Mac mini M2 (10 core, memoria unificata) | **388.000** |
| N5 Radeon 890M, da sola | 218.000 |
| N5 Radeon 890M, con v1 e mT5 accanto | 125.000 |

L'M2 ha la sua banda di memoria; la 890M la divide con la CPU. Il 2B è spento e
il N5 è tornato a due carichi.

## Cosa sblocca

L'export `segnali-tecnografici` che vendiamo oggi ha **zero righe**, e
`azienda_skill` ha la forma giusta ma contenuto inutilizzabile. Questa è la
macchina che li riempie. E il **buttafuori non serve più**: le otto famiglie che
escludevamo (agricoltura, commercio, ristorazione, trasporti, mestieri, sanità)
le escludevamo perché il 2B era lento. Sono proprio quelle con la coda lunga più
preziosa, che nessun concorrente ha perché nessun dizionario la contiene.

## Dove la testa è cieca: le otto famiglie che il 2B non vedeva

*Misurato il 20/09/2026 su 104 annunci nuovi, 13 per famiglia, etichettati a
mano alla cieca (`golden-tec-famiglie/`).*

| | 200 righe del primo golden | 104 righe delle otto famiglie |
|---|---|---|
| precisione | 93,0% | **91,7%** |
| richiamo | 64,9% | **31,9%** |
| F1 | 76,4% | 47,3% |
| annunci senza tecnologie, azzeccati | — | **68 su 69** |

Due cose insieme, e vanno lette insieme.

**La testa è sicura qui.** Non inventa (91,7%), e su 69 annunci che non
contengono nessuna tecnologia ne sbaglia **uno**. Non stiamo vendendo
spazzatura in queste famiglie: stiamo vendendo poco.

**Ma è cieca su tutto ciò che non è informatica.** Trova 22 nomi su 69, e
l'elenco di cosa trova e cosa perde non lascia dubbi:

| trova | perde |
|---|---|
| Lely Horizon, HEXALIS, Prompt EMR, StoreForce, POS | MIG Welding, grinders, zoom boom, D1.1 GMAW |
| MS Office, Word, Excel, Outlook, PowerPoint | tracteurs, moissonneuses, pompes de relevage |
| Notifier, Simplex, Fire Lite, Gamewell FCI, Honeywell | Brandmeldeanlagen, Videosysteme, Zutrittskontrolle |
| Terminal Radio, PEG J | X-rays, Ultrasonography, AED, CGM, snowmobile, NFPA 72 |

**La causa non è il modello, è il dataset — ma non come sembrava.**

La prima spiegazione che mi ero dato era che il 2B quelle famiglie non le
avesse mai lette, perché il buttafuori le scartava. **È falsa, e verificarla ha
richiesto una query.** Il 2B le ha lette eccome — il buttafuori è arrivato il
19/09, dopo:

| famiglia | annunci letti dal 2B | tecnologie per annuncio | lasciati vuoti |
|---|---|---|---|
| Software | 5.530 | 6,11 | 9,7% |
| Engineering | 6.295 | 2,84 | 24,3% |
| **Trades** | **8.677** | **0,16** | **90,7%** |
| Healthcare | 7.830 | 0,13 | 92,8% |
| Retail | 8.482 | 0,05 | 96,6% |

Sulle 13 righe di Trades etichettate a mano ci sono **2,15 tecnologie per
annuncio**. Il 2B ne trovava 0,16: **ne vedeva il 7%**.

Quindi non è un buco nei dati. Sono **circa 40.000 esempi che insegnano
attivamente a tacere** su mestieri, sanità, commercio e trasporti, e la testa
ha imparato benissimo quella lezione — è [[addestramento-copia-il-maestro]]
nella sua forma più costosa. Un'assenza si colma aggiungendo dati; una lezione
sbagliata va prima tolta.

**Il buttafuori è ancora lì, ma si è spostato.** Prima stava nel codice e
tagliava il 28% del flusso; adesso sta dentro i pesi e taglia due terzi delle
tecnologie di quel 28%. La cura non è addestrare di più: serve un maestro che
quelle tecnologie le veda, e **va misurato prima** — sulle 104 righe a mano,
che esistono apposta.

Due falsi positivi in 104 annunci: `Mozilla` (dalla riga «usa Chrome o Firefox
per candidarti» — la trappola messa apposta nel metro) e un `SAP` in
Transportation.

## Il maestro nuovo, misurato prima di addestrarci sopra

`gpt-oss-120b` via Groq (gratis), con la rubrica nel messaggio di sistema,
sulle stesse 104 righe a mano — e le sue risposte passate dallo stesso filtro
d'ancoraggio della produzione:

| | precisione | richiamo | F1 |
|---|---|---|---|
| il 2B, che ha fatto il dataset di oggi | — | **~7% sui mestieri** | — |
| testa v1 in produzione | 91,7% | 31,9% | 47,3% |
| maestro nuovo, grezzo | 63,5% | 78,3% | 70,1% |
| **maestro + filtro della rubrica** | **70,1%** | **78,3%** | **74,0%** |

Per famiglia, dove il 2B era cieco: **Trades 27 prese su 28** (il 2B ne vedeva
il 7%), Sports 14 su 15, Retail 3 su 3. Resta debole su Agriculture (2 su 8) e
Food & Beverage.

**Il filtro della rubrica** (`scripts/filtro_rubrica.py`) non è un secondo
modello: è un elenco scritto con la ragione accanto. Toglie ciò che il
messaggio di sistema già escludeva a parole e che il maestro ha violato lo
stesso — certificazioni personali (BLS, ACLS, PALS, NRP), benefit con un nome
proprio (Wagestream, BHN rewards), i browser nominati per candidarsi, le
categorie di patente. Vale 6,6 punti di precisione e non costa niente.

**Degli ultimi 23 errori, 8 non sono errori**: sono lo stesso strumento scritto
in un altro modo — il maestro dice `Svetness App`, io avevo scritto `Svetness
Fitness App`; dice `GPS`, io `Global Positioning System`. Il confronto è a
sottostringa e non li riconosce, quindi li conta due volte: come falso e come
mancanza. Contandoli per quello che sono, la precisione vera è **~80%**.

Non ho cambiato il confronto: è lo stesso con cui sono stati misurati 2B e 8B,
e cambiarlo renderebbe i voti non confrontabili. Ma è un limite del metro da
sapere quando si legge la precisione di chiunque.

## Le trappole, perché non si ripetano

- **Il golden di settembre non conteneva tecnologie.** Il «47,3%» di allora era
  misurato contro le etichette di DeepSeek, cioè contro un altro maestro. Contro
  la verità quel modello valeva 38,0% ed era *peggiore del 2B*.
- **Su 200 righe l'incertezza è ±9 punti.** I checkpoint vanno confrontati
  appaiati (stessa riga, differenza riga per riga), o si sceglie il più fortunato.
- **I contatori dei demoni sono cumulativi.** «0,63 nomi per annuncio» e «2,04»
  sembravano due modelli: erano due infornate consecutive dello stesso.
- **Uccidere il ciclo bash non uccide il python figlio.** Sei processi e tre
  modelli su una macchina da 8 GB: usare `deploy/riavvia_tec_mini.sh`.
- **Su macOS «memoria libera» mente**: guardare `memory_pressure`.

## v2 (21/09/2026): il maestro nuovo, e il buttafuori tolto dai pesi

Il dataset v2 sostituisce le righe del 2B ovunque tranne nelle quattro famiglie
informatiche (Software, Technology, Data & Analytics, Engineering, dove il 2B
lasciava vuoto meno del 25%): **31.631 annunci** etichettati da DeepSeek con la
stessa rubrica misurata sui golden, 1.000 per famiglia su tutte e 33, filtrati
dalla rubrica (`filtro_rubrica.py`, che ora conosce anche le app di benessere e
gli oggetti generici), spesa 9,23 $. Il maestro, misurato prima: sulle 200 righe
IT precisione 71,1%, richiamo 88,9%; sulle 104 delle otto famiglie richiamo
79,4%, Trades 28/28. Le righe più lunghe della finestra sono spezzate in
finestre sovrapposte come in produzione (`--finestra 1024`), e il taglio a
20.000 caratteri dell'addestramento è sparito.

Addestrato su RunPod (A40, 3.000 passi, checkpoint ogni 250), esaminato **ogni
checkpoint** sui due golden, tagliato e a finestre. Il picco è al passo
**1.750**; dopo, il richiamo fuori dall'informatica cala (copia il maestro,
compresi i silenzi). A finestre, come in produzione:

| | precisione | richiamo | F1 |
|---|---|---|---|
| IT, v1 in produzione | 93,0% | 64,9% | 76,4% |
| **IT, v2 ck-01750, soglia 0,40** | **89,9%** | **72,6%** | **80,4%** |
| 8 famiglie, v1 in produzione | 91,7% | 31,9% | 47,3% |
| **8 famiglie, v2 ck-01750, soglia 0,40** | **76,3%** | **65,2%** | **70,3%** |

La soglia 0,40 è scelta per la precisione: a 0,30 l'F1 è lo stesso ma la
precisione IT scende a 85,2% e quella delle otto famiglie a 69,7%. A 0,50 si
risale (92,2% e 73,5%) pagando il richiamo (66,6% e 52,2%): è il bottone da
girare se il prodotto preferisce dire meno cose e più sicure. **Il prezzo del
richiamo fuori dall'informatica è la precisione**: da 92% a 76% su 104 righe
(±9 punti), con un maestro che a sua volta sta al 70-80%. Il golden delle otto
famiglie va allargato prima di stringere ancora.

In produzione sul Mac mini dal 21/09 alle 16:38 (`tec-v2-ck01750`, soglia
0,40, lotto 4 dopo un «MPS out of memory» a lotto 8): 406.000 offerte/giorno,
1,9 nomi per annuncio contro 1,8 della v1 nello stesso flusso.

## v3 (24/09/2026): piu' dati dallo STESSO maestro non battono il maestro

Esperimento completo, chiuso in giornata: 22.947 annunci nuovi delle 8
famiglie deboli etichettati da DeepSeek ($5,83, 0 muti), dataset ricostruito
(72.750 righe, 504 golden escluse), 3.000 passi su A40 RunPod in 1,33 h
(~$1), esame di OGNI checkpoint sui due golden allargati (200 IT + 304
famiglie, stesso banco, stesse finestre, soglia di produzione 0,40):

| | IT P/R/F1 | 8 famiglie P/R/F1 |
|---|---|---|
| v2 ck01750 (produzione) | 89,9 / 72,6 / 80,4 | 58,0 / 70,1 / 63,5 |
| v3 ck-01250 (migliore) | 84,0 / 78,1 / 81,0 | 49,3 / 77,1 / 60,1 |

IT migliora di mezzo punto ma la precisione cade di 6; sulle famiglie
PERDE 3,4 punti. **Non rilasciata.** La lezione confermata: l'allievo non
batte il maestro — piu' volume dallo stesso maestro sposta solo il rumore.
Il salto vero richiede il maestro migliore (gpt-oss-120b col filtro stava
a 74,0% sulle 104; da rimisurare sulle 304). Artefatti: esami e ck-01250
archiviati, pod RunPod eliminato a fine lavoro.

## v4-pilota (25/09/2026): la qualita' del maestro senza il volume non basta

Il campionato dei maestri sulle 104 golden indipendenti aveva eletto gli
agenti Kimi (F1 87,0% contro 74,0% di gpt-oss-120b e ~65% di DeepSeek).
Ma il pilota addestrato SOLO sulle 5.596 etichette Kimi (+ IT dal 2B):
miglior checkpoint famiglie 62,0%, IT 77,3% — SOTTO la v2 (63,5 / 80,4).
Cinque volte meno dati di qualita' migliore pareggiano quasi, ma non
bastano: il volume serve, come diceva UniversalNER (decine di migliaia,
non migliaia). La via v4: campagna agenti a volume pieno (~30k), o
l'equivalente veloce a pagamento. Pod spento a fine esame (~$1).
