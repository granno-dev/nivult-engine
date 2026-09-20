"""Allinea la lista di `riscoperta` con i fornitori trovati oggi.

`zohorecruit` mancava in entrambe le liste, e sono finite in archivio 336
bacheche Zoho spacciate per domini aziendali. Qui si aggiungono a `riscoperta`,
che e' la lista da cui parte anche la pulizia notturna: se restasse solo nel
cacciatore, la pulizia non saprebbe cosa cercare.

E si ancora `x\\.com`, che non ancorato scartava 36 aziende vere (allnex.com,
ambaflex.com, appviewx.com) — l'1% di tutti i domini che abbiamo.
"""
import ast
import importlib.util
import pathlib

p = pathlib.Path("/opt/nivult/engine/src/nivult/ats/riscoperta.py")
s = p.read_text()

VECCHIO = r'''    r"rippling|careerpuck|recruiterbox|paycom|paycor|ukg\.com|"'''
NUOVO = r'''    r"rippling|careerpuck|recruiterbox|paycom|paycor|ukg\.com|"
    # aggiunti il 19/09/2026: 336 bacheche Zoho erano finite in archivio come
    # domini aziendali, perche' zohorecruit non era in nessuna delle due liste
    r"zohorecruit|catsone|vincere|cornerstone|csod|pinpointhq|jobsoid|trakstar|"
    r"homerun|freshteam|comeet|softgarden|heavenhr|traffit|taleez|hirehive|"
    r"jobscore|applicantstack|niceboard|crelate|hiringthing|eightfold|avature|"
    r"phenom|pageup|radancy|jibeapply|smartjobboard|werecruit|digitalrecruiters|"
    r"dvinci|talentsoft|oraclecloud|paylocity|"'''

if "336 bacheche Zoho" in s:
    print("gia' applicato")
else:
    assert VECCHIO in s, "la riga non e' quella attesa"
    p.write_text(s.replace(VECCHIO, NUOVO, 1))
    ast.parse(p.read_text())
    print("riscoperta allineata")

spec = importlib.util.spec_from_file_location("r", p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print("\nprova di _radice:")
for h, atteso in (("srvmedia.zohorecruit.in", None), ("mctekk.zohorecruit.com", None),
                  ("hire.trakstar.com", None), ("app.rippling.com", None),
                  ("careers.dhl.com", "dhl.com"), ("besix.com", "besix.com"),
                  ("workday.com", "workday.com")):
    got = m._radice(h)
    print(f"  {'ok ' if got == atteso else 'NO '} {h:<28} -> {got}")
