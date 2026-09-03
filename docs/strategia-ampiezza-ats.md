# Strategia per l'ampiezza — raggiungere e superare 80+ ATS

Obiettivo: allargare la copertura di piattaforme ATS fino a pareggiare (e
superare) i fornitori a pagamento tipo jobdataapi. Documento di strategia
e roadmap, con la ricerca del 04/09/2026 alle spalle.

## Dove siamo

51 adapter. **Abbiamo gia' tutti i grandi** (Greenhouse, Workday, Lever,
Ashby, Oracle, Phenom, SuccessFactors, iCIMS, SmartRecruiters, Cornerstone,
Workable, BambooHR) e molti europei (Personio, softgarden, Teamtailor,
werecruit, In-recruiting, Talentsoft, Factorial). Il gap verso "80+" NON
sono grandi ATS mancanti: e' la **coda lunga** (decine di ATS piccoli e
regionali, ~1% delle aziende ciascuno) + i CRM di **staffing** (agenzie).

Verificato: gli aggregatori pro usano i nostri stessi due meccanismi (API
JSON pubbliche degli ATS + scoperta da Common Crawl). Non c'e' magia da
recuperare, solo ampiezza.

## Il vincolo di realta'

Ogni nuovo ATS richiede il **ciclo completo**, per-ATS:
1. ricerca del pattern-host e dell'endpoint pubblico;
2. `scoperta_archivi` (Common Crawl/Wayback) per censire i tenant;
3. scrittura dell'adapter;
4. test su tenant reali.

Non e' un'operazione uno-shot per venti piattaforme: farla di fretta
produce adapter fragili (vedi i bug di "sede buttata" del 04/09). Si fa a
lotti piccoli, ognuno testato — come i 5 adapter aggiunti il 04/09
(personio, recruiterbox, icims, zohorecruit + i fix).

## Le strategie, per leva

### Strategia 1 — Adapter generico JSON-LD (leva massima)

Quasi ogni pagina-offerta emette `schema.org/JobPosting` in JSON-LD (per
Google for Jobs): titolo, azienda, sede, paese, data, salario, in formato
standard identico ovunque. Un **singolo adapter generico** guidato dal
**sitemap** del career site (sitemap → pagine offerta → JSON-LD) copre
in un colpo decine di ATS piccoli e i career site aziendali propri.

Stato: la logica di estrazione JSON-LD esiste gia' (phenom). Il pezzo
mancante e' la **scoperta dei career site** (input diverso dallo slug-ATS)
e il rispetto del costo (una fetch per offerta → serve prudenza sui
volumi). E' un progetto a se', ad alto ritorno.

### Strategia 2 — Scoperta guidata dai dati

Il guardiano (`salute.py` #7) rileva domini con molti tenant non gestiti,
MA scansiona il motore, che contiene solo offerte da ATS gia' noti: non
scopre ATS che non abbiamo ancora onboardato. Per trovare i nuovi serve
lanciare `scoperta_archivi` con **pattern nuovi** e ordinare per numero
di tenant trovati. Priorita' = aziende reali, non popolarita' teorica.

### Strategia 3 — Aggiunte mirate (a mano), per il mercato EU

Ordine di valore:
1. **Bullhorn** — re dello staffing (10.000+ agenzie). ATTENZIONE:
   verificato il 04/09 che l'API pubblica richiede `cls`+`corpToken`
   per-cliente e i portali sono siti custom molto vari (bankwstaffing.com
   non ha ne' config ne' JSON-LD standard). Non e' un adapter a slug
   pulito: va trattato per-cliente (token nel censimento, come `pub_key`
   di In-recruiting) o via JSON-LD dove c'e'. Progetto dedicato.
2. **Regionali EU**: DE (d.vinci, Concludis, Rexx, onlyfy/XING, Prescreen,
   HRworks, Haufe/Umantis), FR (Flatchr, Beetween, DigitalRecruiters,
   Taleez), Nordici (Jobylon, Talentech, Emply, HR-Manager).
3. **Enterprise mancanti**: UKG, Dayforce/Ceridian, Kenexa/BrassRing (IBM),
   SumTotal.
4. **CRM agenzie**: Vincere, Loxo, Crelate, JobDiva, Ceipal.

Si aggiunge solo chi ha **feed pubblico** (JSON/XML/JSON-LD) e **link
diretto** (la regola di casa). Login-walled (jobteaser) e assessment
(HireVue) restano fuori.

## Framework di priorita'

Per ogni candidato:

**valore = (n° tenant scopribili) × (scrapabilita') ÷ sforzo**

- scrapabilita' ALTA: feed JSON/XML pubblico o JSON-LD → adapter rapido
  (personio, softgarden).
- scrapabilita' BASSA: SPA/Firebase/login → costoso o impossibile
  (recruiterbox era Firebase ma aveva il feed RSS; jobteaser login →
  scartato; hirevue assessment → scartato).

## Sequenza consigliata

1. **Strategia 1** (adapter JSON-LD generico) — porta oltre 80+ effettivi
   con un pezzo solo; da progettare bene (scoperta career site + costo).
2. **Regionali EU a feed pulito** (Strategia 3.2) — a lotti piccoli,
   testati: Jobylon, d.vinci, Concludis… uno alla volta.
3. **Bullhorn** (Strategia 3.1) — progetto dedicato per l'angolo agenzie.

## Perche' NON compriamo (jobdataapi)

$495+/mese per gli stessi ATS che raccogliamo gratis. Ha senso solo per
smettere di mantenere adapter, a fatturato acquisito. Finche' costruiamo,
il costo marginale di un adapter e' zero. Vedi [`raccolta-ats.md`].
