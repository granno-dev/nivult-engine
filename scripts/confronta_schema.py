"""`schema.sql` sa ricostruire il database ATS, o si e' allontanato dalla realta'?

Lo schema e' il documento che dice com'e' fatto l'archivio. Se le colonne vere
sono state aggiunte a mano o da moduli sparsi, il file mente — e il giorno che
serve ricostruire da zero (un disastro, un secondo ambiente, una prova) ci si
accorge che non funziona.

Sospetto concreto: `expired_at` non compare in `schema.sql`, ne' nella CREATE
TABLE ne' in un ALTER, eppure ogni query del motore ci filtra sopra.

Si applica lo schema a un database vuoto, si confrontano le colonne con quelle
di produzione, e si stampa cosa manca.
"""
import os
import subprocess
import sys

import psycopg

PROVA = "nivult_prova_schema"


def colonne(dsn: str) -> dict[str, set[str]]:
    fuori: dict[str, set[str]] = {}
    with psycopg.connect(dsn) as c:
        for t, col in c.execute("""
            SELECT table_name, column_name FROM information_schema.columns
             WHERE table_schema = 'public' ORDER BY 1, 2"""):
            fuori.setdefault(t, set()).add(col)
    return fuori


base = os.environ["ATS_DATABASE_URL"]
vere = colonne(base)
finte = colonne(base.rsplit("/", 1)[0] + "/" + PROVA)

print(f"tabelle in produzione: {len(vere)}   ricostruite dallo schema: {len(finte)}\n")

mancanti_tab = sorted(set(vere) - set(finte))
if mancanti_tab:
    print("TABELLE che lo schema non sa creare:")
    for t in mancanti_tab:
        print(f"  {t}")
    print()

print("COLONNE che lo schema non sa creare (solo tabelle che conosce):")
trovato = False
for t in sorted(set(vere) & set(finte)):
    manca = sorted(vere[t] - finte[t])
    if manca:
        trovato = True
        print(f"  {t}:")
        for m in manca:
            print(f"      {m}")
if not trovato:
    print("  nessuna")

print("\nCOLONNE che lo schema crea ma in produzione non ci sono (schema avanti):")
avanti = False
for t in sorted(set(vere) & set(finte)):
    piu = sorted(finte[t] - vere[t])
    if piu:
        avanti = True
        print(f"  {t}: {', '.join(piu)}")
if not avanti:
    print("  nessuna")
