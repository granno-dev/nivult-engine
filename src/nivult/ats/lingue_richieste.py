"""Le lingue RICHIESTE dall'annuncio — un'altra cosa dalla lingua in cui
e' scritto. «Fluent French required» in un annuncio inglese: la lingua
del testo e' en, la richiesta e' fr. Il campo `languages_required` porta
la seconda; `lang` resta la prima. Mai confuse (osservazione di Giuseppe,
05/09).

Prima le regole, che sono gratis e coerenti: pattern in sette lingue
attorno a un nome di lingua («fluent French», «français courant»,
«verhandlungssicheres Deutsch», «madrelingua inglese», «nivel alto de
inglés», «vloeiend Engels», «flytande svenska»). L'AI resta per il
residuo sfumato, dopo; e questa colonna diventera' una testa del
modellino distillato (v1), etichettata proprio da queste regole.

Pagine a chiave e lotti ordinati: regola di casa."""
from __future__ import annotations

import logging
import re
import time

import psycopg

log = logging.getLogger("nivult.ats.lingue")

# nome della lingua (in varie lingue) -> ISO 639-1
_NOMI = {
    "en": r"english|inglese|anglais|englisch|ingl[eé]s|engels|engelska|engelsk|englanti",
    "fr": r"french|francese|fran[cç]ais|franz[oö]sisch|franc[eé]s|frans|franska|fransk|ranska",
    "de": r"german|tedesco|allemand|deutsch|alem[aá]n|duits|tyska|tysk|saksa",
    "it": r"italian|italiano|italien|italienisch|italiaans|italienska|italiensk",
    "es": r"spanish|spagnolo|espagnol|spanisch|espa[nñ]ol|spaans|spanska|spansk",
    "pt": r"portuguese|portoghese|portugais|portugiesisch|portugu[eê]s|portugees",
    "nl": r"dutch|olandese|n[eé]erlandais|niederl[aä]ndisch|holand[eé]s|nederlands|flemish|fiammingo",
    "sv": r"swedish|svedese|su[eé]dois|schwedisch|sueco|zweeds|svenska",
    "da": r"danish|danese|danois|d[aä]nisch|dan[eé]s|deens|dansk",
    "no": r"norwegian|norvegese|norv[eé]gien|norwegisch|noruego|noors|norsk",
    "fi": r"finnish|finlandese|finnois|finnisch|fin[eé]s|fins|finska|suomi",
    "pl": r"polish|polacco|polonais|polnisch|polaco|pools|polska|polski",
    "ru": r"russian|russo|russe|russisch|ruso|russisch",
    "ar": r"arabic|arabo|arabe|arabisch|[aá]rabe",
    "zh": r"chinese|mandarin|cinese|chinois|chinesisch|chino|mandarijn",
    "ja": r"japanese|giapponese|japonais|japanisch|japon[eé]s",
    "tr": r"turkish|turco|turc|t[uü]rkisch",
    "cs": r"czech|ceco|tch[eè]que|tschechisch|checo",
    "el": r"greek|greco|grec|griechisch|griego",
    "ro": r"romanian|rumeno|roumain|rum[aä]nisch|rumano",
    "hu": r"hungarian|ungherese|hongrois|ungarisch|h[uú]ngaro",
}
_LINGUA = "(?P<lingua>" + "|".join(f"(?P<{k}>{v})" for k, v in _NOMI.items()) + ")"

# «richiesto/fluente/ottima conoscenza/madrelingua/livello» PRIMA o DOPO il nome
_PRIMA = (r"(?:fluent(?:ly)?|native|proficien(?:t|cy)(?: in)?|good|excellent|strong|"
          r"working knowledge of|knowledge of|command of|business[- ]level|"
          r"(?:very )?good (?:command|knowledge|level) of|advanced|"
          r"(?:c1|c2|b2)(?: level)?(?: in)?|bilingual(?: in)?|"
          r"ottim[ao] conoscenza (?:dell[ao']|dell[ao] lingua )?|conoscenza (?:fluente |ottima |buona )?(?:dell[ao']|dell[ao] lingua )?|"
          r"madrelingua|fluente(?: in)?|"
          r"ma[iî]trise (?:du |de l')|(?:tr[eè]s )?bonne ma[iî]trise (?:du |de l')|niveau (?:courant|avanc[eé]|c1|c2|b2) (?:en |d')?|"
          r"courant|bilingue(?: en)?|"
          r"(?:sehr )?gute|fließende?s?|verhandlungssicher(?:e[sn]?)?|sichere?|"
          r"(?:nivel )?(?:alto|avanzado|fluido|nativo) (?:de |en )?|dominio del|"
          r"(?:goede|uitstekende|vloeiende?) (?:beheersing van (?:het |de )?)?|"
          r"(?:flytande|goda kunskaper i|mycket goda kunskaper i)|"
          r"(?:sujuva|erinomainen)\s+)")
_DOPO = (r"\s*(?:\((?:c1|c2|b2)\)|c1|c2|b2)?\s*(?:"
         r"required|mandatory|essential|is a must|a must|needed|"
         r"fluent|proficiency|skills|language skills|speaking|speaker|"
         r"(?:in )?(?:parola e scritto|scritto e parlato)|richiest[ao]|indispensabile|obbligatori[ao]|"
         r"courant|exig[eé]|indispensable|imp[eé]ratif|obligatoire|"
         r"erforderlich|zwingend|vorausgesetzt|in wort und schrift|fließend|verhandlungssicher|"
         r"(?:nivel )?(?:alto|avanzado|fluido|nativo)|imprescindible|requerido|"
         r"vereist|verplicht|vloeiend|"
         r"krävs|flytande|obligatoriskt|"
         r"vaaditaan|sujuva)")

_COMPOSTO = r"(?:sprach)?kenntnisse?"   # Englischkenntnisse, Deutschsprachkenntnisse
_RX_PRIMA = re.compile(_PRIMA + r"\s*(?:in |en |di |de |del |della |dell'|des |die |der )?" + _LINGUA + r"(?:" + _COMPOSTO + r")?\b", re.I)
_RX_DOPO = re.compile(r"\b" + _LINGUA + r"(?:" + _COMPOSTO + r")?\b" + _DOPO, re.I)
# «is a plus / nice to have / gradito / von Vorteil»: gradita, NON richiesta
_GRADITA = re.compile(r"(?:is |sarebbe |est |ist |es |is een )?(?:a |un |une |ein |un )?"
    r"(?:plus|bonus|nice[- ]to[- ]have|advantage|asset|preferred|desirable|"
    r"gradit[ao]|apprezzat[ao]|preferibile|titolo preferenziale|"
    r"souhait[eé]e?|appr[eé]ci[eé]e?|atout|"
    r"von vorteil|w[uü]nschenswert|vorteilhaft|"
    r"valorad[ao]|deseable|se valorar[aá]|"
    r"een pr[eé]|pluspunt|meriterande)", re.I)
_TAG = re.compile(r"<[^>]+>")


def estrai(testo: str) -> list[str]:
    """Codici ISO delle lingue richieste, in ordine di apparizione, senza doppi."""
    if not testo:
        return []
    t = _TAG.sub(" ", testo)
    trovate: list[str] = []
    for rx in (_RX_PRIMA, _RX_DOPO):
        for m in rx.finditer(t):
            # «gradita» vale solo dentro la STESSA proposizione: si taglia
            # alla prima virgola/punto, se no «French required, English is
            # a plus» perdeva il francese per colpa dell'inglese
            coda = re.split(r"[,;.\n]", t[m.end():m.end() + 60], 1)[0]
            if _GRADITA.search(coda):
                continue            # gradita, non richiesta
            for cod in _NOMI:
                if m.group(cod) and cod not in trovate:
                    trovate.append(cod)
    return trovate[:5]


def _colonna_manca(c, tabella: str, colonna: str) -> bool:
    return c.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
        (tabella, colonna)).fetchone() is None


def passa(dsn: str, tetto: int = 300000) -> dict:
    """Scorre le offerte attive con descrizione non ancora esaminate."""
    st = {"viste": 0, "con_lingue": 0}
    with psycopg.connect(dsn, autocommit=True) as c:
        if _colonna_manca(c, "ats_jobs", "languages_required"):
            c.execute("ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS languages_required text[]")
            c.execute("ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS lingue_at timestamptz")
        ultimo = None
        while st["viste"] < tetto:
            righe = c.execute("""
                SELECT id, coalesce(raw->>'description', raw->>'descriptionPlain',
                                    raw->>'descriptionHtml', '')
                  FROM ats_jobs
                 WHERE expired_at IS NULL AND lingue_at IS NULL
                   AND (%s::uuid IS NULL OR id > %s)
                 ORDER BY id LIMIT 500""", (ultimo, ultimo)).fetchall()
            if not righe:
                break
            aggiornamenti = []
            for jid, desc in righe:
                st["viste"] += 1
                lingue = estrai(desc) if desc and len(desc) > 80 else []
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


def main() -> int:
    import argparse, json
    from .runner import ATS_DSN
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.lingue_richieste")
    ap.add_argument("--tetto", type=int, default=300000)
    ap.add_argument("--prova", action="store_true", help="solo un test dei pattern")
    a = ap.parse_args()
    if a.prova:
        for t in ["Fluent French required, English is a plus",
                  "Ottima conoscenza della lingua inglese, madrelingua italiano",
                  "Verhandlungssicheres Deutsch in Wort und Schrift, gute Englischkenntnisse",
                  "Anglais courant exigé. La maîtrise de l'allemand est un plus",
                  "Nivel alto de inglés imprescindible",
                  "Vloeiend Nederlands en Engels vereist",
                  "Buona conoscenza dell'inglese gradita; francese fluente richiesto",
                  "Gute Deutschkenntnisse erforderlich, Französisch von Vorteil",
                  "We build great products for the German market"]:
            print(estrai(t), "<-", t[:60])
        return 0
    print(json.dumps(passa(ATS_DSN, a.tetto)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
