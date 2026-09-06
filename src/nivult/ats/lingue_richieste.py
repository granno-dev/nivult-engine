"""Le lingue RICHIESTE dall'annuncio — un'altra cosa dalla lingua in cui
e' scritto. «Fluent French required» in un annuncio inglese: la lingua
del testo e' en, la richiesta e' fr. Il campo `languages_required` porta
la seconda; `lang` resta la prima. Mai confuse (osservazione di Giuseppe,
05/09).

**Seconda versione (06/09), per proposizioni.** La prima chiedeva la
parola-chiave ATTACCATA al nome della lingua («fluent French») e mancava
tutto il resto: «fluent in written and spoken English», «Fluency in
English and French», «good command of the Dutch language», «mycket goda
kunskaper i det svenska språket», «C1 level of English». Misurato: il
9,7% delle descrizioni aveva una lingua trovata e un altro 13,1% nominava
una lingua senza estrazione — meta' del richiesto perso. Giuseppe non ci
credeva, e aveva ragione.

Ora: si spezza il testo in proposizioni; una lingua nominata conta se
nella STESSA proposizione c'e' un segnale di requisito (in 7 lingue, in
qualunque ordine, con parole in mezzo) e non c'e' un «gradito». Il
titolo si legge a parte («Polish speaking», «Norsktalande»). Le entita'
HTML si decodificano prima: «&#60;br&#62;Lingue conosciute&#58; Inglese»
era invisibile.

Prima le regole, che sono gratis e coerenti: l'AI resta per il residuo
sfumato, e questa colonna diventera' una testa del modellino distillato
(v1). Pagine a chiave e lotti ordinati: regola di casa."""
from __future__ import annotations

import html
import logging
import re
import time

import psycopg

log = logging.getLogger("nivult.ats.lingue")

# nome della lingua (in varie lingue) -> ISO 639-1
_NOMI = {
    "en": r"english|inglese|anglais|englisch|ingl[eé]s|engels|engelska?|engelsk|englanti|englischkenntnisse",
    "fr": r"french|francese|fran[cç]ais|franz[oö]sisch|franc[eé]s|frans|franska|fransk|ranska",
    "de": r"german|tedesco|allemand|deutsch|alem[aá]n|duits|tyska|tysk|saksa|deutschkenntnisse",
    "it": r"italian|italiano|italien|italienisch|italiaans|italienska|italiensk",
    "es": r"spanish|spagnolo|espagnol|spanisch|espa[nñ]ol|spaans|spanska|spansk",
    "pt": r"portuguese|portoghese|portugais|portugiesisch|portugu[eê]s|portugees",
    "nl": r"dutch|olandese|n[eé]erlandais|niederl[aä]ndisch|holand[eé]s|nederlands|flemish|fiammingo|vlaams",
    "sv": r"swedish|svedese|su[eé]dois|schwedisch|sueco|zweeds|svenska",
    "da": r"danish|danese|danois|d[aä]nisch|dan[eé]s|deens|dansk",
    "no": r"norwegian|norvegese|norv[eé]gien|norwegisch|noruego|noors|norsk",
    "fi": r"finnish|finlandese|finnois|finnisch|fin[eé]s|fins|finska|suomi|suomen",
    "pl": r"polish|polacco|polonais|polnisch|polaco|pools|polska|polski",
    "ru": r"russian|russo|russe|russisch|ruso",
    "ar": r"arabic|arabo|arabe|arabisch|[aá]rabe",
    "zh": r"chinese|mandarin|cinese|chinois|chinesisch|chino|mandarijn",
    "ja": r"japanese|giapponese|japonais|japanisch|japon[eé]s",
    "tr": r"turkish|turco|turc|t[uü]rkisch",
    "cs": r"czech|ceco|tch[eè]que|tschechisch|checo",
    "el": r"greek|greco|grec|griechisch|griego",
    "ro": r"romanian|rumeno|roumain|rum[aä]nisch|rumano",
    "hu": r"hungarian|ungherese|hongrois|ungarisch|h[uú]ngaro",
}
_RX_NOME = re.compile(r"\b(?:" + "|".join(f"(?P<{k}>{v})" for k, v in _NOMI.items()) + r")\b", re.I)

# Segnali di REQUISITO nella stessa proposizione (7 lingue). Non serve che
# stiano attaccati al nome: basta che la frase parli di conoscere/parlare
# una lingua a un certo livello, o che la chieda.
_REQUISITO = re.compile(r"""
    fluen|proficien|native|mother\s+tongue|bilingual|command\s+of|knowledge\s+of|
    speak|spoken|written|writing|reading|read\s+and\s+write|communicat\w*\s+(?:skills?\s+)?in|
    language\s+skills|skills\s+in|level\s+(?:of|in)|\b(?:a1|a2|b1|b2|c1|c2)\b|
    working[-\s]level|business[-\s]level|conversational|advanced|excellent|good|strong|
    required|mandatory|essential|must|need|
    # francese
    courant|couramment|ma[iî]tris|bilingue|niveau|parl|langue|exig|indispensable|imp[eé]ratif|obligatoire|n[eé]cessaire|
    # tedesco
    kenntnisse|sprachkenntnisse|sprache|flie[sß]end|verhandlungssicher|in\s+wort\s+und\s+schrift|sicher|gute|sehr\s+gute|erforderlich|vorausgesetzt|zwingend|sprechen|
    # italiano
    conoscenz|lingua|lingue|parlat|scritt|fluente|madrelingua|ottim|buon|richiest|indispensabile|obbligatori|livello|
    # spagnolo
    dominio|nivel|idioma|habl|imprescindible|requerid|necesari|
    # olandese
    beheersing|vaardighe|spreek|spreken|taal|vloeiend|vereist|verplicht|goede|uitstekende|
    # svedese / norvegese / danese
    kunskaper|talar?|skriv|behärsk|spr[åa]k|flytande|kr[äa]vs|obligatorisk|goda|mycket\s+goda|kommunicer|
    # finlandese
    kielitaito|sujuva|vaaditaan|erinomainen|hyv[aä]
""", re.I | re.X)

# «gradito / plus / von Vorteil»: la lingua e' gradita, non richiesta
_GRADITA = re.compile(r"""
    \bplus\b|\bbonus\b|nice[-\s]to[-\s]have|advantage|\basset\b|preferred|preferabl|desirable|
    highly\s+regarded|regarded|appreciated|welcome|ideally|
    gradit|apprezzat|preferibil|preferenzial|
    souhait|appr[eé]ci|atout|
    von\s+vorteil|w[uü]nschenswert|vorteilhaft|
    valorad|deseable|se\s+valorar|
    een\s+pr[eé]|pluspunt|
    meriterande
""", re.I | re.X)

# contesti in cui il nome di una lingua NON e' una richiesta
_ESCLUDI = re.compile(r"""
    eeo|know\s+your\s+rights|poster|large\s+language\s+model|\bllm|language\s+model|sign\s+language|
    programming\s+language|as\s+a\s+(?:second|foreign)\s+language|som\s+andraspr[åa]k|
    \bteach|\bcourses?\b|\blessons?\b|\bwebsites?\b|\brtl\b|localis|localiz|
    version|traduction|translation|übersetzung|traduzione|
    \bmarket\b|\bmarkt\b|march[eé]|mercato|\bcompany\b|\bunternehmen\b|soci[eé]t[eé]|azienda|
    \bcuisine\b|\bfood\b|\bküche\b|\bcucina\b|\bstyle\b|shepherd|bulldog
""", re.I | re.X)

# nel TITOLO: «Polish speaking», «Norsktalande», «German-speaking», «English Customer Service»
_TITOLO = re.compile(
    r"\b(?P<lingua>" + "|".join(f"(?P<{k}>{v})" for k, v in _NOMI.items()) + r")"
    r"(?:[-\s]?(?:speaking|speaker|talande|talende|sprachig|sprechend|parlant|hablante|falante|sprekend|"
    r"customer\s+service|customer\s+support|support|speaking\s+customer))", re.I)
_TITOLO_INV = re.compile(
    r"(?:with|con|avec|mit|med)\s+(?P<lingua>" + "|".join(f"(?P<{k}>{v})" for k, v in _NOMI.items()) + r")\b", re.I)

_TAG = re.compile(r"<[^>]+>")
# i due punti NON spezzano: «Lingue conosciute: Inglese» e «Requirements:
# English» sono una proposizione sola
_SPEZZA = re.compile(r"[.;!?\n\r•·|●▪■►✓✔☑]|\s[-–—]\s")


def _pulisci(testo: str) -> str:
    t = html.unescape(html.unescape(testo or ""))     # «&#60;br&#62;» era un tag invisibile
    t = _TAG.sub(" ", t)
    t = t.replace("’", "'").replace("‘", "'").replace("\xa0", " ")
    return t


def _codici(m_iter) -> list[str]:
    out = []
    for m in m_iter:
        for cod in _NOMI:
            if m.group(cod) and cod not in out:
                out.append(cod)
    return out


def estrai(testo: str, titolo: str | None = None) -> list[str]:
    """Codici ISO delle lingue richieste, in ordine di apparizione, senza doppi."""
    trovate: list[str] = []
    if titolo:
        for rx in (_TITOLO, _TITOLO_INV):
            for cod in _codici(rx.finditer(_pulisci(titolo))):
                if cod not in trovate:
                    trovate.append(cod)
    if testo:
        t = _pulisci(testo)
        for prop in _SPEZZA.split(t):
            if not prop or len(prop) > 400:
                # una «proposizione» da 400+ caratteri e' un paragrafo senza
                # punteggiatura: troppo largo per attribuire un requisito
                prop = prop[:400] if prop else ""
            nomi = list(_RX_NOME.finditer(prop))
            if not nomi:
                continue
            if _ESCLUDI.search(prop):
                continue
            if not _REQUISITO.search(prop):
                continue
            # «gradita» vale per la sotto-frase: se la proposizione dice
            # «French required, English a plus» si taglia alla virgola
            for pezzo in re.split(r",\s*(?=[^,]*\b(?:plus|bonus|advantage|asset|gradit|atout|vorteil|deseable|pluspunt|meriterande|preferred|nice))", prop, flags=re.I):
                if _GRADITA.search(pezzo):
                    continue
                for cod in _codici(_RX_NOME.finditer(pezzo)):
                    if cod not in trovate:
                        trovate.append(cod)
    return trovate[:5]


def _colonna_manca(c, tabella: str, colonna: str) -> bool:
    return c.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
        (tabella, colonna)).fetchone() is None


def passa(dsn: str, tetto: int = 300000, rifai: bool = False) -> dict:
    """Scorre le offerte attive non ancora esaminate (tutte, con --rifai)."""
    st = {"viste": 0, "con_lingue": 0}
    with psycopg.connect(dsn, autocommit=True) as c:
        if _colonna_manca(c, "ats_jobs", "languages_required"):
            c.execute("ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS languages_required text[]")
            c.execute("ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS lingue_at timestamptz")
        ultimo = None
        while st["viste"] < tetto:
            righe = c.execute("""
                SELECT id, title, coalesce(raw->>'description', raw->>'descriptionPlain',
                                           raw->>'descriptionHtml', '')
                  FROM ats_jobs
                 WHERE expired_at IS NULL AND (lingue_at IS NULL OR %s)
                   AND (%s::uuid IS NULL OR id > %s)
                 ORDER BY id LIMIT 500""", (rifai, ultimo, ultimo)).fetchall()
            if not righe:
                break
            aggiornamenti = []
            for jid, tit, desc in righe:
                st["viste"] += 1
                lingue = estrai(desc if desc and len(desc) > 80 else "", tit)
                if lingue:
                    st["con_lingue"] += 1
                aggiornamenti.append((lingue or None, jid))
            aggiornamenti.sort(key=lambda r: r[1])
            for k in range(0, len(aggiornamenti), 300):
                for _ in range(3):
                    try:
                        with c.cursor() as cc:
                            cc.executemany("UPDATE ats_jobs SET languages_required=%s, "
                                           "lingue_at=now() WHERE id=%s",
                                           aggiornamenti[k:k+300])
                        break
                    except psycopg.errors.DeadlockDetected:
                        time.sleep(1)
            ultimo = righe[-1][0]
            if st["viste"] % 50000 < 500:
                log.info("lingue richieste: %s", st)
    log.info("lingue richieste FINE: %s", st)
    return st


_PROVE = [
    # (testo, titolo, atteso)
    ("Fluent French required, English is a plus", None, ["fr"]),
    ("Ottima conoscenza della lingua inglese, madrelingua italiano", None, ["en", "it"]),
    ("Verhandlungssicheres Deutsch in Wort und Schrift, gute Englischkenntnisse", None, ["de", "en"]),
    ("Anglais courant exigé. La maîtrise de l'allemand est un plus", None, ["en"]),
    ("Nivel alto de inglés imprescindible", None, ["en"]),
    ("Vloeiend Nederlands en Engels vereist", None, ["nl", "en"]),
    ("Buona conoscenza dell'inglese gradita; francese fluente richiesto", None, ["fr"]),
    ("Gute Deutschkenntnisse erforderlich, Französisch von Vorteil", None, ["de"]),
    ("We build great products for the German market", None, []),
    # i mancati del 06/09
    ("Excellent communication skills; fluent in written and spoken English.", None, ["en"]),
    ("Fluency in English and French (both written and spoken)", None, ["en", "fr"]),
    ("Vous parlez couramment anglais et espagnol.", None, ["en", "es"]),
    ("we only accept applicants that have a good command of the Dutch language", None, ["nl"]),
    ("Har mycket goda kunskaper i det svenska språket i både tal och skrift.", None, ["sv"]),
    ("Buen dominio del idioma inglés en un entorno profesional.", None, ["en"]),
    ("If you're an Egyptian graduate with a C1 level of English, we'd love to hear from you!", None, ["en"]),
    ("Maitrise de l’anglais nécessaire.", None, ["en"]),
    ("&#60;br&#62;Lingue conosciute&#58;&#60;br&#62;Inglese&#60;br&#62;", None, ["en"]),
    ("Uitstekende communicatieve vaardigheden in het Nederlands en Engels", None, ["nl", "en"]),
    ("Strong verbal and written English communication skills", None, ["en"]),
    ("Working-level English or Spanish. Based within Barcelona", None, ["en", "es"]),
    ("Maîtriser le français et l'anglais à l'oral comme à l'écrit", None, ["fr", "en"]),
    ("Business and conversational level English and Japanese ability", None, ["en", "ja"]),
    ("Du kommunicerar professionellt på svenska och engelska", None, ["sv", "en"]),
    ("Tala och skriva svenska", None, ["sv"]),
    ("Grundkenntnisse Englisch von Vorteil", None, []),
    ("view the EEO Know Your Rights (English) poster or (Spanish) poster", None, []),
    ("prompt engineering and Large Language Model (LLM) integrations", None, []),
    ("a traditional German company with over 150 years of history", None, []),
    ("Support daily operations in the Polish market", "Contract Operations Analyst (Polish speaking)", ["pl"]),
    ("", "Norsktalande kundsupport", ["no"]),
    ("", "Customer Care Rockstar with Dutch", ["nl"]),
    ("Du kommer undervisa i svenska som andraspråk", None, []),
    ("will teach Math or English courses required to meet graduation requirements", None, []),
    ("Experience supporting multilingual and Arabic/RTL websites", None, []),
    ("Requisiti preferenziali: esperienza in logistica, buona conoscenza di Excel e inglese livello B1", None, []),
    ("Business level Mandarin (spoken and/or written) is highly regarded", None, []),
    ("Language Requirements English: Professional/Fluent (mandatory)", None, ["en"]),
    ("You speak Czech and English", None, ["cs", "en"]),
]


def main() -> int:
    import argparse, json
    from .runner import ATS_DSN
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.lingue_richieste")
    ap.add_argument("--tetto", type=int, default=300000)
    ap.add_argument("--rifai", action="store_true", help="riesamina anche le gia' esaminate")
    ap.add_argument("--prova", action="store_true", help="solo i test dei pattern")
    a = ap.parse_args()
    if a.prova:
        ko = 0
        for testo, titolo, atteso in _PROVE:
            got = estrai(testo, titolo)
            ok = got == atteso
            ko += not ok
            print(("ok " if ok else "KO "), got, "<-", (titolo or testo)[:70], "" if ok else f"(atteso {atteso})")
        print(f"{len(_PROVE)-ko}/{len(_PROVE)} passano")
        return 1 if ko else 0
    print(json.dumps(passa(ATS_DSN, a.tetto, a.rifai)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
