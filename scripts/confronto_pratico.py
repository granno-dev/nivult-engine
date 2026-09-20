"""In pratica, cosa cambia fra il nostro 2B addestrato e l'8B generico?

Non le medie: i casi. Si guardano le offerte una per una e si mostra cosa scrive
l'uno e cosa scrive l'altro, divise per tipo di differenza — perche' «4,3 volte
piu' voci» non dice se sono voci utili o rumore.
"""
from __future__ import annotations
import collections
import json
import re

righe = json.load(open("/opt/nivult/uscite-8b.json"))

NON_TEC = re.compile(
    r"\b(401k|retirement|pension|insurance|bonus|welfare|vacation|holiday|fund|cares|"
    r"engineering|ingegneria|management|gestione|marketing|finance|accounting|logistics|"
    r"construction|costruzion|sales|vendite|budgeting|planning|reporting|forecasting|"
    r"problem solving|leadership|formazione|training|first aid|cpr|diploma|laurea|degree|"
    r"certification|certificazione|additives|chemicals|fibers|copper|rame)\b", re.I)

muto2b = sia = solo2b = 0
for r in righe:
    a, b = set(r["tec_2b"]), set(r["tec_8b"])
    if not a and b:
        muto2b += 1
    elif a and b:
        sia += 1
    elif a and not b:
        solo2b += 1

n = len(righe)
print(f"su {n} offerte:")
print(f"  il 2B non trova NIENTE e l'8B si'   {muto2b:>4}  ({100*muto2b/n:.0f}%)")
print(f"  trovano entrambi qualcosa           {sia:>4}  ({100*sia/n:.0f}%)")
print(f"  solo il 2B trova qualcosa           {solo2b:>4}  ({100*solo2b/n:.0f}%)")
print(f"  nessuno dei due                     {n-muto2b-sia-solo2b:>4}")

print("\n" + "=" * 78)
print("CASI IN CUI IL 2B TACEVA E L'8B PARLA (il guadagno vero)")
print("=" * 78)
visti = 0
for r in righe:
    a, b = set(r["tec_2b"]), set(r["tec_8b"])
    if a or not b or visti >= 4:
        continue
    buone = [x for x in sorted(b) if not NON_TEC.search(x)]
    cattive = [x for x in sorted(b) if NON_TEC.search(x)]
    print(f"\n  {(r['title'] or '?')[:64]}")
    print(f"    2B: (niente)")
    print(f"    8B: {', '.join(buone)[:88]}")
    if cattive:
        print(f"        ...e queste che tecnologie NON sono: {', '.join(cattive)[:56]}")
    visti += 1

print("\n" + "=" * 78)
print("CASI IN CUI PARLANO ENTRAMBI (si vede il confine diverso)")
print("=" * 78)
visti = 0
for r in righe:
    a, b = set(r["tec_2b"]), set(r["tec_8b"])
    if not a or not b or len(b) < len(a) + 3 or visti >= 4:
        continue
    print(f"\n  {(r['title'] or '?')[:64]}")
    print(f"    2B ({len(a)}): {', '.join(sorted(a))[:84]}")
    print(f"    8B ({len(b)}): {', '.join(sorted(b))[:84]}")
    visti += 1

print("\n" + "=" * 78)
print("DOVE IL 2B VINCE: voci che l'8B si perde")
print("=" * 78)
perse = collections.Counter()
for r in righe:
    for x in set(r["tec_2b"]) - set(r["tec_8b"]):
        perse[x] += 1
print(f"  voci che il 2B trova e l'8B no: {sum(perse.values())}")
print(f"  le piu' frequenti: {', '.join(x for x, _ in perse.most_common(12))}")
