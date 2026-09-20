"""Due regole al posto di una, perche' i casi sono due.

Col solo 70% di copertura si scartava `bcm.edu` (giusto) ma anche `motherson.com`
per «Motherson Aerospace» e `kereis.com` per «Kereis Formation» (sbagliato). La
differenza non e' la proporzione: e' che «bcm» e' una sigla di tre lettere e
«motherson» e' una parola intera. Un nome aziendale perde spesso il suffisso
(Aerospace, Formation, Cosmetique) nel dominio, e va bene; quello che non va bene
e' accontentarsi di tre lettere.

Regola: il prefisso vale se la parte in comune e' lunga almeno 6 caratteri,
oppure se copre almeno il 70%.

E un controllo che con le stringhe non c'entra: `anap.gov.ro` per un'azienda
francese, o `bcm.edu` per una ditta di cosmetici, si riconoscono dal DOMINIO DI
PRIMO LIVELLO. Un .gov o un .edu non sono un datore privato, e un suffisso di
paese che contraddice il paese dell'azienda e' un campanello.
"""
import ast
import importlib.util
import pathlib

p = pathlib.Path("/opt/nivult/caccia_domini.py")
s = p.read_text()

VECCHIO = '''    if a.startswith(b) or b.startswith(a):
        copertura = min(len(a), len(b)) / max(len(a), len(b))
        return 1.0 if copertura >= 0.70 else copertura'''

NUOVO = '''    if a.startswith(b) or b.startswith(a):
        comune = min(len(a), len(b))
        copertura = comune / max(len(a), len(b))
        # sei caratteri in comune sono una parola; tre sono una sigla, e le sigle
        # collidono («bcm» sta in «bcmcosmetique» ma bcm.edu e' un'universita')
        if comune >= 6 or copertura >= 0.70:
            return 1.0
        return copertura'''

TLD = '''

# Suffissi che un datore privato non usa: se il candidato finisce cosi' non e'
# l'azienda che cerchiamo (anap.gov.ro per una societa' francese, bcm.edu per una
# ditta di cosmetici). Restano ammessi se l'azienda stessa e' un ente pubblico o
# una scuola, e lo si capisce dal suo nome.
TLD_PUBBLICI = (".gov", ".gov.", ".edu", ".edu.", ".mil", ".int")
PAROLE_PUBBLICHE = re.compile(
    r"\\b(comune|ville|citta|city|county|council|ministe|govern|region|province|"
    r"university|universit|college|school|ecole|scuola|hochschule|academy|"
    r"hospital|ospedale|krankenhaus|chu|nhs|agency|agence|agenzia)\\b", re.I)


def tld_plausibile(nome: str, dominio: str, paese: str | None) -> bool:
    """Il suffisso del dominio e' compatibile con questa azienda?"""
    d = (dominio or "").lower()
    if any(d.endswith(t.rstrip(".")) or (t + "") in d for t in TLD_PUBBLICI):
        return bool(PAROLE_PUBBLICHE.search(nome or ""))
    return True

'''

if "tld_plausibile" in s:
    print("gia' applicato")
else:
    assert VECCHIO in s, "la funzione non e' quella attesa"
    s = s.replace(VECCHIO, NUOVO, 1)
    i = s.find("\nclass Cacciatore")
    s = s[:i] + TLD + s[i:]
    # e il controllo entra nella scelta del livello 2
    v = "            if migliore and punteggio >= SOGLIA_NOME and esiste(migliore):"
    n = ("            if (migliore and punteggio >= SOGLIA_NOME\n"
         "                    and tld_plausibile(nome, migliore, paese) and esiste(migliore)):")
    assert v in s, "il punto del livello 2 non e' quello atteso"
    s = s.replace(v, n, 1)
    p.write_text(s)
    ast.parse(s)
    print("regola dei 6 caratteri + controllo del suffisso")

spec = importlib.util.spec_from_file_location("cd", p)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print(f"\nsoglia {m.SOGLIA_NOME}:")
casi = (("Bcm Cosmetique", "bcm.edu", "FR", False), ("Vacoa", "vaco.com", "US", False),
        ("Anap", "anap.gov.ro", "FR", False), ("Solidom", "solidigm.com", "FR", False),
        ("ARMOR LUX", "armorlux.com", "FR", True), ("ADELIA MEDICAL", "adeliamedical.fr", "FR", True),
        ("Motherson Aerospace", "motherson.com", "FR", True), ("Kereis Formation", "kereis.com", "FR", True),
        ("Cvshealth", "cvshealth.com", "US", True), ("Hupp Intérim", "huppinterim.fr", "FR", True),
        ("Ville De Bondy", "ville-bondy.fr", "FR", True))
for nome, d, paese, atteso in casi:
    v = m.somiglia_al_nome(nome, d)
    passa = v >= m.SOGLIA_NOME and m.tld_plausibile(nome, d, paese)
    print(f"  {'ok ' if passa == atteso else 'NO '} {nome[:24]:<26}{d:<22}{v:.2f}  {'passa' if passa else 'no'}")
