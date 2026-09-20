"""Il prompt corretto risolve i due difetti dell'8B?

Confronto APPAIATO su tre fronti, sulle stesse 200 offerte:
  - il 2B di produzione (gia' in archivio)
  - l'8B col prompt vecchio (gia' misurato, /opt/nivult/uscite-8b.json)
  - l'8B col prompt nuovo (questa prova)

Si misura quello che il prompt dovrebbe aggiustare:
  1. quante voci NON sono tecnologie (erano l'11% delle aggiunte);
  2. quanto i nomi si allineano a quelli del 2B (erano 89 le voci «perse», in
     parte solo scritte diversamente).
"""
from __future__ import annotations
import concurrent.futures as cf
import gzip
import importlib.util
import json
import re
import sys
import time

sp = importlib.util.spec_from_file_location("b", "/opt/nivult/banco_http.py")
b = importlib.util.module_from_spec(sp)
sp.loader.exec_module(b)
sp2 = importlib.util.spec_from_file_location("p", "/opt/nivult/prompt2.py")
p2 = importlib.util.module_from_spec(sp2)
sp2.loader.exec_module(p2)

URL = sys.argv[1]
N = int(sys.argv[2]) if len(sys.argv) > 2 else 200

NON_TEC = re.compile(
    r"\b(401k|retirement|pension|insurance|bonus|welfare|vacation|holiday|fund|cares|"
    r"engineering|ingegneria|management|gestione|marketing|finance|accounting|logistics|"
    r"construction|costruzion|sales|vendite|budgeting|planning|reporting|forecasting|"
    r"problem solving|leadership|formazione|training|first aid|cpr|diploma|laurea|degree|"
    r"certification|certificazione|additives|chemicals|fibers|copper|rame)\b", re.I)

righe = [json.loads(l) for _, l in zip(range(N), gzip.open("/opt/nivult/banco-gpu.jsonl.gz", "rt"))]
vecchie = {r["title"]: r for r in json.load(open("/opt/nivult/uscite-8b.json"))}

# si usa il prompt NUOVO al posto di quello vecchio
b.SISTEMA_TEC = p2.NUOVO


def una(r):
    t, _, _ = b.chiedi(URL, "Qwen/Qwen3-8B", b.prompt(r), 600, True)
    d = b.estrai_json(t)
    return {"title": r.get("title"), "tec_2b": sorted(b.nomi(r.get("tecnologie_2b"))),
            "tec_new": sorted(b.nomi((d or {}).get("tecnologie"))),
            "rotta": d is None}


t0 = time.time()
with cf.ThreadPoolExecutor(48) as ex:
    fuori = list(ex.map(una, righe))
dt = time.time() - t0
print(f"{len(fuori)} offerte in {dt:.0f}s  ({3600*len(fuori)/dt:,.0f}/ora)\n")

rotte = sum(1 for x in fuori if x["rotta"])
print(f"risposte illeggibili: {rotte} ({100*rotte/len(fuori):.0f}%)\n")


def sporche(voci):
    return sum(1 for v in voci if NON_TEC.search(v))


tot_v = tot_n = sp_v = sp_n = 0
mute_v = mute_n = 0
for x in fuori:
    v = vecchie.get(x["title"], {}).get("tec_8b", [])
    n = x["tec_new"]
    tot_v += len(v); tot_n += len(n)
    sp_v += sporche(v); sp_n += sporche(n)
    mute_v += (not v); mute_n += (not n)

print(f"{'':<34}{'prompt vecchio':>16}{'prompt nuovo':>15}")
print(f"  {'voci in totale':<32}{tot_v:>16,}{tot_n:>15,}")
print(f"  {'di cui NON tecnologie':<32}{sp_v:>13,} ({100*sp_v/max(tot_v,1):.0f}%){sp_n:>11,} ({100*sp_n/max(tot_n,1):.0f}%)")
print(f"  {'offerte lasciate vuote':<32}{mute_v:>16}{mute_n:>15}")

# quanto i nomi si allineano al 2B: voci che il 2B ha e il modello scrive uguale
acc_v = acc_n = base = 0
for x in fuori:
    due = set(x["tec_2b"])
    if not due:
        continue
    base += len(due)
    acc_v += len(due & set(vecchie.get(x["title"], {}).get("tec_8b", [])))
    acc_n += len(due & set(x["tec_new"]))
print(f"  {'voci del 2B scritte identiche':<32}{acc_v:>10,}/{base} ({100*acc_v/max(base,1):.0f}%)"
      f"{acc_n:>7,}/{base} ({100*acc_n/max(base,1):.0f}%)")

with open("/opt/nivult/uscite-8b-prompt2.json", "w") as f:
    json.dump(fuori, f, ensure_ascii=False, indent=1)

print("\nesempi (2B | vecchio | nuovo):")
mostrati = 0
for x in fuori:
    v = vecchie.get(x["title"], {}).get("tec_8b", [])
    if not v or mostrati >= 5:
        continue
    print(f"\n  {(x['title'] or '?')[:60]}")
    print(f"    2B      {', '.join(x['tec_2b'])[:78] or '(niente)'}")
    print(f"    vecchio {', '.join(v)[:78]}")
    print(f"    nuovo   {', '.join(x['tec_new'])[:78]}")
    mostrati += 1
