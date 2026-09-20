"""Una lista sola di ATS, non tre.

Oggi la stessa lacuna ci ha fregati tre volte:
  - `cdn.ashbyprd.com` mancava nel filtro del cacciatore   (stamattina)
  - `rippling`, `careerpuck`, `cloud.sap` mancavano in `riscoperta._ATS_HOST`
  - `zohorecruit` mancava nel cacciatore   -> 336 bacheche scritte come aziende

La causa non e' la disattenzione: sono DUE liste diverse in due file diversi, e
chi aggiorna l'una non sa dell'altra. Qui il cacciatore importa la lista di
`riscoperta` e la unisce alla propria, cosi' ogni fornitore aggiunto da una parte
vale anche dall'altra.

E si aggiungono i mancanti misurati oggi.
"""
import ast
import pathlib

p = pathlib.Path("/opt/nivult/caccia_domini.py")
s = p.read_text()

AGGIUNTA = '''
# UNA LISTA SOLA. Gli ATS li elenca gia' `nivult.ats.riscoperta._ATS_HOST`, che e'
# la lista usata da chi scrive i domini dall'URL dell'offerta. Tenerne due
# separate ci e' costato tre volte in un giorno — `ashbyprd`, `rippling`,
# `zohorecruit` — perche' chi aggiornava l'una non sapeva dell'altra.
try:
    import sys as _sys
    _sys.path.insert(0, "/opt/nivult/engine/src")
    from nivult.ats.riscoperta import _ATS_HOST as _ALTRA_LISTA
    NON_AZIENDA = re.compile(f"(?:{NON_AZIENDA.pattern})|(?:{_ALTRA_LISTA.pattern})", re.I)
except Exception:                                                # noqa: BLE001
    pass    # se l'import non riesce resta la lista locale: meglio parziale che rotta
'''

if "_ALTRA_LISTA" in s:
    print("gia' applicato")
else:
    # zohorecruit e gli altri trovati oggi vanno comunque nella lista locale
    v = 'r"recruiterbox|jazz|smartrecruit|"'
    n = ('r"recruiterbox|jazz|smartrecruit|"\n'
         '    # aggiunti il 19/09/2026: 336 bacheche Zoho scritte come se fossero aziende,\n'
         '    # perche\' il candidato ERA il nostro tenant e quindi rimandava a se stesso\n'
         '    r"zohorecruit|catsone|vincere|cornerstone|csod|pinpointhq|jobsoid|trakstar|"\n'
         '    r"homerun|freshteam|comeet|softgarden|heavenhr|traffit|taleez|hirehive|"\n'
         '    r"jobscore|applicantstack|niceboard|crelate|hiringthing|eightfold|avature|"\n'
         '    r"phenom|pageup|radancy|jibeapply|smartjobboard|werecruit|digitalrecruiters|"\n'
         '    r"dvinci|talentsoft|careerpuck|cloud\\.sap|sapsf|oraclecloud|paylocity|"\n'
         '    r"paycom|paycor|ukg\\.com|"')
    assert v in s, "la lista locale non e' quella attesa"
    s = s.replace(v, n, 1)
    # e l'unione con l'altra lista, subito dopo la sua definizione
    i = s.find("PAROLE_LAVORO")
    assert i > 0
    s = s[:i] + AGGIUNTA + "\n" + s[i:]
    p.write_text(s)
    ast.parse(s)
    print("liste unite e mancanti aggiunti")

import importlib.util
spec = importlib.util.spec_from_file_location("cd", p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print("\nprova del filtro:")
for d, atteso in (("srvmedia.zohorecruit.in", True), ("mctekk.zohorecruit.com", True),
                  ("app.rippling.com", True), ("cdn.ashbyprd.com", True),
                  ("career5.cloud.sap", True), ("hire.trakstar.com", True),
                  ("cvshealth.com", False), ("besix.com", False),
                  ("drmax.eu", False), ("workingreenland.gl", False)):
    bloccato = bool(m.NON_AZIENDA.search(d))
    print(f"  {'ok ' if bloccato == atteso else 'NO '} {d:<30}{'scartato' if bloccato else 'accettato'}")
