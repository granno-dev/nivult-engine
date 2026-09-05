"""Il profilo dell'offerta: seniority, remote, skill — come i grandi player.

Tre strati, dal gratuito al misurato:
1. STRUTTURATO — molti ATS lo dicono gia' nel raw (workplaceType di ashby,
   is_remote di niceboard, remote di traffit...): costo zero, verita' della
   fonte.
2. DIZIONARI — pattern multilingue su titolo e localita' (senior/junior/
   stage/alternance/praktikum; remote/hybrid/télétravail/homeoffice...) e
   un lessico di skill sul testo. Copre l'80-90%, costo zero.
3. GLM-4.5-FLASH (gratuito) — SOLO per il residuo con descrizione e senza
   segnali, a lotti con tetto: mai un centesimo di credito pagato. Il
   modello dice «unknown» quando non sa: si salva solo cio' che afferma.
"""
from __future__ import annotations

import json
import logging
import os
import re

import psycopg

log = logging.getLogger("nivult.ats.profilo")

# ── seniority dal titolo (l'ordine conta: dal grado piu' alto) ──────
_SENIORITY = [
    ("head",   re.compile(r"\b(head of|director|vp\b|vice president|chief|"
                          r"cto|ceo|cfo|coo|direttore|directeur|leiter(in)?\b|"
                          r"responsabile)\b", re.I)),
    ("lead",   re.compile(r"\b(lead|principal|team ?lead|tech ?lead|"
                          r"capo (squadra|progetto)|chef de projet)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?\b|expert(e)?\b|confirmé)\b", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?\b|entry.?level|graduate|"
                          r"berufseinsteiger|débutant)\b", re.I)),
    ("intern", re.compile(r"\b(intern(ship)?|stage|stagiaire|stagista|"
                          r"tirocin\w+|praktik\w+|werkstudent\w*|trainee|"
                          r"apprenti\w*|alternan\w+|co-?op\b|"
                          r"ausbildung|azubi|apprendist\w+)\b", re.I)),
]

_REMOTE = re.compile(r"\b(remote|remoto|télétravail|teletravail|home ?office|"
                     r"smart ?working|work from home|wfh|100 ?% ?remote|"
                     r"fully remote|da remoto)\b", re.I)
_HYBRID = re.compile(r"\b(hybrid|ibrido|hybride|hibrido)\b", re.I)

# ── lessico skill: mirato e verificabile (estendibile / sostituibile
#    con ESCO in seguito). Match a parola intera, case-insensitive. ──
_SKILLS = [
    # software & data
    "python", "java", "javascript", "typescript", "react", "angular", "vue",
    "node.js", "c#", ".net", "c++", "golang", "rust", "php", "ruby", "swift",
    "kotlin", "sql", "postgresql", "mysql", "mongodb", "redis", "kafka",
    "aws", "azure", "gcp", "docker", "kubernetes", "terraform", "linux",
    "git", "ci/cd", "devops", "machine learning", "deep learning", "pytorch",
    "tensorflow", "data science", "power bi", "tableau", "excel", "sap",
    "salesforce", "etl", "spark", "hadoop", "api", "rest", "graphql",
    "microservices", "html", "css", "django", "flask", "spring", "laravel",
    "selenium", "cypress", "jira", "scrum", "agile", "kanban",
    # business & ufficio
    "crm", "seo", "sem", "google ads", "facebook ads", "content marketing",
    "copywriting", "e-commerce", "shopify", "wordpress", "photoshop",
    "illustrator", "figma", "indesign", "autocad", "solidworks", "revit",
    "project management", "prince2", "pmp", "lean", "six sigma",
    "controlling", "contabilità", "accounting", "payroll", "recruiting",
    "customer service", "call center", "b2b", "b2c", "crm dynamics",
    # sanita' & sociale
    "infermiere", "nursing", "oss", "physiotherapie", "fisioterapia",
    "pflege", "caregiver", "first aid",
    # tecnico & industria
    "plc", "siemens", "cnc", "saldatura", "welding", "elettricista",
    "electrician", "hvac", "forklift", "muletto", "cariste", "gru",
    "manutenzione", "maintenance", "quality assurance", "iso 9001",
    "haccp", "gmp", "logistics", "supply chain", "warehouse", "wms",
    # lingue (skill richieste)
    "english", "inglese", "französisch", "deutsch", "tedesco", "français",
    "spagnolo", "español", "italiano", "dutch", "nederlands",
    "snowflake",
    "databricks",
    "dbt",
    "airflow",
    "elasticsearch",
    "redshift",
    "bigquery",
    "looker",
    "qlik",
    "sas",
    "matlab",
    "scala",
    "perl",
    "bash",
    "powershell",
    "vmware",
    "citrix",
    "active directory",
    "jenkins",
    "gitlab",
    "github actions",
    "ansible",
    "puppet",
    "prometheus",
    "grafana",
    "splunk",
    "datadog",
    "cybersecurity",
    "penetration testing",
    "siem",
    "firewall",
    "cisco",
    "voip",
    "intune",
    "jamf",
    "servicenow",
    "sharepoint",
    "dynamics 365",
    "netsuite",
    "peoplesoft",
    "abap",
    "fiori",
    "s/4hana",
    "next.js",
    "nuxt",
    "svelte",
    "tailwind",
    "sass",
    "redux",
    "fastapi",
    "unity",
    "unreal",
    "blender",
    "after effects",
    "embedded",
    "verilog",
    "vhdl",
    "fpga",
    "iot",
    "mqtt",
    "opencv",
    "nlp",
    "llm",
    "generative ai",
    "rag",
    "computer vision",
    "react native",
    "flutter",
    "ios",
    "android",
    "ifrs",
    "gaap",
    "sox",
    "kyc",
    "aml",
    "treasury",
    "audit",
    "quickbooks",
    "xero",
    "stripe",
    "hubspot",
    "marketo",
    "lead generation",
    "cold calling",
    "google analytics",
    "ga4",
    "linkedin ads",
    "tiktok ads",
    "mailchimp",
    "klaviyo",
    "icu",
    "phlebotomy",
    "cpr",
    "acls",
    "bls",
    "radiology",
    "pharmacy",
    "dental",
    "veterinary",
    "telemetry",
    "tig",
    "mig",
    "blueprint reading",
    "osha",
    "crane",
    "scaffolding",
    "plumbing",
    "carpentry",
    "solar",
    "refrigeration",
    "cdl",
    "catia",
    "ansys",
    "gd&t",
    "injection molding",
    "kaizen",
    "5s",
    "tpm",
    "fmea",
    "ppap",
    "barista",
    "food safety",
    "servsafe",
    "sommelier",
    "mandarin",
    "arabic",
    "português",
    "polski",
    "japanese",
    "korean",
    "svenska",
    "norsk",
    "dansk",
    "gdpr",
    "hris",
    "onboarding",
]
_SKILL_RX = [(s, re.compile(r"(?<![a-z0-9])" + re.escape(s).replace(r"\ ", r"\s+")
                            + r"(?![a-z0-9])", re.I)) for s in _SKILLS]


def _descrizione(raw: dict) -> str:
    if not isinstance(raw, dict):
        return ""
    for k in ("description", "externalDescription",
              "descriptionHtml", "descriptionPlain",
              "description_html", "content", "descriptionBody"):
        v = raw.get(k)
        if isinstance(v, str) and v:
            return re.sub(r"<[^>]+>", " ", v)[:8000]
    return ""


def _remote_da_raw(raw: dict) -> str | None:
    """Il dato strutturato della fonte, quando c'e', vince su tutto."""
    if not isinstance(raw, dict):
        return None
    wt = str(raw.get("workplaceType") or "").lower()
    if wt in ("remote", "hybrid"):
        return wt
    if wt == "onsite":
        return "onsite"
    ir = raw.get("is_remote")
    if ir is True:
        return "remote"
    rm = raw.get("remote")
    if rm in (True, "1", 1):
        return "remote"
    if raw.get("remote_only") is True:
        return "remote"
    return None


def deterministico(titolo: str, luogo: str, raw: dict):
    """(seniority, remote, skills) dai soli strati gratuiti."""
    testo_breve = f"{titolo} {luogo or ''}"
    seniority = None
    for nome, rx in _SENIORITY:
        if rx.search(titolo or ""):
            seniority = nome
            break
    remote = _remote_da_raw(raw)
    if remote is None:
        if _HYBRID.search(testo_breve):
            remote = "hybrid"
        elif _REMOTE.search(testo_breve):
            remote = "remote"
    descr = _descrizione(raw)
    campo_skill = f"{titolo}\n{descr}"
    skills = [s for s, rx in _SKILL_RX if rx.search(campo_skill)][:20]
    # ESCO: 13.485 competenze in 28 lingue, canonicalizzate in inglese
    # («saldatura» -> «welding»). Entra SOLO a calibrazione avvenuta
    # (cancello in esco.py); se spento, questa riga non cambia nulla.
    try:
        from nivult.ats import esco as _esco
        per_esco = _esco.estrai(campo_skill, massimo=15)
        skills += [e for e in per_esco if e not in skills]
        skills = skills[:30]
    except Exception:                                # noqa: BLE001
        pass
    return seniority, remote, skills


# ── GLM-4.5-Flash: solo il residuo, gratuito, con tetto ─────────────

_PROMPT = """Analizza questa offerta di lavoro. Rispondi SOLO con JSON:
{{"seniority":"intern|junior|mid|senior|lead|head|unknown","remote":"remote|hybrid|onsite|unknown"}}
TITOLO: {t}
LUOGO: {l}
TESTO: {d}"""

_VAL_SEN = {"intern", "junior", "mid", "senior", "lead", "head"}
_VAL_REM = {"remote", "hybrid", "onsite"}


def _glm_flash():
    from nivult.matching.llm import GLM
    m = GLM()
    m.model = "glm-4.5-flash"          # il modello GRATUITO: mai credito pagato
    return m


def col_flash(modello, titolo, luogo, descr):
    """Ritorna (seniority, remote, ok). ok=False = la CHIAMATA e' fallita
    (429/credito/rete): il chiamante deve poter distinguere il modello
    che non sa rispondere dal modello che non risponde affatto."""
    try:
        r = modello.chat([{"role": "user", "content": _PROMPT.format(
            t=(titolo or "")[:100], l=(luogo or "")[:60],
            d=(descr or "")[:350])}], max_tokens=60)
    except Exception:                                # noqa: BLE001
        return None, None, False
    try:
        j = json.loads(re.search(r"\{.*\}", r, re.S).group(0))
        sen = j.get("seniority") if j.get("seniority") in _VAL_SEN else None
        rem = j.get("remote") if j.get("remote") in _VAL_REM else None
        return sen, rem, True
    except Exception:                                # noqa: BLE001
        return None, None, True


def arricchisci_profilo(dsn: str, limite: int = 50000,
                        glm_max: int = 0) -> dict:
    """Strati 1-2 su tutto il lotto; strato 3 (GLM) su al piu' glm_max
    offerte del residuo CON descrizione."""
    stats = {"viste": 0, "seniority": 0, "remote": 0, "con_skill": 0,
             "glm": 0, "glm_riempiti": 0, "glm_errori": 0}
    modello = None
    glm_ko_di_fila = 0   # 3 di fila (429/credito) = spento per il lotto
    with psycopg.connect(dsn, autocommit=True) as c:
        righe = c.execute("""
            SELECT id, title, coalesce(location, city, ''), raw
              FROM ats_jobs
             WHERE expired_at IS NULL AND profiled_at IS NULL
             ORDER BY posted_at DESC NULLS LAST
             LIMIT %s""", (limite,)).fetchall()
        for jid, titolo, luogo, raw in righe:
            stats["viste"] += 1
            sen, rem, skills = deterministico(titolo, luogo, raw)
            descr = _descrizione(raw)
            if (sen is None and rem is None and descr
                    and stats["glm"] < glm_max and glm_ko_di_fila < 3):
                if modello is None:
                    modello = _glm_flash()
                stats["glm"] += 1
                g_sen, g_rem, ok = col_flash(modello, titolo, luogo, descr)
                if not ok:
                    stats["glm_errori"] += 1
                    glm_ko_di_fila += 1
                    if glm_ko_di_fila >= 3:
                        log.warning("GLM giu' (429/credito?): spento "
                                    "per il resto del lotto")
                else:
                    glm_ko_di_fila = 0
                if g_sen or g_rem:
                    stats["glm_riempiti"] += 1
                sen, rem = sen or g_sen, rem or g_rem
            c.execute("""UPDATE ats_jobs
                            SET seniority=%s, remote=%s, skills=%s,
                                profiled_at=now()
                          WHERE id=%s""",
                      (sen, rem, skills or None, jid))
            if sen:
                stats["seniority"] += 1
            if rem:
                stats["remote"] += 1
            if skills:
                stats["con_skill"] += 1
    log.info("profilo: %s", stats)
    return stats


_PROMPT_PAESE = """In quale paese si trova questa offerta di lavoro? Rispondi SOLO con JSON: {{"country":"codice ISO2 maiuscolo oppure XX se incerto"}}
TITOLO: {t}
LUOGO: {l}
TESTO: {d}"""

_ISO2 = re.compile(r"^[A-Z]{2}$")


def paese_glm(dsn: str, limite: int = 300) -> dict:
    """Il paese per il residuo che il geocoder non scioglie: GLM Flash
    (gratuito) su titolo+luogo+testo. Accuratezza misurata prima di
    attivarlo; XX/incerto NON si salva — meglio nessun paese che uno
    sbagliato (finirebbe nel digest del cluster sbagliato)."""
    stats = {"esaminate": 0, "riempite": 0, "incerte": 0, "errori": 0}
    modello = _glm_flash()
    ko_di_fila = 0
    with psycopg.connect(dsn, autocommit=True) as c:
        righe = c.execute("""
            SELECT id, title, coalesce(location, city, ''), raw
              FROM ats_jobs
             WHERE expired_at IS NULL AND country IS NULL
               AND country_glm_at IS NULL
               AND (location IS NOT NULL OR city IS NOT NULL
                    OR raw ?| array['description','externalDescription'])
             ORDER BY posted_at DESC NULLS LAST
             LIMIT %s""", (limite,)).fetchall()
        for jid, titolo, luogo, raw in righe:
            stats["esaminate"] += 1
            paese = None
            try:
                r = modello.chat([{"role": "user",
                                   "content": _PROMPT_PAESE.format(
                    t=(titolo or "")[:80], l=(luogo or "")[:80],
                    d=_descrizione(raw)[:250])}], max_tokens=30)
            except Exception:                        # noqa: BLE001
                # Chiamata fallita (429/credito/rete): la riga NON si
                # marca — bruciarla significherebbe non riprovarla mai.
                stats["errori"] += 1
                ko_di_fila += 1
                if ko_di_fila >= 3:
                    log.warning("GLM giu' (429/credito?): lotto interrotto")
                    break
                continue
            ko_di_fila = 0
            try:
                g = json.loads(re.search(r"\{.*\}", r, re.S)
                               .group(0)).get("country", "XX").upper()
                if _ISO2.match(g) and g != "XX":
                    paese = g
            except Exception:                        # noqa: BLE001
                pass
            c.execute("UPDATE ats_jobs SET country = coalesce(country, %s), "
                      "country_glm_at = now() WHERE id = %s", (paese, jid))
            if paese:
                stats["riempite"] += 1
            else:
                stats["incerte"] += 1
    log.info("paese_glm: %s", stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.profilo")
    ap.add_argument("--limite", type=int, default=50000)
    ap.add_argument("--glm-max", type=int, default=0,
                    help="quante offerte del residuo mandare a GLM Flash")
    ap.add_argument("--paese-glm", type=int, default=0, metavar="N",
                    help="paese via GLM Flash per N offerte senza paese")
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    if args.paese_glm:
        print(paese_glm(dsn, args.paese_glm))
        return 0
    print(arricchisci_profilo(dsn, args.limite, args.glm_max))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
