"""Un prefisso di tre lettere non e' una corrispondenza.

La mia funzione dava 1.0 ogni volta che una stringa iniziava con l'altra, senza
guardare QUANTO ne copriva. Cosi' «Bcm Cosmetique» -> `bcm.edu` prendeva 1.0
(«bcmcosmetique» inizia per «bcm») ed e' il Baylor College of Medicine. Stessa
cosa per «Vacoa» -> `vaco.com` e «Anap» -> `anap.gov.ro`.

La calibrazione di prima non lo aveva visto perche' confrontava coppie VERE con
coppie a CASO — e a caso non capita quasi mai che una sia prefisso dell'altra.
I candidati di un motore di ricerca invece sono tutti plausibili, ed e' proprio
li' che la scorciatoia sbagliava.

La regola nuova: il prefisso vale solo se copre almeno il 70% della stringa piu'
lunga. «armorlux» / «armorlux» si', «bcm» / «bcmcosmetique» (23%) no.
"""
import ast
import importlib.util
import pathlib

p = pathlib.Path("/opt/nivult/caccia_domini.py")
s = p.read_text()

VECCHIO = '''    if a == b or a.startswith(b) or b.startswith(a):
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()'''

NUOVO = '''    if a == b:
        return 1.0
    # Un prefisso vale solo se copre quasi tutto: «bcm» dentro «bcmcosmetique»
    # sono tre lettere su tredici, e infatti bcm.edu e' il Baylor College of
    # Medicine, non una ditta di cosmetici (19/09/2026).
    if a.startswith(b) or b.startswith(a):
        copertura = min(len(a), len(b)) / max(len(a), len(b))
        return 1.0 if copertura >= 0.70 else copertura
    return difflib.SequenceMatcher(None, a, b).ratio()'''

if "copertura" in s:
    print("gia' applicato")
else:
    assert VECCHIO in s, "la funzione non e' quella attesa"
    p.write_text(s.replace(VECCHIO, NUOVO, 1))
    ast.parse(p.read_text())
    print("prefisso: serve il 70% di copertura")

spec = importlib.util.spec_from_file_location("cd", p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print(f"\nsoglia {m.SOGLIA_NOME} — i casi che avevano sbagliato, e quelli giusti:")
for nome, d, atteso in (("Bcm Cosmetique", "bcm.edu", False),
                        ("Vacoa", "vaco.com", False),
                        ("Anap", "anap.gov.ro", True),   # qui il nome E' anap: resta un falso
                        ("Solidom", "solidigm.com", False),
                        ("ARMOR LUX", "armorlux.com", True),
                        ("ADELIA MEDICAL", "adeliamedical.fr", True),
                        ("Motherson Aerospace", "motherson.com", True),
                        ("Kereis Formation", "kereis.com", True),
                        ("Cvshealth", "cvshealth.com", True),
                        ("Hupp Intérim", "huppinterim.fr", True)):
    v = m.somiglia_al_nome(nome, d)
    passa = v >= m.SOGLIA_NOME
    print(f"  {'ok ' if passa == atteso else 'NO '} {nome[:24]:<26}{d:<22}{v:.2f}  {'passa' if passa else 'no'}")
