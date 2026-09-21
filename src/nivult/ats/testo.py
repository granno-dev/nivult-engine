"""Il testo dell'annuncio, DOVUNQUE l'adapter l'abbia messo.

Ogni ATS chiama la descrizione a modo suo e gli adapter l'hanno conservata
con il nome della fonte: Greenhouse `content`, Ashby `descriptionHtml` /
`descriptionPlain`, Zoho `Job_Description`, Workday `jobDescription`,
Teamtailor `body`. Il 08/09/2026 il demone di v1 e l'estrattore del
dataset leggevano solo `description`: 162.000 offerte Greenhouse e
64.000 Ashby risultavano «senza testo» e restavano fuori da tutto, mentre
il testo c'era. Da qui in poi il testo si legge SOLO passando da qui.
"""
from __future__ import annotations

import re

CHIAVI = ('description', 'content', 'descriptionHtml', 'descriptionPlain', 'externalDescription', 'jobDescription', 'job_description', 'Job_Description', 'body', 'content_html', 'description_html', 'descriptionBody', 'text')

# per le query SQL: il PRIMO campo con almeno 80 caratteri. Non COALESCE:
# COALESCE si ferma su una stringa vuota (Zoho ha description='' e il testo
# in Job_Description), e 11.774 offerte con il testo risultavano senza.
SQL_TESTO = "(SELECT v FROM unnest(ARRAY[raw->>'description', raw->>'content', raw->>'descriptionHtml', raw->>'descriptionPlain', raw->>'externalDescription', raw->>'jobDescription', raw->>'job_description', raw->>'Job_Description', raw->>'body', raw->>'content_html', raw->>'description_html', raw->>'descriptionBody', raw->>'text', raw->'_jobposting'->>'description', raw->>'ShortDescriptionStr']) v WHERE length(v) >= 80 LIMIT 1)"
SQL_HA_TESTO = f"({SQL_TESTO}) IS NOT NULL"


def descrizione(raw: dict | None, massimo: int = 30000) -> str:
    """Il primo campo di testo non vuoto, con i tag HTML tolti."""
    if not isinstance(raw, dict):
        return ""
    candidati = [raw.get(k) for k in CHIAVI] + [(raw.get("_jobposting") or {}).get("description") if isinstance(raw.get("_jobposting"), dict) else None,
                                               raw.get("ShortDescriptionStr")]
    for v in candidati:
        if isinstance(v, str) and len(v) >= 80:
            return re.sub(r"<[^>]+>", " ", v)[:massimo]
    return ""


# ── testo PULITO, per chi lo vende ──────────────────────────────────
# Misurato il 21/09/2026 su un campione dell'1% delle attive: TUTTE le
# 200k offerte Greenhouse hanno il testo con le entita' HTML codificate
# («&lt;div class=&quot;...»; 35k codificate due volte), e SmartRecruiters,
# Workday, iCIMS, Workable, Lever, Ashby lo hanno con i tag dentro. Il
# compratore vuole testo, non markup: da qui passa la descrizione esportata.
_BLOCCHI_RX = re.compile(r"<(script|style|noscript)\b.*?</\1\s*>", re.I | re.S)
_A_CAPO_RX = re.compile(r"<\s*(/?)(p|div|br|li|ul|ol|h[1-6]|tr|table|section|article|header|footer|blockquote|pre)\b[^>]*>", re.I)
_TAG_RX = re.compile(r"<[^>]+>")
_ENTITA_RX = re.compile(r"&(#\d+|#x[0-9a-f]+|[a-z]+);", re.I)


def pulito(v, massimo: int | None = None) -> str:
    """HTML (anche codificato una o due volte) -> testo piano con gli a capo
    dei blocchi. Nessun taglio se `massimo` e' None: la regola della casa e'
    che l'annuncio non si accorcia."""
    import html as _html
    if not isinstance(v, str) or not v:
        return ""
    t = v
    # decodifica finche' compaiono entita' (Greenhouse: due giri)
    for _ in range(3):
        if not _ENTITA_RX.search(t):
            break
        t = _html.unescape(t)
    t = _BLOCCHI_RX.sub(" ", t)
    t = re.sub(r"<\s*li\b[^>]*>", "\n- ", t, flags=re.I)
    t = re.sub(r"<\s*/li\s*>", "", t, flags=re.I)
    t = _A_CAPO_RX.sub("\n", t)
    t = _TAG_RX.sub(" ", t)
    # le entita' comparse DOPO aver tolto i tag (testo che citava «&amp;»)
    if _ENTITA_RX.search(t):
        t = _html.unescape(t)
    t = t.replace("\xa0", " ").replace("​", "")
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:massimo] if massimo else t


# ── la prova lessicale dello stage ──────────────────────────────────
# Misurato il 21/09/2026: 72k offerte attive con contratto «internship», 30k
# senza NESSUNA parola da stage nel titolo, e fra loro «Senior Alliance
# Manager», «Head of Global Product Quality», «Psychiater». Un annuncio di
# stage lo dice, in qualunque lingua: se non lo dice, l'etichetta non e'
# una lettura, e' un'invenzione del modello. Usata dal demone v1 (prima di
# scrivere) e dalla riparazione del 22/09.
_STAGE_RX = re.compile(
    r"(?<![a-zÀ-ɏ])(intern|interns|internship|internships|stagiaire|stagiaires|stagista|stagisti|"
    r"tirocin\w*|praktik\w*|werkstudent\w*|working student|trainee|traineeship|est[aá]gio|estagi[aá]ri[oa]|"
    r"pr[aá]cticas|becari[oa]|beca|pasant[ií]a|pasante|alternance|alternant[es]?|apprenti[es]?|apprentissage|"
    r"apprentice|apprenticeship|apprendist\w*|azubi|ausbildung|auszubildende[rn]?|lehrling|lehrstelle|"
    r"leerling|stageplaats|stagiaire?|harjoittelija|harjoittelu|praktikplats|l[æe]rling|"
    r"graduate program(me)?|summer analyst|co-op|thesis|tesi di laurea|tfg|tfm|duales? studium|dual student)"
    r"(?![a-zÀ-ɏ])", re.I)
# «stage» vuol dire stage solo dove la lingua lo dice (fr, nl, it): in
# inglese e' «early stage», «stage 2», «backstage»
_STAGE_LINGUA_RX = re.compile(r"(?<![a-z])stages?(?![a-z])", re.I)
_STAGE_CJK_RX = re.compile(r"实习|インターン|인턴")


def evidenza_stage(titolo: str | None, testo: str | None, lang: str | None = None) -> bool:
    """C'e' una parola che dica stage/tirocinio/apprendistato? Nel titolo ne
    basta una; nel testo ne servono due, perche' «we also offer internships»
    in un annuncio da senior non fa di quell'annuncio uno stage."""
    def conta(pezzo: str) -> int:
        n = len(_STAGE_RX.findall(pezzo)) + len(_STAGE_CJK_RX.findall(pezzo))
        if (lang or "") in ("fr", "nl", "it"):
            n += len(_STAGE_LINGUA_RX.findall(pezzo))
        return n
    if titolo and conta(titolo) >= 1:
        return True
    return bool(testo) and conta(testo) >= 2
