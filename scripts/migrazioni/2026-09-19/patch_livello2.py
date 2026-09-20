"""Il cacciatore accetta anche il livello 2, e dichiara sempre quale livello e'.

Fin qui scriveva un dominio SOLO se il sito rimandava al nostro tenant ATS
(livello 1). E' la prova piu' forte che abbiamo — prende Orbotech -> kla.com, che
dal nome non si indovinerebbe mai — ma la lasciano superare solo le aziende che
quel link ce l'hanno in chiaro. CVS Health, Broadcom e Colliers no, e infatti
erano fra i nostri fallimenti pur avendo un dominio ovvio.

Il livello 2 e' la corrispondenza fra nome e dominio. Calibrato su 1.003 coppie
gia' provate dal giudice: soglia 0,80 prende l'83,5% delle coppie vere e lo 0,2%
di coppie accoppiate a caso.

Cio' che NON si fa: scaricare il sito per confermarlo. Misurato — Cloudflare
risponde 403 e le testate da browser non cambiano niente.
"""
import ast
import pathlib

p = pathlib.Path("/opt/nivult/caccia_domini.py")
s = p.read_text()

FUNZIONI = '''
# --- livello 2: la corrispondenza fra nome e dominio ------------------------
# Calibrata il 19/09/2026 su 1.003 coppie gia' provate dal giudice: a 0,80 ci sta
# dentro l'83,5% delle coppie vere e lo 0,2% di quelle accoppiate a caso.
SOGLIA_NOME = float(os.environ.get("SOGLIA_NOME", "0.80"))
_FORME = re.compile(
    r"\\b(gmbh|ag|inc|llc|ltd|limited|corp|corporation|co|sa|srl|spa|bv|nv|plc|"
    r"group|gruppe|holding|holdings|international|emea|usa|careers?|jobs?)\\b", re.I)


def _nome_nudo(n: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _FORME.sub(" ", (n or "").lower()))


def _radice_dominio(d: str) -> str:
    d = (d or "").lower().removeprefix("www.")
    return re.sub(r"[^a-z0-9]", "", d.split(".")[0] if d else "")


def somiglia_al_nome(nome: str, dominio: str) -> float:
    """Quanto il dominio dice il nome dell'azienda. 1.0 se uno contiene l'altro."""
    a, b = _nome_nudo(nome), _radice_dominio(dominio)
    if not a or not b or len(b) < 3:
        return 0.0
    if a == b or a.startswith(b) or b.startswith(a):
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()

'''

if "somiglia_al_nome" in s:
    print("gia' applicato")
else:
    # difflib serve, e va importato accanto agli altri
    assert "import difflib" not in s
    s = s.replace("import json", "import difflib\nimport json", 1)
    # le funzioni vanno prima della classe che le usa
    i = s.find("\nclass Cacciatore")
    assert i > 0, "non trovo la classe Cacciatore"
    s = s[:i] + "\n" + FUNZIONI + s[i:]
    p.write_text(s)
    ast.parse(s)
    print("funzioni del livello 2 inserite")

import importlib.util
spec = importlib.util.spec_from_file_location("cd", p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print(f"soglia: {m.SOGLIA_NOME}")
for n, d, atteso in (("Cvshealth", "cvshealth.com", True),
                     ("Besix", "besix.com", True),
                     ("Broadcom", "broadcom.com", True),
                     ("Colliersinternationalemea", "colliers.com", True),
                     ("Konzmann Gmbh", "konzmann-gruppe.de", True),
                     ("Homelandls", "dhs.gov", False),
                     ("Orbotech", "kla.com", False),
                     ("Mental Health Association", "mhainc.org", False)):
    v = m.somiglia_al_nome(n, d)
    ok = (v >= m.SOGLIA_NOME) == atteso
    print(f"  {'ok ' if ok else 'NO '} {n[:28]:<30}{d:<24}{v:.2f}  {'passa' if v >= m.SOGLIA_NOME else 'no'}")
