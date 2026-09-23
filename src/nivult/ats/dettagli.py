"""I dettagli dell'annuncio che il mercato vende e noi non avevamo (21/09/2026).

Confrontato il dizionario dei dati di Coresignal con le nostre colonne: sui
campi dell'annuncio eravamo alla pari o davanti (famiglia, seniority,
contratto, remoto, lingue, salario, tecnologie PER annuncio), ma mancavano
una dozzina di campi che loro espongono e che qui si ricavano SENZA modelli,
a costo di CPU, da due fonti:

  1. il DICHIARATO nel raw della piattaforma (scadenza, stato/regione, CAP,
     turni, ore, benefit, recruiter, coordinate): esatto per costruzione;
  2. il TESTO, con regole scritte e leggibili (urgenza, turni, benefit,
     livello di gestione): niente da addestrare, niente da esaminare con
     un giudice, ma vanno tenute per quello che sono — regole.

Piu' la geografia offline (GeoNames, gia' in geografia.py): stato/regione e
coordinate dalla citta'. Tutto finisce in `offerte_dettagli` (una riga per
offerta) e `ats_jobs.dettagli_at` dice che l'offerta e' stata vista. Non
tocca ne' la scheda del N5 ne' i demoni: gira su Hetzner, a lotti, con nice.

    python -m nivult.ats.dettagli [--limite N] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import date

import psycopg

log = logging.getLogger("nivult.ats.dettagli")

DDL = """
CREATE TABLE IF NOT EXISTS offerte_dettagli (
  job_id              uuid PRIMARY KEY REFERENCES ats_jobs(id) ON DELETE CASCADE,
  management_level    text,        -- executive / manager / individual_contributor
  is_decision_maker   boolean,
  shift_schedule      text,        -- day / night / rotating / weekend / flexible
  shift_testo         text,        -- la frase o il campo da cui viene
  work_hours          text,        -- ore dichiarate ("37.5", "32-36", "9am-5pm")
  is_urgently_hiring  boolean,
  benefits            jsonb,       -- etichette canoniche trovate
  benefits_dichiarati text,        -- il campo benefit della piattaforma, com'e'
  salary_text         text,        -- la frase del testo con la cifra
  valid_through       date,
  state               text,        -- stato / regione / provincia
  postal_code         text,
  latitude            real,
  longitude           real,
  geo_da              text,        -- dichiarato / geonames
  applicants_count    integer,
  is_easy_apply       boolean,
  recruiter           jsonb,       -- {nome, email, profilo}
  calcolato_at        timestamptz NOT NULL DEFAULT now());
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS dettagli_at timestamptz;
CREATE INDEX IF NOT EXISTS ats_jobs_dettagli_idx ON ats_jobs (dettagli_at) WHERE dettagli_at IS NULL AND expired_at IS NULL;
"""

CAMPI_TESTO = ("description", "content", "descriptionHtml", "descriptionPlain", "externalDescription",
               "jobDescription", "job_description", "Job_Description", "body", "content_html",
               "description_html", "descriptionBody", "text", "ShortDescriptionStr")
_TAG = re.compile(r"<[^>]+>")

# ── le chiavi del raw, per famiglia di campo (misurate sul 21/09 su un campione dell'1%) ──
CHIAVI = {
    "valid_through": ("validThrough", "valid_through", "application_deadline", "postingExpirationDate",
                      "deadline_at", "deadline", "expires", "expiration_date", "expirationDate",
                      "closingDate", "closing_date", "dateExpires", "endDate", "end_date"),
    "state": ("state", "state_name", "State", "Address_State", "Address_Region", "region", "Region",
              "province", "addressRegion", "state_code", "State-City"),
    "postal_code": ("postal_code", "postalCode", "PostalCode", "Address_PostalCode", "zip", "zipcode", "zip_code"),
    "shift": ("Shift", "Shifts", "JobShift", "Shift Schedule", "shift", "shift_schedule"),
    "schedule": ("Schedule", "JobSchedule", "Position Schedule", "positionScheduleCodes", "working_hours_type",
                 "schedule", "workSchedule"),
    "hours": ("WorkHours", "workHours", "Work Hours", "Hours", "Standard Hours", "work_hours", "hours",
              "min_hours_per_week", "max_hours_per_week", "min_hours", "max_hours", "hoursPerWeek"),
    "benefits": ("benefits", "jobBenefits", "benefits_header", "perks", "Benefits"),
    "recruiter": ("creator", "recruiter", "contact", "application_contacts", "hiringManager", "hiring_manager",
                  "recruiterName", "contact_person", "contactPerson"),
    "applicants": ("applicantCount", "applicants_count", "applicantsCount", "numberOfApplicants", "applicants"),
    "easy_apply": ("isEasyApply", "easyApply", "easy_apply", "is_easy_apply", "quickApply"),
}

# ── le regole sul testo (le lingue del magazzino: en it de fr es nl sv pt pl) ──
URGENTE = re.compile(
    r"(?i)\b(urgent(ly)?( hiring| need| requirement)?|immediate (start|hire|opening)|start (immediately|asap)|asap|"
    r"hiring now|apply today, start tomorrow|ricerca urgente|urgente|inserimento immediato|"
    r"dringend|ab sofort|sofortige[rn]? (einstieg|start)|schnellstm[oö]glich|"
    r"urgent|d[eè]s que possible|prise de poste imm[eé]diate|"
    r"incorporaci[oó]n inmediata|urge|"
    r"per direct|zo snel mogelijk|omg[aå]ende|snarast|"
    r"in[ií]cio imediato|pilnie|od zaraz)\b")

TURNI = (
    ("night", re.compile(r"(?i)\b(night ?shifts?|nights|notturn[oi]|turno di notte|nachtschicht|nachtdienst|de nuit|"
                         r"turno de noche|nachtdienst|nattskift|noturno|nocn[ay])\b")),
    ("rotating", re.compile(r"(?i)\b(rotating( shifts?)?|rotational|shift rotation|[23] ?x ?8|3-shift|three shifts?|"
                            r"turni a rotazione|su tre turni|ciclo continuo|wechselschicht|schichtdienst|3-schicht|"
                            r"en 3x8|en 2x8|horaires? post[eé]s?|turnos rotativos|a turnos|ploegendienst|"
                            r"skiftarbete|escala de revezamento|system zmianowy)\b")),
    ("weekend", re.compile(r"(?i)\b(weekends? (required|shifts?|work)|work(ing)? weekends|saturdays? and sundays?|"
                           r"fine settimana|nei weekend|wochenendarbeit|am wochenende|le week-?end|fines de semana|"
                           r"weekendwerk|helger|fins de semana)\b")),
    ("day", re.compile(r"(?i)\b(day ?shifts?|daytime|first shift|1st shift|turno diurno|di giorno|tagschicht|"
                       r"tagesschicht|de jour|turno de d[ií]a|dagdienst|dagtid|diurno)\b")),
    ("flexible", re.compile(r"(?i)\b(flexible (hours|schedule|working hours|shifts?)|flextime|flexi-?time|"
                            r"orario flessibile|gleitzeit|flexible arbeitszeit(en)?|horaires? flexibles?|"
                            r"horario flexible|flexibele (uren|werktijden)|flexibla arbetstider|hor[aá]rio flex[ií]vel)\b")),
)
ORE = re.compile(r"(?i)\b(\d{1,2}(?:[.,]\d)?\s?(?:-|–|to|a|bis|à|tot)\s?\d{1,2}(?:[.,]\d)?\s?(?:hours?|hrs|ore|stunden|heures|horas|uur|timmar)(?: per| a| pro| par| por| per)? ?(?:week|settimana|woche|semaine|semana|vecka)?|"
                 r"\d{2}(?:[.,]\d)?\s?(?:hours?|hrs|ore|stunden|heures|horas|uur|timmar)\s?(?:per|a|pro|par|por|/)\s?(?:week|settimana|woche|semaine|semana|vecka|wk)|"
                 r"\d{1,2}(?::\d{2})?\s?(?:am|pm|h)?\s?(?:-|–|to|à|bis|a)\s?\d{1,2}(?::\d{2})?\s?(?:am|pm|h|uhr))\b")

# benefit: (etichetta canonica, espressione). Etichette in inglese perche' sono un vocabolario, non una traduzione.
BENEFIT = (
    ("health_insurance", r"health (insurance|coverage|benefits?)|medical(,| and| &)? dental|dental|vision (insurance|coverage)|"
                         r"assicurazione sanitaria|polizza sanitaria|mutuelle|krankenversicherung|betriebliche krankenversicherung|"
                         r"seguro m[eé]dico|plano de sa[uú]de|zorgverzekering|sjukv[aå]rdsf[oö]rs[aä]kring|private health"),
    ("pension", r"401\s?\(?k\)?|403\s?\(?b\)?|pension( scheme| plan| contribution)?|retirement (plan|savings)|"
                r"fondo pensione|previdenza|betriebliche altersvorsorge|altersvorsorge|retraite|plan de pensiones|"
                r"pensioen|tj[aä]nstepension|previd[eê]ncia|superannuation"),
    ("equity", r"\b(equity|stock options?|RSUs?|ESPP|share (scheme|options?)|azioni|mitarbeiteraktien|BSPCE|"
               r"participation aux b[eé]n[eé]fices|intéressement|int[eé]ressement)\b"),
    ("bonus", r"\b(bonus(es)?|performance(-| )based (bonus|pay)|commission|provvigioni|premio (di )?(produzione|risultato)|"
              r"pr[aä]mie|prime(s)? (annuelle|sur objectifs)|13(th|ème|°|\.)? (month|mese|mois|gehalt|monatsgehalt)|"
              r"tredicesima|quattordicesima|urlaubs- und weihnachtsgeld|weihnachtsgeld|13º sal[aá]rio)\b"),
    ("paid_time_off", r"\b(paid (time off|vacation|leave|holidays?)|PTO|unlimited (pto|vacation|time off)|"
                      r"\d{2} (days|giorni|tage|jours|d[ií]as|dagen|dagar) (of )?(annual leave|vacation|holiday|ferie|urlaub|cong[eé]s|vacaciones|vakantie|semester)|"
                      r"ferie retribuite|urlaubstage|cong[eé]s pay[eé]s|vacaciones pagadas)\b"),
    ("parental_leave", r"\b(parental leave|maternity leave|paternity leave|cong[eé] (maternit[eé]|parental)|"
                       r"congedo parentale|elternzeit|licencia (de )?(maternidad|paternidad)|ouderschapsverlof|f[oö]r[aä]ldraledighet)\b"),
    ("remote_allowance", r"\b(home ?office (allowance|budget|stipend)|work[- ]from[- ]home (allowance|stipend|budget)|"
                         r"remote (work )?(allowance|stipend)|buono (smart working|lavoro agile)|"
                         r"homeoffice(-| )pauschale|indemnit[eé] (de )?t[eé]l[eé]travail)\b"),
    ("meal_vouchers", r"\b(meal (vouchers?|allowance|card)|lunch (vouchers?|allowance)|free (lunch|meals|food)|"
                      r"buoni pasto|ticket restaurant|tickets? restaurant|titres?-restaurant|essenszuschuss|"
                      r"cheques? (de )?comida|vale[- ](refei[cç][aã]o|alimenta[cç][aã]o)|maaltijdcheques?|lunchf[oö]rm[aå]n)\b"),
    ("transport", r"\b(commuter (benefits?|allowance)|travel (allowance|card|pass)|company car|auto aziendale|"
                  r"firmenwagen|dienstwagen|jobticket|deutschlandticket|bike ?leasing|jobrad|cycle ?to ?work|cycle2work|"
                  r"prise en charge (des )?transports?|navigo|ayuda (de )?transporte|reiskostenvergoeding|"
                  r"leaseauto|f[oö]rm[aå]nsbil|vale[- ]transporte)\b"),
    ("training", r"\b(training (budget|allowance|programs?)|learning (budget|stipend|and development)|L&D budget|"
                 r"tuition (reimbursement|assistance)|student loan (repayment|refinancing)|formazione (continua|professionale)|"
                 r"weiterbildung|fortbildung|formation (continue|professionnelle)|formaci[oó]n continua|opleidingsbudget|"
                 r"kompetensutveckling|reembolso de (matr[ií]cula|estudos))\b"),
    ("wellness", r"\b(gym (membership|discount|allowance)|wellness (program|stipend|budget)|fitness (membership|allowance)|"
                 r"employee assistance program(me)?|EAP|mental health (support|benefits?)|palestra|"
                 r"egym|wellpass|urban sports club|gympass|wellhub|classpass|betriebssport|"
                 r"salle de sport|friskv[aå]rdsbidrag|vale[- ]academia)\b"),
    ("life_insurance", r"\b(life insurance|life assurance|disability (insurance|coverage)|AD&D|assicurazione vita|"
                       r"lebensversicherung|berufsunf[aä]higkeitsversicherung|pr[eé]voyance|seguro de vida|levensverzekering|"
                       r"livf[oö]rs[aä]kring)\b"),
    ("relocation", r"\b(relocation (package|assistance|support|bonus)|visa sponsorship|sponsored (work )?visa|"
                   r"umzugskosten|aide [aà] la mobilit[eé]|ayuda (de )?reubicaci[oó]n|verhuisvergoeding|flyttbidrag)\b"),
    ("childcare", r"\b(child ?care( support| benefits?| assistance)?|daycare|nursery (support|benefits?)|"
                  r"asilo nido|kita-?zuschuss|kinderbetreuung|cr[eè]che|guarder[ií]a|kinderopvang|barnomsorg|aux[ií]lio[- ]creche)\b"),
    ("employee_discount", r"\b(employee discounts?|staff discounts?|sconti (per i )?dipendenti|mitarbeiterrabatte?|corporate benefits|"
                          r"personalrabatt|remise (sur vos achats|personnel)|descuentos? (para )?empleados|personeelskorting)\b"),
)
BENEFIT_RE = [(k, re.compile(r"(?i)(" + p + ")")) for k, p in BENEFIT]

DIRIGENTE = re.compile(r"(?i)\b(chief \w+ officer|c[eftom]o|vp|vice[- ]president|president|director|direttore|direttrice|"
                       r"directeur|directrice|direktor|geschäftsführer|geschaeftsfuehrer|head of|responsabile (di )?(direzione|divisione)|"
                       r"managing director|general manager|partner|founder|co-?founder|amministratore|dirigente|"
                       r"gerente general|directeur g[eé]n[eé]ral|leiter(in)? (der|des)|bereichsleiter)\b")
MANAGER = re.compile(r"(?i)\b(manager|managerin|team ?lead(er)?|supervisor|coordinator|coordinatore|coordinatrice|"
                     r"responsabile|capo|chef d[e'] ?[eé]quipe|responsable|teamleiter|teamleitung|projektleiter|"
                     r"project ?lead|superintendent|foreman|capo ?squadra|capo ?reparto|store manager|encargado|"
                     r"gerente|leiter|teamleider|chef|arbetsledare)\b")
DECISORE = re.compile(r"(?i)\b(budget (ownership|responsibility|authority)|p&l|own(s|ing)? the (budget|p&l)|hiring (authority|decisions)|"
                      r"responsabilit[aà] di budget|conto economico|budgetverantwortung|responsabilit[eé] (budg[eé]taire|du p&l)|"
                      r"responsabilidad (de )?presupuesto|final say|decision[- ]maker|approva(l)? (authority|rights))\b")

SALARIO = re.compile(r"(?i)(?:[$€£¥]|\b(?:usd|eur|gbp|chf|sek|nok|dkk|pln|czk|huf|cad|aud|inr|brl|mxn)\b)[^.\n]{0,40}?\d[\d.,\s]{2,}|"
                     r"\d[\d.,\s]{2,}\s?(?:[$€£]|\b(?:usd|eur|gbp|chf|sek|nok|dkk|pln|czk|k)\b)")
PAROLE_PAGA = re.compile(r"(?i)\b(salary|salaire|stipendio|retribuzione|ral|gehalt|verg[uü]tung|lohn|sueldo|salario|"
                         r"sal[aá]rio|salaris|l[oö]n|pay|wage|compensation|hourly|per hour|/hr|per year|annual|"
                         r"brut|lordo|brutto|bruto|netto|per annum|p\.a\.|otel?)\b")


def pulito(t) -> str:
    t = t if isinstance(t, str) else (json.dumps(t, ensure_ascii=False) if t is not None else "")
    import html as _html
    t = _html.unescape(_html.unescape(t))
    return re.sub(r"\s+", " ", _TAG.sub(" ", t).replace("\xa0", " ")).strip()


def _cerca(raw: dict, chiavi: tuple[str, ...]):
    """La prima chiave presente, al primo livello o dentro un oggetto annidato."""
    if not isinstance(raw, dict):
        return None
    for k in chiavi:
        if k in raw and raw[k] not in (None, "", [], {}):
            return raw[k]
    basso = {k.lower(): v for k, v in raw.items()}
    for k in chiavi:
        v = basso.get(k.lower())
        if v not in (None, "", [], {}):
            return v
    for v in raw.values():
        if isinstance(v, dict):
            for k in chiavi:
                if k in v and v[k] not in (None, "", [], {}):
                    return v[k]
    return None


def _testo(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (int, float, bool)):
        return str(v)
    if isinstance(v, dict):
        for k in ("name", "label", "text", "value", "title", "code"):
            if v.get(k):
                return _testo(v[k])
        return ""
    if isinstance(v, list):
        return ", ".join(x for x in (_testo(y) for y in v) if x)[:300]
    return str(v)


def _data(v) -> date | None:
    s = _testo(v)[:30]
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d if 2000 < d.year < 2100 else None
        except ValueError:
            return None
    m = re.search(r"(\d{2})[/.](\d{2})[/.](\d{4})", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def _recruiter(v, email: str | None) -> dict | None:
    out: dict = {}
    if isinstance(v, dict):
        nome = v.get("name") or v.get("fullName") or " ".join(x for x in (v.get("firstName"), v.get("lastName")) if x)
        if nome:
            out["nome"] = str(nome)[:120]
        if v.get("email"):
            out["email"] = str(v["email"])[:120]
        for k in ("profileUrl", "profile_url", "url", "linkedin"):
            if v.get(k):
                out["profilo"] = str(v[k])[:200]
                break
    elif isinstance(v, list) and v:
        return _recruiter(v[0], email)
    elif isinstance(v, str) and v.strip() and "@" not in v:
        out["nome"] = v.strip()[:120]
    if email and "email" not in out:
        out["email"] = email
    return out or None


def _shift(dichiarato: str, testo: str) -> tuple[str | None, str | None]:
    base = dichiarato or ""
    for etichetta, patt in TURNI:
        m = patt.search(base)
        if m:
            return etichetta, base[:160]
    for etichetta, patt in TURNI:
        m = patt.search(testo)
        if m:
            a = max(0, m.start() - 60)
            return etichetta, testo[a:m.end() + 60].strip()
    return (None, base[:160] or None)


def _livello(titolo: str, testo: str, seniority: str | None) -> tuple[str, bool]:
    t = titolo or ""
    if seniority == "head" or DIRIGENTE.search(t):
        return "executive", True
    decisore = bool(DECISORE.search(testo))
    if seniority == "lead" or MANAGER.search(t):
        return "manager", decisore
    return "individual_contributor", decisore


def _salary_text(testo: str) -> str | None:
    for m in SALARIO.finditer(testo):
        a, b = max(0, m.start() - 80), min(len(testo), m.end() + 80)
        pezzo = testo[a:b]
        if PAROLE_PAGA.search(pezzo):
            # la frase intera che lo contiene
            ini = testo.rfind(". ", 0, m.start()) + 2
            fin = testo.find(". ", m.end())
            fin = len(testo) if fin < 0 else fin + 1
            return testo[max(ini, m.start() - 200):min(fin, m.end() + 200)].strip()[:400]
    return None


_geo = None


def _geo_citta():
    """GeoNames offline (geonamescache): citta' -> (lat, lon, admin1, paese). La piu' popolosa vince."""
    global _geo
    if _geo is not None:
        return _geo
    try:
        import geonamescache
        from nivult.ats.geografia import _piatto
        gc = geonamescache.GeonamesCache()
        tab: dict = {}
        for c in gc.get_cities().values():
            k = (_piatto(c["name"]), c["countrycode"])
            pop = c.get("population", 0) or 0
            if k not in tab or pop > tab[k][3]:
                tab[k] = (float(c["latitude"]), float(c["longitude"]), c.get("admin1code") or None, pop)
            for alt in (c.get("alternatenames") or [])[:20]:
                ka = (_piatto(alt), c["countrycode"])
                if ka not in tab:
                    tab[ka] = (float(c["latitude"]), float(c["longitude"]), c.get("admin1code") or None, pop)
        _geo = (tab, _piatto)
    except Exception as e:                                        # noqa: BLE001
        log.warning("geonames non disponibile: %s", e)
        _geo = ({}, lambda s: s)
    return _geo


def _coordinate(citta: str | None, paese: str | None):
    if not citta or not paese:
        return None
    tab, piatto = _geo_citta()
    k = (piatto(citta.split(",")[0]), paese.upper())
    v = tab.get(k)
    return (v[0], v[1], v[2]) if v else None


def dettagli(riga) -> dict:
    (jid, titolo, raw, citta, paese, seniority, contact_email, grezzo) = riga
    raw = raw if isinstance(raw, dict) else {}
    testo = pulito(grezzo)
    out: dict = {"job_id": jid}
    out["management_level"], out["is_decision_maker"] = _livello(titolo or "", testo, seniority)
    out["is_urgently_hiring"] = bool(URGENTE.search(testo)) or bool(_cerca(raw, ("isUrgent", "urgent", "is_urgent")))
    out["shift_schedule"], out["shift_testo"] = _shift(_testo(_cerca(raw, CHIAVI["shift"])) or _testo(_cerca(raw, CHIAVI["schedule"])), testo)
    ore = _testo(_cerca(raw, CHIAVI["hours"]))
    if not ore:
        m = ORE.search(testo)
        ore = m.group(0) if m else ""
    out["work_hours"] = ore[:80] or None
    trovati = [k for k, patt in BENEFIT_RE if patt.search(testo)]
    dichiarati = _testo(_cerca(raw, CHIAVI["benefits"]))
    for k, patt in BENEFIT_RE:
        if k not in trovati and dichiarati and patt.search(dichiarati):
            trovati.append(k)
    out["benefits"] = json.dumps(trovati) if trovati else None
    out["benefits_dichiarati"] = dichiarati[:600] or None
    out["salary_text"] = _salary_text(testo)
    out["valid_through"] = _data(_cerca(raw, CHIAVI["valid_through"]))
    # geografia: prima il dichiarato (jsonld jobLocation.address / geo), poi GeoNames
    stato = _testo(_cerca(raw, CHIAVI["state"]))
    cap = _testo(_cerca(raw, CHIAVI["postal_code"]))
    lat = lon = None
    jl = raw.get("jobLocation")
    if isinstance(jl, list) and jl:
        jl = jl[0]
    if isinstance(jl, dict):
        addr = jl.get("address") if isinstance(jl.get("address"), dict) else {}
        stato = stato or _testo(addr.get("addressRegion"))
        cap = cap or _testo(addr.get("postalCode"))
        geo = jl.get("geo") if isinstance(jl.get("geo"), dict) else {}
        try:
            lat, lon = float(geo.get("latitude")), float(geo.get("longitude"))
        except (TypeError, ValueError):
            lat = lon = None
    geo_da = "dichiarato" if lat is not None else None
    if lat is None:
        c = _coordinate(citta, paese)
        if c:
            lat, lon, adm = c
            geo_da = "geonames"
            # il codice admin1 di GeoNames («DE.07») diventa un nome («North
            # Rhine-Westphalia») con la tabella geo_admin1; senza nome, niente
            stato = stato or (ADMIN1.get(f"{paese.upper()}.{adm}") if adm else None) or ""
    out["state"] = (stato or "")[:80] or None
    out["postal_code"] = (cap or "")[:20] or None
    out["latitude"], out["longitude"], out["geo_da"] = lat, lon, geo_da
    n = _cerca(raw, CHIAVI["applicants"])
    try:
        out["applicants_count"] = int(n) if n is not None and str(n).isdigit() else None
    except (TypeError, ValueError):
        out["applicants_count"] = None
    ea = _cerca(raw, CHIAVI["easy_apply"])
    out["is_easy_apply"] = (bool(ea) if isinstance(ea, bool) else (str(ea).lower() in ("true", "1", "yes"))) if ea is not None else None
    r = _recruiter(_cerca(raw, CHIAVI["recruiter"]), contact_email)
    out["recruiter"] = json.dumps(r, ensure_ascii=False) if r else None
    return out


SQL_CODA = """
SELECT j.id, j.title, j.raw, j.city, j.country, j.seniority, j.contact_email,
       coalesce(t.v, '')
  FROM ats_jobs j
  -- 23/09/2026: LATERAL, non la subquery doppia — il testo si valuta UNA
  -- volta per riga (la versione con la subquery nel WHERE la pagava due,
  -- e sotto carico la coda prendeva 6 minuti). Il JOIN filtra le senza
  -- testo: restano in coda finche' il testo non arriva.
  JOIN LATERAL (SELECT v FROM unnest(ARRAY[{campi}]) v
                WHERE length(v) >= 80 LIMIT 1) t ON true
 WHERE j.expired_at IS NULL AND j.dettagli_at IS NULL
 ORDER BY j.posted_at DESC NULLS LAST
 LIMIT %s
""".format(campi=", ".join(f"j.raw->>'{c}'" for c in CAMPI_TESTO))

COLONNE = ("job_id", "management_level", "is_decision_maker", "shift_schedule", "shift_testo", "work_hours",
           "is_urgently_hiring", "benefits", "benefits_dichiarati", "salary_text", "valid_through", "state",
           "postal_code", "latitude", "longitude", "geo_da", "applicants_count", "is_easy_apply", "recruiter")
SQL_SCRIVI = ("INSERT INTO offerte_dettagli (" + ", ".join(COLONNE) + ") VALUES (" + ", ".join(["%s"] * len(COLONNE)) + ") "
              "ON CONFLICT (job_id) DO UPDATE SET " + ", ".join(f"{c} = EXCLUDED.{c}" for c in COLONNE[1:]) + ", calcolato_at = now()")


ADMIN1: dict[str, str] = {}      # «DE.07» -> «North Rhine-Westphalia», da geo_admin1 (migrazione 2026-09-21)


def _schema(c) -> None:
    """Il DDL solo se manca qualcosa. `ALTER TABLE ... ADD COLUMN IF NOT
    EXISTS` prende il lock ESCLUSIVO su ats_jobs anche quando la colonna
    c'e' gia': il 21/09 e' rimasto in coda dietro una lettura lunga e ha
    messo in fila demoni, API ed export per quattro minuti. Se il DDL
    serve davvero, non aspetta piu' di dieci secondi."""
    tabella = c.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'offerte_dettagli'").fetchone()
    colonna = c.execute("SELECT 1 FROM information_schema.columns WHERE table_name = 'ats_jobs' AND column_name = 'dettagli_at'").fetchone()
    if tabella and colonna:
        return
    c.execute("SET lock_timeout = '10s'")
    c.execute(DDL)
    c.execute("RESET lock_timeout")


def applica(dsn: str, limite: int = 200_000, lotto: int = 2000, dry: bool = False) -> dict:
    st = {"viste": 0, "urgenti": 0, "turni": 0, "benefit": 0, "salario_testo": 0, "scadenza": 0, "geo": 0, "recruiter": 0}
    t0 = time.time()
    with psycopg.connect(dsn, autocommit=True) as c:
        _schema(c)
        try:
            ADMIN1.update(dict(c.execute("SELECT codice, nome FROM geo_admin1").fetchall()))
        except psycopg.errors.UndefinedTable:
            log.warning("geo_admin1 assente: lo stato restera' vuoto dove non e' dichiarato")
        while st["viste"] < limite:
            righe = c.execute(SQL_CODA, (min(lotto, limite - st["viste"]),)).fetchall()
            if not righe:
                break
            valori, ids = [], []
            for r in righe:
                d = dettagli(r)
                ids.append(r[0])
                valori.append(tuple(d[k] for k in COLONNE))
                st["viste"] += 1
                st["urgenti"] += bool(d["is_urgently_hiring"]); st["turni"] += bool(d["shift_schedule"])
                st["benefit"] += bool(d["benefits"]); st["salario_testo"] += bool(d["salary_text"])
                st["scadenza"] += bool(d["valid_through"]); st["geo"] += d["latitude"] is not None
                st["recruiter"] += bool(d["recruiter"])
            if not dry:
                with c.cursor() as cur:
                    cur.executemany(SQL_SCRIVI, valori)
                # ats_jobs e' aggiornata anche dai demoni (v1, ripassi) a lotti:
                # il primo riempimento e' morto per deadlock alla riga 84.000
                # (21/09). Id in ordine e ritentativi, come nelle migrazioni.
                ids.sort()
                for tentativo in range(10):
                    try:
                        c.execute("UPDATE ats_jobs SET dettagli_at = now() WHERE id = ANY(%s::uuid[])", (ids,))
                        break
                    except psycopg.errors.DeadlockDetected:
                        log.warning("dettagli: deadlock su ats_jobs, tentativo %d", tentativo + 1)
                        time.sleep(2 + 3 * tentativo)
                else:
                    raise RuntimeError("dettagli: deadlock persistente su ats_jobs")
            dt = time.time() - t0
            log.info("dettagli: %s", {**st, "al_secondo": round(st["viste"] / max(dt, 1e-6), 1)})
    return st


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=200_000)
    ap.add_argument("--lotto", type=int, default=2000)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    dsn = os.environ.get("ATS_DATABASE_URL")
    if not dsn:
        for f in ("/opt/nivult/.env", "/opt/nivult/engine/.env"):
            try:
                m = re.search(r"^POSTGRES_PASSWORD=(.*)$", open(f).read(), re.M)
                if m:
                    dsn = "postgresql://nivult:" + m.group(1).strip() + "@127.0.0.1:5432/nivult_ats"
                    break
            except OSError:
                pass
    print("Dettagli:", applica(dsn, a.limite, a.lotto, a.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
