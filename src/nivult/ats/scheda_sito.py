"""Organico e settore dalla pagina «chi siamo» dell'azienda.

L'idea nuda «chiedi a GLM quanti dipendenti ha X» produce numeri
inventati con sicurezza: il modello non fa ricerche, pesca dalla
memoria, e sulla coda lunga la memoria non c'e'. La versione ANCORATA
invece funziona: si scarica la pagina about/chi-siamo dal dominio che
gia' conosciamo, e GLM Flash (gratuito) estrae SOLO cio' che il testo
dice, con la citazione a prova. Se la pagina non lo dice, unknown, e
il campo resta NULL.

E' la via per l'Italia e la Germania senza registri gratuiti, e per
qualunque paese: l'azienda che si racconta sul proprio sito e' una
fonte citabile — «dichiarato dall'azienda sul suo sito» — non una
stima.

Catena di fiducia in esporta: registri > wikidata > sito > annunci.
"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx
import psycopg

log = logging.getLogger("nivult.ats.scheda_sito")

_UA = ("Mozilla/5.0 (compatible; NivultBot/1.0; "
       "+https://nivult.com)")

_ABOUT_RX = re.compile(
    r'href=["\x27]([^"\x27]*(?:about|chi-siamo|chisiamo|azienda|'
    r'ueber-uns|über-uns|unternehmen|qui-sommes|societe|société|'
    r'om-oss|om-os|meista|meistä|quienes-somos|sobre-nosotros|'
    r'over-ons|company|who-we-are)[^"\x27]*)["\x27]', re.I)

_TAG_RX = re.compile(r"<script[^>]*>.*?</script>|<style[^>]*>.*?</style>"
                     r"|<[^>]+>", re.S | re.I)

_PROMPT = """Below is text from a company's own website. Extract ONLY facts the text states explicitly. Answer ONLY with JSON:
{{"employees": <integer or null>, "industry": "<short English industry label, or null>", "evidence": "<the exact sentence you used, or null>"}}
Rules: employees must be a number the TEXT states for the whole company (not one office). If the text does not state it, use null. Never estimate, never use outside knowledge.
TEXT:
{t}"""


def _colonna_manca(c, tabella: str, colonna: str) -> bool:
    return c.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (tabella, colonna)).fetchone() is None


def _pagina(cli: httpx.Client, url: str) -> str | None:
    """None = guasto di trasporto (rete o 5xx): non un esito, si riprova.
    "" = risposta vera senza contenuto utile (404, non-HTML): esito."""
    try:
        r = cli.get(url)
    except httpx.HTTPError:
        return None
    if r.status_code >= 500:
        return None
    if r.status_code != 200 or "html" not in r.headers.get(
            "content-type", "html"):
        return ""
    return r.text[:400_000]


# la descrizione che l'azienda scrive di se' nell'intestazione del sito:
# <meta name="description"> o og:description. E' il campo company_description
# di Coresignal per chi non ha jsonld negli annunci (21/09/2026).
_META_RX = re.compile(
    r'<meta\s+(?:[^>]*?\b(?:name|property)\s*=\s*["\'](?:description|og:description|twitter:description)["\'][^>]*?\bcontent\s*=\s*["\']([^"\']{40,2000})["\']'
    r'|[^>]*?\bcontent\s*=\s*["\']([^"\']{40,2000})["\'][^>]*?\b(?:name|property)\s*=\s*["\'](?:description|og:description|twitter:description)["\'])',
    re.I | re.S)


def descrizione_sito(html_pagina: str) -> str | None:
    import html as _html
    for m in _META_RX.finditer(html_pagina or ""):
        d = _html.unescape(m.group(1) or m.group(2) or "").strip()
        d = re.sub(r"\s+", " ", d)
        if len(d) >= 40:
            return d[:1500]
    return None


def _testo_azienda(cli: httpx.Client, dominio: str, meta: dict | None = None) -> str | None:
    """La home piu' l'eventuale pagina about: testo pulito, tetto 12k.
    Se `meta` e' un dict ci mette la descrizione dall'intestazione.
    None se la home non e' stata letta (trasporto): chi chiama non marca."""
    base = f"https://{dominio}"
    try:
        home = _pagina(cli, base)
    except Exception:                                # noqa: BLE001
        return None
    if home is None:
        return None
    if meta is not None:
        meta["descrizione"] = descrizione_sito(home)
    pezzi = [home]
    m = _ABOUT_RX.search(home or "")
    if m:
        link = m.group(1)
        if link.startswith("/"):
            link = base + link
        if link.startswith("http") and dominio in link:
            try:
                about = _pagina(cli, link)
                if about:
                    pezzi.append(about)
            except Exception:                        # noqa: BLE001
                pass
    testo = " ".join(_TAG_RX.sub(" ", p) for p in pezzi if p)
    return re.sub(r"\s+", " ", testo)[:12_000]


def arricchisci(dsn: str, limite: int = 300) -> dict:
    from .profilo import _glm_flash
    from .organico_dichiarato import _numeri
    from .registri_imprese import _norm
    stats = {"esaminate": 0, "con_testo": 0, "organico": 0,
             "settore": 0, "errori_glm": 0}
    cli = httpx.Client(timeout=15, headers={"User-Agent": _UA},
                       follow_redirects=True)
    modello = _glm_flash()
    ko_di_fila = 0
    # quando GLM e' giu' (credito a zero, misurato il 23/09/2026) la corsa
    # NON deve fermarsi ne' buttare cio' che le regole trovano: si passa in
    # modalita' «solo regole» — il numero deterministico si scrive, settore
    # e citazione restano vuoti, e GLM non si chiama piu' finche' non torna.
    solo_regex = False
    with psycopg.connect(dsn, autocommit=True) as c:
        if _colonna_manca(c, "ats_companies", "site_checked_at"):
            # lock_timeout come nel blocco sotto: ADD COLUMN IF NOT EXISTS
            # prende il lock esclusivo anche se la colonna esiste gia'
            c.execute("SET lock_timeout = '10s'")
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT "
                      "EXISTS employees_site int")
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT "
                      "EXISTS industry_site text")
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT "
                      "EXISTS site_evidence text")
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT "
                      "EXISTS site_checked_at timestamptz")
            c.execute("RESET lock_timeout")
        if _colonna_manca(c, "ats_companies", "site_description"):
            c.execute("SET lock_timeout = '10s'")
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS site_description text")
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS site_description_at timestamptz")
            c.execute("RESET lock_timeout")
        righe = c.execute("""
            SELECT platform_id, slug,
                   coalesce(logo_domain, site_domain), company_name
              FROM ats_companies
             WHERE is_active AND job_count > 0
               AND coalesce(logo_domain, site_domain) IS NOT NULL
               AND site_checked_at IS NULL
               AND employees_reg IS NULL AND employees_wd IS NULL
             ORDER BY job_count DESC
             LIMIT %s""", (limite,)).fetchall()
        for pid, slug, dominio, nome in righe:
            stats["esaminate"] += 1
            meta: dict = {}
            testo = _testo_azienda(cli, dominio, meta)
            time.sleep(0.5)
            if testo is None:
                # sito non letto (trasporto): niente marcatore, si riprova
                stats["errori_rete"] = stats.get("errori_rete", 0) + 1
                continue
            if meta.get("descrizione"):
                c.execute("UPDATE ats_companies SET site_description = %s, site_description_at = now() "
                          "WHERE platform_id=%s AND slug=%s", (meta["descrizione"], pid, slug))
            if len(testo) < 300:
                c.execute("UPDATE ats_companies SET site_checked_at=now() "
                          "WHERE platform_id=%s AND slug=%s", (pid, slug))
                continue
            # prova d'identita': il nome dell'azienda deve comparire
            # nella pagina — protegge dai domini-candidato sbagliati
            # (Brandfetch su un omonimo) e dai logo_domain sporchi.
            if nome and _norm(nome) and len(_norm(nome)) >= 4 \
                    and _norm(nome) not in _norm(testo):
                c.execute("UPDATE ats_companies SET site_checked_at=now() "
                          "WHERE platform_id=%s AND slug=%s", (pid, slug))
                continue
            stats["con_testo"] += 1
            dip = sett = prova = None
            # primo lo strato deterministico, gratis e senza modello
            nums = _numeri(testo)
            if nums:
                dip = max(nums)
            if not solo_regex:
                try:
                    r = modello.chat([{"role": "user", "content":
                                       _PROMPT.format(t=testo[:8000])}],
                                     max_tokens=150)
                    g = json.loads(re.search(r"\{.*\}", r, re.S).group(0))
                    if isinstance(g.get("employees"), int) \
                            and 10 <= g["employees"] <= 3_000_000:
                        # GLM vince solo se concorda con una dichiarazione
                        # regex o se la regex tace: mai un numero senza
                        # un'eco nel testo
                        if dip is None or g["employees"] in nums:
                            dip = g["employees"]
                    if isinstance(g.get("industry"), str) \
                            and 2 < len(g["industry"]) < 60:
                        sett = g["industry"]
                    if isinstance(g.get("evidence"), str):
                        prova = g["evidence"][:300]
                    ko_di_fila = 0
                except Exception:                        # noqa: BLE001
                    stats["errori_glm"] += 1
                    ko_di_fila += 1
                    if ko_di_fila >= 3:
                        solo_regex = True
                        log.warning("GLM giu': da qui in poi SOLO regole — "
                                    "i numeri deterministici si scrivono comunque, "
                                    "i primi %d buttati si rileggeranno al prossimo giro",
                                    ko_di_fila)
            # si scrive SEMPRE (anche in modalita' solo regole): il numero
            # deterministico non si butta piu' per un modello che non risponde
            c.execute("""UPDATE ats_companies
                            SET employees_site = %s, industry_site = %s,
                                site_evidence = %s, site_checked_at = now()
                          WHERE platform_id = %s AND slug = %s""",
                      (dip, sett, prova, pid, slug))
            stats["organico"] += 1 if dip else 0
            stats["settore"] += 1 if sett else 0
    log.info("scheda sito: %s", stats)
    return stats


def descrizioni(dsn: str, limite: int = 1500) -> dict:
    """Solo la descrizione dall'intestazione della home, una richiesta per
    azienda, niente GLM: per le aziende con dominio che non l'hanno ancora.
    Chi non ne ha una nell'intestazione si rimarca lo stesso (ricontrollo
    fra 90 giorni)."""
    stats = {"esaminate": 0, "descrizioni": 0}
    cli = httpx.Client(timeout=12, headers={"User-Agent": _UA}, follow_redirects=True)
    with psycopg.connect(dsn, autocommit=True) as c:
        if _colonna_manca(c, "ats_companies", "site_description"):
            c.execute("SET lock_timeout = '10s'")
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS site_description text")
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS site_description_at timestamptz")
            c.execute("RESET lock_timeout")
        righe = c.execute("""
            SELECT platform_id, slug, coalesce(site_domain, logo_domain)
              FROM ats_companies
             WHERE is_active AND job_count > 0
               AND coalesce(site_domain, logo_domain) IS NOT NULL
               AND (site_description_at IS NULL OR (site_description IS NULL AND site_description_at < now() - interval '90 days'))
             ORDER BY job_count DESC LIMIT %s""", (limite,)).fetchall()
        for pid, slug, dominio in righe:
            stats["esaminate"] += 1
            try:
                home = _pagina(cli, f"https://{dominio}")
            except Exception:                        # noqa: BLE001
                home = None
            if home is None:
                # trasporto: niente UPDATE — scrivere NULL qui cancellava
                # una site_description esistente al ricontrollo dei 90 giorni
                continue
            d = descrizione_sito(home)
            c.execute("UPDATE ats_companies SET site_description = %s, site_description_at = now() "
                      "WHERE platform_id=%s AND slug=%s", (d, pid, slug))
            stats["descrizioni"] += bool(d)
            time.sleep(0.3)
    log.info("descrizioni dal sito: %s", stats)
    return stats


def main() -> int:
    import argparse
    from .runner import ATS_DSN
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.scheda_sito")
    ap.add_argument("--limite", type=int, default=300)
    ap.add_argument("--descrizioni", action="store_true",
                    help="solo la descrizione dall'intestazione della home, senza GLM")
    a = ap.parse_args()
    if a.descrizioni:
        print(json.dumps(descrizioni(ATS_DSN, a.limite)))
        return 0
    print(json.dumps(arricchisci(ATS_DSN, a.limite)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
