"""Radar Indeed: i DATORI che assumono adesso, presi dal link «candidati
sul sito dell'azienda». Nient'altro.

Cosa entra e cosa no (decisione di Giuseppe, 13/09/2026):
  - da Indeed si prende SOLO il link di candidatura diretta, cioe' l'indirizzo
    del datore o del suo tenant ATS. Ne' titolo, ne' testo, ne' salario, ne'
    la pagina Indeed: il contenuto lo leggiamo dal datore, che vuole essere
    letto, con gli adapter e la scoperta jsonld di sempre.
  - un tenant ATS riconosciuto dagli `url_pattern` del registro entra
    direttamente in ats_companies (discovered_from='indeed_radar');
  - un sito aziendale entra in company_domains con source='indeed_radar',
    stato 'pending', e se lo avevamo gia' bollato no_ats/no_careers/dead
    torna in coda: Indeed dice che sta assumendo ORA, quindi il verdetto
    vecchio e' da rifare. Il detector gli da' la precedenza.
  - i link verso bacheche e aggregatori (Indeed Apply compreso) si scartano.

Come funziona: python-jobspy interroga l'API GraphQL dell'app Indeed (la
stessa che usa l'app iPhone, con la sua chiave). Non e' un'API pubblica: il
robots.txt di Indeed vieta /graphql e i Termini vietano l'accesso automatico.
E' la posizione piu' leggera possibile — un puntatore, nessun contenuto — ma
non e' «pubblica al 100%», e puo' smettere di funzionare da un giorno
all'altro (la parte Google della stessa libreria e' morta a settembre 2025).
Gira da Hetzner, MAI dall'IP di casa (regola del 13/09/2026).

Misurato il 13/09/2026 su 576 offerte (IT+DE, 6 ricerche): 79% con link
diretto, 154 siti aziendali distinti di cui 64 (42%) mai censiti; dei 90
censiti, 33 erano `no_ats`, 17 `pending`, solo 16 gia' in raccolta.

Le ricerche sono mestieri generici x citta' per paese; il segnalibro in
/var/lib/nivult/radar_indeed.json fa ruotare la lista fra un giro e l'altro,
cosi' in una settimana si copre tutto e `--ore 168` prende solo le offerte
di quella settimana.

    python -m nivult.ats.radar_indeed --giro --limite 250
    python -m nivult.ats.radar_indeed --prova magazziniere "Torino, Italia" IT
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
import warnings
from urllib.parse import urlsplit

import psycopg

from nivult.ats.detector import _pattern_registro
from nivult.ats.jsonld import _dsn
from nivult.ingestion.urls import JOB_BOARD_DOMAINS, NATIONAL_AGENCY_DOMAINS

log = logging.getLogger("nivult.ats.radar_indeed")

SEGNALIBRO = "/var/lib/nivult/radar_indeed.json"
SORGENTE = "indeed_radar"

# Bacheche e aggregatori oltre a quelli di ingestion/urls.py: un link diretto
# che porta qui NON e' un datore. La lista non e' completa, per questo i
# domini con parole da bacheca nel nome vengono marcati `sospetto` e non
# entrano finche' qualcuno non li guarda (`--sospetti`).
BACHECHE = frozenset({
    "indeed.com", "linkedin.com", "glassdoor.com", "glassdoor.it", "glassdoor.de", "glassdoor.fr",
    "monster.com", "monster.it", "monster.de", "monster.fr", "stepstone.de", "stepstone.at", "stepstone.nl",
    "stepstone.be", "infojobs.it", "infojobs.net", "subito.it", "bakeca.it", "kijiji.it", "xing.com",
    "kununu.com", "jobrapido.com", "jooble.org", "adzuna.com", "adzuna.it", "adzuna.de", "adzuna.fr",
    "talent.com", "neuvoo.com", "careerjet.com", "careerjet.it", "trovit.com", "trovit.it", "bebee.com",
    "jobtome.com", "welcometothejungle.com", "hellowork.com", "apec.fr", "jobijoba.com", "meteojob.com",
    "keljob.com", "regionsjob.com", "cadremploi.fr", "jobs.ch", "jobup.ch", "jobscout24.ch", "karriere.at",
    "willhaben.at", "jobs.de", "meinestadt.de", "kimeta.de", "joblift.de", "jobware.de", "stellenanzeigen.de",
    "finn.no", "jobbnorge.no", "jobindex.dk", "ofir.dk", "oikotie.fi", "duunitori.fi", "pracuj.pl", "olx.pl",
    "olx.pt", "infoempleo.com", "tecnoempleo.com", "jobat.be", "ictjob.be", "nationalevacaturebank.nl",
    "werkzoeken.nl", "jobbird.com", "reed.co.uk", "totaljobs.com", "cv-library.co.uk", "jobsite.co.uk",
    "irishjobs.ie", "jobs.ie", "ziprecruiter.com", "simplyhired.com", "dice.com", "lensa.com", "whatjobs.com",
    "jobisjob.com", "mitula.com", "jobbsafari.se", "blocket.se", "arbetsformedlingen.se", "manpower.com",
    "google.com", "facebook.com", "instagram.com", "youtube.com", "wa.me", "whatsapp.com",
}) | JOB_BOARD_DOMAINS | NATIONAL_AGENCY_DOMAINS

_RX_SOSPETTO = re.compile(r"(job|lavor|emploi|stellen|empleo|vacatur|trabaj|praca|vagas|karrier|carrier|career|recruit|staffing|offerte|annunci)", re.I)

# Host di piattaforme ATS / software di recruiting che i pattern del registro
# non coprono (o coprono solo in una forma): un link che porta qui NON e' il
# sito del datore, e' il suo fornitore. Senza tenant leggibile si registra
# come `ats_ignoto` — materiale per un pattern o un adapter nuovo — e NON
# entra in company_domains. Misurato il 13/09/2026 sul primo giro: nove di
# questi erano entrati come «siti aziendali» (intervieweb, allibo, altamira,
# talkpush, paradox, xtramile, velorahr, join-us, apply.workable corto).
_HOST_ATS_MANUALI = frozenset({
    "intervieweb.it", "inrecruiting.it", "ncoreplat.com", "allibo.com", "altamiraweb.com", "talkpush.com",
    "paradox.ai", "xtramile.io", "velorahr.com", "join-us.it", "workable.com", "myworkdayjobs.com",
    "successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu", "oraclecloud.com", "csod.com",
    "taleo.net", "icims.com", "brassring.com", "jobvite.com", "lever.co", "greenhouse.io", "ashbyhq.com",
    "breezy.hr", "recruitee.com", "teamtailor.com", "personio.de", "personio.com", "softgarden.io",
    "softgarden.de", "softgarden.com", "hibob.com", "ultipro.com", "ukg.net", "paylocity.com", "adp.com",
    "talentlyft.com", "zohorecruit.com", "zohorecruit.eu", "jobteaser.com", "welcomekit.co", "flatchr.io",
    "digitalrecruiters.com", "beetween.com", "talentsoft.com", "cornerstoneondemand.com", "phenom.com",
    "phenompeople.com", "eightfold.ai", "avature.net", "pageuppeople.com", "rippling.com", "rippling-ats.com",
    "bamboohr.com", "applicantpro.com", "jobscore.com", "jazzhr.com", "applytojob.com", "jobadder.com",
    "catsone.com", "homerun.co", "join.com", "onlyfy.jobs", "prescreen.io", "concludis.de", "rexx-systems.com",
    "guidecom.de", "umantis.com", "hrworks.de", "kenjo.io", "recruitis.io", "jobylon.com", "varbi.com",
    "reachmee.com", "talentech.com", "hr-on.com", "emply.com", "dvinci-easy.com", "dvinci-hr.com",
    "bewerbermanagement.net", "connectoor.de", "applicantpool.com", "performahrm.com", "taleez.com",
    "traffit.com", "vincere.io", "werecruit.io", "eploy.net", "hirehive.com", "pinpointhq.com", "occupop.com",
    "manatal.com", "freshteam.com", "hirevue.com", "comeet.co", "recruiterbox.com", "recruiterflow.com",
    "gohire.io", "heavenhr.com", "jobsoid.com", "crelate.com", "cvwarehouse.com", "clearcompany.com",
    "hiringthing.com", "hireology.com", "jobdiva.com", "loxo.co", "niceboard.co", "recooty.com",
    "skillfuel.com", "beamery.com", "fountain.com", "ceipal.com", "applicantstack.com",
})

_PATTERN_DB: list[tuple[str, str]] = []   # (platform_id, url_pattern) dal db, caricati a inizio giro
_HOST_ATS: set[str] = set()
_INFRA = {"www", "cdn", "static", "api", "assets", "app", "jobs", "careers", "career", "apply", "boards",
          "help", "support", "tt", "pp-cdn", "j", "go", "my"}


def _host_da_pattern(pattern: str) -> str | None:
    """Il dominio letterale dentro un url_pattern: `jobs\\.smartrecruiters\\.com/([^/]+)`
    -> smartrecruiters.com. Grezzo apposta: serve solo a dire «e' un ATS»."""
    p = pattern.replace("\\.", ".")
    cand = [m for m in re.findall(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}", p) if "-]" not in m]
    if not cand:
        return None
    return dominio_base(max(cand, key=len))


def carica_pattern(db=None) -> None:
    """Gli host ATS: dal registro statico, dai pattern del db e dalla lista a mano."""
    from nivult.ats.registry import REGISTRY
    _HOST_ATS.clear(); _HOST_ATS.update(_HOST_ATS_MANUALI)
    for p in REGISTRY:
        h = _host_da_pattern(p.get("url_pattern") or "")
        if h:
            _HOST_ATS.add(h)
    _PATTERN_DB.clear()
    if db is not None:
        for pid, pat in db.execute("SELECT id, url_pattern FROM ats_platforms "
                                   "WHERE url_pattern IS NOT NULL AND url_pattern <> '' AND is_active"):
            _PATTERN_DB.append((pid, pat))
            h = _host_da_pattern(pat)
            if h:
                _HOST_ATS.add(h)


def _tenant_db(url: str) -> tuple[str, str] | None:
    for pid, pat in _PATTERN_DB:
        try:
            m = re.search(pat, url)
        except re.error:
            continue
        if m and m.lastindex and m.group(1):
            slug = m.group(1).lower()
            if "." not in slug and len(slug) > 2 and slug not in _INFRA and "@" not in slug:
                return pid, slug
    return None

# Suffissi pubblici a due livelli piu' comuni: careers.acme.co.uk -> acme.co.uk
_SUFFISSI_2 = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk", "com.au", "net.au", "org.au",
    "com.br", "com.mx", "com.ar", "com.tr", "co.za", "co.jp", "co.in", "co.nz", "com.sg", "com.hk",
    "com.pl", "com.pt", "com.es", "com.it", "co.it", "com.de", "co.at", "or.at", "com.ua", "com.ru",
})

# Mestieri generici x citta' per paese: larghi apposta, perche' il radar
# cerca DATORI, non offerte. (nome per jobspy, ISO2, mestieri, citta')
PAESI: dict[str, tuple[str, list[str], list[str]]] = {
    "IT": ("italy", ["operaio", "impiegato", "commerciale", "magazziniere", "infermiere", "sviluppatore", "ingegnere",
                     "addetto vendite", "contabile", "cuoco", "autista", "receptionist"],
           ["Milano", "Roma", "Torino", "Napoli", "Bologna", "Firenze", "Padova", "Bari"]),
    "DE": ("germany", ["Mitarbeiter", "Sachbearbeiter", "Vertrieb", "Lagermitarbeiter", "Pflegekraft", "Softwareentwickler",
                       "Ingenieur", "Verkäufer", "Buchhalter", "Koch", "Fahrer", "Empfang"],
           ["Berlin", "München", "Hamburg", "Köln", "Frankfurt am Main", "Stuttgart", "Düsseldorf", "Leipzig"]),
    "FR": ("france", ["employé", "assistant", "commercial", "préparateur de commandes", "infirmier", "développeur",
                      "ingénieur", "vendeur", "comptable", "cuisinier", "chauffeur", "réceptionniste"],
           ["Paris", "Lyon", "Marseille", "Toulouse", "Lille", "Bordeaux", "Nantes", "Strasbourg"]),
    "ES": ("spain", ["operario", "administrativo", "comercial", "mozo de almacén", "enfermero", "desarrollador",
                     "ingeniero", "dependiente", "contable", "cocinero", "conductor", "recepcionista"],
           ["Madrid", "Barcelona", "Valencia", "Sevilla", "Bilbao", "Málaga", "Zaragoza", "Alicante"]),
    "NL": ("netherlands", ["medewerker", "administratief medewerker", "sales", "magazijnmedewerker", "verpleegkundige",
                           "software developer", "engineer", "verkoper", "boekhouder", "kok", "chauffeur", "receptionist"],
           ["Amsterdam", "Rotterdam", "Utrecht", "Eindhoven", "Den Haag", "Groningen", "Tilburg", "Breda"]),
    "BE": ("belgium", ["medewerker", "employé", "commercial", "magazijnier", "infirmier", "développeur", "ingénieur",
                       "vendeur", "comptable", "chauffeur", "boekhouder", "verpleegkundige"],
           ["Bruxelles", "Antwerpen", "Gent", "Liège", "Charleroi", "Leuven", "Brugge", "Namur"]),
    "AT": ("austria", ["Mitarbeiter", "Sachbearbeiter", "Vertrieb", "Lagermitarbeiter", "Pflegekraft", "Softwareentwickler",
                       "Ingenieur", "Verkäufer", "Buchhalter", "Koch", "Fahrer", "Empfang"],
           ["Wien", "Graz", "Linz", "Salzburg", "Innsbruck", "Klagenfurt", "Villach", "Wels"]),
    "CH": ("switzerland", ["Mitarbeiter", "Sachbearbeiter", "Vertrieb", "Lagermitarbeiter", "Pflegefachperson",
                           "Softwareentwickler", "Ingenieur", "Verkäufer", "Buchhalter", "employé", "commercial", "infirmier"],
           ["Zürich", "Genève", "Basel", "Bern", "Lausanne", "Winterthur", "Luzern", "Lugano"]),
    "PT": ("portugal", ["operador", "administrativo", "comercial", "operador de armazém", "enfermeiro", "programador",
                        "engenheiro", "vendedor", "contabilista", "cozinheiro", "motorista", "rececionista"],
           ["Lisboa", "Porto", "Braga", "Coimbra", "Faro", "Setúbal", "Aveiro", "Funchal"]),
    "PL": ("poland", ["pracownik", "specjalista", "handlowiec", "magazynier", "pielęgniarka", "programista", "inżynier",
                      "sprzedawca", "księgowa", "kucharz", "kierowca", "recepcjonista"],
           ["Warszawa", "Kraków", "Wrocław", "Poznań", "Gdańsk", "Łódź", "Katowice", "Szczecin"]),
    "SE": ("sweden", ["medarbetare", "administratör", "säljare", "lagerarbetare", "sjuksköterska", "utvecklare", "ingenjör",
                      "butikssäljare", "redovisningsekonom", "kock", "chaufför", "receptionist"],
           ["Stockholm", "Göteborg", "Malmö", "Uppsala", "Västerås", "Örebro", "Linköping", "Helsingborg"]),
    "DK": ("denmark", ["medarbejder", "kontorassistent", "sælger", "lagermedarbejder", "sygeplejerske", "udvikler", "ingeniør",
                       "butiksassistent", "bogholder", "kok", "chauffør", "receptionist"],
           ["København", "Aarhus", "Odense", "Aalborg", "Esbjerg", "Randers", "Kolding", "Vejle"]),
    "NO": ("norway", ["medarbeider", "saksbehandler", "selger", "lagermedarbeider", "sykepleier", "utvikler", "ingeniør",
                      "butikkmedarbeider", "regnskapsfører", "kokk", "sjåfør", "resepsjonist"],
           ["Oslo", "Bergen", "Trondheim", "Stavanger", "Drammen", "Kristiansand", "Tromsø", "Fredrikstad"]),
    "FI": ("finland", ["työntekijä", "assistentti", "myyjä", "varastotyöntekijä", "sairaanhoitaja", "ohjelmistokehittäjä",
                       "insinööri", "myymälätyöntekijä", "kirjanpitäjä", "kokki", "kuljettaja", "vastaanottovirkailija"],
           ["Helsinki", "Espoo", "Tampere", "Vantaa", "Oulu", "Turku", "Jyväskylä", "Lahti"]),
    "IE": ("ireland", ["operative", "administrator", "sales executive", "warehouse operative", "nurse", "software developer",
                       "engineer", "sales assistant", "accountant", "chef", "driver", "receptionist"],
           ["Dublin", "Cork", "Galway", "Limerick", "Waterford", "Drogheda", "Dundalk", "Kilkenny"]),
    "GB": ("uk", ["operative", "administrator", "sales executive", "warehouse operative", "nurse", "software developer",
                  "engineer", "sales assistant", "accountant", "chef", "driver", "receptionist"],
           ["London", "Manchester", "Birmingham", "Leeds", "Glasgow", "Bristol", "Edinburgh", "Liverpool"]),
}


def ricerche(paese: str | None = None) -> list[tuple[str, str, str]]:
    """Tutte le (mestiere, citta', ISO2), in ordine stabile per la rotazione."""
    out = []
    for iso, (_, mestieri, citta) in PAESI.items():
        if paese and iso != paese:
            continue
        for c in citta:
            for m in mestieri:
                out.append((m, c, iso))
    return out


def dominio_base(host: str) -> str:
    host = (host or "").lower().strip(".")
    host = host[4:] if host.startswith("www.") else host
    parti = host.split(".")
    if len(parti) >= 3 and ".".join(parti[-2:]) in _SUFFISSI_2:
        return ".".join(parti[-3:])
    return ".".join(parti[-2:]) if len(parti) >= 2 else host


def _in(host: str, insieme) -> bool:
    return any(host == d or host.endswith("." + d) for d in insieme)


def classifica_link(url: str) -> dict | None:
    """Da un link di candidatura: {'tipo': 'tenant'|'sito'|'sospetto', ...} o
    None se e' una bacheca / non e' un URL."""
    if not url or not url.startswith("http"):
        return None
    host = (urlsplit(url).hostname or "").lower()
    if not host or _in(host, BACHECHE):
        return None
    hit = _pattern_registro(url) or _tenant_db(url)
    if hit:
        return {"tipo": "tenant", "platform_id": hit[0], "slug": hit[1], "host": host}
    base = dominio_base(host)
    if not base or "." not in base:
        return None
    if not _HOST_ATS:
        carica_pattern()
    if _in(host, _HOST_ATS):
        return {"tipo": "ats_ignoto", "dominio": base, "host": host}
    if _RX_SOSPETTO.search(base.split(".")[0]) and not base.endswith(".jobs"):
        return {"tipo": "sospetto", "dominio": base, "host": host}
    return {"tipo": "sito", "dominio": base, "host": host}


def _leggi_segnalibro() -> dict:
    try:
        with open(SEGNALIBRO) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _scrivi_segnalibro(seg: dict) -> None:
    os.makedirs(os.path.dirname(SEGNALIBRO), exist_ok=True)
    tmp = SEGNALIBRO + ".tmp"
    with open(tmp, "w") as f:
        json.dump(seg, f, indent=1)
    os.replace(tmp, SEGNALIBRO)


def cerca(mestiere: str, citta: str, iso: str, ore: int = 168, quanti: int = 100) -> list[dict]:
    """Una ricerca Indeed: ritorna i soli link di candidatura con il nome
    del datore. Il resto della risposta si butta qui, subito."""
    warnings.filterwarnings("ignore")
    from jobspy import scrape_jobs  # importato qui: pandas e' pesante e serve solo al radar
    nome_jobspy = PAESI[iso][0]
    df = scrape_jobs(site_name=["indeed"], search_term=mestiere, location=f"{citta}",
                     results_wanted=quanti, country_indeed=nome_jobspy, hours_old=ore, verbose=0)
    out = []
    if df is None or len(df) == 0:
        return out
    for _, r in df.iterrows():
        u = r.get("job_url_direct")
        if not isinstance(u, str) or not u.startswith("http"):
            continue
        azienda = r.get("company")
        out.append({"url": u, "azienda": azienda if isinstance(azienda, str) else None})
    return out


def _tabella(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS radar_indeed (
        chiave text PRIMARY KEY,          -- 'sito:acme.it' | 'tenant:workday/acme' | 'sospetto:jobs4u.it'
        tipo text NOT NULL,
        dominio text, platform_id text, slug text,
        azienda text, paese text,
        url_esempio text,
        viste integer NOT NULL DEFAULT 1,
        primo_visto timestamptz NOT NULL DEFAULT now(),
        ultimo_visto timestamptz NOT NULL DEFAULT now())""")


def _registra(db, c: dict, azienda: str | None, iso: str, url: str, stats: dict) -> None:
    if c["tipo"] == "tenant":
        chiave = f"tenant:{c['platform_id']}/{c['slug']}"
    else:
        chiave = f"{c['tipo']}:{c['dominio']}"
    nuovo = db.execute("""INSERT INTO radar_indeed (chiave, tipo, dominio, platform_id, slug, azienda, paese, url_esempio)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                          ON CONFLICT (chiave) DO UPDATE SET viste = radar_indeed.viste + 1, ultimo_visto = now(),
                            azienda = COALESCE(radar_indeed.azienda, EXCLUDED.azienda)
                          RETURNING (xmax = 0)""",
                       (chiave, c["tipo"], c.get("dominio"), c.get("platform_id"), c.get("slug"), azienda, iso, url[:500])
                       ).fetchone()[0]
    stats["link_" + c["tipo"]] += 1
    if c["tipo"] in ("sospetto", "ats_ignoto"):
        return
    if c["tipo"] == "tenant":
        wd_server = wd_instance = None
        if c["platform_id"] == "workday":
            m = re.search(r"\.(wd\d+)\.myworkdayjobs\.com/(?:[^/?]+/)?([^/?]+)", url)
            if m:
                wd_server, wd_instance = m.group(1), m.group(2)
        ins = db.execute("""INSERT INTO ats_companies (platform_id, slug, company_name, country, discovered_from, wd_server, wd_instance)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (platform_id, slug) DO UPDATE SET
                              company_name = COALESCE(ats_companies.company_name, EXCLUDED.company_name),
                              wd_server = COALESCE(ats_companies.wd_server, EXCLUDED.wd_server),
                              wd_instance = COALESCE(ats_companies.wd_instance, EXCLUDED.wd_instance)
                            RETURNING (xmax = 0)""",
                         (c["platform_id"], c["slug"], azienda, iso, SORGENTE, wd_server, wd_instance)).fetchone()[0]
        if ins:
            stats["tenant_nuovi"] += 1
        return
    # sito aziendale: in coda al detector; se era bollato senza ATS, il
    # verdetto si rifa' (Indeed dice che assume ORA).
    riga = db.execute("""INSERT INTO company_domains (domain, company_name, country, source, status)
                         VALUES (%s, %s, %s, %s, 'pending')
                         ON CONFLICT (domain) DO UPDATE SET
                           company_name = COALESCE(company_domains.company_name, EXCLUDED.company_name),
                           country = COALESCE(company_domains.country, EXCLUDED.country),
                           status = CASE WHEN company_domains.status IN ('no_ats', 'no_careers', 'dead', 'error')
                                         THEN 'pending' ELSE company_domains.status END,
                           checked_at = CASE WHEN company_domains.status IN ('no_ats', 'no_careers', 'dead', 'error')
                                             THEN NULL ELSE company_domains.checked_at END
                         RETURNING (xmax = 0), status""",
                      (c["dominio"], azienda, iso, SORGENTE)).fetchone()
    if riga[0]:
        stats["domini_nuovi"] += 1
    elif nuovo and riga[1] == "pending":
        stats["domini_riaperti"] += 1


def giro(dsn: str, limite: int = 250, ore: int = 168, paese: str | None = None, pausa: float = 1.5) -> dict:
    stats = {"ricerche": 0, "errori": 0, "link": 0, "link_tenant": 0, "link_sito": 0, "link_sospetto": 0,
             "link_ats_ignoto": 0, "tenant_nuovi": 0, "domini_nuovi": 0, "domini_riaperti": 0}
    tutte = ricerche(paese)
    seg = _leggi_segnalibro()
    pos = int(seg.get("pos", 0)) % max(1, len(tutte)) if not paese else 0
    with psycopg.connect(dsn, autocommit=True) as db:
        _tabella(db)
        carica_pattern(db)
        for i in range(min(limite, len(tutte))):
            mestiere, citta, iso = tutte[(pos + i) % len(tutte)]
            try:
                link = cerca(mestiere, citta, iso, ore=ore)
            except Exception as exc:  # noqa: BLE001 — una ricerca rotta non ferma il giro
                stats["errori"] += 1
                log.warning("  %s/%s/%s: %s %s", iso, citta, mestiere, type(exc).__name__, str(exc)[:120])
                if stats["errori"] >= 10 and stats["ricerche"] == 0:
                    log.error("Indeed non risponde: mi fermo (10 errori di fila)")
                    break
                time.sleep(pausa * 4)
                continue
            stats["ricerche"] += 1
            for x in link:
                c = classifica_link(x["url"])
                if not c:
                    continue
                stats["link"] += 1
                _registra(db, c, x["azienda"], iso, x["url"], stats)
            time.sleep(pausa)
    if not paese:
        _scrivi_segnalibro({"pos": (pos + stats["ricerche"] + stats["errori"]) % max(1, len(tutte)),
                            "totale": len(tutte), "ultimo_giro": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    return stats


def ripulisci(dsn: str) -> dict:
    """Riclassifica le righe gia' scritte con le regole di oggi: un «sito» che
    in realta' e' un host ATS diventa ats_ignoto ed esce da company_domains
    (solo se ce l'aveva messo il radar)."""
    esito = {"riclassificati": 0, "tolti_da_coda": 0}
    with psycopg.connect(dsn, autocommit=True) as db:
        _tabella(db)
        carica_pattern(db)
        for chiave, dominio, url in db.execute(
                "SELECT chiave, dominio, url_esempio FROM radar_indeed WHERE tipo IN ('sito','sospetto')").fetchall():
            c = classifica_link(url or "")
            if c and c["tipo"] in ("ats_ignoto", "tenant"):
                nuovo_tipo = c["tipo"]
                nuova_chiave = (f"tenant:{c['platform_id']}/{c['slug']}" if nuovo_tipo == "tenant"
                                else f"ats_ignoto:{c['dominio']}")
                try:
                    db.execute("UPDATE radar_indeed SET tipo=%s, chiave=%s, platform_id=%s, slug=%s WHERE chiave=%s",
                               (nuovo_tipo, nuova_chiave, c.get("platform_id"), c.get("slug"), chiave))
                except psycopg.errors.UniqueViolation:
                    # la chiave nuova esiste gia': la riga vecchia e' un doppione
                    db.execute("DELETE FROM radar_indeed WHERE chiave=%s", (chiave,))
                esito["riclassificati"] += 1
                n = db.execute("DELETE FROM company_domains WHERE domain=%s AND source=%s AND status='pending'",
                               (dominio, SORGENTE)).rowcount
                esito["tolti_da_coda"] += n
    return esito


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.ats.radar_indeed", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--giro", action="store_true", help="un giro di ricerche, scrive nel db")
    ap.add_argument("--limite", type=int, default=250, help="ricerche per giro")
    ap.add_argument("--ore", type=int, default=168, help="solo offerte piu' recenti di N ore")
    ap.add_argument("--paese", default=None, help="ISO2, solo quel paese (senza segnalibro)")
    ap.add_argument("--prova", nargs=3, metavar=("MESTIERE", "CITTA", "ISO2"), help="una ricerca, niente scritture")
    ap.add_argument("--sospetti", action="store_true", help="elenca i domini con nome da bacheca, da guardare")
    ap.add_argument("--ignoti", action="store_true", help="elenca gli host ATS senza tenant leggibile (lavoro per pattern/adapter)")
    ap.add_argument("--ripulisci", action="store_true", help="riclassifica le righe scritte con regole vecchie")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    if a.prova:
        m, c, iso = a.prova
        t0 = time.time()
        with psycopg.connect(_dsn()) as db:
            carica_pattern(db)
        link = cerca(m, c, iso.upper(), ore=a.ore)
        conteggio = {"tenant": 0, "sito": 0, "sospetto": 0, "ats_ignoto": 0, "bacheca": 0}
        for x in link:
            k = classifica_link(x["url"])
            if not k:
                conteggio["bacheca"] += 1
                continue
            conteggio[k["tipo"]] += 1
            print(f"  {k['tipo']:8} {k.get('dominio') or (k['platform_id'] + '/' + k['slug']):45} {x['azienda'] or ''}")
        print(f"{len(link)} link in {time.time()-t0:.0f}s: {conteggio}")
        return 0
    if a.sospetti or a.ignoti:
        tipo = "sospetto" if a.sospetti else "ats_ignoto"
        with psycopg.connect(_dsn()) as db:
            for r in db.execute("SELECT dominio, azienda, paese, viste, url_esempio FROM radar_indeed "
                                "WHERE tipo=%s ORDER BY viste DESC LIMIT 200", (tipo,)):
                print(f"  {r[0]:40} {str(r[1] or '')[:30]:30} {r[2]} x{r[3]}  {r[4][:70]}")
        return 0
    if a.ripulisci:
        print("ripulitura:", ripulisci(_dsn()))
        return 0
    if a.giro:
        print("radar indeed:", giro(_dsn(), a.limite, a.ore, a.paese))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
