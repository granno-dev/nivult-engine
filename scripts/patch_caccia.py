"""Il cacciatore trovava il sito e lo buttava. Tre correzioni, misurate prima.

Su 40 tenant «falliti» presi a caso, 18 avevano il sito dell'azienda scritto
sulla bacheca (tonyrobbins.com, 3st.de, texadeutschland.com, salixdata.com):
`da_logo` li trovava, e poi due giudici li scartavano (20/09/2026).

1. LA VERIFICA NON PUO' FUNZIONARE SUI DOMINI PERSONALIZZATI. `verifica` cerca
   nella pagina l'impronta del tenant («axpogroup.teamtailor.com»), ma
   Teamtailor, Personio, Recruitee, Workable servono la bacheca SUL DOMINIO
   DELL'AZIENDA (careers.axpo.com): li' quella stringa non compare mai. La prova
   pero' c'era gia': abbiamo chiesto la bacheca del tenant e siamo stati
   REINDIRIZZATI su quel dominio. Un fornitore serve la bacheca di un tenant
   solo sul CNAME di quel tenant. Il reindirizzamento e' prova di livello 1,
   purche' la pagina d'arrivo porti ancora l'impronta del fornitore (e' ancora
   la bacheca, non un parcheggio).

2. IL CONFRONTO COL NOME LEGGEVA L'ETICHETTA SBAGLIATA. `_radice_dominio`
   prendeva la PRIMA etichetta dell'host: per «careers.axpo.com» confrontava
   «axpo» con «careers» e dava zero. Ora si prova ogni etichetta (tranne il
   suffisso) e vale la migliore.

3. SI SALVA LA RADICE, NON IL SOTTODOMINIO. «careers.axpo.com» non e' il sito
   dell'azienda: e' la sua pagina carriere. Nel prodotto va «axpo.com», anche
   perche' i doppioni si riconoscono per dominio e due tenant della stessa
   azienda con «jobs.» e «careers.» davanti non si sarebbero mai agganciati.
"""
from __future__ import annotations
import pathlib
import sys

P = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                 else "/opt/nivult/engine/scripts/caccia_domini.py")
s = P.read_text()
fatte = []

# ---------------------------------------------------------------- 2 + 3: radice
vecchio = '''def _radice_dominio(d: str) -> str:
    d = (d or "").lower().removeprefix("www.")
    return re.sub(r"[^a-z0-9]", "", d.split(".")[0] if d else "")'''
nuovo = '''# Prefissi che dicono «pagina carriere», non «azienda»: careers.axpo.com e'
# axpo.com. Si tolgono solo se quel che resta e' ancora un dominio (ha un punto).
PREFISSI_CARRIERE = re.compile(
    r"^(careers?|jobs?|jobb|karriere|karriera|karriere-portal|work|talent|talents|join|"
    r"recruit(ing|ment)?|hr|empleo|emploi|lavoro|vacatures|vacancies|stellen|"
    r"stellenangebote|bewerbung|candidat|candidature|carriere|carriere)\\.", re.I)


def radice_sito(d: str) -> str:
    """Il dominio da SALVARE: senza www. e senza il prefisso «carriere»."""
    d = (d or "").lower().removeprefix("www.")
    r = PREFISSI_CARRIERE.sub("", d)
    return r if "." in r else d


def _etichette(d: str) -> list[str]:
    """Le etichette dell'host che possono dire il nome: tutte tranne il suffisso.
    Per «careers.axpo.com» sono «careers» e «axpo»; prima si guardava solo la
    prima, e «axpo» contro «careers» dava zero (20/09/2026)."""
    d = (d or "").lower().removeprefix("www.")
    parti = [re.sub(r"[^a-z0-9]", "", p) for p in d.split(".")]
    parti = [p for p in parti if p]
    if len(parti) >= 2:
        parti = parti[:-1]                       # via il TLD
        # via anche «com»/«co» in com.br, co.uk: due lettere-tre, mai un nome
        if len(parti) >= 2 and parti[-1] in ("com", "co", "net", "org", "ac", "gov", "edu"):
            parti = parti[:-1]
    return parti


def _radice_dominio(d: str) -> str:
    e = _etichette(d)
    return e[-1] if e else ""'''
assert vecchio in s, "_radice_dominio non combacia"
s = s.replace(vecchio, nuovo)
fatte.append("radice")

vecchio = '''def somiglia_al_nome(nome: str, dominio: str) -> float:
    """Quanto il dominio dice il nome dell'azienda. 1.0 se uno contiene l'altro."""
    a, b = _nome_nudo(nome), _radice_dominio(dominio)
    if not a or not b or len(b) < 3:
        return 0.0'''
nuovo = '''def somiglia_al_nome(nome: str, dominio: str) -> float:
    """Quanto il dominio dice il nome dell'azienda. 1.0 se uno contiene l'altro.
    Si prova ogni etichetta dell'host e vale la migliore."""
    a = _nome_nudo(nome)
    if not a:
        return 0.0
    return max((_somiglia_etichetta(a, b) for b in _etichette(dominio)), default=0.0)


def _somiglia_etichetta(a: str, b: str) -> float:
    if not b or len(b) < 3:
        return 0.0'''
assert vecchio in s, "somiglia_al_nome non combacia"
s = s.replace(vecchio, nuovo)
fatte.append("somiglia")

# ---------------------------------------------------------------- 1: redirect
vecchio = '''    def da_logo(self, plat, slug, wds, wdi) -> list[str]:
        for url in BACHECA.get(plat, lambda *_: [])(slug, wds, wdi):
            try:
                r = self.cli.get(url, timeout=12, follow_redirects=True)
            except Exception:
                continue
            if r.status_code >= 400:
                continue
            fuori = []'''
nuovo = '''    def da_logo(self, plat, slug, wds, wdi) -> list[str]:
        self.redirect = None
        for url in BACHECA.get(plat, lambda *_: [])(slug, wds, wdi):
            try:
                r = self.cli.get(url, timeout=12, follow_redirects=True)
            except Exception:
                continue
            if r.status_code >= 400:
                continue
            # LA BACHECA CI HA REINDIRIZZATI sul dominio dell'azienda? E' la prova
            # piu' forte che abbiamo: un fornitore serve la bacheca di un tenant
            # solo sul CNAME di quel tenant. Si accetta solo se la pagina d'arrivo
            # porta ancora l'impronta del fornitore — e' ancora la bacheca, non un
            # parcheggio o una home qualunque.
            arrivo = dominio_di(str(r.url))
            if (arrivo and not NON_AZIENDA.search(arrivo) and plat not in arrivo
                    and plat in r.text[:300000].lower()):
                self.redirect = arrivo
            fuori = []'''
assert vecchio in s, "da_logo non combacia"
s = s.replace(vecchio, nuovo)
fatte.append("redirect")

vecchio = '''        visti = []                       # i candidati raccolti strada facendo
        for nome_fonte, prendi in fonti:
            for dom in prendi():
                visti.append((nome_fonte, dom))
                via = self.verifica(dom, patt)
                if via:
                    return (cid, dom, nome_fonte, via, 1)'''
nuovo = '''        visti = []                       # i candidati raccolti strada facendo
        for nome_fonte, prendi in fonti:
            candidati = prendi()
            # il reindirizzamento della bacheca (vedi da_logo) vale livello 1
            # senza altre richieste: la prova e' nel viaggio, non nella pagina
            if nome_fonte == "logo-ats" and getattr(self, "redirect", None):
                return (cid, radice_sito(self.redirect), "bacheca-redirect", "redirect", 1)
            for dom in candidati:
                visti.append((nome_fonte, dom))
                via = self.verifica(dom, patt)
                if via:
                    return (cid, radice_sito(dom), nome_fonte, via, 1)'''
assert vecchio in s, "il ciclo dei candidati non combacia"
s = s.replace(vecchio, nuovo)
fatte.append("ciclo")

vecchio = '''                return (cid, migliore, da, f"nome~{punteggio:.2f}", 2)'''
nuovo = '''                return (cid, radice_sito(migliore), da, f"nome~{punteggio:.2f}", 2)'''
assert vecchio in s, "il ritorno di livello 2 non combacia"
s = s.replace(vecchio, nuovo)
fatte.append("livello2")

P.write_text(s)
print("modifiche:", ", ".join(fatte))
