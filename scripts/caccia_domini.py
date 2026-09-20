"""A caccia del dominio aziendale: tre fonti in cascata, un solo giudice.

Il 18/09/2026 conoscevamo il dominio del 3% delle aziende con offerte attive, e senza
dominio non si aggancia niente: ne' il settore, ne' i dipendenti, ne' i bilanci.

Le tre fonti, misurate su uno stesso campione di 60 aziende:
  1. LOGO nella bacheca ATS       53%  (95-97% su greenhouse/smartrecruiters/teamtailor)
     Quando un'azienda configura il tenant, l'ATS le chiede il sito per il link in
     testata: leggerlo non e' indovinare, e' leggere un campo che ha compilato lei.
  2. EMAIL dentro gli annunci     20%  (dati gia' nostri, nessuna richiesta a terzi)
     Trova quello che nessun generatore trova: eliseai -> meetelise.com
  3. SearXNG                      18%  (3.178 query/ora sul N5, senza chiavi)
     Prende le sigle: otppb -> otpp.com, Forges de Zeebrugge -> fz.be

E UN SOLO GIUDICE per tutte e tre. Nessuna fonte scrive nel database di suo: il
dominio entra solo se quel sito rimanda al NOSTRO identico tenant ATS. E' una prova,
non una somiglianza di nomi — e su un dato che rivendiamo la differenza e' tutto.
SearXNG propone nell'85% dei casi e passa la prova nel 18%: senza il giudice
riempiremmo il database di domini plausibili e falsi, che e' peggio del campo vuoto.

Il giudice guarda la home e SEGUE i link che parlano di lavoro (un livello): la prova
sta spesso in fondo, /career-oportunities con il refuso, o /blog/job-family/...
"""
from __future__ import annotations
import argparse, difflib, json, os, re, sys, time, urllib.parse, urllib.request
import concurrent.futures as cf
from urllib.parse import urljoin, urlparse

import httpx
import psycopg

SEARX = os.environ.get("SEARX_URL", "http://100.119.200.7:8899/search")
MOTORI_SEARX = os.environ.get("MOTORI_SEARX", "google,yahoo")
UA = "Mozilla/5.0 (compatible; nivult/1.0; +https://nivult.com)"

# --- le bacheche, una per ATS: la radice dove sta il logo in testata ------------------
BACHECA = {
 "lever":           lambda s, a, b: [f"https://jobs.lever.co/{s}"],
 "greenhouse":      lambda s, a, b: [f"https://job-boards.greenhouse.io/{s}", f"https://boards.greenhouse.io/{s}"],
 "smartrecruiters": lambda s, a, b: [f"https://careers.smartrecruiters.com/{s}", f"https://jobs.smartrecruiters.com/{s}"],
 "personio":        lambda s, a, b: [f"https://{s}.jobs.personio.de", f"https://{s}.jobs.personio.com"],
 "ashby":           lambda s, a, b: [f"https://jobs.ashbyhq.com/{s}"],
 "workable":        lambda s, a, b: [f"https://apply.workable.com/{s}/"],
 "teamtailor":      lambda s, a, b: [f"https://{s}.teamtailor.com"],
 "recruitee":       lambda s, a, b: [f"https://{s}.recruitee.com"],
 "workday":         lambda s, a, b: ([f"https://{a}/{b}"] if a and b else []),
 # Aggiunte il 19/09/2026 dopo averle misurate: jazzhr 96%, icims 76%, breezy 72%,
 # recruiterbox 48%. bamboohr risponde ma non espone link esterni (pagina in JavaScript).
 "bamboohr":        lambda s, a, b: [f"https://{s}.bamboohr.com/careers", f"https://{s}.bamboohr.com/jobs"],
 "jazzhr":          lambda s, a, b: [f"https://{s}.applytojob.com", f"https://{s}.applytojob.com/apply"],
 "icims":           lambda s, a, b: [f"https://careers-{s}.icims.com/jobs", f"https://{s}.icims.com/jobs"],
 "breezy":          lambda s, a, b: [f"https://{s}.breezy.hr"],
 "recruiterbox":    lambda s, a, b: [f"https://{s}.recruiterbox.com", f"https://{s}.recruiterbox.com/jobs"],
 # Aggiunte il 20/09/2026 dopo averle sondate su 8 tenant ciascuna: la pagina
 # risponde sempre, e porta il sito dell'azienda per hiringthing 8/8, jobscore
 # 8/8, join 7/8, hirehive 5/8, catsone 3/8, freshteam 2/8. Rippling 0/8 (pagina
 # in JavaScript, come bamboohr): non si aggiunge, sarebbero richieste a vuoto.
 "hiringthing":     lambda s, a, b: [f"https://{s}.hiringthing.com/"],
 "jobscore":        lambda s, a, b: [f"https://careers.jobscore.com/careers/{s}"],
 "join":            lambda s, a, b: [f"https://join.com/companies/{s}"],
 "hirehive":        lambda s, a, b: [f"https://{s}.hirehive.com/"],
 "catsone":         lambda s, a, b: [f"https://{s}.catsone.com/careers"],
 "freshteam":       lambda s, a, b: [f"https://{s}.freshteam.com/jobs"],
}

# Domini che non sono mai l'azienda: gli ATS, i social, le bacheche, i servizi.
# hellowork e xing erano finiti dentro come primo link esterno di una pagina ATS.
NON_AZIENDA = re.compile(
 # gli ATS e i LORO domini di servizio. Escludere solo il nome non basta: ashby dava
 # il 100% di successi ed erano tutti cdn.ashbyprd.com, la sua rete di distribuzione.
 r"(lever\.co|greenhouse|smartrecruiters|personio|ashby|workable|teamtailor|recruitee|"
 r"myworkday|workday|icims|successfactors|taleo|jobvite|bamboohr|rippling|applytojob|breezy|"
 r"recruiterbox|jazz|smartrecruit|"
    # aggiunti il 19/09/2026: 336 bacheche Zoho scritte come se fossero aziende,
    # perche' il candidato ERA il nostro tenant e quindi rimandava a se stesso
    r"zohorecruit|catsone|vincere|cornerstone|csod|pinpointhq|jobsoid|trakstar|"
    r"homerun|freshteam|comeet|softgarden|heavenhr|traffit|taleez|hirehive|"
    r"jobscore|applicantstack|niceboard|crelate|hiringthing|eightfold|avature|"
    r"phenom|pageup|radancy|jibeapply|smartjobboard|werecruit|digitalrecruiters|"
    r"dvinci|talentsoft|careerpuck|cloud\.sap|sapsf|oraclecloud|paylocity|"
    r"paycom|paycor|ukg\.com|"
 # chi regge il sito non e' l'azienda: reti di distribuzione, hosting, costruttori di siti
 r"b-cdn\.net|bunnycdn|cloudfront|akamai|fastly|jsdelivr|unpkg|cdnjs|wpengine|wixsite|wix\.com|"
 r"squarespace|shopify|webflow|netlify|vercel|herokuapp|azurewebsites|amazonaws|zyrosite|"
 r"googleusercontent|gstatic|googleapis|cloudflare|typekit|fontawesome|jquery|bootstrapcdn|"
 # traccianti e servizi di terze parti
 r"googletagmanager|doubleclick|cookiebot|onetrust|usercentrics|hotjar|sentry|segment|"
 r"hubspot|marketo|intercom|zendesk|calendly|"
 # social, bacheche di annunci, enciclopedie
 r"linkedin|facebook|twitter|(^|\.)x\.com|instagram|youtube|youtu\.be|tiktok|glassdoor|indeed|xing|"
 r"hellowork|monster|stepstone|infojobs|welcometothejungle|totaljobs|reed\.co|seek\.com|"
 r"wikipedia|wikidata|europa\.eu|crunchbase|bloomberg|zoominfo|apollo\.io|pitchbook|dnb\.com|"
 r"google|apple\.com|microsoft|adobe|mozilla|w3\.org|schema\.org|gravatar)", re.I)


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

PAROLE_LAVORO = re.compile(
 r"(career|careers|job|jobs|vacanc|lavora|carriere|opportunit|karriere|stellen|jobb|"
 r"emploi|rejoign|empleo|trabaja|vagas|werken|vacature|about|chi-siamo|team|unternehmen)", re.I)

FORME = (r"\b(spa|srl|gmbh|ag|sa|se|nv|bv|ltd|limited|inc|llc|plc|oy|ab|as|aps|sas|sarl|"
         r"co|corp|corporation|company|group|holding|holdings)\b")
TLD_PAESE = {"DE":"de","FR":"fr","IT":"it","ES":"es","NL":"nl","GB":"co.uk","SE":"se","NO":"no",
             "DK":"dk","FI":"fi","PL":"pl","AT":"at","CH":"ch","BE":"be","PT":"pt","IE":"ie",
             "CZ":"cz","IN":"in","CA":"ca","AU":"com.au","BR":"com.br"}

def norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    return re.sub(r"\s+", " ", re.sub(FORME, " ", s)).strip()

def dominio_di(u: str) -> str:
    try:
        h = (urlparse(u).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""

def impronta_ats(platform: str, slug: str) -> re.Pattern:
    """Come apparirebbe, dentro una pagina, un rimando al NOSTRO tenant."""
    s = re.escape(slug)
    return re.compile({
        "workday":         rf"{s}\.wd\d*\.myworkdayjobs|myworkdayjobs\.com/[^\"'\s]*{s}",
        "greenhouse":      rf"greenhouse\.io/{s}\b",
        "lever":           rf"lever\.co/{s}\b",
        "smartrecruiters": rf"smartrecruiters\.com/{s}\b",
        "personio":        rf"{s}\.jobs\.personio",
        "ashby":           rf"ashbyhq\.com/{s}\b",
        "workable":        rf"{s}\.workable\.com|apply\.workable\.com/{s}",
        "teamtailor":      rf"{s}\.teamtailor\.com",
        "recruitee":       rf"{s}\.recruitee\.com",
        "hiringthing":     rf"{s}\.hiringthing\.com",
        "jobscore":        rf"jobscore\.com/careers/{s}\b",
        "join":            rf"join\.com/companies/{s}\b",
        "hirehive":        rf"{s}\.hirehive\.com",
        "catsone":         rf"{s}\.catsone\.com",
        "freshteam":       rf"{s}\.freshteam\.com",
    }.get(platform, rf"\b{s}\b"), re.I)



# --- livello 2: la corrispondenza fra nome e dominio ------------------------
# Calibrata il 19/09/2026 su 1.003 coppie gia' provate dal giudice: a 0,80 ci sta
# dentro l'83,5% delle coppie vere e lo 0,2% di quelle accoppiate a caso.
SOGLIA_NOME = float(os.environ.get("SOGLIA_NOME", "0.80"))
_FORME = re.compile(
    r"\b(gmbh|ag|inc|llc|ltd|limited|corp|corporation|co|sa|srl|spa|bv|nv|plc|"
    r"group|gruppe|holding|holdings|international|emea|usa|careers?|jobs?)\b", re.I)


def _nome_nudo(n: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _FORME.sub(" ", (n or "").lower()))


# Prefissi che dicono «pagina carriere», non «azienda»: careers.axpo.com e'
# axpo.com. Si tolgono solo se quel che resta e' ancora un dominio (ha un punto).
PREFISSI_CARRIERE = re.compile(
    r"^(careers?|jobs?|jobb|karriere|karriera|karriere-portal|work|talent|talents|join|"
    r"recruit(ing|ment)?|hr|empleo|emploi|lavoro|vacatures|vacancies|stellen|"
    r"stellenangebote|bewerbung|candidat|candidature|carriere|carriere)\.", re.I)


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
    return e[-1] if e else ""


def somiglia_al_nome(nome: str, dominio: str) -> float:
    """Quanto il dominio dice il nome dell'azienda. 1.0 se uno contiene l'altro.
    Si prova ogni etichetta dell'host e vale la migliore."""
    a = _nome_nudo(nome)
    if not a:
        return 0.0
    return max((_somiglia_etichetta(a, b) for b in _etichette(dominio)), default=0.0)


def _somiglia_etichetta(a: str, b: str) -> float:
    if not b or len(b) < 3:
        return 0.0
    if a == b:
        return 1.0
    # Un prefisso vale solo se copre quasi tutto: «bcm» dentro «bcmcosmetique»
    # sono tre lettere su tredici, e infatti bcm.edu e' il Baylor College of
    # Medicine, non una ditta di cosmetici (19/09/2026).
    if a.startswith(b) or b.startswith(a):
        comune = min(len(a), len(b))
        copertura = comune / max(len(a), len(b))
        # sei caratteri in comune sono una parola; tre sono una sigla, e le sigle
        # collidono («bcm» sta in «bcmcosmetique» ma bcm.edu e' un'universita')
        if comune >= 6 or copertura >= 0.70:
            return 1.0
        return copertura
    return difflib.SequenceMatcher(None, a, b).ratio()



def esiste(dominio: str) -> bool:
    """Il dominio risolve? Una domanda al DNS, non una richiesta HTTP.

    Serve perche' un candidato plausibile puo' semplicemente non esistere, e il
    DNS lo dice in millisecondi senza che Cloudflare possa rispondere 403.
    """
    import socket
    for h in (dominio, "www." + dominio):
        try:
            socket.getaddrinfo(h, None)
            return True
        except OSError:
            continue
    return False



# Suffissi che un datore privato non usa: se il candidato finisce cosi' non e'
# l'azienda che cerchiamo (anap.gov.ro per una societa' francese, bcm.edu per una
# ditta di cosmetici). Restano ammessi se l'azienda stessa e' un ente pubblico o
# una scuola, e lo si capisce dal suo nome.
TLD_PUBBLICI = (".gov", ".gov.", ".edu", ".edu.", ".mil", ".int")
PAROLE_PUBBLICHE = re.compile(
    r"\b(comune|ville|citta|city|county|council|ministe|govern|region|province|"
    r"university|universit|college|school|ecole|scuola|hochschule|academy|"
    r"hospital|ospedale|krankenhaus|chu|nhs|agency|agence|agenzia)\b", re.I)


def tld_plausibile(nome: str, dominio: str, paese: str | None) -> bool:
    """Il suffisso del dominio e' compatibile con questa azienda?"""
    d = (dominio or "").lower()
    if any(d.endswith(t.rstrip(".")) or (t + "") in d for t in TLD_PUBBLICI):
        return bool(PAROLE_PUBBLICHE.search(nome or ""))
    return True


class Cacciatore:
    def __init__(self, cli: httpx.Client):
        self.cli = cli

    # --- IL GIUDICE ------------------------------------------------------------------
    def verifica(self, dom: str, patt: re.Pattern, salti: int = 5) -> str | None:
        """Quel sito rimanda al nostro tenant? Guarda la home, poi segue i link
        che parlano di lavoro. Ritorna la pagina della prova, o None."""
        try:
            r = self.cli.get(f"https://{dom}", timeout=10, follow_redirects=True)
        except Exception:
            return None
        if r.status_code >= 400:
            return None
        html = r.text[:400000]
        if patt.search(html):
            return "/"
        base, visti = str(r.url), 0
        for m in re.finditer(r'<a[^>]+href=["\']([^"\'>\s]+)["\'][^>]*>(.{0,120}?)</a>', html, re.I | re.S):
            if visti >= salti:
                break
            href, testo = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
            if not (PAROLE_LAVORO.search(href) or PAROLE_LAVORO.search(testo)):
                continue
            u = urljoin(base, href)
            if dom not in (urlparse(u).hostname or ""):
                continue                      # non si esce dal sito dell'azienda
            visti += 1
            try:
                rr = self.cli.get(u, timeout=8, follow_redirects=True)
            except Exception:
                continue
            if rr.status_code < 400 and patt.search(rr.text[:400000]):
                return urlparse(u).path or "/"
        return None

    # --- LE TRE FONTI ----------------------------------------------------------------
    def da_logo(self, plat, slug, wds, wdi) -> list[str]:
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
            fuori = []
            for m in re.finditer(r'href=["\'](https?://[^"\'>\s]+)', r.text[:300000], re.I):
                d = dominio_di(m.group(1))
                if d and d.count(".") <= 3 and not NON_AZIENDA.search(d) and d not in fuori:
                    fuori.append(d)
            if fuori:
                return fuori[:4]
        return []

    def da_searx(self, nome, paese) -> list[str]:
        q = urllib.parse.quote(f"{nome} {paese} official website careers".strip()[:120])
        # Solo i motori che rispondono davvero da questo IP (misurato il 19/09/2026):
        # gli altri dell'insieme predefinito danno zero e fanno aspettare il timeout.
        try:
            rq = urllib.request.Request(f"{SEARX}?q={q}&format=json&engines={MOTORI_SEARX}",
                                        headers={"User-Agent": "nivult/1.0"})
            # 12 secondi, non 40: quando i motori rispondono lo fanno in meno di
            # cinque; quando scadono, aspettare 40 non li fa arrivare. Sui tenant
            # dove tutto fallisce questo timeout ERA la velocita' del cacciatore:
            # 161 all'ora contro 1.066 (20/09/2026).
            with urllib.request.urlopen(rq, timeout=12) as r:
                d = json.load(r)
        except Exception:
            return []
        # Una risposta 200 con zero risultati NON e' un successo: vuol dire che i
        # motori ci hanno bloccati. Si conta, cosi' si vede nei numeri del demone
        # invece di sparire in silenzio come e' successo per 691 aziende.
        if not d.get("results"):
            self.searx_vuote = getattr(self, "searx_vuote", 0) + 1
        fuori = []
        for x in d.get("results", [])[:12]:
            h = dominio_di(x.get("url", ""))
            if h and "." in h and not NON_AZIENDA.search(h) and h not in fuori:
                fuori.append(h)
        return fuori[:5]

    def da_nome(self, nome, slug, paese) -> list[str]:
        basi = set()
        for s in (norm(nome), norm(slug).replace("-", " ")):
            if not s:
                continue
            basi.add(s.replace(" ", ""))
            basi.add(s.replace(" ", "-"))
        basi = {b for b in basi if 3 <= len(b) <= 30}
        tld = ["com"]
        t = TLD_PAESE.get(paese or "")
        if t and t != "com":
            tld.insert(0, t)
        return [f"{b}.{x}" for b in sorted(basi) for x in tld][:4]

    def caccia(self, riga) -> tuple:
        """Le fonti in cascata, dalla piu' ricca alla piu' incerta. Si ferma alla prima
        che supera la prova: le altre non servono e costano."""
        cid, plat, slug, nome, paese, wds, wdi, email_dom = riga
        patt = impronta_ats(plat, slug)
        fonti = [("logo-ats", lambda: self.da_logo(plat, slug, wds, wdi))]
        if email_dom:
            fonti.append(("email-annuncio", lambda: [email_dom]))
        fonti.append(("nome-generato", lambda: self.da_nome(nome, slug, paese)))
        fonti.append(("searxng", lambda: self.da_searx(nome or slug, paese or "")))
        visti = []                       # i candidati raccolti strada facendo
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
                    return (cid, radice_sito(dom), nome_fonte, via, 1)
        # Livello 1 fallito. Fra i candidati che abbiamo gia' in mano — nessuna
        # richiesta in piu' — ce n'e' uno che dice il nome dell'azienda?
        # Il nome dell'azienda, non lo slug del tenant: lo slug e' spesso una
        # storpiatura («Cvshealth», «Colliersinternationalemea») e farebbe
        # passare corrispondenze che non sono tali.
        if nome:
            migliore, punteggio, da = None, 0.0, None
            for nome_fonte, dom in visti:
                # `nome-generato` COSTRUISCE il dominio dal nome: confermarlo col
                # nome sarebbe circolare. Valgono solo le fonti indipendenti.
                if nome_fonte == "nome-generato":
                    continue
                v = somiglia_al_nome(nome, dom)
                if v > punteggio:
                    migliore, punteggio, da = dom, v, nome_fonte
            # e deve almeno esistere: il DNS costa nulla e non lo blocca nessuno
            if (migliore and punteggio >= SOGLIA_NOME
                    and tld_plausibile(nome, migliore, paese) and esiste(migliore)):
                return (cid, radice_sito(migliore), da, f"nome~{punteggio:.2f}", 2)
        return (cid, None, None, None, None)


SQL_CODA = """
UPDATE ats_companies c SET dominio_cercato_at = now()
 WHERE c.id IN (
   SELECT k.id FROM ats_companies k
    WHERE k.is_active AND k.site_domain IS NULL AND k.job_count > 0
      AND (k.dominio_cercato_at IS NULL OR k.dominio_cercato_at < now() - interval '30 days')
    -- Prima le piattaforme che rendono, poi i datori piu' grossi. Chi non ha
    -- ancora una resa affidabile prende la media generale.
    -- Sottoquery e non LEFT JOIN: con l'outer join Postgres rifiuta il FOR UPDATE
    -- («cannot be applied to the nullable side of an outer join»), e la
    -- prenotazione con SKIP LOCKED non e' negoziabile.
    ORDER BY coalesce((SELECT r.resa FROM caccia_resa r
                        WHERE r.platform_id = k.platform_id), %s) DESC,
             k.job_count DESC
    LIMIT %s FOR UPDATE SKIP LOCKED)
RETURNING c.id, c.platform_id, c.slug, coalesce(c.company_name,''), coalesce(c.country,''),
          coalesce(c.wd_server,''), coalesce(c.wd_instance,'')
"""

# La resa per piattaforma, ricalcolata dai dati. Serve all'ordine di pesca: non
# ha senso spendere il tempo su una piattaforma che rende il 3% mentre ce n'e'
# una al 52% ancora tutta da provare. Sotto le 20 prove la resa non e'
# affidabile e si lascia NULL, cosi' chi pesca usa la media generale.
SQL_RESA = """
INSERT INTO caccia_resa (platform_id, provate, trovate, resa, aggiornato_at)
SELECT k.platform_id,
       count(*) FILTER (WHERE k.dominio_cercato_at IS NOT NULL),
       count(*) FILTER (WHERE k.dominio_cercato_at IS NOT NULL AND k.site_domain IS NOT NULL),
       CASE WHEN count(*) FILTER (WHERE k.dominio_cercato_at IS NOT NULL) >= 20
            THEN count(*) FILTER (WHERE k.dominio_cercato_at IS NOT NULL AND k.site_domain IS NOT NULL)::real
                 / count(*) FILTER (WHERE k.dominio_cercato_at IS NOT NULL)
       END,
       now()
  FROM ats_companies k
 WHERE k.is_active AND k.job_count > 0
 GROUP BY 1
ON CONFLICT (platform_id) DO UPDATE SET
  provate = EXCLUDED.provate, trovate = EXCLUDED.trovate,
  resa = EXCLUDED.resa, aggiornato_at = now()
"""

MEDIA = """
SELECT coalesce(sum(trovate)::real / nullif(sum(provate), 0), 0.20) FROM caccia_resa
"""

SQL_EMAIL = """
SELECT lower((regexp_matches(j.raw::text,
       '[A-Za-z0-9._%%+-]+@([A-Za-z0-9.-]+\\.[A-Za-z]{2,})'))[1])
  FROM ats_jobs j
 WHERE j.platform_id = %s AND j.slug = %s AND j.expired_at IS NULL
   AND j.raw::text ~ '[A-Za-z0-9._%%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}'
 LIMIT 5
"""

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=200)
    ap.add_argument("--par", type=int, default=6)
    ap.add_argument("--continuo", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cli = httpx.Client(headers={"User-Agent": UA}, verify=False, follow_redirects=True)
    cac = Cacciatore(cli)
    st = {"viste": 0, "trovati": 0}
    per_fonte: dict[str, int] = {}
    t0 = time.time()

    with psycopg.connect(os.environ["ATS_DATABASE_URL"], autocommit=True) as c:
        while True:
            # la resa si ricalcola a ogni giro: e' una lettura sola e tiene
            # l'ordine di pesca aggiornato da se'
            c.execute(SQL_RESA)
            media = c.execute(MEDIA).fetchone()[0] or 0.20
            righe = c.execute(SQL_CODA, (media, a.limite)).fetchall()
            if not righe:
                print("caccia: niente da fare", flush=True)
                if not a.continuo:
                    break
                time.sleep(300); continue
            # l'email si prende in blocco, una query per azienda ma senza scaricare nulla
            lavoro = []
            for cid, plat, slug, nome, paese, wds, wdi in righe:
                dom_email = ""
                for (d,) in c.execute(SQL_EMAIL, (plat, slug)).fetchall():
                    if d and not NON_AZIENDA.search(d) and not re.search(
                            r"(gmail|googlemail|hotmail|outlook|yahoo|icloud|libero|web\.de|gmx|protonmail|aol)\.", d):
                        dom_email = d
                        break
                lavoro.append((cid, plat, slug, nome, paese, wds, wdi, dom_email))

            with cf.ThreadPoolExecutor(a.par) as ex:
                esiti = list(ex.map(cac.caccia, lavoro))
            st["viste"] += len(esiti)
            scritte = [(d, f, liv, cid) for cid, d, f, _, liv in esiti if d]
            for _, _, f, _, liv in [e for e in esiti if e[1]]:
                per_fonte[f] = per_fonte.get(f, 0) + 1
                st[f"livello{liv}"] = st.get(f"livello{liv}", 0) + 1
            if scritte and not a.dry_run:
                with c.cursor() as cur:
                    cur.executemany(
                        "UPDATE ats_companies SET site_domain = %s, site_domain_source = %s, "
                        "site_domain_livello = %s, site_checked_at = now() "
                        "WHERE id = %s AND site_domain IS NULL", scritte)
            st["trovati"] += len(scritte)
            dt = max(time.time() - t0, 1e-6)
            print(f"caccia: viste {st['viste']} | trovati {st['trovati']} "
                  f"({100*st['trovati']/max(st['viste'],1):.0f}%) | {per_fonte} | "
                  f"liv1 {st.get('livello1', 0)} liv2 {st.get('livello2', 0)} | "
                  f"{int(3600*st['trovati']/dt)}/ora", flush=True)
            if not a.continuo:
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
