"""Ancora `x.com` al confine di dominio.

Twitter si chiama `x.com`, ma la regola non era ancorata e prendeva qualunque
dominio che finisse cosi': allnex.com, ambaflex.com, amdax.com, appviewx.com,
besix.com. Misurato: 36 aziende vere scartate, l'1% di tutto quello che abbiamo.

`(^|\.)x\.com` vuol dire «x.com all'inizio, oppure preceduto da un punto» —
cioe' il dominio vero o un suo sottodominio, non una coincidenza di lettere.
"""
import ast, importlib.util, pathlib, re
p = pathlib.Path("/opt/nivult/caccia_domini.py")
s = p.read_text()
if r"(^|\.)x\.com" in s:
    print("gia' applicato")
else:
    n = s.count(r"x\.com")
    assert n >= 1, "non trovo x\\.com nella lista"
    s = s.replace(r"|x\.com|", r"|(^|\.)x\.com|")
    p.write_text(s); ast.parse(s); print(f"ancorato ({n} occorrenze trovate)")

spec = importlib.util.spec_from_file_location("cd", p)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print("\nprova:")
for d, atteso in (("besix.com", False), ("allnex.com", False), ("appviewx.com", False),
                  ("netflix.com", False), ("x.com", True), ("www.x.com", True),
                  ("srvmedia.zohorecruit.in", True), ("cvshealth.com", False)):
    b = bool(m.NON_AZIENDA.search(d))
    print(f"  {'ok ' if b == atteso else 'NO '} {d:<26}{'scartato' if b else 'accettato'}")
