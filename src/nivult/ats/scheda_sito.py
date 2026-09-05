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


def _pagina(cli: httpx.Client, url: str) -> str:
    r = cli.get(url)
    if r.status_code != 200 or "html" not in r.headers.get(
            "content-type", "html"):
        return ""
    return r.text[:400_000]


def _testo_azienda(cli: httpx.Client, dominio: str) -> str:
    """La home piu' l'eventuale pagina about: testo pulito, tetto 12k."""
    base = f"https://{dominio}"
    try:
        home = _pagina(cli, base)
    except Exception:                                # noqa: BLE001
        return ""
    pezzi = [home]
    m = _ABOUT_RX.search(home or "")
    if m:
        link = m.group(1)
        if link.startswith("/"):
            link = base + link
        if link.startswith("http") and dominio in link:
            try:
                pezzi.append(_pagina(cli, link))
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
    with psycopg.connect(dsn, autocommit=True) as c:
        if _colonna_manca(c, "ats_companies", "site_checked_at"):
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT "
                      "EXISTS employees_site int")
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT "
                      "EXISTS industry_site text")
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT "
                      "EXISTS site_evidence text")
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT "
                      "EXISTS site_checked_at timestamptz")
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
            testo = _testo_azienda(cli, dominio)
            time.sleep(0.5)
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
                    log.warning("GLM giu': mi fermo, righe non marcate")
                    break
                continue
            c.execute("""UPDATE ats_companies
                            SET employees_site = %s, industry_site = %s,
                                site_evidence = %s, site_checked_at = now()
                          WHERE platform_id = %s AND slug = %s""",
                      (dip, sett, prova, pid, slug))
            stats["organico"] += 1 if dip else 0
            stats["settore"] += 1 if sett else 0
    log.info("scheda sito: %s", stats)
    return stats


def main() -> int:
    import argparse
    from .runner import ATS_DSN
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.scheda_sito")
    ap.add_argument("--limite", type=int, default=300)
    a = ap.parse_args()
    print(json.dumps(arricchisci(ATS_DSN, a.limite)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
