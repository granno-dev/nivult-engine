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
    s = re.sub(r"\b(srl|spa|s\.p\.a\.|gmbh|ag|bv|b\.v\.|inc|llc|ltd|limited|llp|sa|"
               r"s\.a\.|sas|sasu|oy|oyj|ab|as|asa|aps|a/s|plc|co|corp|"
               r"s\.r\.o\.|a\.s\.|spol|"
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


# ── la SCHEDA di registro (21/09/2026) ──────────────────────────────
# Oltre a settore e dipendenti i registri danno indirizzo della sede,
# forma giuridica, data di fondazione e (Francia) la categoria INSEE
# dell'impresa: PME / ETI / GE. Quest'ultima e' la cura al difetto visto il
# 21/09: la fascia SIRENE conta l'UNITA' LEGALE (Veolia Environnement SA:
# 1.000-1.999; Renault: 3-5) mentre il compratore vuole il gruppo. GE vuol
# dire 5.000+ dipendenti a livello d'impresa, ETI 250-4.999.
# Codici «nature juridique» INSEE piu' comuni -> etichetta leggibile
_NATURE_FR = {
    "1000": "Entrepreneur individuel", "5202": "SNC", "5410": "SARL", "5498": "SARL unipersonnelle (EURL)",
    "5499": "SARL", "5505": "SA à directoire", "5510": "SA", "5599": "SA à conseil d'administration",
    "5710": "SAS", "5720": "SASU", "5800": "Société européenne (SE)", "6220": "GIE",
    "6540": "SCI", "9210": "Association non déclarée", "9220": "Association déclarée",
    "3120": "Société commerciale étrangère immatriculée au RCS", "7210": "Commune",
    "4110": "EPIC", "5306": "SCS", "5370": "Société de participation financière de profession libérale",
}


def _scheda(**kv) -> dict:
    return {k: v for k, v in kv.items() if v not in (None, "", [])}


# ── gli adattatori: nome -> (settore, dipendenti, fascia, scheda) o None ─
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
        sede = ris.get("siege") or {}
        nat = str(ris.get("nature_juridique") or "") or None
        scheda = _scheda(
            legal_name=ris.get("nom_raison_sociale") or ris.get("nom_complet"),
            registro_id=ris.get("siren"), legal_form_code=nat,
            legal_form=_NATURE_FR.get(nat or ""),
            street=sede.get("adresse"), postal_code=sede.get("code_postal"),
            city=sede.get("libelle_commune"), country="FR",
            latitude=_num(sede.get("latitude")), longitude=_num(sede.get("longitude")),
            founded=ris.get("date_creation"), categoria=ris.get("categorie_entreprise"),
            status="active" if ris.get("etat_administratif") == "A" else None)
        cand = (_nace(ris.get("activite_principale")),
                fascia[1] if fascia else None,
                fascia[0] if fascia else None, scheda)
        if migliore is None or (cand[1] or -1) > (migliore[1] or -1):
            migliore = cand
    return migliore


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _no(cli: httpx.Client, nome: str):
    r = cli.get("https://data.brreg.no/enhetsregisteret/api/enheter",
                params={"navn": nome, "size": 3})
    r.raise_for_status()
    for ris in (r.json().get("_embedded") or {}).get("enheter", []):
        if not _combacia(nome, [ris.get("navn")]):
            continue
        cod = (ris.get("naeringskode1") or {}).get("kode")
        forma = ris.get("organisasjonsform") or {}
        ind = ris.get("forretningsadresse") or {}
        scheda = _scheda(
            legal_name=ris.get("navn"), registro_id=ris.get("organisasjonsnummer"),
            legal_form_code=forma.get("kode"), legal_form=forma.get("beskrivelse"),
            street=", ".join(ind.get("adresse") or []) or None, postal_code=ind.get("postnummer"),
            city=ind.get("poststed"), region=ind.get("kommune"), country=ind.get("landkode") or "NO",
            founded=ris.get("stiftelsesdato"), website=ris.get("hjemmeside"),
            status="inactive" if ris.get("konkurs") or ris.get("underAvvikling") else "active")
        return (_nace(cod), ris.get("antallAnsatte"), None, scheda)
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
        forma = forma_cod = None
        for f in ris.get("companyForms") or []:
            if f.get("endDate"):
                continue
            forma_cod = f.get("type")
            for d in f.get("descriptions") or []:
                if d.get("languageCode") == "3":          # 3 = inglese
                    forma = d.get("description")
            forma = forma or next((d.get("description") for d in f.get("descriptions") or []), None)
        via = cap = citta = None
        for a in ris.get("addresses") or []:
            if a.get("endDate"):
                continue
            via = a.get("street") or via
            cap = a.get("postCode") or cap
            citta = next((po.get("city") for po in a.get("postOffices") or [] if po.get("languageCode") in ("1", "3")), citta)
            if a.get("type") == 1:      # 1 = sede, 2 = postale
                break
        scheda = _scheda(legal_name=nomi[0] if nomi else None, registro_id=ris.get("businessId", {}).get("value") if isinstance(ris.get("businessId"), dict) else ris.get("businessId"),
                         legal_form_code=forma_cod, legal_form=forma, street=via, postal_code=cap, city=citta,
                         country="FI", founded=ris.get("registrationDate"), website=ris.get("website"))
        # il codice YTJ e' TOL 2008 = NACE con cifre in piu'
        return (_nace(mbl.get("type")), None, None, scheda)
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
    fondata = ris.get("startdate")
    if isinstance(fondata, str) and re.match(r"^\d{2}/\d{2}/\d{4}$", fondata):
        g, m_, a = fondata.split("/")
        fondata = f"{a}-{m_}-{g}"
    else:
        fondata = None
    scheda = _scheda(legal_name=ris.get("name"), registro_id=str(ris.get("vat") or "") or None,
                     legal_form=ris.get("companydesc"), street=ris.get("address"), postal_code=ris.get("zipcode"),
                     city=ris.get("city"), country="DK", founded=fondata,
                     status="inactive" if ris.get("enddate") else "active")
    return (ris.get("industrydesc"), dip, fascia, scheda)


# ── Regno Unito: Companies House (22/09/2026) ─────────────────────────
# API gratuita con chiave (account sviluppatore di Giuseppe), 600 chiamate
# ogni 5 minuti. La ricerca da' sede legale, tipo e data di costituzione; il
# dettaglio i codici SIC 2007, che nelle prime due cifre coincidono con le
# divisioni NACE Rev.2: la stessa mappa dei registri europei li traduce.
_TIPI_GB = {
    "ltd": "Private limited company", "plc": "Public limited company", "llp": "Limited liability partnership",
    "private-unlimited": "Private unlimited company", "private-limited-guarant-nsc": "Private company limited by guarantee",
    "private-limited-guarant-nsc-limited-exemption": "Private company limited by guarantee",
    "oversea-company": "Overseas company (UK establishment)", "limited-partnership": "Limited partnership",
    "scottish-partnership": "Scottish partnership", "royal-charter": "Royal charter company",
    "charitable-incorporated-organisation": "Charitable incorporated organisation",
    "registered-society-non-jurisdictional": "Registered society", "industrial-and-provident-society": "Industrial and provident society",
    "unregistered-company": "Unregistered company", "european-public-limited-liability-company-se": "Societas Europaea",
}


def _chiave_env(nome: str) -> str | None:
    import os
    v = os.environ.get(nome)
    if v:
        return v.strip()
    for f in ("/opt/nivult/engine/.env", "/opt/nivult/.env"):
        try:
            m = re.search(rf"^{nome}=(.*)$", open(f).read(), re.M)
            if m:
                return m.group(1).strip().strip('"')
        except OSError:
            pass
    return None


def _gb(cli: httpx.Client, nome: str):
    chiave = _chiave_env("COMPANIES_HOUSE_KEY")
    if not chiave:
        raise RuntimeError("COMPANIES_HOUSE_KEY mancante")
    auth = (chiave, "")
    r = cli.get("https://api.company-information.service.gov.uk/search/companies",
                params={"q": nome, "items_per_page": 5}, auth=auth)
    if r.status_code == 429:
        time.sleep(30)
        r = cli.get("https://api.company-information.service.gov.uk/search/companies",
                    params={"q": nome, "items_per_page": 5}, auth=auth)
    r.raise_for_status()
    for it in r.json().get("items", []):
        if it.get("company_status") not in (None, "active", "open"):
            continue
        if not _combacia(nome, [it.get("title")]):
            continue
        num = it.get("company_number")
        ind = it.get("address") or {}
        settore = None
        if num:
            time.sleep(0.6)
            d = cli.get(f"https://api.company-information.service.gov.uk/company/{num}", auth=auth)
            if d.status_code == 200:
                j = d.json()
                sic = (j.get("sic_codes") or [None])[0]
                settore = _nace(sic) if sic else None
                ind = j.get("registered_office_address") or ind
        via = ", ".join(x for x in (ind.get("premises"), ind.get("address_line_1"), ind.get("address_line_2")) if x) or None
        scheda = _scheda(legal_name=it.get("title"), registro_id=num, legal_form_code=it.get("company_type"),
                         legal_form=_TIPI_GB.get(it.get("company_type") or "", it.get("company_type")),
                         street=via, postal_code=ind.get("postal_code"), city=ind.get("locality"), region=ind.get("region"),
                         country="GB", founded=it.get("date_of_creation"),
                         status="active" if it.get("company_status") in ("active", "open") else it.get("company_status"))
        return (settore, None, None, scheda)
    return None


# ── Cechia: ARES (22/09/2026), senza chiave: sede, forma, fondazione, NACE ─
_FORME_CZ = {"121": "Akciová společnost (a.s.)", "112": "Společnost s ručením omezeným (s.r.o.)", "111": "Veřejná obchodní společnost",
             "113": "Komanditní společnost", "205": "Družstvo", "301": "Státní podnik", "421": "Odštěpný závod zahraniční osoby",
             "101": "Fyzická osoba podnikající", "141": "Obecně prospěšná společnost", "161": "Ústav", "706": "Spolek",
             "801": "Obec", "331": "Příspěvková organizace", "932": "Evropská společnost (SE)", "941": "Evropské hospodářské zájmové sdružení"}


def _cz(cli: httpx.Client, nome: str):
    r = cli.post("https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/vyhledat",
                 json={"obchodniJmeno": nome, "pocet": 5})
    r.raise_for_status()
    for e in r.json().get("ekonomickeSubjekty", []):
        if not _combacia(nome, [e.get("obchodniJmeno")]):
            continue
        sede = e.get("sidlo") or {}
        nace = next((c for c in (e.get("czNace2008") or e.get("czNace") or []) if c and c != "00"), None)
        forma = str(e.get("pravniForma") or "") or None
        via = " ".join(str(x) for x in (sede.get("nazevUlice"), sede.get("cisloDomovni")) if x) or sede.get("textovaAdresa")
        scheda = _scheda(legal_name=e.get("obchodniJmeno"), registro_id=e.get("ico"), legal_form_code=forma,
                         legal_form=_FORME_CZ.get(forma or ""), street=via, postal_code=str(sede.get("psc") or "") or None,
                         city=sede.get("nazevObce"), region=sede.get("nazevKraje"), country=sede.get("kodStatu") or "CZ",
                         founded=e.get("datumVzniku"), status="inactive" if e.get("datumZaniku") else "active")
        return (_nace(nace) if nace else None, None, None, scheda)
    return None


# ── Slovacchia: RPO (22/09/2026), senza chiave: sede e fondazione dalla
# ricerca, forma giuridica dal dettaglio ─────────────────────────────────
def _sk(cli: httpx.Client, nome: str):
    r = cli.get("https://api.statistics.sk/rpo/v1/search", params={"fullName": nome, "onlyActive": "true"})
    r.raise_for_status()
    for e in r.json().get("results", []):
        nomi = [n.get("value") for n in e.get("fullNames", []) if not n.get("validTo")]
        if not _combacia(nome, [n for n in nomi if n]):
            continue
        ind = next((a for a in e.get("addresses", []) if not a.get("validTo")), {})
        ico = next((i.get("value") for i in e.get("identifiers", []) if not i.get("validTo")), None)
        forma = forma_cod = settore = None
        try:
            time.sleep(0.4)
            d = cli.get(f"https://api.statistics.sk/rpo/v1/entity/{e.get('id')}")
            if d.status_code == 200:
                j = d.json()
                lf = next((f.get("value") or {} for f in j.get("legalForms", []) if not f.get("validTo")), {})
                forma, forma_cod = lf.get("value"), lf.get("code")
                act = next((a for a in j.get("activities", []) if not a.get("validTo")), {})
                settore = (act.get("economicActivityDescription") or act.get("value")) if isinstance(act, dict) else None
                if isinstance(settore, dict):
                    settore = settore.get("value")
        except Exception:                        # noqa: BLE001
            pass
        via = " ".join(str(x) for x in (ind.get("street"), ind.get("buildingNumber")) if x) or None
        scheda = _scheda(legal_name=nomi[0] if nomi else None, registro_id=ico, legal_form_code=forma_cod, legal_form=forma,
                         street=via, postal_code=(ind.get("postalCodes") or [None])[0],
                         city=(ind.get("municipality") or {}).get("value"), country="SK",
                         founded=e.get("establishment"), status="inactive" if e.get("termination") else "active")
        return (settore[:80] if isinstance(settore, str) else None, None, None, scheda)
    return None


# ── Belgio: KBO/BCE (22/09/2026) ─────────────────────────────────────
# Nessuna API: lo zip mensile scaricato da Giuseppe entra in `kbo_imprese`
# con scripts/kbo_carica.py. Qui si cerca per nome normalizzato nella
# tabella locale; la connessione la presta arricchisci() (_DB_LOCALE).
_DB_LOCALE = None


def _be(cli: httpx.Client, nome: str):
    c = _DB_LOCALE
    if c is None:
        return None
    chiave = _norm(nome)
    if len(chiave) < 3:
        return None
    try:
        righe = c.execute("""SELECT numero, stato, forma_codice, forma, inizio, nome, nomi, via, civico, cap, comune, nace
                               FROM kbo_imprese WHERE nome_norm = %s OR nome_norm LIKE %s LIMIT 20""",
                          (chiave, chiave + " %")).fetchall()
    except psycopg.errors.UndefinedTable:
        return None
    # fra le omonime vince l'attiva con NACE e sede; i nomi si confrontano
    # tutti (sociale, commerciale, abbreviazione)
    righe.sort(key=lambda r: (r[1] != "AC", r[11] is None, r[7] is None))
    for numero, stato, fcod, forma, inizio, nome_reg, nomi, via, civico, cap, comune, nace in righe:
        if not _combacia(nome, [nome_reg] + [x for x in (nomi or "").split(" | ") if x]):
            continue
        scheda = _scheda(legal_name=nome_reg, registro_id=numero, legal_form_code=fcod, legal_form=forma,
                         street=" ".join(x for x in (via, civico) if x) or None, postal_code=cap, city=comune,
                         country="BE", founded=inizio.isoformat() if inizio else None,
                         status="active" if stato == "AC" else "inactive")
        return (_nace(nace) if nace else None, None, None, scheda)
    return None


# ── Canada: Corporations Canada (22/09/2026), tabella locale ca_imprese
# caricata da scripts/ca_carica.py (CSV federali aperti, ogni giorno). Solo
# le societa' FEDERALI: le provinciali (Ontario, Québec) non ci sono.
_LEGGI_CA = {"Canada Business Corporations Act": "Business corporation (CBCA)",
             "Canada Not-for-profit Corporations Act": "Not-for-profit corporation",
             "Canada Cooperatives Act": "Cooperative", "Boards of Trade Act - Part II": "Board of trade",
             "Canada Corporations Act - Part II": "Corporation (CCA Part II)"}


def _norm_ca(s: str) -> str:
    s = re.sub(r"\b(inc|incorporated|ltd|limited|ltee|ltée|corp|corporation|co|company|llc|llp|"
               r"group|groupe|holding|holdings|canada)\b\.?", " ", s.lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _ca(cli: httpx.Client, nome: str):
    c = _DB_LOCALE
    if c is None:
        return None
    chiave = _norm_ca(nome)
    if len(chiave) < 3:
        return None
    try:
        righe = c.execute("""SELECT numero, nome, nome2, legge, stato, anniversario, via, citta, provincia, paese, cap
                               FROM ca_imprese WHERE nome_norm = %s OR nome_norm LIKE %s LIMIT 20""",
                          (chiave, chiave + " %")).fetchall()
    except psycopg.errors.UndefinedTable:
        return None
    righe.sort(key=lambda r: (r[4] != "Active", r[6] is None))
    for numero, nome_reg, nome2, legge, stato, ann, via, citta, prov, paese, cap in righe:
        if not _combacia(nome, [x for x in (nome_reg, nome2) if x]):
            continue
        scheda = _scheda(legal_name=nome_reg, registro_id=numero, legal_form_code=legge,
                         legal_form=_LEGGI_CA.get(legge or "", legge), street=via or None, postal_code=cap,
                         city=citta, region=prov, country=paese or "CA",
                         founded=ann.isoformat() if ann else None,
                         status="active" if stato == "Active" else "inactive")
        return (None, None, None, scheda)
    return None


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
                j = r.json()
                ind = (j.get("addresses") or {}).get("business") or {}
                scheda = _scheda(legal_name=j.get("name"), registro_id=cik, website=j.get("website") or None,
                                 street=", ".join(x for x in (ind.get("street1"), ind.get("street2")) if x) or None,
                                 city=ind.get("city"), region=ind.get("stateOrCountry"), postal_code=ind.get("zipCode"),
                                 country="US" if len(ind.get("stateOrCountry") or "") == 2 and (ind.get("stateOrCountry") or "").isupper() else None,
                                 legal_form=j.get("entityType"))
                return (j.get("sicDescription") or None, None, None, scheda)
        return None


_PAESI = {"FR": (_fr, 0.5), "NO": (_no, 1.0), "FI": (_fi, 1.0),
          "DK": (_dk, 2.0), "GB": (_gb, 1.0), "CZ": (_cz, 0.5),
          "SK": (_sk, 0.5), "BE": (_be, 0.0), "CA": (_ca, 0.0)}   # fonte, secondi di pausa
_FONTI = {"FR": "sirene", "NO": "brreg", "FI": "prh", "DK": "cvr",
          "GB": "companies_house", "CZ": "ares", "SK": "rpo", "BE": "kbo", "CA": "corporations_canada"}


DDL_REGISTRO = """
CREATE TABLE IF NOT EXISTS aziende_registro (
  company_id      uuid NOT NULL REFERENCES ats_companies(id) ON DELETE CASCADE,
  fonte           text NOT NULL,      -- sirene / brreg / prh / cvr / edgar / gleif
  legal_name      text,
  registro_id     text,               -- SIREN, org.nr, Y-tunnus, CVR, CIK, LEI
  legal_form_code text,
  legal_form      text,
  street          text,
  postal_code     text,
  city            text,
  region          text,
  country         text,
  latitude        real,
  longitude       real,
  founded         date,
  website         text,
  categoria       text,               -- INSEE: PME / ETI / GE (solo Francia)
  status          text,
  fetched_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (company_id, fonte));
"""
SQL_SCHEDA = """
INSERT INTO aziende_registro (company_id, fonte, legal_name, registro_id, legal_form_code, legal_form, street, postal_code,
                              city, region, country, latitude, longitude, founded, website, categoria, status)
VALUES (%(company_id)s, %(fonte)s, %(legal_name)s, %(registro_id)s, %(legal_form_code)s, %(legal_form)s, %(street)s,
        %(postal_code)s, %(city)s, %(region)s, %(country)s, %(latitude)s, %(longitude)s, %(founded)s, %(website)s,
        %(categoria)s, %(status)s)
ON CONFLICT (company_id, fonte) DO UPDATE SET
  legal_name = EXCLUDED.legal_name, registro_id = EXCLUDED.registro_id, legal_form_code = EXCLUDED.legal_form_code,
  legal_form = EXCLUDED.legal_form, street = EXCLUDED.street, postal_code = EXCLUDED.postal_code, city = EXCLUDED.city,
  region = EXCLUDED.region, country = EXCLUDED.country, latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
  founded = EXCLUDED.founded, website = EXCLUDED.website, categoria = EXCLUDED.categoria, status = EXCLUDED.status,
  fetched_at = now()
"""
_CAMPI_SCHEDA = ("legal_name", "registro_id", "legal_form_code", "legal_form", "street", "postal_code", "city",
                 "region", "country", "latitude", "longitude", "founded", "website", "categoria", "status")


def scrivi_scheda(c, company_id, fonte: str, scheda: dict | None) -> bool:
    """Una riga in aziende_registro, se la scheda dice qualcosa. La data di
    fondazione arriva in forme diverse (2026-01-01, 01/01/2026 gia' girata,
    o solo l'anno): si scrive solo se e' una data."""
    if not scheda:
        return False
    riga = {k: scheda.get(k) for k in _CAMPI_SCHEDA}
    # i registri incartano alcuni valori (PRH: website = {"url": ...}):
    # un dict o una lista non entrano in una colonna di testo
    for k, v in list(riga.items()):
        if isinstance(v, dict):
            riga[k] = next((v[x] for x in ("url", "value", "name", "description") if isinstance(v.get(x), str)), None)
        elif isinstance(v, (list, tuple)):
            riga[k] = next((x for x in v if isinstance(x, str)), None)
    f = riga.get("founded")
    if isinstance(f, str):
        f = f.strip()[:10]
        if re.match(r"^\d{4}$", f):
            f += "-01-01"
        riga["founded"] = f if re.match(r"^\d{4}-\d{2}-\d{2}$", f) else None
    else:
        riga["founded"] = None
    for k in ("street", "city", "legal_name", "legal_form", "website"):
        if isinstance(riga.get(k), str):
            riga[k] = riga[k].strip()[:300] or None
    if not any(riga.get(k) for k in ("street", "city", "legal_form", "founded", "categoria")):
        return False
    c.execute(SQL_SCHEDA, {"company_id": company_id, "fonte": fonte, **riga})
    return True


def prepara(c) -> None:
    if not _tabella_ce(c, "aziende_registro"):
        c.execute("SET lock_timeout = '10s'")
        c.execute(DDL_REGISTRO)
        c.execute("RESET lock_timeout")
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
                paesi: list[str] | None = None, rifai: bool = False) -> dict:
    """`rifai`: ripassa le aziende GIA' trovate in un registro ma senza
    scheda (indirizzo, forma giuridica): quelle lette prima del 21/09."""
    stats: dict = {"esaminate": 0, "trovate": 0, "settore": 0,
                   "dipendenti": 0, "schede": 0, "errori": 0}
    cli = httpx.Client(timeout=25, headers={"User-Agent": _UA},
                       follow_redirects=True)
    edgar = _Edgar(cli)
    ammessi = set(paesi or list(_PAESI) + ["US"])
    global _DB_LOCALE
    with psycopg.connect(dsn, autocommit=True) as c:
        prepara(c)
        _DB_LOCALE = c            # per i registri caricati in tabella (BE, CA)
        # il paese: quello dell'azienda, o quello dove pubblica di piu'
        con_geo = _tabella_ce(c, "azienda_paese")
        geo_sql = """coalesce(ac.country, (
              SELECT ap.country FROM azienda_paese ap
               WHERE ap.platform_id = ac.platform_id AND ap.slug = ac.slug
               ORDER BY ap.jobs DESC LIMIT 1))""" if con_geo \
            else "ac.country"
        condizione = ("ac.reg_source IS NOT NULL AND NOT EXISTS (SELECT 1 FROM aziende_registro r WHERE r.company_id = ac.id AND r.fonte = ac.reg_source)"
                      if rifai else "ac.reg_checked_at IS NULL")
        righe = c.execute(f"""
            SELECT ac.platform_id, ac.slug, ac.company_name,
                   {geo_sql} AS paese, ac.id
              FROM ats_companies ac
             WHERE ac.is_active AND ac.job_count > 0
               AND ac.company_name IS NOT NULL
               AND {condizione}
             ORDER BY ac.job_count DESC""").fetchall()
        for pid, slug, nome, paese, cid in righe:
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
                    esito, fonte = fn(cli, nome), _FONTI[paese]
                    time.sleep(pausa)
            except Exception as exc:                  # noqa: BLE001
                stats["errori"] += 1
                log.warning("%s %s/%s: %s", paese, pid, slug,
                            type(exc).__name__)
                time.sleep(3)
                continue      # errore di rete: NON si marca, si riprova
            settore = dip = fascia = scheda = None
            if esito:
                settore, dip, fascia, scheda = (tuple(esito) + (None,))[:4]
            if settore or dip is not None:
                stats["trovate"] += 1
                stats["settore"] += 1 if settore else 0
                stats["dipendenti"] += 1 if dip is not None else 0
            if scrivi_scheda(c, cid, fonte, scheda):
                stats["schede"] += 1
            if rifai:
                continue          # settore e dipendenti restano quelli di prima
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
    ap.add_argument("--rifai", action="store_true",
                    help="scheda di registro (indirizzo, forma) per le aziende gia' trovate")
    ap.add_argument("--mix", action="store_true",
                    help="solo il settore derivato dal corpus")
    a = ap.parse_args()
    if a.mix:
        print(json.dumps(settore_dal_mix(ATS_DSN)))
        return 0
    paesi = a.paesi.upper().split(",") if a.paesi else None
    print(json.dumps(arricchisci(ATS_DSN, a.limite, paesi, a.rifai)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
