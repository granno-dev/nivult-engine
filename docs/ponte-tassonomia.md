# Ponte tassonomia — 33 famiglie Nivult → O\*NET e ESCO/ISCO-08

**BOZZA da revisione umana, 2026-09-23.** Non è codice, non è eseguita da nulla:
è il documento che si mostra a un enterprise quando chiede «parlate O\*NET? ESCO?».

## Scopo

L'enterprise compra tassonomie STANDARD (O\*NET negli USA, ESCO in UE), non la
nostra. Questa tabella è la risposta dichiarata: **una mappatura onesta, non
perfetta**. Le 33 famiglie restano l'identità del prodotto (sono i valori di
`ai_taxonomies_a` del fornitore, vedi `migrations/0022_job_families_and_corrections.sql:30-42`);
il ponte serve a renderle leggibili da chi vive dentro gli standard.

## Regola d'esportazione (non negoziabile)

In ogni esportazione verso clienti, il ponte va **ACCANTO** alla famiglia
Nivult, **mai al posto suo**. I campi si aggiungono, non si rinominano:
`family` resta il valore Nivult verbatim, e si affiancano
`onet_major_group`, `onet_major_group_name`, `isco08_group`, `isco08_group_name`.
Rinominare o sostituire significherebbe perdere i confini che la rubrica
(`docs/rubrica-classificazione.md`) ha deciso una volta per tutte
(Trades vs Construction, Retail vs Sales, Software vs Technology…).

## Metodo

- Famiglie copiate verbatim dalla migrazione 0022 (33 righe).
- O\*NET: tassonomia O\*NET-SOC 2019 (allineata al SOC 2018), 23 major groups
  (fonte: onetcenter.org/taxonomy.html, bls.gov/soc/2018/major_groups.htm).
- ESCO: il pilastro occupazioni di ESCO è ISCO-08 ai livelli 1-4; si mappa al
  **sub-major group (2 cifre)** o **minor group (3 cifre)**, il livello che il
  progetto già usa in `src/nivult/ats/tassonomie.py` (mappa ISCO→famiglia
  misurata su dati reali). Il livello ESCO vero e proprio (5+) è per singola
  occupazione: fuori scala per una famiglia.
- La mappa interna ISCO→famiglia esistente fa da controllo incrociato: dove il
  ponte propone un gruppo, quel gruppo in `tassonomie.py` deve rimandare (anche)
  a quella famiglia.

## La tabella

| # | Famiglia Nivult | O\*NET major group | ISCO-08 (ESCO) | Note |
|---|---|---|---|---|
| 1 | Administrative | 43-0000 Office and Administrative Support Occupations | 41 General and keyboard clerks (411, 412, 441) | Alta |
| 2 | Agriculture | 45-0000 Farming, Fishing, and Forestry Occupations | 61 Market-oriented skilled agricultural workers (+62, 921) | Alta |
| 3 | Art & Design | 27-0000 Arts, Design, Entertainment, Sports, and Media Occupations | 216 Architects, planners, surveyors and designers (+731 handicraft) | Alta; in ISCO attraversa i major group 2, 3 e 7 |
| 4 | Construction | 47-0000 Construction and Extraction Occupations | 71 Building and related trades workers, excluding electricians (711-713; +931) | Alta; il confine Construction/Trades di Nivult (elettricisti→Trades) coincide con ISCO (741 è fuori dal 71) |
| 5 | Consulting | 13-0000 Business and Financial Operations Occupations (àncora: 13-1110 Management Analysts) | 242 Administration professionals (àncora: unit 2421 Management and organisation analysts) | **DEBOLE** — vedi sotto |
| 6 | Creative & Media | 27-0000 Arts, Design, Entertainment, Sports, and Media Occupations | 264 Authors, journalists and linguists; 265 Creative and performing artists; 262 Librarians, archivists and curators | Alta; condivide 27-0000 con Art & Design e Sports & Recreation |
| 7 | Customer Service & Support | 43-0000 Office and Administrative Support Occupations (àncora: 43-4050 Customer Service Representatives) | 42 Customer services clerks (422 Client information workers) | Alta |
| 8 | Data & Analytics | 15-0000 Computer and Mathematical Occupations (àncora: 15-2051 Data Scientists) | 212 Mathematicians, actuaries and statisticians (bordo: 251) | **DEBOLE** — vedi sotto |
| 9 | Education | 25-0000 Educational Instruction and Library Occupations | 23 Teaching professionals (231-235) | Alta; confine con 531 (child care/teachers' aides) deciso dalla rubrica caso per caso |
| 10 | Energy | *(nessun gruppo dedicato)* àncora: 17-0000 Architecture and Engineering Occupations | *(nessun gruppo dedicato)* àncora: 2151 Electrical engineers; 3131 Power production plant operators | **DEBOLE** — vedi sotto |
| 11 | Engineering | 17-0000 Architecture and Engineering Occupations | 214 Engineering professionals (excluding electrotechnology); 215 Electrotechnology engineers | Alta |
| 12 | Environmental & Sustainability | àncora: 19-0000 Life, Physical, and Social Science Occupations (19-2041 Environmental Scientists) | àncora: 2133 Environmental protection professionals (in 213 Life science professionals) | **DEBOLE** — vedi sotto |
| 13 | Finance & Accounting | 13-0000 Business and Financial Operations Occupations (13-2010, 13-2050) | 241 Finance professionals (+331 associate, 431 clerks) | Alta; condivide 13-0000 con Consulting, HR, Marketing |
| 14 | Food & Beverage | 35-0000 Food Preparation and Serving Related Occupations | 512 Cooks; 513 Waiters and bartenders; 751 Food processing and related trades workers | Alta; attenzione: il SOC mette i fornai in 51-3011 (Production), Nivult e ISCO in F&B — divergenza dichiarata |
| 15 | Government & Public Sector | *(nessun gruppo)* àncora: 11-1030 Legislators (in 11-0000) | 111 Legislators and senior officials; 335 Regulatory government associate professionals | **DEBOLE** — vedi sotto |
| 16 | Healthcare | 29-0000 Healthcare Practitioners and Technical Occupations + 31-0000 Healthcare Support Occupations | 22 Health professionals + 32 Health associate professionals (+532) | Alta; i veterinari stanno qui (rubrica), ISCO 225 |
| 17 | Hospitality | àncora: 43-4080 Hotel Desk Clerks (in 43-0000); 11-9081 Lodging Managers (in 11-0000) | 1411 Hotel managers; 5151 Cleaning and housekeeping supervisors; 4224 Hotel receptionists | **DEBOLE** — vedi sotto |
| 18 | Human Resources | 13-0000 Business and Financial Operations Occupations (13-1071 Human Resources Specialists) | unit 2423 Personnel and careers professionals | Alta a livello unit group; nota: la mappa interna manda il minor 242 a Consulting — il ponte resta sul 4° livello |
| 19 | Legal | 23-0000 Legal Occupations | 261 Legal professionals (+3411 associate) | Alta |
| 20 | Logistics | 53-0000 Transportation and Material Moving (metà material moving); 13-1081 Logisticians (in 13-0000) | 432 Material-recording and transport clerks; 3331 Clearing and forwarding agents; 933 | Media-alta; confine con Transportation è il nostro (magazzino vs guida) |
| 21 | Management & Leadership | 11-0000 Management Occupations | 12 Administrative and commercial managers; 13 Production and specialized services managers; 112 Managing directors and chief executives | Alta come gruppo, ma **trasversale per disegno**: vale solo quando guidare persone/P&L è il contenuto del ruolo (rubrica: «Senior Manager Sales» resta Sales) |
| 22 | Manufacturing | 51-0000 Production Occupations | 81 Stationary plant and machine operators; 82 Assemblers; 722 Blacksmiths, toolmakers | Alta |
| 23 | Marketing | 13-0000 Business and Financial Operations Occupations (13-1161 Market Research Analysts and Marketing Specialists) | 243 Sales, marketing and public relations professionals | Alta; nota: il SOC mette i PR specialist in 27-0000 — divergenza dichiarata |
| 24 | Retail | 41-0000 Sales and Related Occupations (41-2031 Retail Salespersons, 41-2010 Cashiers) | 522 Shop salespersons; 523 Cashiers and ticket clerks; 142 Retail and wholesale trade managers | Alta; condivide 41-0000 con Sales |
| 25 | Sales | 41-0000 Sales and Related Occupations (41-3000/41-4000 sales representatives) | 332 Sales and purchasing agents and brokers; 524 Other sales workers | Alta; il confine Retail/Sales (banco vs clienti) è nostro, gli standard non lo hanno |
| 26 | Science & Research | 19-0000 Life, Physical, and Social Science Occupations | 211 Physical and earth science professionals; 213 Life science professionals; 314 Life science technicians | Alta; i ricercatori universitari toccano anche 231 (Education) |
| 27 | Security & Safety | 33-0000 Protective Service Occupations (+55-0000 Military Specific) | 541 Protective services workers; major group 0 Armed forces | Alta |
| 28 | Social Services | 21-0000 Community and Social Service Occupations | 263 Social and religious professionals; 341 Legal, social and religious associate professionals | Alta |
| 29 | Software | 15-0000 Computer and Mathematical Occupations (15-1252 Software Developers) | 251 Software and applications developers and analysts | Alta; il confine Software/Technology è il nostro taglio di ISCO 25 |
| 30 | Sports & Recreation | 27-0000 (27-2020 Athletes/Coaches) + 39-0000 (39-9030 fitness/recreation workers) | 342 Sports and fitness workers | Alta sul lato ISCO; O\*NET la spezza fra 27 e 39 |
| 31 | Technology | 15-0000 Computer and Mathematical Occupations (15-1230 support, 15-1240 network) | 252 Database and network professionals; 35 Information and communications technicians (351, 352) | Alta |
| 32 | Trades | àncora: 49-0000 Installation, Maintenance, and Repair Occupations (+37-0000 pulizie, +parti di 47-0000) | 74 Electrical and electronic trades workers; 723 Machinery mechanics; 752; 91 Cleaners and helpers | **DEBOLE** — vedi sotto |
| 33 | Transportation | 53-0000 Transportation and Material Moving Occupations (metà drivers) | 83 Drivers and mobile plant operators (831-833, 835; 834 resta Logistics); 315 | Alta |

## Le mappature deboli — contano quanto la tabella

1. **Energy** — è un SETTORE, non un mestiere. Né O\*NET né ISCO hanno un
   gruppo «energia»: l'ingegnere elettrico è in 17-0000/2151, l'operatore di
   centrale in 51-801x/3131, il montatore di linee in 49-905x/7413. Qualunque
   àncora è una frazione della famiglia. Trattamento proposto: àncora
   dichiarata (17-0000/2151) + nota esplicita «industry family» in export.
2. **Government & Public Sector** — stesso problema di settore. ISCO aiuta un
   po' (111 legislatori/alti funzionari, 335 ispettori regolatori), ma un
   urbanista comunale o un infermiere ASL ricadono altrove. Àncora 111+335
   dichiarata come parziale.
3. **Environmental & Sustainability** — famiglia giovane, sparsa: scienziati
   ambientali (19-2041/2133), sustainability manager (11-0000), specialisti
   (13-0000). L'àncora scientifica copre la metà visibile.
4. **Trades** — convenzione NOSTRA per disegno: elettricisti, idraulici,
   meccanici, manutentori E pulizie. Taglia tre major group O\*NET
   (37 pulizie, 47 elettricisti/idraulici, 49 manutenzione) e due ISCO (7 e 9).
   Non è correggibile senza rinunciare alla convenzione: va dichiarata, non
   «aggiustata».
5. **Hospitality** — O\*NET la disperde (reception in 43-0000, lodging manager
   in 11-0000, housekeeping in 37-0000, concierge in 39-0000); inoltre la
   nostra separazione Hospitality/Food & Beverage non esiste nel SOC (tutto in
   35-0000). Àncora multipla dichiarata.
6. **Consulting** — è un MODO di lavorare, non un mestiere: il consulente IT è
   indistinguibile da un sistemista, il consulente HR da un HR. Àncora
   13-1110/2421 per il management consulting, sapendo che consulenza
   specializzata ricade nella famiglia del mestiere.
7. **Data & Analytics** — debole solo a metà: O\*NET la copre bene (15-2051
   Data Scientists è nel SOC 2018), ma ISCO-08 è del 2008 e i data scientist
   non esistono ancora come gruppo: stanno fra 212 (statistici) e 251
   (software). Àncora 212 + nota.

**Famiglie con àncora solida ma nota obbligata:** Management & Leadership
(trasversale: la rubrica decide caso per caso), Retail/Sales e
Software/Technology (confini nostri dentro un unico gruppo standard),
Food & Beverage (fornai: SOC Production vs Nivult F&B).

## Fonti

- `migrations/0022_job_families_and_corrections.sql:30-42` — le 33 famiglie verbatim.
- `src/nivult/ats/tassonomie.py:118-166` — mappa ISCO-08→famiglia esistente, usata come controllo incrociato.
- `docs/rubrica-classificazione.md` — i confini decisi (Trades/Construction, Retail/Sales, Software/Technology…).
- <https://www.onetcenter.org/taxonomy.html> — struttura O\*NET-SOC 2019 (23 major groups).
- <https://www.bls.gov/soc/2018/major_groups.htm> — nomi ufficiali dei 23 major groups SOC 2018.
- <https://esco.ec.europa.eu/en/classification/occupation_main> — «The ESCO occupations pillar is built on ISCO-08… ISCO-08 provides the top four levels».
- Struttura ISCO-08: ILO non raggiungibile direttamente; verificata su fonte secondaria e incrociata con la mappa interna del repo.
