"""«Ne trova di piu'» non vuol dire «meglio»: quante sono davvero tecnologie?

Il banco ha detto che l'8B trova piu' voci del 2B nell'82% dei casi. Ma negli
esempi comparivano «401k retirement savings» e «bass pro cares fund» (benefit),
«civil engineering» (una disciplina), «additives, chemicals, fibers» (materiali).
Contare le voci misura la quantita', non la correttezza — ed e' esattamente il
tipo di metrica che fa sembrare un peggioramento un miglioramento.

Qui si guarda la PRECISIONE: delle voci in piu' che l'8B ha aggiunto, quante
sono plausibilmente tecnologie e quante sono altro? Il giudizio e' grezzo — un
elenco di parole che tradiscono le categorie sbagliate — ma serve a capire
l'ordine di grandezza prima di spendere altro.
"""
from __future__ import annotations
import collections
import json
import re

USCITE = "/opt/nivult/uscite-8b.json"

# categorie che NON sono tecnologie, riconosciute da parole spia
NON_TECNOLOGIA = {
    "benefit e compenso": re.compile(
        r"\b(401k|retirement|pension|insurance|assicurazion|bonus|welfare|ferie|vacation|"
        r"holiday|maternity|paternit|discount|sconto|mensa|buoni pasto|ticket|"
        r"smart working|remote work|flexible|fund|cares|wellness|gym|palestra)\b", re.I),
    "disciplina o settore": re.compile(
        r"\b(engineering|ingegneria|management|gestione|marketing|finance|finanza|"
        r"accounting|contabilit|logistics|logistica|architettura|architecture|"
        r"construction|costruzion|design(?! system)|sales|vendite|hr|risorse umane)\b", re.I),
    "processo o attivita'": re.compile(
        r"\b(budgeting|planning|pianificazion|reporting|forecasting|change management|"
        r"problem solving|team work|comunicazione|communication|negoziazione|leadership|"
        r"formazione|training|coaching|supervision|supervisione|installazione|manutenzione)\b", re.I),
    "materiale o prodotto": re.compile(
        r"\b(additives|chemicals|fibers|fibre|nonwoven|copper|rame|acciaio|steel|"
        r"cemento|concrete|plastic|plastica|legno|wood|vernici|paint)\b", re.I),
    "titolo o certificazione": re.compile(
        r"\b(first aid|cpr|primo soccorso|patente|licen[sz]|diploma|laurea|degree|"
        r"certification|certificazione|abilitazione)\b", re.I),
}

righe = json.load(open(USCITE))
print(f"offerte esaminate: {len(righe)}\n")

agg_tot = 0
sospette = collections.Counter()
esempi = collections.defaultdict(list)
pulite = []

for r in righe:
    aggiunte = set(r["tec_8b"]) - set(r["tec_2b"])
    agg_tot += len(aggiunte)
    for voce in aggiunte:
        for categoria, patt in NON_TECNOLOGIA.items():
            if patt.search(voce):
                sospette[categoria] += 1
                if len(esempi[categoria]) < 5:
                    esempi[categoria].append(voce)
                break
        else:
            if len(pulite) < 14:
                pulite.append(voce)

sos = sum(sospette.values())
print(f"voci AGGIUNTE dall'8B rispetto al 2B: {agg_tot:,}")
print(f"  che NON sembrano tecnologie:        {sos:,}  ({100*sos/max(agg_tot,1):.0f}%)")
print(f"  che sembrano tecnologie vere:       {agg_tot-sos:,}  ({100*(agg_tot-sos)/max(agg_tot,1):.0f}%)\n")

print("per categoria sbagliata:")
for cat, q in sospette.most_common():
    print(f"  {cat:<24}{q:>5}   {', '.join(esempi[cat])[:58]}")

print(f"\nesempi di voci aggiunte che sembrano vere tecnologie:")
print(f"  {', '.join(pulite)}")

# e quante voci in tutto, per capire se l'8B e' semplicemente prolisso
t2 = sum(len(r["tec_2b"]) for r in righe)
t8 = sum(len(r["tec_8b"]) for r in righe)
print(f"\nvoci in totale:  2B {t2:,}   8B {t8:,}   ({t8/max(t2,1):.1f} volte tanto)")
