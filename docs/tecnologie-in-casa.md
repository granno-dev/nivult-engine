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
