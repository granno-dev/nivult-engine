"""Settore e dipendenti dai REGISTRI PUBBLICI delle imprese.

Wikidata copre le aziende famose; i registri nazionali coprono TUTTE
quelle del loro paese, gratis e con licenza aperta:

  FR  recherche-entreprises.api.gouv.fr  NAF + fascia dipendenti, no chiave
  NO  data.brreg.no (Enhetsregisteret)   NACE + dipendenti esatti, no chiave
  FI  avoindata.prh.fi (YTJ)             linea di business, no chiave
  DK  cvrapi.dk                          settore + dipendenti (uso educato)
  US  SEC EDGAR                          SIC (solo quotate), no chiave

Il match e' per nome col nocciolo normalizzato (stessa filosofia
anti-omonimi di wikidata_ditte): un nome che non combacia NON entra —
meglio NULL di un settore di un'altra azienda. Ogni valore porta la
fonte in reg_source: il compratore del dataset sa da dove viene.

I codici NAF/NACE dei registri europei condividono le prime due cifre
(divisioni NACE Rev.2): un'unica mappa li traduce tutti in etichette
inglesi leggibili.

Il marcatore reg_checked_at si scrive anche sui buchi: un'azienda gia'
cercata e non trovata non si ricerca a ogni giro.
"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx
import psycopg

log = logging.getLogger("nivult.ats.registri")

_UA = "nivult-ats/1.0 (firmographics da registri pubblici; contact: ops@nivult.com)"


def _colonna_manca(c, tabella: str, colonna: str) -> bool:
    return c.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (tabella, colonna)).fetchone() is None


def _tabella_ce(c, tabella: str) -> bool:
    return c.execute("SELECT to_regclass(%s)",
                     (tabella,)).fetchone()[0] is not None


# ── nomi: nocciolo e guardia anti-omonimi ───────────────────────────
def _norm(s: str) -> str:
    s = re.sub(r"\b(srl|spa|s\.p\.a\.|gmbh|ag|bv|b\.v\.|inc|llc|ltd|sa|"
               r"s\.a\.|sas|sasu|oy|oyj|ab|as|asa|aps|a/s|plc|co|corp|"
               r"group|groupe|holding)\b\.?", " ", s.lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _combacia(nome: str, candidati: list[str],
              stretto: bool = False) -> bool:
    """stretto=True: solo uguaglianza del nocciolo. Serve dove si
    confronta contro un INDICE grande (EDGAR: 10k quotate) — con la
    sottostringa «Levertest» si prendeva il settore di «Lever» e
    «Redolentech» finiva in agricoltura. Meglio nessun match che uno
    inventato."""
    core = _norm(re.sub(r"^(careers?|jobs)\s+", "", nome,
                        flags=re.I))
    if len(core) < 4:
        return False
    for cand in candidati:
        cn = _norm(cand or "")
        if not cn:
            continue
        if cn == core:
            return True
        if not stretto and (core in cn or cn in core):
            return True
    return False


# ── NACE Rev.2, divisioni (prime 2 cifre) -> etichetta inglese ──────
_NACE = {
 "01": "Agriculture", "02": "Forestry", "03": "Fishing",
 "05": "Coal mining", "06": "Oil & gas extraction", "07": "Metal ores mining",
 "08": "Other mining", "09": "Mining support services",
 "10": "Food products", "11": "Beverages", "12": "Tobacco",
 "13": "Textiles", "14": "Wearing apparel", "15": "Leather",
 "16": "Wood products", "17": "Paper products", "18": "Printing",
 "19": "Refined petroleum", "20": "Chemicals", "21": "Pharmaceuticals",
 "22": "Rubber & plastic", "23": "Non-metallic minerals",
 "24": "Basic metals", "25": "Fabricated metal products",
 "26": "Electronics & optical products", "27": "Electrical equipment",
 "28": "Machinery & equipment", "29": "Motor vehicles",
 "30": "Other transport equipment", "31": "Furniture",
 "32": "Other manufacturing", "33": "Machinery repair & installation",
 "35": "Energy & utilities", "36": "Water supply", "37": "Sewerage",
 "38": "Waste management", "39": "Environmental remediation",
 "41": "Building construction", "42": "Civil engineering",
 "43": "Specialised construction",
 "45": "Vehicle trade & repair", "46": "Wholesale trade",
 "47": "Retail trade",
 "49": "Land transport", "50": "Water transport", "51": "Air transport",
 "52": "Warehousing & logistics", "53": "Postal & courier",
 "55": "Accommodation", "56": "Food & beverage service",
 "58": "Publishing", "59": "Film, TV & music", "60": "Broadcasting",
 "61": "Telecommunications", "62": "IT services & software",
 "63": "Information services",
 "64": "Financial services", "65": "Insurance",
 "66": "Auxiliary financial services",
 "68": "Real estate", "69": "Legal & accounting",
 "70": "Management consultancy", "71": "Architecture & engineering",
 "72": "Scientific R&D", "73": "Advertising & market research",
 "74": "Other professional services", "75": "Veterinary",
 "77": "Rental & leasing", "78": "Employment & staffing",
 "79": "Travel agencies", "80": "Security & investigation",
 "81": "Facility services & landscaping", "82": "Office & business support",
 "84": "Public administration", "85": "Education",
 "86": "Human health", "87": "Residential care", "88": "Social work",
 "90": "Arts & entertainment", "91": "Libraries & museums",
 "92": "Gambling", "93": "Sports & recreation",
 "94": "Membership organisations", "95": "Repair of personal goods",
 "96": "Other personal services", "97": "Household employers",
 "99": "Extraterritorial organisations",
}


def _nace(codice: str | None) -> str | None:
    if not codice:
        return None
    return _NACE.get(re.sub(r"[^0-9]", "", codice)[:2])


# ── INSEE: codice fascia -> (etichetta, punto medio) ────────────────
# La fascia e' il dato VERO; il punto medio e' la traduzione numerica
# per i filtri, e reg_source dice che viene da una fascia.
_TRANCHE = {
 "00": ("0", 0),        "01": ("1-2", 1),      "02": ("3-5", 4),
 "03": ("6-9", 7),      "11": ("10-19", 14),   "12": ("20-49", 34),
 "21": ("50-99", 74),   "22": ("100-199", 149), "31": ("200-249", 224),
 "32": ("250-499", 374), "41": ("500-999", 749),
 "42": ("1000-1999", 1499), "51": ("2000-4999", 3499),
 "52": ("5000-9999", 7499), "53": ("10000+", 15000),
}


# ── gli adattatori: nome -> (settore, dipendenti, fascia) o None ────
def _fr(cli: httpx.Client, nome: str):
    r = cli.get("https://recherche-entreprises.api.gouv.fr/search",
                params={"q": nome, "per_page": 10})
    r.raise_for_status()
    # fra le omonime vince l'entita' con PIU' dipendenti: i gruppi
    # francesi hanno decine di unita' legali col medesimo nome, e la
    # prima del motore di ricerca puo' essere una filiale minuscola
    # (visto: Bureau Veritas -> 14 dipendenti).
    migliore = None
    for ris in r.json().get("results", []):
        nomi = [ris.get("nom_complet"), ris.get("nom_raison_sociale"),
                ris.get("sigle")]
        if not _combacia(nome, [n for n in nomi if n]):
            continue
        fascia = _TRANCHE.get(ris.get("tranche_effectif_salarie") or "")
        cand = (_nace(ris.get("activite_principale")),
                fascia[1] if fascia else None,
                fascia[0] if fascia else None)
        if migliore is None or (cand[1] or -1) > (migliore[1] or -1):
            migliore = cand
    return migliore


def _no(cli: httpx.Client, nome: str):
    r = cli.get("https://data.brreg.no/enhetsregisteret/api/enheter",
                params={"navn": nome, "size": 3})
    r.raise_for_status()
    for ris in (r.json().get("_embedded") or {}).get("enheter", []):
        if not _combacia(nome, [ris.get("navn")]):
            continue
        cod = (ris.get("naeringskode1") or {}).get("kode")
        return (_nace(cod), ris.get("antallAnsatte"), None)
    return None


def _fi(cli: httpx.Client, nome: str):
    r = cli.get("https://avoindata.prh.fi/opendata-ytj-api/v3/companies",
                params={"name": nome, "page": 1})
    r.raise_for_status()
    for ris in r.json().get("companies", [])[:3]:
        nomi = [n.get("name") for n in ris.get("names", [])]
        if not _combacia(nome, nomi):
            continue
        mbl = ris.get("mainBusinessLine") or {}
        # il codice YTJ e' TOL 2008 = NACE con cifre in piu'
        return (_nace(mbl.get("type")), None, None)
    return None


def _dk(cli: httpx.Client, nome: str):
    r = cli.get("https://cvrapi.dk/api",
                params={"search": nome, "country": "dk"})
    if r.status_code != 200:
        return None
    ris = r.json()
    if not isinstance(ris, dict) or not _combacia(nome, [ris.get("name")]):
        return None
    # employees puo' essere un numero O una fascia testuale ("200-499")
    dip, fascia = ris.get("employees"), None
    if isinstance(dip, str):
        m = re.match(r"^(\d+)\s*-\s*(\d+)$", dip.strip())
        if m:
            fascia = dip.strip()
            dip = (int(m.group(1)) + int(m.group(2))) // 2
        else:
            dip = int(dip) if dip.strip().isdigit() else None
    return (ris.get("industrydesc"), dip, fascia)


class _Edgar:
    """SEC EDGAR: l'indice dei nomi si scarica UNA volta per giro
    (company_tickers.json, ~10k quotate), poi ogni match costa una
    chiamata a submissions/ per la sicDescription."""

    def __init__(self, cli: httpx.Client):
        self.cli = cli
        self.indice: list[tuple[str, str]] | None = None   # (nome, cik)

    def cerca(self, nome: str):
        if self.indice is None:
            r = self.cli.get("https://www.sec.gov/files/company_tickers.json")
            r.raise_for_status()
            self.indice = [(v["title"], str(v["cik_str"]).zfill(10))
                           for v in r.json().values()]
        core = _norm(nome)
        if len(core) < 4:
            return None
        for titolo, cik in self.indice:
            if _combacia(nome, [titolo], stretto=True):
                r = self.cli.get(
                    f"https://data.sec.gov/submissions/CIK{cik}.json")
                if r.status_code != 200:
                    return None
                return (r.json().get("sicDescription") or None, None, None)
        return None


_PAESI = {"FR": (_fr, 0.5), "NO": (_no, 1.0), "FI": (_fi, 1.0),
          "DK": (_dk, 2.0)}          # fonte, secondi di pausa fra chiamate


def prepara(c) -> None:
    if _colonna_manca(c, "ats_companies", "reg_checked_at"):
        c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS "
                  "industry_reg text")
        c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS "
                  "employees_reg int")
        c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS "
                  "employees_reg_band text")
        c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS "
                  "reg_source text")
        c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS "
                  "reg_checked_at timestamptz")


def arricchisci(dsn: str, limite: int = 1000,
                paesi: list[str] | None = None) -> dict:
    stats: dict = {"esaminate": 0, "trovate": 0, "settore": 0,
                   "dipendenti": 0, "errori": 0}
    cli = httpx.Client(timeout=25, headers={"User-Agent": _UA},
                       follow_redirects=True)
    edgar = _Edgar(cli)
    ammessi = set(paesi or list(_PAESI) + ["US"])
    with psycopg.connect(dsn, autocommit=True) as c:
        prepara(c)
        # il paese: quello dell'azienda, o quello dove pubblica di piu'
        con_geo = _tabella_ce(c, "azienda_paese")
        geo_sql = """coalesce(ac.country, (
              SELECT ap.country FROM azienda_paese ap
               WHERE ap.platform_id = ac.platform_id AND ap.slug = ac.slug
               ORDER BY ap.jobs DESC LIMIT 1))""" if con_geo \
            else "ac.country"
        righe = c.execute(f"""
            SELECT ac.platform_id, ac.slug, ac.company_name,
                   {geo_sql} AS paese
              FROM ats_companies ac
             WHERE ac.is_active AND ac.job_count > 0
               AND ac.company_name IS NOT NULL
               AND ac.reg_checked_at IS NULL
             ORDER BY ac.job_count DESC""").fetchall()
        for pid, slug, nome, paese in righe:
            if stats["esaminate"] >= limite:
                break
            if paese not in ammessi:
                continue
            stats["esaminate"] += 1
            esito, fonte = None, None
            try:
                if paese == "US":
                    esito, fonte = edgar.cerca(nome), "edgar"
                    time.sleep(0.3)
                else:
                    fn, pausa = _PAESI[paese]
                    esito, fonte = fn(cli, nome), {
                        "FR": "sirene", "NO": "brreg",
                        "FI": "prh", "DK": "cvr"}[paese]
                    time.sleep(pausa)
            except Exception as exc:                  # noqa: BLE001
                stats["errori"] += 1
                log.warning("%s %s/%s: %s", paese, pid, slug,
                            type(exc).__name__)
                time.sleep(3)
                continue      # errore di rete: NON si marca, si riprova
            settore = dip = fascia = None
            if esito:
                settore, dip, fascia = esito
            if settore or dip is not None:
                stats["trovate"] += 1
                stats["settore"] += 1 if settore else 0
                stats["dipendenti"] += 1 if dip is not None else 0
            c.execute("""UPDATE ats_companies
                            SET industry_reg = coalesce(%s, industry_reg),
                                employees_reg = coalesce(%s, employees_reg),
                                employees_reg_band = coalesce(%s, employees_reg_band),
                                reg_source = CASE WHEN %s::text IS NOT NULL
                                     OR %s::int IS NOT NULL THEN %s
                                     ELSE reg_source END,
                                reg_checked_at = now()
                          WHERE platform_id = %s AND slug = %s""",
                      (settore, dip, fascia, settore, dip, fonte,
                       pid, slug))
    log.info("registri: %s", stats)
    return stats


def settore_dal_mix(dsn: str) -> dict:
    """Il settore DERIVATO dal nostro stesso corpus: se >=60%% delle
    offerte attive di un'azienda sta in una famiglia professionale (con
    almeno 5 offerte), quella famiglia dice il mestiere dell'azienda.
    Campo suo (industry_mix), mai mescolato coi registri."""
    with psycopg.connect(dsn, autocommit=True) as c:
        if _colonna_manca(c, "ats_companies", "industry_mix"):
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT "
                      "EXISTS industry_mix text")
        n = c.execute("""
            WITH mix AS (
              SELECT j.platform_id, j.slug, jc.family,
                     count(*) AS n,
                     sum(count(*)) OVER (PARTITION BY j.platform_id,
                                                      j.slug) AS tot
                FROM ats_jobs j
                JOIN job_classifications jc ON jc.job_id = j.id
               WHERE j.expired_at IS NULL
               GROUP BY 1, 2, 3),
            dominante AS (
              SELECT DISTINCT ON (platform_id, slug)
                     platform_id, slug, family
                FROM mix
               WHERE n >= 5 AND n * 100 >= tot * 60
               ORDER BY platform_id, slug, n DESC)
            UPDATE ats_companies ac
               SET industry_mix = d.family
              FROM dominante d
             WHERE ac.platform_id = d.platform_id
               AND ac.slug = d.slug
               AND ac.industry_mix IS DISTINCT FROM d.family""").rowcount
    log.info("settore dal mix: %d aziende", n)
    return {"aggiornate": n}


def main() -> int:
    import argparse
    from .runner import ATS_DSN
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.registri_imprese")
    ap.add_argument("--limite", type=int, default=1000)
    ap.add_argument("--paesi", help="es. FR,NO (default: tutti)")
    ap.add_argument("--mix", action="store_true",
                    help="solo il settore derivato dal corpus")
    a = ap.parse_args()
    if a.mix:
        print(json.dumps(settore_dal_mix(ATS_DSN)))
        return 0
    paesi = a.paesi.upper().split(",") if a.paesi else None
    print(json.dumps(arricchisci(ATS_DSN, a.limite, paesi)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
