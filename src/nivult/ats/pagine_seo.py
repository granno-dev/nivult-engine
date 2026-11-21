"""Le pagine SEO programmatiche della vetrina: ATS e software di job board.

L'idea è quella di fantastic.jobs (/ats/{slug}, /job-board/{slug}): una
pagina per fonte, coi numeri VIVI del nostro indice. Ogni pagina nasce da
due metà che non si mescolano:

  · i dati editoriali in dati/seo/ats.json e dati/seo/jobboards.json,
    scritti a mano una volta (lo stato reale dell'API pubblica di
    quell'ATS, un esempio di codice vero, le FAQ specifiche) — è la parte
    che rende la pagina unica e non un clone;
  · le misure dal database ATS, ricalcolate a ogni giro sul perimetro attivo
    deduplicato dell'API: offerte, aziende, ultimo fetch, ultima modifica del
    contenuto, aziende ed esempi risolti, paesi e fill rate. Il lastmod segue
    content_updated_at con fallback sul fetch, mai la data di build.

**Quality gate.** Una pagina ATS si pubblica solo se: esiste la voce
editoriale, la piattaforma è attiva, e ha almeno --min-attive offerte vive
(default 500). Sotto soglia la pagina sarebbe vuota e farebbe più danno
che traffico. Chi salta il giro viene stampato col motivo.

**Idempotente e solo scrittura di vetrina/.** Il modulo non tocca il DB
(sole SELECT) e riscrive ogni volta gli stessi file: rigenerare non
accumula niente. --dry-run stampa senza scrivere.

    .venv/bin/python -m nivult.ats.pagine_seo --dry-run
    .venv/bin/python -m nivult.ats.pagine_seo
    .venv/bin/python -m nivult.ats.pagine_seo --solo workday
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

from nivult.ponte_ats import ats_database_url

log = logging.getLogger("nivult.ats.pagine_seo")

SITO = "https://nivult.com"
RADICE = Path(__file__).resolve().parents[3]

# Actor dedicati su Apify: solo questi due hanno una pagina loro, gli altri
# puntano all'indice globale.
ACTOR_DEDICATI = {
    "workday": "https://apify.com/nivult/workday-jobs-dataset",
    "greenhouse": "https://apify.com/nivult/greenhouse-jobs-dataset",
}
ACTOR_GLOBALE = "https://apify.com/nivult/global-job-postings"

# %b dipende dal locale del server: i mesi li scriviamo noi, così la pagina
# è identica ovunque giri.
MESI = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

STATI_API = {"public-json", "public-feed", "official-partner",
             "none-public", "unknown"}

_DESCR_CAMPI = (
    "raw->>'description'", "raw->>'content'", "raw->>'descriptionHtml'",
    "raw->>'descriptionPlain'", "raw->>'externalDescription'",
    "raw->>'jobDescription'", "raw->>'job_description'",
    "raw->>'Job_Description'", "raw->>'body'", "raw->>'content_html'",
    "raw->>'description_html'", "raw->>'descriptionBody'", "raw->>'text'",
    "raw->'_jobposting'->>'description'", "raw->>'ShortDescriptionStr'",
)
_DESCR = ("CASE " + " ".join(
    f"WHEN length({campo}) >= 80 THEN {campo}" for campo in _DESCR_CAMPI
) + " ELSE '' END")
_DOVE_ATTIVE = (
    "j.expired_at IS NULL "
    "AND (j.posted_at IS NULL OR j.posted_at <= now()) "
    "AND NOT EXISTS (SELECT 1 FROM ats_jobs d"
    "  WHERE d.duplicate_key = j.duplicate_key"
    "    AND j.duplicate_key IS NOT NULL"
    "    AND d.expired_at IS NULL"
    "    AND (COALESCE(d.created_at, d.posted_at, d.fetched_at), d.id)"
    "        < (COALESCE(j.created_at, j.posted_at, j.fetched_at), j.id))"
)


def _data(dt: datetime | None) -> str:
    """«11 Oct 2026», oppure una dicitura onesta se il fetch manca."""
    if dt is None:
        return "no fetch recorded yet"
    return f"{dt.day} {MESI[dt.month - 1]} {dt.year}"


def _n(n: int) -> str:
    return f"{n:,}"


def _arrotonda(n: int) -> str:
    if n >= 1_000_000:
        valore = (n // 100_000) * 100_000
        cifre = f"{valore / 1_000_000:.1f}".rstrip("0").rstrip(".")
        return f"{cifre}M+"
    if n >= 100_000:
        return f"{((n // 5_000) * 5_000) // 1_000}K+"
    if n >= 10_000:
        return f"{n // 1_000}K+"
    return f"{_n((n // 500) * 500)}+"


def _testo_pulito(valore: object) -> str:
    testo = html.unescape(str(valore or ""))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", testo)).strip()


def _chiave_nome(valore: str) -> str:
    return re.sub(r"[^a-z0-9]", "", valore.casefold())


_NOMI_AZIENDE = {
    "cvshealth": "CVS Health",
    "trinityhealth": "Trinity Health",
    "globalhr": "Global HR",
    "nvidia": "NVIDIA",
    "citi": "Citi",
    "thermofisher": "Thermo Fisher",
    "petco": "Petco",
    "babilou": "Babilou",
}

_PAESI_FINALI = {
    "argentina", "australia", "austria", "belgium", "brazil", "bulgaria",
    "canada", "chile", "china", "colombia", "costa rica", "croatia",
    "czech republic", "czechia", "denmark", "estonia", "finland", "france",
    "germany", "greece", "hong kong", "hungary", "iceland", "india",
    "indonesia", "ireland", "israel", "italy", "japan", "latvia",
    "lithuania", "luxembourg", "malaysia", "mexico", "morocco",
    "netherlands", "the netherlands", "new zealand", "norway", "peru",
    "philippines", "poland", "portugal", "romania", "saudi arabia",
    "singapore", "slovakia", "slovenia", "south africa", "south korea",
    "spain", "sweden", "switzerland", "taiwan", "thailand", "turkey",
    "ukraine", "united arab emirates", "united kingdom", "uk",
    "united states", "united states of america", "usa", "vietnam",
}


def _senza_paese_finale(nome: str) -> str:
    match = re.search(r"\s*\(([^()]+)\)\s*$", nome)
    if match and match.group(1).casefold() in _PAESI_FINALI:
        return nome[:match.start()].strip()
    return nome


def _nome_azienda(nome: str | None, slug: str) -> str | None:
    nome = _testo_pulito(nome)
    if not nome:
        return None
    verificato = (_NOMI_AZIENDE.get(nome.casefold())
                  or _NOMI_AZIENDE.get(_chiave_nome(nome))
                  or _NOMI_AZIENDE.get(slug.casefold()))
    if verificato:
        return _senza_paese_finale(verificato)
    if re.match(r"(?:Careers|Field|Jobs|Internal)\s", nome, re.IGNORECASE):
        return None
    if _chiave_nome(nome) == _chiave_nome(slug):
        return None
    return _senza_paese_finale(nome) or None


def _url_valido(url: object) -> bool:
    testo = str(url or "")
    if re.search(r"\s", testo):
        return False
    try:
        parti = urlsplit(testo)
        return parti.scheme == "https" and bool(parti.netloc)
    except ValueError:
        return False


# ── misure dal database ATS ────────────────────────────────────────────────

@dataclass(slots=True)
class Misure:
    """Tutto ciò che una pagina ATS mostra, misurato in un colpo solo."""
    attive: int = 0
    aziende: int = 0
    ultimo_fetch: datetime | None = None
    ultimo: datetime | None = None
    top_aziende: list[tuple[str, int]] = field(default_factory=list)
    esempi: list[dict] = field(default_factory=list)
    paesi: list[tuple[str, int]] = field(default_factory=list)
    quota_salario: float = 0.0
    fill_rate: dict[str, float] = field(default_factory=dict)


_CHIAVI_ESEMPIO = [
    "id", "external_id", "company_slug", "title", "company",
    "source_company", "country", "city", "location", "url", "posted_at",
    "created_at", "fetched_at", "remote", "seniority", "skills",
    "salary_min", "salary_max", "salary_currency",
]
_SOURCE_COMPANY_SQL = """COALESCE(
    j.raw->'company'->>'name', j.raw->'employer'->>'name',
    j.raw->'entreprise'->>'nom', j.raw->>'firma',
    j.raw->'hiringOrganization'->>'name', j.raw->>'company_name',
    j.raw->>'organization'
)"""


def _campioni(cur: psycopg.Cursor, platform_id: str) -> list[dict]:
    scelti: list[dict] = []
    aziende: set[str] = set()
    for campo in ("city", "location"):
        cur.execute(f"""
            SELECT * FROM (
              SELECT DISTINCT ON (COALESCE(c.company_name,
                                           {_SOURCE_COMPANY_SQL}, j.slug))
                     j.id, j.external_id, j.slug AS company_slug,
                     j.title, c.company_name AS company,
                     CASE WHEN c.company_name IS NULL
                          THEN ({_SOURCE_COMPANY_SQL}) ELSE NULL END AS source_company,
                     j.country, j.city, j.location, j.url, j.posted_at,
                     j.created_at, j.fetched_at, j.remote, j.seniority,
                     j.skills, j.salary_min, j.salary_max, j.salary_currency
                FROM ats_jobs j
                LEFT JOIN ats_companies c
                       ON c.platform_id = j.platform_id AND c.slug = j.slug
               WHERE j.platform_id = %s AND {_DOVE_ATTIVE}
                 AND j.posted_at IS NOT NULL
                 AND NULLIF(btrim(j.{campo}), '') IS NOT NULL
                 AND EXISTS (SELECT 1 FROM iso_paesi p
                              WHERE p.code = j.country)
               ORDER BY COALESCE(c.company_name, {_SOURCE_COMPANY_SQL}, j.slug),
                        j.posted_at DESC, j.id
            ) t ORDER BY posted_at DESC, id DESC""", (platform_id,))
        for valori in cur.fetchall():
            e = dict(zip(_CHIAVI_ESEMPIO, valori))
            e["title"] = _testo_pulito(e["title"])
            e["company"] = (_nome_azienda(e["company"], e["company_slug"])
                            or _nome_azienda(e["source_company"],
                                             e["company_slug"]))
            e["city"] = _testo_pulito(e["city"])
            e["location"] = _testo_pulito(e["location"])
            if not all((e["title"], e["company"], e[campo], e["country"])):
                continue
            if not _url_valido(e["url"]):
                continue
            azienda = e["company"].casefold()
            if azienda in aziende:
                continue
            scelti.append(e)
            aziende.add(azienda)
            if len(scelti) == 3:
                return scelti
    return scelti


_FILL_CAMPI = ("description", "location", "country", "salary_observed",
               "date_posted")


def fill_rate_export() -> dict[str, dict[str, float]]:
    """Fill rate per ATS dal daily export deduplicato, in una sola passata."""
    import duckdb

    from nivult.api_clienti.dati import PERCORSO

    with duckdb.connect(PERCORSO, read_only=True) as conn:
        conn.execute("SET memory_limit = '2GB'")
        conn.execute("SET preserve_insertion_order = false")
        conn.execute("SET threads = 1")
        tmp = Path(PERCORSO).resolve().parent / "duckdb-tmp"
        if tmp.is_dir():
            conn.execute("SET temp_directory = '" + str(tmp).replace("'", "''") + "'")
        righe = conn.execute("""
            SELECT ats, count(*),
                   count(*) FILTER (WHERE length(raw->>'description') >= 80),
                   count(*) FILTER (WHERE NULLIF(trim(raw->>'location'), '') IS NOT NULL),
                   count(*) FILTER (WHERE NULLIF(trim(raw->>'country'), '') IS NOT NULL),
                   count(*) FILTER (WHERE raw->>'salary_min' IS NOT NULL),
                   count(*) FILTER (WHERE raw->>'posted_at' IS NOT NULL)
              FROM offerte GROUP BY ats
        """).fetchall()
    return {
        slug: {campo: 100.0 * n / totale if totale else 0.0
               for campo, n in zip(_FILL_CAMPI, valori)}
        for slug, totale, *valori in righe
    }


def misura(conn: psycopg.Connection, platform_id: str,
           fill_rate: dict[str, float] | None = None) -> Misure:
    m = Misure()
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT count(*), count(DISTINCT j.slug), max(j.fetched_at),
                   max(COALESCE(j.content_updated_at, j.fetched_at))
              FROM ats_jobs j
             WHERE j.platform_id = %s AND {_DOVE_ATTIVE}""", (platform_id,))
        m.attive, m.aziende, m.ultimo_fetch, m.ultimo = cur.fetchone()

        if fill_rate is None:
            cur.execute(f"""
                SELECT count(*),
                       count(*) FILTER (WHERE length({_DESCR}) >= 80),
                       count(*) FILTER (WHERE NULLIF(btrim(j.location), '') IS NOT NULL),
                       count(*) FILTER (WHERE NULLIF(btrim(j.country), '') IS NOT NULL),
                       count(*) FILTER (WHERE j.salary_min IS NOT NULL),
                       count(*) FILTER (WHERE j.posted_at IS NOT NULL)
                  FROM ats_jobs j
                 WHERE j.platform_id = %s AND j.expired_at IS NULL
                   AND (j.posted_at IS NULL OR j.posted_at <= now())""",
                        (platform_id,))
            valori = cur.fetchone()
            m.fill_rate = {
                campo: (100.0 * n / valori[0] if valori[0] else 0.0)
                for campo, n in zip(_FILL_CAMPI, valori[1:])
            }
        else:
            m.fill_rate = fill_rate
        m.quota_salario = m.fill_rate["salary_observed"]

        cur.execute(f"""
            SELECT c.company_name,
                   CASE WHEN c.company_name IS NULL
                        THEN ({_SOURCE_COMPANY_SQL}) ELSE NULL END,
                   j.slug, count(*)
              FROM ats_jobs j
              LEFT JOIN ats_companies c
                     ON c.platform_id = j.platform_id AND c.slug = j.slug
             WHERE j.platform_id = %s AND {_DOVE_ATTIVE}
             GROUP BY 1, 2, 3 ORDER BY 4 DESC, 3 LIMIT 100""", (platform_id,))
        aggregate: dict[str, tuple[str, int]] = {}
        for nome, fonte, slug, n in cur.fetchall():
            pulito = (_nome_azienda(nome, slug)
                      or _nome_azienda(fonte, slug))
            if not pulito:
                continue
            chiave = pulito.casefold()
            if chiave in aggregate:
                aggregate[chiave] = (aggregate[chiave][0],
                                     aggregate[chiave][1] + n)
            else:
                aggregate[chiave] = (pulito, n)
        m.top_aziende = sorted(aggregate.values(),
                               key=lambda r: (-r[1], r[0].casefold()))[:15]

        m.esempi = _campioni(cur, platform_id)

        cur.execute(f"""
            SELECT COALESCE(j.country, '??'), count(*)
              FROM ats_jobs j
             WHERE j.platform_id = %s AND {_DOVE_ATTIVE}
             GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 6""", (platform_id,))
        m.paesi = [(p, n) for p, n in cur.fetchall()]
    return m


def piattaforme(conn: psycopg.Connection) -> list[dict]:
    """Le piattaforme attive col nome: il «66+» del copy si misura, non si scrive."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM ats_platforms WHERE is_active "
                    "ORDER BY id")
        return [{"slug": s, "name": n} for s, n in cur.fetchall()]


# ── dati editoriali ────────────────────────────────────────────────────────

def carica_editoriali(cartella: Path, nome: str) -> dict[str, dict]:
    percorso = cartella / nome
    dati = json.loads(percorso.read_text(encoding="utf-8"))
    voci = {v["slug"]: v for v in dati["voci"]}
    for slug, v in voci.items():
        stato = v.get("api_status")
        if nome == "ats.json" and stato not in STATI_API:
            raise ValueError(f"{nome}: {slug} ha api_status «{stato}» "
                             f"fuori vocabolario {sorted(STATI_API)}")
    return voci


# ── scheletro condiviso con la vetrina ─────────────────────────────────────
# Nav e footer di vetrina/index.html, ma con TUTTI i path assoluti: queste
# pagine stanno in sottodirectory e i relativi si romperebbero.

_NAV = """
<nav class="fixed top-0 inset-x-0 z-50 border-b border-slate-900/5 bg-paper/70 backdrop-blur-xl">
  <div class="max-w-7xl mx-auto px-6 h-16 flex items-center gap-8">
    <a href="/"><img src="/brand/nivult.svg" alt="Nivult" class="h-6 w-auto"></a>
    <div class="hidden sm:flex items-center gap-8">
      <a href="/#data" class="text-sm font-semibold text-slate-500 hover:text-slate-900 transition">Data</a>
      <a href="/#changes" class="text-sm font-semibold text-slate-500 hover:text-slate-900 transition">Changes</a>
      <a href="/#coverage" class="text-sm font-semibold text-slate-500 hover:text-slate-900 transition">Coverage</a>
      <a href="/#api" class="text-sm font-semibold text-slate-500 hover:text-slate-900 transition">API</a>
      <a href="/#pricing" class="text-sm font-semibold text-slate-500 hover:text-slate-900 transition">Pricing</a>
      <a href="/ats/" class="text-sm font-semibold text-brand-600 hover:text-brand-700 transition">ATS coverage</a>
    </div>
    <div class="flex-1"></div>
    <a href="/account" class="hidden sm:inline-block text-sm font-semibold text-slate-500 hover:text-slate-900 transition">Sign in</a>
    <a href="/account" class="hidden sm:inline-block rounded-xl bg-brand-500 hover:bg-brand-600 transition px-4 py-2 text-sm font-bold text-white shadow-lg shadow-brand-500/25">Get a trial key</a>
  </div>
</nav>
"""

_FOOTER = """
<footer class="relative overflow-hidden bg-slate-950 text-slate-400">
  <div class="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-brand-500 via-violet-500 to-mint-500"></div>
  <div class="relative max-w-7xl mx-auto px-6 py-16">
    <div class="grid gap-12 lg:grid-cols-[1.4fr_1fr_1fr_1fr]">
      <div>
        <img src="/brand/nivult.svg" alt="Nivult" class="h-6 w-auto" style="filter:brightness(0) invert(1)">
        <p class="mt-5 text-sm leading-relaxed text-slate-400 max-w-xs">The hiring market, measured at the source. Every field carries its provenance.</p>
      </div>
      <div>
        <p class="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">Product</p>
        <ul class="mt-5 space-y-3 text-sm">
          <li><a href="/#data" class="hover:text-white transition">The data</a></li>
          <li><a href="/#coverage" class="hover:text-white transition">Coverage</a></li>
          <li><a href="/#api" class="hover:text-white transition">API</a></li>
          <li><a href="/#pricing" class="hover:text-white transition">Pricing</a></li>
        </ul>
      </div>
      <div>
        <p class="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">Sources</p>
        <ul class="mt-5 space-y-3 text-sm">
          <li><a href="/ats/" class="hover:text-white transition">ATS coverage</a></li>
          <li><a href="/job-board/" class="hover:text-white transition">Job board software</a></li>
        </ul>
      </div>
      <div>
        <p class="text-[11px] font-bold uppercase tracking-[0.16em] text-slate-500">Company</p>
        <ul class="mt-5 space-y-3 text-sm">
          <li><a href="mailto:hello@nivult.com" class="hover:text-white transition">hello@nivult.com</a></li>
          <li><a href="/booking" class="hover:text-white transition">Book a call</a></li>
          <li><a href="/account" class="hover:text-white transition">Your account</a></li>
          <li><a href="/terms" class="hover:text-white transition">Terms</a> · <a href="/privacy" class="hover:text-white transition">Privacy</a></li>
        </ul>
      </div>
    </div>
    <div class="mt-14 pt-7 border-t border-white/10 flex flex-wrap items-center justify-between gap-3 text-[13px] text-slate-500">
      <span>© 2026 Nivult · hiring and company intelligence</span>
      <span class="inline-flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3.5 py-1.5"><span class="live-dot h-2 w-2 rounded-full bg-mint-400"></span><span class="font-semibold text-slate-300">Live index</span><span class="text-slate-500">· updated daily</span></span>
    </div>
  </div>
</footer>
"""


def _testa(title: str, description: str, canonical: str,
           json_ld: list[dict]) -> str:
    blocchi = "\n".join(
        f'<script type="application/ld+json">\n'
        f'{json.dumps(b if "@context" in b else {"@context": "https://schema.org", **b}, ensure_ascii=False, indent=1)}\n</script>'
        for b in json_ld)
    return f"""<!doctype html>
<!-- Pagina generata da nivult.ats.pagine_seo: i numeri sono misurati sul
     database ATS al momento del giro, il copy editoriale viene da
     dati/seo/. Non editare a mano: il prossimo giro sovrascrive.
     Convenzione della vetrina: mai em dash nel copy. -->
<html lang="en" class="scroll-smooth">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(description)}">
<link rel="canonical" href="{canonical}">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{html.escape(description)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{canonical}">
<meta property="og:site_name" content="Nivult">
<meta name="twitter:card" content="summary">
<script src="/vendor/tailwind.js"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<script>
tailwind.config = {{
  theme: {{ extend: {{
    fontFamily: {{ sans: ['Inter','system-ui','sans-serif'], mono: ['JetBrains Mono','monospace'] }},
    colors: {{
      brand: {{50:'#eef4ff',100:'#dfeaff',400:'#5e8bff',500:'#3b6ef6',600:'#2f5de0',700:'#2549b8'}},
      mint: {{400:'#2fe0b6',500:'#0fbf9a',600:'#0aa588'}},
      paper: '#f5f4ef'
    }}
  }}}}
}}
</script>
<style>
@keyframes pulse-dot {{ 0%,100% {{ box-shadow: 0 0 0 0 rgba(15,191,154,.5);}} 50% {{ box-shadow: 0 0 0 8px rgba(15,191,154,0);}} }}
.live-dot {{ animation: pulse-dot 2.2s infinite; }}
.codebox {{ min-width: 0; }}
.codebox::-webkit-scrollbar {{ height: 8px; width: 8px; }}
.codebox::-webkit-scrollbar-track {{ background: transparent; }}
.codebox::-webkit-scrollbar-thumb {{ background: #33415c; border-radius: 99px; }}
.k{{color:#8ab8ff}}.s{{color:#7ee8c3}}.n{{color:#ffc078}}
@media (prefers-reduced-motion: reduce) {{ .live-dot {{ animation: none !important; }} }}
</style>
{blocchi}
</head>
<body class="bg-paper text-slate-900 font-sans antialiased">
{_NAV}"""


def _codebox(etichetta: str, contenuto: str) -> str:
    """Il blocco scuro della vetrina, col codice in chiaro."""
    return f"""<div class="relative rounded-xl bg-slate-950 border border-slate-900/10 overflow-hidden shadow-lg shadow-slate-900/10 min-w-0">
  <div class="flex items-center gap-2 px-4 py-2.5 border-b border-white/10">
    <div class="flex gap-1.5"><i class="h-2.5 w-2.5 rounded-full bg-[#ff6159] block"></i><i class="h-2.5 w-2.5 rounded-full bg-[#ffbd2e] block"></i><i class="h-2.5 w-2.5 rounded-full bg-[#28c840] block"></i></div>
    <span class="ml-2 font-mono text-[11.5px] text-slate-500">{html.escape(etichetta)}</span>
  </div>
  <pre class="codebox p-5 overflow-auto text-[12.5px] leading-relaxed font-mono text-slate-200">{html.escape(contenuto)}</pre>
</div>"""


def _pre_json(obj: dict, etichetta: str) -> str:
    """Codebox con il JSON colorato come sulla homepage (chiavi/valori)."""
    testo = html.escape(json.dumps(obj, ensure_ascii=False, indent=1))
    import re as _re
    testo = _re.sub(r'&quot;([^&]+?)&quot;:',
                    r'<span class="k">"\1"</span>:', testo)
    testo = _re.sub(r': &quot;(.*?)&quot;',
                    r': <span class="s">"\1"</span>', testo)
    testo = _re.sub(r': (-?\d[\d.]*)', r': <span class="n">\1</span>', testo)
    return f"""<div class="relative rounded-xl bg-slate-950 border border-slate-900/10 overflow-hidden shadow-lg shadow-slate-900/10 min-w-0">
  <div class="flex items-center gap-2 px-4 py-2.5 border-b border-white/10">
    <div class="flex gap-1.5"><i class="h-2.5 w-2.5 rounded-full bg-[#ff6159] block"></i><i class="h-2.5 w-2.5 rounded-full bg-[#ffbd2e] block"></i><i class="h-2.5 w-2.5 rounded-full bg-[#28c840] block"></i></div>
    <span class="ml-2 font-mono text-[11.5px] text-slate-500">{html.escape(etichetta)}</span>
  </div>
  <pre class="codebox p-5 overflow-auto text-[12.5px] leading-relaxed font-mono text-slate-200">{testo}</pre>
</div>"""


# ── JSON-LD ────────────────────────────────────────────────────────────────

def _ld_breadcrumb(voci: list[tuple[str, str]]) -> dict:
    return {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1,
             "name": nome, "item": url}
            for i, (nome, url) in enumerate(voci)
        ],
    }


def _ld_faq(faq: list[dict]) -> dict:
    return {
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": f["q"],
             "acceptedAnswer": {"@type": "Answer", "text": f["a"]}}
            for f in faq
        ],
    }


def _ld_dataset(nome: str, descrizione: str, canonical: str,
                ultimo: datetime | None, slug: str | None = None) -> dict:
    """Il Dataset schema.org. MAI JobPosting: sulle pagine lista è vietato."""
    dato = {
        "@type": "Dataset",
        "name": nome,
        "description": descrizione,
        "url": canonical,
        "creator": {"@type": "Organization", "name": "Nivult", "url": SITO},
        "measurementTechnique": (
            "Daily reading of employer career systems; a posting counts as "
            "active while it appears on the employer's own board, and is "
            "marked closed only when it leaves the source."),
    }
    if slug:
        if ultimo:
            dato["dateModified"] = ultimo.isoformat()
        dato["distribution"] = [
            {"@type": "DataDownload", "encodingFormat": "application/json",
             "contentUrl": f"https://api.nivult.com/v1/jobs?ats={slug}&limit=25"},
            {"@type": "DataDownload", "encodingFormat": "application/x-ndjson",
             "contentUrl": ("https://api.nivult.com/v1/exports/latest/"
                            "offerte-attive"),
             "description": ("The download is a gzip-compressed .jsonl.gz "
                             "NDJSON file.")},
        ]
        dato["variableMeasured"] = [
            "job title", "company", "location", "salary", "posted date",
            "closing date", "full description",
        ]
    return dato


# ── FAQ generate dai dati ──────────────────────────────────────────────────

def _faq_ats(nome: str, edit: dict, m: Misure, fonti: int) -> list[dict]:
    stato = edit["api_status"]
    if stato == "public-json":
        r_api = (f"Yes. {nome} serves public job board data as JSON with no "
                 f"API key. The exact endpoint and a working request are in "
                 f"the «The official route» section above. What no single "
                 f"endpoint gives you is the whole market: you still need to "
                 f"know every company board, fetch each one, and notice "
                 f"yourself when a posting disappears.")
    elif stato == "public-feed":
        r_api = (f"Yes, as a public feed: every {nome} career site exposes "
                 f"its open positions as a document you can fetch without a "
                 f"key. Details and a working request are in «The official "
                 f"route» above. The feed answers one company at a time; "
                 f"nothing on their side enumerates all {nome} companies.")
    elif stato == "official-partner":
        r_api = (f"Only through an official partner program. {nome} does not "
                 f"offer a self-serve public API for job board data.")
    elif stato == "none-public":
        r_api = (f"No. {nome} does not publish a public job board API: "
                 f"postings live behind JavaScript portals or internal "
                 f"endpoints that are neither documented nor supported. "
                 f"Reading them reliably at scale is a maintained "
                 f"integration, which is the part we already run daily.")
    else:
        r_api = (f"There is no documented public job board API for {nome} "
                 f"that we could verify. Our index reads the public career "
                 f"pages directly, the same pages candidates see.")

    faq = [
        {"q": f"Does {nome} have a public API?", "a": r_api},
        {"q": f"How many {nome} job postings are live right now?",
         "a": (f"As of the last refresh ({_data(m.ultimo_fetch)}), our index "
               f"holds {_n(m.attive)} active {nome} postings across "
               f"{_n(m.aziende)} companies. A posting counts as active only "
               f"while it appears on the employer's own board; when it "
               f"leaves the source we mark it closed, we never let it age "
               f"out silently.")},
        {"q": f"How do I get all {nome} jobs in one feed?",
         "a": (f"Through the Nivult index: {nome} postings arrive already "
               f"unified with {fonti}+ other ATS sources in one schema, "
               f"enriched with seniority, workplace, language, skills and a "
               f"salary signal. Get a free Nivult API key and sample from "
               f"your account, or book a call for the full daily export.")},
    ]
    faq.extend(edit.get("faq_extra") or [])
    return faq


# ── pagina ATS ─────────────────────────────────────────────────────────────

def _riga_feed(slug: str, e: dict) -> dict:
    """Un sottoinsieme del contratto API/export, senza campi inventati."""
    riga: dict = {
        "title": e["title"],
        "company": e["company"],
        "company_slug": e["company_slug"],
        "ats": slug,
        "country": e["country"],
        "city": e["city"],
        "location": e["location"],
        "url": e["url"],
        "remote": e.get("remote"),
        "seniority": e.get("seniority"),
        "skills": list(e.get("skills") or [])[:6],
        "salary_min": (float(e["salary_min"])
                       if e.get("salary_min") is not None else None),
        "salary_max": (float(e["salary_max"])
                       if e.get("salary_max") is not None else None),
        "salary_currency": e.get("salary_currency"),
        "posted_at": e["posted_at"].isoformat(),
        "first_seen": (e["created_at"].isoformat()
                       if e.get("created_at") else None),
        "last_seen": (e["fetched_at"].isoformat()
                      if e.get("fetched_at") else None),
    }
    for campo in ("id", "external_id"):
        if e.get(campo) is not None:
            riga[campo] = str(e[campo])
    return {k: v for k, v in riga.items() if v not in (None, "", [])}


def pagina_ats(plat: dict, m: Misure, edit: dict,
               altri: list[dict], fonti: int) -> str:
    slug, nome = plat["slug"], plat["name"]
    canonical = f"{SITO}/ats/{slug}/"
    title = (f"{nome} Jobs API & Dataset "
             f"({_arrotonda(m.attive)} Live Postings) | Nivult")
    description = (f"{_n(m.attive)} live {nome} job postings from "
                   f"{_n(m.aziende)} companies. Access the Nivult API and "
                   f"daily dataset, refreshed across {fonti}+ ATS sources.")

    faq = _faq_ats(nome, edit, m, fonti)
    ld = [{
        "@context": "https://schema.org",
        "@graph": [
            _ld_breadcrumb([("Home", f"{SITO}/"),
                            ("ATS coverage", f"{SITO}/ats/"),
                            (nome, canonical)]),
            _ld_dataset(
                f"{nome} Job Postings Dataset",
                f"{_n(m.attive)} live postings from {_n(m.aziende)} companies "
                f"on {nome}; latest content update {_data(m.ultimo)}.",
                canonical, m.ultimo, slug),
            _ld_faq(faq),
        ],
    }]

    parti = [_testa(title, description, canonical, ld)]

    # ── breadcrumb visivo + H1 + badge numeri vivi
    parti.append(f"""
<header class="pt-28 pb-12 relative overflow-hidden">
  <div class="pointer-events-none absolute -top-40 -left-40 h-[480px] w-[480px] rounded-full bg-gradient-to-br from-brand-400/20 to-violet-400/10 blur-[100px]"></div>
  <div class="pointer-events-none absolute -top-20 right-[-100px] h-[420px] w-[420px] rounded-full bg-gradient-to-bl from-mint-400/20 to-amber-300/10 blur-[100px]"></div>
  <div class="max-w-5xl mx-auto px-6 relative">
    <nav class="text-[13px] font-semibold text-slate-400 mb-6" aria-label="Breadcrumb">
      <a href="/" class="hover:text-brand-600 transition">Home</a>
      <span class="mx-2 text-slate-300">/</span>
      <a href="/ats/" class="hover:text-brand-600 transition">ATS coverage</a>
      <span class="mx-2 text-slate-300">/</span>
      <span class="text-slate-600">{html.escape(nome)}</span>
    </nav>
    <span class="inline-flex items-center gap-2.5 rounded-full border border-slate-900/10 bg-white/70 backdrop-blur px-4 py-1.5 text-[13px] font-semibold text-slate-600 shadow-sm">
      <span class="live-dot h-2 w-2 rounded-full bg-mint-500"></span>
      Live ATS index
    </span>
    <h1 class="mt-6 text-4xl sm:text-5xl font-black leading-[1.05] tracking-tight">{html.escape(nome)} Job Postings API &amp; Dataset</h1>
    <p class="mt-3 text-xl font-bold text-brand-600">{_n(m.attive)} live postings right now</p>
    <p class="mt-5 text-lg text-slate-600 max-w-2xl leading-relaxed">{html.escape(edit["headline_angle"])}</p>
    <div class="mt-8 grid grid-cols-2 lg:grid-cols-4 gap-4">
      <div class="rounded-2xl border border-slate-900/5 bg-white p-5 shadow-sm">
        <b class="block text-2xl font-black tracking-tight tabular-nums text-brand-600">{_n(m.attive)}</b>
        <span class="text-[12.5px] text-slate-500">active postings</span>
      </div>
      <div class="rounded-2xl border border-slate-900/5 bg-white p-5 shadow-sm">
        <b class="block text-2xl font-black tracking-tight tabular-nums text-brand-600">{_n(m.aziende)}</b>
        <span class="text-[12.5px] text-slate-500">companies hiring</span>
      </div>
      <div class="rounded-2xl border border-slate-900/5 bg-white p-5 shadow-sm">
        <b class="block text-2xl font-black tracking-tight tabular-nums text-brand-600">{m.quota_salario:.0f}%</b>
        <span class="text-[12.5px] text-slate-500">with employer-declared salary</span>
      </div>
      <div class="rounded-2xl border border-slate-900/5 bg-white p-5 shadow-sm">
        <b class="block text-2xl font-black tracking-tight tabular-nums text-mint-600">{_data(m.ultimo_fetch)}</b>
        <span class="text-[12.5px] text-slate-500">last refresh · daily cycle</span>
      </div>
    </div>""")

    # distribuzione paesi, top 6
    if m.paesi:
        chips = "".join(
            f'<span class="rounded-lg bg-brand-50 text-brand-700 px-3 py-1.5 text-[12.5px] font-bold">{html.escape("unknown" if p == "??" else p)} · {_n(n)}</span>'
            for p, n in m.paesi)
        parti.append(f"""
    <div class="mt-6 flex flex-wrap items-center gap-2">
      <span class="text-[12px] font-bold uppercase tracking-[0.12em] text-slate-400 mr-2">Top countries</span>
      {chips}
    </div>""")
    parti.append("\n  </div>\n</header>\n")

    # ── The official route
    blocco_codice = ""
    if edit.get("code_example"):
        blocco_codice = f'\n      <div class="mt-6">{_codebox("the official route, verbatim", edit["code_example"])}</div>'
    docs = ""
    if edit.get("api_docs_url"):
        docs = (f'\n      <p class="mt-4 text-[13.5px] font-semibold"><a class="text-brand-600 hover:text-brand-700 underline underline-offset-4" href="{html.escape(edit["api_docs_url"])}" rel="noopener nofollow">Official documentation ↗</a></p>')
    parti.append(f"""
<section class="py-14 border-t border-slate-900/5 bg-white/60">
  <div class="max-w-5xl mx-auto px-6">
    <p class="text-[12px] font-bold uppercase tracking-[0.14em] text-brand-600 mb-3">The official route</p>
    <h2 class="text-3xl font-black tracking-tight">What {html.escape(nome)} gives you directly</h2>
    <p class="mt-4 text-[16px] text-slate-600 leading-relaxed max-w-3xl">{html.escape(edit["official_api_explainer"])}</p>{blocco_codice}{docs}
  </div>
</section>""")

    # ── The Nivult route
    apify = ACTOR_DEDICATI.get(slug, ACTOR_GLOBALE)
    parti.append(f"""
<section class="py-14 border-t border-slate-900/5">
  <div class="max-w-5xl mx-auto px-6">
    <p class="text-[12px] font-bold uppercase tracking-[0.14em] text-mint-600 mb-3">The Nivult route</p>
    <h2 class="text-3xl font-black tracking-tight">One schema across {fonti}+ sources, closures included</h2>
    <p class="mt-4 text-[16px] text-slate-600 leading-relaxed max-w-3xl">
      Reading {html.escape(nome)} yourself buys you one source and a maintenance burden. The Nivult index already reads {html.escape(nome)} next to {fonti}+ other career systems, every day, in one schema: seniority, workplace, language, skills and a salary signal added by our own measured models. A posting is marked closed only when it leaves the employer's page, never because it grew old.
    </p>
    <div class="mt-7 flex flex-wrap gap-3.5">
      <a href="/account" class="rounded-xl bg-brand-500 hover:bg-brand-600 transition px-6 py-3 font-bold text-white shadow-lg shadow-brand-500/25">Get a free API key →</a>
      <a href="{SITO}/booking" class="rounded-xl border border-slate-900/15 px-6 py-3 font-bold text-slate-700 hover:border-brand-500/60 hover:bg-white/60 transition">Book a call</a>
    </div>
    <p class="mt-3 text-[13px] text-slate-400 font-medium">1,000 free monthly credits · no credit card required · <a href="https://api.nivult.com/v1/demo/export-campione?tipo=offerte" class="text-brand-600 font-semibold hover:underline">Download free sample</a></p>
    <p class="mt-5 text-[12px] text-slate-400"><a href="{apify}" rel="noopener nofollow" class="hover:text-slate-600 hover:underline">Also available on Apify</a></p>
  </div>
</section>""")

    # ── top aziende
    if m.top_aziende:
        righe = "".join(
            f'<tr class="hover:bg-slate-50/70 transition"><td class="px-6 py-3 text-slate-400 tabular-nums">{i + 1}</td>'
            f'<td class="px-6 py-3 font-semibold">{html.escape(nome_az)}</td>'
            f'<td class="px-6 py-3 text-right tabular-nums font-bold text-brand-600">{_n(n)}</td></tr>'
            for i, (nome_az, n) in enumerate(m.top_aziende))
        parti.append(f"""
<section class="py-14 border-t border-slate-900/5 bg-white/60">
  <div class="max-w-5xl mx-auto px-6">
    <p class="text-[12px] font-bold uppercase tracking-[0.14em] text-brand-600 mb-3">Live right now</p>
    <h2 class="text-3xl font-black tracking-tight">Top companies hiring via {html.escape(nome)} right now</h2>
    <p class="mt-3 text-[15px] text-slate-500">Counted on active postings in our index, {_data(m.ultimo)}. Only resolved, normalized company names are shown; unresolved board slugs stay out.</p>
    <div class="mt-7 rounded-2xl border border-slate-900/5 bg-white overflow-hidden shadow-sm">
      <table class="w-full text-[14.5px]">
        <thead><tr class="bg-slate-50 text-left text-[11px] font-bold uppercase tracking-[0.1em] text-slate-500">
          <th class="px-6 py-3.5 w-12">#</th><th class="px-6 py-3.5">Company</th><th class="px-6 py-3.5 text-right">Active postings</th></tr></thead>
        <tbody class="divide-y divide-slate-100">{righe}</tbody>
      </table>
    </div>
  </div>
</section>""")

    # ── offerte di esempio
    if m.esempi:
        carte = ""
        for e in m.esempi:
            localita = e["city"] or e["location"]
            dove = " · ".join(p for p in (localita, e["country"]) if p)
            quando = e["posted_at"].date().isoformat()
            carte += f"""
      <a href="{html.escape(e["url"])}" rel="noopener nofollow" class="lift block rounded-2xl border border-slate-900/5 bg-white p-6 hover:border-brand-500/40 hover:shadow-xl transition">
        <b class="block text-[15.5px] font-bold leading-snug">{html.escape(e["title"])}</b>
        <span class="mt-1.5 block text-[13px] text-slate-500">{html.escape(e["company"])} · {html.escape(dove)} · {quando}</span>
        <span class="mt-3 inline-block text-[12px] font-bold text-brand-600">View at the source ↗</span>
      </a>"""
        titolo_esempi = ("Three postings we read today" if len(m.esempi) == 3
                         else "Recent postings with complete source fields")
        parti.append(f"""
<section class="py-14 border-t border-slate-900/5">
  <div class="max-w-5xl mx-auto px-6">
    <p class="text-[12px] font-bold uppercase tracking-[0.14em] text-brand-600 mb-3">Real rows, real links</p>
    <h2 class="text-3xl font-black tracking-tight">{titolo_esempi}</h2>
    <p class="mt-3 text-[15px] text-slate-500">The most recent live {html.escape(nome)} postings with resolved companies and real locations. Links go to the employer's own page.</p>
    <div class="mt-7 grid md:grid-cols-3 gap-4">{carte}
    </div>
  </div>
</section>""")

    # ── blocco developer: API, record reale e copertura specifica dell'ATS
    curl = (f'curl -H "X-Api-Key: $NIVULT_API_KEY" \\\n'
            f'  "https://api.nivult.com/v1/jobs?ats={slug}&limit=25"')
    righe_fill = "".join(
        f'<tr><td class="px-5 py-3 font-mono text-[12px] text-slate-600">{campo}</td>'
        f'<td class="px-5 py-3 text-right font-bold tabular-nums text-brand-600">'
        f'{m.fill_rate.get(campo, 0.0):.1f}%</td></tr>'
        for campo in ("description", "location", "country",
                      "salary_observed", "date_posted"))
    record = (_pre_json(_riga_feed(slug, m.esempi[0]),
                        "API record · selected fields from a real posting")
              if m.esempi else
              '<p class="text-sm text-slate-500">No example row is shown: no posting currently passes every company, location and source-quality check.</p>')
    testo_record = ("The JSON below is a real posting from this ATS, reduced "
                    "to selected non-personal fields." if m.esempi else
                    "The example row stays hidden when no posting passes "
                    "every source-quality check.")
    parti.append(f"""
<section class="py-14 border-t border-slate-900/5 bg-white/60">
  <div class="max-w-5xl mx-auto px-6">
    <p class="text-[12px] font-bold uppercase tracking-[0.14em] text-mint-600 mb-3">For developers</p>
    <h2 class="text-3xl font-black tracking-tight">Query {html.escape(nome)} through the Nivult API</h2>
    <p class="mt-3 text-[15px] text-slate-500 max-w-3xl">Use your trial key or an existing key. {testo_record}</p>
    <div class="mt-7">{_codebox("GET /v1/jobs", curl)}</div>
    <div class="mt-6 grid lg:grid-cols-[1.4fr_1fr] gap-5 items-start">
      {record}
      <div class="rounded-2xl border border-slate-900/5 bg-white overflow-hidden shadow-sm">
        <p class="px-5 pt-5 font-bold text-slate-900">Field fill rate for {html.escape(nome)}</p>
        <p class="px-5 pt-1 pb-3 text-[12px] text-slate-500">Measured for this ATS on the latest deduplicated daily export.</p>
        <table class="w-full text-[13px]"><tbody class="divide-y divide-slate-100">{righe_fill}</tbody></table>
      </div>
    </div>
  </div>
</section>""")

    # ── FAQ
    # 11/10/2026: con un numero dispari di domande l'ultima card restava
    # orfana a meta' riga (il "buco" segnalato da Giuseppe sulle pagine ATS).
    # Se il numero e' dispari, l'ultima occupa tutta la riga.
    dispari = len(faq) % 2 == 1
    voci_faq = "".join(f"""
      <div class="rounded-2xl border border-slate-900/5 bg-white p-7{' md:col-span-2' if dispari and i == len(faq) - 1 else ''}">
        <h3 class="font-bold text-[16px] tracking-tight">{html.escape(f["q"])}</h3>
        <p class="mt-2.5 text-[14.5px] text-slate-600 leading-relaxed">{html.escape(f["a"])}</p>
      </div>""" for i, f in enumerate(faq))
    parti.append(f"""
<section class="py-14 border-t border-slate-900/5">
  <div class="max-w-5xl mx-auto px-6">
    <p class="text-[12px] font-bold uppercase tracking-[0.14em] text-brand-600 mb-3">Before you ask</p>
    <h2 class="text-3xl font-black tracking-tight mb-8">Questions buyers actually ask about {html.escape(nome)} data</h2>
    <div class="grid md:grid-cols-2 gap-5">{voci_faq}
    </div>
  </div>
</section>""")

    # ── cross-link e nota metodologica
    link_altri = "".join(
        f'<a href="/ats/{a["slug"]}/" class="rounded-xl border border-slate-900/10 bg-white px-4 py-2.5 text-[13.5px] font-bold text-slate-600 hover:border-brand-500/50 hover:text-brand-600 transition">{html.escape(a["name"])}</a>'
        for a in altri)
    parti.append(f"""
<section class="py-14 border-t border-slate-900/5 bg-white/60">
  <div class="max-w-5xl mx-auto px-6">
    <h2 class="text-2xl font-black tracking-tight">We also read</h2>
    <div class="mt-5 flex flex-wrap gap-2.5">{link_altri}
      <a href="/ats/" class="rounded-xl border border-brand-500/30 bg-brand-50 px-4 py-2.5 text-[13.5px] font-bold text-brand-600 hover:bg-brand-100 transition">All {fonti}+ sources →</a>
    </div>
  </div>
</section>

<section class="py-10 border-t border-slate-900/5">
  <div class="max-w-5xl mx-auto px-6">
    <p class="text-[13px] text-slate-400 leading-relaxed max-w-3xl">
      Numbers on this page are measured from our own deduplicated index of employer career systems, refreshed daily. «Last refresh» records the latest source read; structured data and sitemap dates use the latest real content change, never the page build time.
      <a href="/#coverage" class="text-brand-600 font-semibold hover:underline">How we collect data</a> ·
      <a href="/ats/" class="text-brand-600 font-semibold hover:underline">All ATS sources</a>
    </p>
  </div>
</section>
""")

    parti.append(_FOOTER)
    parti.append("\n</body>\n</html>\n")
    return "".join(parti)


# ── hub ATS ────────────────────────────────────────────────────────────────

def pagina_hub_ats(pubblicate: list[dict], fonti: int, totale_reale: int) -> str:
    canonical = f"{SITO}/ats/"
    # 11/10/2026: il numero in testa e' l'indice INTERO, non la somma delle
    # pagine pubblicate — 2,5M contro 4,03M reali, segnalato da Giuseppe.
    # Le piattaforme senza pagina esistono lo stesso, e si dicono.
    title = (f"ATS Jobs API & Coverage: {fonti}+ Platforms, "
             f"{_arrotonda(totale_reale)} Live Postings | Nivult")
    description = (f"Access {_n(totale_reale)} live postings across {fonti}+ "
                   f"applicant tracking systems through one API and dataset. "
                   f"Explore dedicated coverage pages for {len(pubblicate)} "
                   f"major platforms, with exact counts and real sample rows.")
    ld = [{
        "@context": "https://schema.org",
        "@graph": [
            _ld_breadcrumb([("Home", f"{SITO}/"),
                            ("ATS coverage", canonical)]),
            {"@type": "CollectionPage", "name": title,
             "description": description, "url": canonical,
             "mainEntity": {
                 "@type": "ItemList", "numberOfItems": len(pubblicate),
                 "itemListElement": [
                     {"@type": "ListItem", "position": i + 1,
                      "name": p["name"],
                      "url": f"{SITO}/ats/{p['slug']}/"}
                     for i, p in enumerate(pubblicate)
                 ]}},
        ],
    }]

    carte = ""
    for p in pubblicate:
        m = p["misure"]
        carte += f"""
      <a href="/ats/{p["slug"]}/" class="lift rounded-2xl border border-slate-900/5 bg-white p-6 hover:border-brand-500/40 hover:shadow-xl transition block">
        <div class="flex items-baseline justify-between gap-3">
          <b class="text-[16.5px] font-bold tracking-tight">{html.escape(p["name"])}</b>
          <span class="rounded-md bg-mint-500/10 text-mint-600 px-2 py-0.5 text-[11px] font-black shrink-0">{_n(m.attive)} live</span>
        </div>
        <p class="mt-2 text-[13px] text-slate-500">{_n(m.aziende)} companies · updated {_data(m.ultimo)}</p>
        <p class="mt-2.5 text-[13px] text-slate-500 leading-relaxed">{html.escape(p["editoriale"]["headline_angle"])}</p>
      </a>"""

    return (_testa(title, description, canonical, ld) + f"""
<header class="pt-28 pb-12 relative overflow-hidden">
  <div class="pointer-events-none absolute -top-40 -left-40 h-[480px] w-[480px] rounded-full bg-gradient-to-br from-brand-400/20 to-violet-400/10 blur-[100px]"></div>
  <div class="max-w-6xl mx-auto px-6 relative">
    <h1 class="text-4xl sm:text-5xl font-black tracking-tight leading-[1.05]">
      ATS Jobs API &amp; Coverage, <span class="bg-gradient-to-r from-brand-500 via-violet-500 to-mint-500 bg-clip-text text-transparent">measured live</span>
    </h1>
    <p class="mt-5 text-lg text-slate-600 max-w-2xl leading-relaxed">
      {_n(totale_reale)} active postings across {fonti}+ applicant tracking systems in the Nivult index. The {len(pubblicate)} major platforms below have dedicated detail pages; every source is available through the same API and daily dataset.
    </p>
  </div>
</header>
<section class="pb-16">
  <div class="max-w-6xl mx-auto px-6 grid md:grid-cols-2 lg:grid-cols-3 gap-4">{carte}
  </div>
</section>
<section class="py-10 border-t border-slate-900/5">
  <div class="max-w-6xl mx-auto px-6">
    <p class="text-[13px] text-slate-400 leading-relaxed max-w-3xl">
      Counts are measured from our own index of employer career systems, refreshed daily. A platform appears here only when it carries enough live postings for the numbers to mean something.
      <a href="/#coverage" class="text-brand-600 font-semibold hover:underline">How we collect data</a> ·
      <a href="/job-board/" class="text-brand-600 font-semibold hover:underline">Job board software feeds</a>
    </p>
  </div>
</section>
""" + _FOOTER + "\n</body>\n</html>\n")


# ── pagine job board ───────────────────────────────────────────────────────

def _faq_jobboard(nome: str, edit: dict) -> list[dict]:
    faq = [
        {"q": f"Does {nome} have built-in job backfill?",
         "a": edit["native_backfill"]},
        {"q": f"How do I import a job feed into {nome}?",
         "a": (f"Intake routes documented or reported for {nome}: "
               + (", ".join(edit["feed_formats"]) if edit["feed_formats"]
                  else "none we could verify publicly")
               + ". Whatever the format, the feed needs fresh, "
                 "closure-verified postings: that is the part Nivult "
                 "supplies, as a daily JSONL export or through the API.")},
    ]
    return faq


def pagina_jobboard(edit: dict, altri: list[dict], oggi: str, fonti: int) -> str:
    slug, nome = edit["slug"], edit["name"]
    canonical = f"{SITO}/job-board/{slug}/"
    title = f"{nome} job backfill: real employer postings, daily | Nivult"
    description = (f"Backfill your {nome} board with verified employer job "
                   f"postings: refreshed daily, closures included, one "
                   f"schema across {fonti}+ ATS sources. Free sample export.")

    faq = _faq_jobboard(nome, edit)
    ld = [
        _ld_dataset(f"Job feed for {nome} boards (Nivult index)",
                    f"Active job postings from employer career systems, "
                    f"formatted for import into {nome} job boards.",
                    canonical, None),
        _ld_faq(faq),
        _ld_breadcrumb([("Home", f"{SITO}/"),
                        ("Job board software", f"{SITO}/job-board/"),
                        (nome, canonical)]),
    ]
    # contenuto editoriale: lastmod = giorno di build
    ld[0]["temporalCoverage"] = oggi

    formati = " · ".join(edit["feed_formats"]) if edit["feed_formats"] \
        else "none documented publicly"
    link_altri = "".join(
        f'<a href="/job-board/{a["slug"]}/" class="rounded-xl border border-slate-900/10 bg-white px-4 py-2.5 text-[13.5px] font-bold text-slate-600 hover:border-brand-500/50 hover:text-brand-600 transition">{html.escape(a["name"])}</a>'
        for a in altri)
    dispari = len(faq) % 2 == 1
    faq_html = "".join(f"""
      <div class="rounded-2xl border border-slate-900/5 bg-white p-7{' md:col-span-2' if dispari and i == len(faq) - 1 else ''}">
        <h3 class="font-bold text-[16px] tracking-tight">{html.escape(f["q"])}</h3>
        <p class="mt-2.5 text-[14.5px] text-slate-600 leading-relaxed">{html.escape(f["a"])}</p>
      </div>""" for i, f in enumerate(faq))

    return (_testa(title, description, canonical, ld) + f"""
<header class="pt-28 pb-12 relative overflow-hidden">
  <div class="pointer-events-none absolute -top-40 -left-40 h-[480px] w-[480px] rounded-full bg-gradient-to-br from-brand-400/20 to-violet-400/10 blur-[100px]"></div>
  <div class="max-w-5xl mx-auto px-6 relative">
    <nav class="text-[13px] font-semibold text-slate-400 mb-6" aria-label="Breadcrumb">
      <a href="/" class="hover:text-brand-600 transition">Home</a>
      <span class="mx-2 text-slate-300">/</span>
      <a href="/job-board/" class="hover:text-brand-600 transition">Job board software</a>
      <span class="mx-2 text-slate-300">/</span>
      <span class="text-slate-600">{html.escape(nome)}</span>
    </nav>
    <h1 class="text-4xl sm:text-5xl font-black leading-[1.05] tracking-tight">
      {html.escape(nome)} backfill,
      <span class="bg-gradient-to-r from-brand-500 via-violet-500 to-mint-500 bg-clip-text text-transparent">with postings that close when the source closes</span>
    </h1>
    <p class="mt-5 text-lg text-slate-600 max-w-2xl leading-relaxed">{html.escape(edit["angle"])}</p>
  </div>
</header>

<section class="py-14 border-t border-slate-900/5 bg-white/60">
  <div class="max-w-5xl mx-auto px-6">
    <p class="text-[12px] font-bold uppercase tracking-[0.14em] text-brand-600 mb-3">The native option</p>
    <h2 class="text-3xl font-black tracking-tight">What {html.escape(nome)} ships out of the box</h2>
    <p class="mt-4 text-[16px] text-slate-600 leading-relaxed max-w-3xl">{html.escape(edit["native_backfill"])}</p>
    <p class="mt-4 text-[14px] text-slate-500"><span class="font-bold text-slate-600">Documented intake formats:</span> {html.escape(formati)}</p>
  </div>
</section>

<section class="py-14 border-t border-slate-900/5">
  <div class="max-w-5xl mx-auto px-6">
    <p class="text-[12px] font-bold uppercase tracking-[0.14em] text-mint-600 mb-3">The Nivult route</p>
    <h2 class="text-3xl font-black tracking-tight">A feed you do not have to apologize for</h2>
    <p class="mt-4 text-[16px] text-slate-600 leading-relaxed max-w-3xl">
      Backfill networks pay you per click and keep the listing when the visitor leaves. Nivult sells you the data itself: millions of active postings read daily from employer career systems, each with title, company, location, seniority, workplace and a salary signal, and each marked closed the day it leaves the source. Import it as daily JSONL or read it through the API; on your board the jobs are yours.
    </p>
    <div class="mt-7 flex flex-wrap gap-3.5">
      <a href="{ACTOR_GLOBALE}" class="rounded-xl bg-brand-500 hover:bg-brand-600 transition px-6 py-3 font-bold text-white shadow-lg shadow-brand-500/25">Get the feed on Apify →</a>
      <a href="/booking" class="rounded-xl border border-slate-900/15 px-6 py-3 font-bold text-slate-700 hover:border-brand-500/60 hover:bg-white/60 transition">Book a call</a>
    </div>
    <p class="mt-3 text-[13px] text-slate-400 font-medium">Free sample export on the actor page · no credit card required</p>
  </div>
</section>

<section class="py-14 border-t border-slate-900/5 bg-white/60">
  <div class="max-w-5xl mx-auto px-6">
    <h2 class="text-2xl font-black tracking-tight mb-8">Before you ask</h2>
    <div class="grid md:grid-cols-2 gap-5">{faq_html}
    </div>
  </div>
</section>

<section class="py-14 border-t border-slate-900/5">
  <div class="max-w-5xl mx-auto px-6">
    <h2 class="text-2xl font-black tracking-tight">Feeding a different board?</h2>
    <div class="mt-5 flex flex-wrap gap-2.5">{link_altri}
      <a href="/job-board/" class="rounded-xl border border-brand-500/30 bg-brand-50 px-4 py-2.5 text-[13.5px] font-bold text-brand-600 hover:bg-brand-100 transition">All job board software →</a>
    </div>
  </div>
</section>

<section class="py-10 border-t border-slate-900/5 bg-white/60">
  <div class="max-w-5xl mx-auto px-6">
    <p class="text-[13px] text-slate-400 leading-relaxed max-w-3xl">
      This page is editorial: it describes {html.escape(nome)}'s native options as publicly documented on {_data(datetime.now())}, and our own feed as measured on the index. Vendor plans change; verify the current plan terms with {html.escape(nome)} before you sign.
      <a href="/#coverage" class="text-brand-600 font-semibold hover:underline">How we collect data</a>
    </p>
  </div>
</section>
""" + _FOOTER + "\n</body>\n</html>\n")


def pagina_hub_jobboard(voci: list[dict]) -> str:
    canonical = f"{SITO}/job-board/"
    title = f"Job board software feeds: {len(voci)} platforms covered | Nivult"
    description = ("Backfill any job board with verified employer postings: "
                   "per-platform notes on native backfill, import formats "
                   "and the honest trade-offs.")
    ld = [_ld_breadcrumb([("Home", f"{SITO}/"),
                          ("Job board software", canonical)])]
    carte = "".join(f"""
      <a href="/job-board/{v["slug"]}/" class="lift rounded-2xl border border-slate-900/5 bg-white p-6 hover:border-brand-500/40 hover:shadow-xl transition block">
        <b class="text-[16.5px] font-bold tracking-tight">{html.escape(v["name"])}</b>
        <p class="mt-2.5 text-[13px] text-slate-500 leading-relaxed">{html.escape(v["angle"])}</p>
      </a>""" for v in voci)
    return (_testa(title, description, canonical, ld) + f"""
<header class="pt-28 pb-12 relative overflow-hidden">
  <div class="pointer-events-none absolute -top-40 -left-40 h-[480px] w-[480px] rounded-full bg-gradient-to-br from-brand-400/20 to-violet-400/10 blur-[100px]"></div>
  <div class="max-w-6xl mx-auto px-6 relative">
    <h1 class="text-4xl sm:text-5xl font-black tracking-tight leading-[1.05]">
      Job board software, <span class="bg-gradient-to-r from-brand-500 via-violet-500 to-mint-500 bg-clip-text text-transparent">fed properly</span>
    </h1>
    <p class="mt-5 text-lg text-slate-600 max-w-2xl leading-relaxed">
      Every platform below can take an external job feed. For each one: what the native backfill really does, which import formats exist, and where Nivult's verified postings fit.
    </p>
  </div>
</header>
<section class="pb-16">
  <div class="max-w-6xl mx-auto px-6 grid md:grid-cols-2 lg:grid-cols-3 gap-4">{carte}
  </div>
</section>
<section class="py-10 border-t border-slate-900/5">
  <div class="max-w-6xl mx-auto px-6">
    <p class="text-[13px] text-slate-400 leading-relaxed max-w-3xl">
      Platform notes are editorial, written from public documentation and marked conservative where we could not verify. Our side of the page is measured.
      <a href="/ats/" class="text-brand-600 font-semibold hover:underline">The ATS sources we read</a>
    </p>
  </div>
</section>
""" + _FOOTER + "\n</body>\n</html>\n")


# ── sitemap e robots ───────────────────────────────────────────────────────

def _scrivi(percorso: Path, contenuto: str, dry_run: bool) -> None:
    if dry_run:
        return
    percorso.parent.mkdir(parents=True, exist_ok=True)
    percorso.write_text(contenuto, encoding="utf-8")


def sitemap_urlset(urls: list[tuple[str, str | None]]) -> str:
    righe = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod in urls:
        righe.append("  <url>")
        righe.append(f"    <loc>{html.escape(loc)}</loc>")
        if lastmod:
            righe.append(f"    <lastmod>{lastmod}</lastmod>")
        righe.append("  </url>")
    righe.append("</urlset>")
    return "\n".join(righe) + "\n"


def sitemap_index(voci: list[tuple[str, str | None]]) -> str:
    righe = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod in voci:
        righe.append("  <sitemap>")
        righe.append(f"    <loc>{loc}</loc>")
        if lastmod:
            righe.append(f"    <lastmod>{lastmod}</lastmod>")
        righe.append("  </sitemap>")
    righe.append("</sitemapindex>")
    return "\n".join(righe) + "\n"


# ── il giro ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        prog="python -m nivult.ats.pagine_seo",
        description="Genera le pagine SEO della vetrina (ATS e job board) "
                    "coi numeri vivi del database ATS.")
    ap.add_argument("--min-attive", type=int, default=500,
                    help="soglia minima di offerte attive per pubblicare "
                         "una pagina ATS (default 500)")
    ap.add_argument("--vetrina", type=Path, default=RADICE / "vetrina",
                    help="cartella della vetrina (default: vetrina/ del repo)")
    ap.add_argument("--dati", type=Path, default=RADICE / "dati" / "seo",
                    help="cartella dei file editoriali (default: dati/seo/)")
    ap.add_argument("--solo", metavar="SLUG",
                    help="genera solo questa piattaforma (debug)")
    ap.add_argument("--dry-run", action="store_true",
                    help="stampa il riepilogo senza scrivere file")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    editoriali_ats = carica_editoriali(args.dati, "ats.json")
    editoriali_jb = carica_editoriali(args.dati, "jobboards.json")

    try:
        fill_rates = fill_rate_export()
    except Exception as e:  # noqa: BLE001
        log.warning("export fill rate non disponibile (%s): fallback Postgres", e)
        fill_rates = {}

    conn = psycopg.connect(ats_database_url())
    try:
        tutte = piattaforme(conn)
        fonti = len(tutte)
        totale_reale, ultimo_globale = conn.execute(f"""
            SELECT count(*), max(COALESCE(j.content_updated_at, j.fetched_at))
              FROM ats_jobs j WHERE {_DOVE_ATTIVE}
        """).fetchone()

        pubblicate: list[dict] = []
        saltate: list[tuple[str, str]] = []
        for plat in tutte:
            slug = plat["slug"]
            if args.solo and slug != args.solo:
                continue
            edit = editoriali_ats.get(slug)
            if edit is None:
                saltate.append((slug, ("voce editoriale mancante "
                                       "in dati/seo/ats.json")))
                continue
            m = misura(conn, slug, fill_rates.get(slug))
            if m.attive < args.min_attive:
                saltate.append((slug, (f"sotto soglia: {m.attive} attive "
                                       f"< {args.min_attive}")))
                continue
            pubblicate.append({**plat,
                               # il nome nel DB porta qualificatori italiani
                               # («(Svezia)»): la voce editoriale può
                               # sostituirlo con la forma inglese
                               "name": edit.get("display_name") or plat["name"],
                               "editoriale": edit, "misure": m})
    finally:
        conn.close()

    if args.solo and not pubblicate and not any(s == args.solo
                                                for s, _ in saltate):
        log.error("--solo %s: piattaforma sconosciuta o non attiva", args.solo)
        return 1

    oggi = datetime.now().date().isoformat()
    lastmod_dati = (ultimo_globale.date().isoformat()
                    if ultimo_globale else None)

    # ── pagine ATS: gli 8 cross-link ruotano in modo deterministico,
    #    così ogni pagina linka vicini diversi senza ricostruire grafi
    for i, p in enumerate(pubblicate):
        altri = [pubblicate[(i + 1 + k) % len(pubblicate)]
                 for k in range(min(8, len(pubblicate) - 1))] \
            if len(pubblicate) > 1 else []
        html_pagina = pagina_ats(p, p["misure"], p["editoriale"], altri, fonti)
        _scrivi(args.vetrina / "ats" / p["slug"] / "index.html",
                html_pagina, args.dry_run)

    if not args.solo:
        _scrivi(args.vetrina / "ats" / "index.html",
                pagina_hub_ats(pubblicate, fonti, totale_reale), args.dry_run)

    # Le pagine job board descrivono il feed globale: il lastmod segue i dati.
    voci_jb = list(editoriali_jb.values())
    for v in voci_jb:
        altri = [x for x in voci_jb if x["slug"] != v["slug"]][:8]
        _scrivi(args.vetrina / "job-board" / v["slug"] / "index.html",
                pagina_jobboard(v, altri, oggi, fonti), args.dry_run)
    _scrivi(args.vetrina / "job-board" / "index.html",
            pagina_hub_jobboard(voci_jb), args.dry_run)

    # ── sitemap: il lastmod viene dai dati o dai file, mai dalla build
    if not args.solo:
        urls_ats = [(f"{SITO}/ats/", lastmod_dati)]
        for p in pubblicate:
            m = p["misure"]
            urls_ats.append((f"{SITO}/ats/{p['slug']}/",
                             m.ultimo.date().isoformat() if m.ultimo else None))
        _scrivi(args.vetrina / "sitemap-ats.xml",
                sitemap_urlset(urls_ats), args.dry_run)

        urls_jb = [(f"{SITO}/job-board/", lastmod_dati)]
        urls_jb += [(f"{SITO}/job-board/{v['slug']}/", lastmod_dati)
                    for v in voci_jb]
        _scrivi(args.vetrina / "sitemap-jobboard.xml",
                sitemap_urlset(urls_jb), args.dry_run)

        # sitemap.xml diventa un indice: se esisteva come urlset, il suo
        # contenuto si sposta in sitemap-main.xml e resta referenziato
        principale = args.vetrina / "sitemap.xml"
        voci_index = []
        if principale.exists():
            vecchio = principale.read_text(encoding="utf-8")
            if "<urlset" in vecchio:
                _scrivi(args.vetrina / "sitemap-main.xml", vecchio,
                        args.dry_run)
                voci_index.append((f"{SITO}/sitemap-main.xml", lastmod_dati))
                log.info("sitemap.xml era un urlset: spostato in "
                         "sitemap-main.xml e referenziato nell'indice")
        # Una noindex in sitemap e' un segnale contraddittorio (booking).
        urls_sito = []
        for pagina, url in (("index.html", "/"), ("booking.html", "/booking"),
                            ("privacy.html", "/privacy"), ("terms.html", "/terms"),
                            ("account.html", "/account")):
            f = args.vetrina / pagina
            if pagina == "booking.html":
                continue
            if f.exists() and 'content="noindex"' in f.read_text(encoding="utf-8").lower():
                continue
            lm = (datetime.fromtimestamp(f.stat().st_mtime).date().isoformat()
                  if f.exists() else None)
            urls_sito.append((f"{SITO}{url}", lm))
        _scrivi(args.vetrina / "sitemap-sito.xml",
                sitemap_urlset(urls_sito), args.dry_run)
        lastmod_sito = max((lm for _, lm in urls_sito if lm), default=None)
        voci_index += [(f"{SITO}/sitemap-sito.xml", lastmod_sito),
                       (f"{SITO}/sitemap-ats.xml", lastmod_dati),
                       (f"{SITO}/sitemap-jobboard.xml", lastmod_dati)]
        _scrivi(principale, sitemap_index(voci_index), args.dry_run)

        robots = args.vetrina / "robots.txt"
        if not robots.exists():
            _scrivi(robots,
                    "User-agent: *\nAllow: /\n\n"
                    f"Sitemap: {SITO}/sitemap.xml\n", args.dry_run)

    # ── riepilogo
    print(f"\n{'DRY-RUN: ' if args.dry_run else ''}pagine SEO — "
          f"{len(pubblicate)} ATS pubblicate, {len(voci_jb)} job board")
    for p in pubblicate:
        print(f"  ✓ /ats/{p['slug']}/  {_n(p['misure'].attive)} attive, "
              f"{_n(p['misure'].aziende)} aziende")
    for slug, motivo in saltate:
        print(f"  ✗ {slug}: {motivo}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
