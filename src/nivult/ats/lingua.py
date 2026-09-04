"""La lingua dell'offerta: il requisito che sta scritto solo nel testo.

Un annuncio pubblicato solo in tedesco chiede tedesco — e' il requisito
linguistico piu' onesto che esista, e nessun campo strutturato lo porta.
Il motore ha gia' il binario (`ai_job_language`, e il funnel esclude le
lingue che l'utente non parla, MA il vuoto non esclude): qui riempiamo
il lato ATS, che era tutto NULL.

Due regole, entrambe deterministiche e gratuite:
1. la LINGUA DEL TESTO, contando le parole-funzione ("und/der/mit" contro
   "the/with/your"): serve una descrizione vera (>= 200 caratteri), un
   margine netto sul secondo posto, altrimenti NULL — il dubbio non
   esclude nessuno;
2. il REQUISITO ESPLICITO negli annunci in inglese ("fluent German",
   "Dutch is required"): vince sulla lingua del testo, perche' un
   annuncio inglese che pretende il tedesco va dato solo a chi lo parla.

`lang` usa i codici ISO ("de", "fr") come France Travail e Platsbanken,
cosi' il funnel confronta mele con mele. `lang_at` marca l'esaminato,
anche quando l'esito e' «non si capisce»: niente riesami eterni.
"""
from __future__ import annotations

import logging
import os
import re

import psycopg

log = logging.getLogger("nivult.ats.lingua")

# Parole-funzione distintive per lingua: brevi, frequentissime, e per
# quanto possibile non condivise. Il punteggio e' il numero di presenze.
_PAROLE = {
    "en": {"the", "and", "with", "your", "will", "our", "are", "you",
           "of", "to", "we", "this", "have", "from"},
    "de": {"und", "der", "die", "das", "für", "mit", "wir", "sie",
           "eine", "einen", "werden", "bei", "oder", "auf", "zu"},
    "fr": {"et", "les", "des", "une", "pour", "vous", "nous", "avec",
           "dans", "est", "vos", "sur", "être", "aux"},
    "nl": {"het", "een", "van", "voor", "met", "wij", "je", "bij",
           "aan", "naar", "onze", "wordt", "zijn"},
    "it": {"di", "il", "la", "per", "con", "una", "del", "che",
           "delle", "sono", "nel", "alla", "più"},
    "es": {"la", "el", "para", "con", "una", "que", "los", "las",
           "nuestro", "trabajo", "será", "empresa", "usted"},
    "pt": {"o", "da", "em", "para", "com", "uma", "que", "não",
           "você", "são", "dos", "nossa", "mais"},
    "pl": {"i", "w", "na", "do", "z", "oraz", "pracy", "jest",
           "się", "nie", "które", "przez"},
    "sv": {"och", "att", "för", "med", "du", "vi", "är", "som",
           "av", "på", "din", "hos", "arbeta"},
    "da": {"og", "at", "til", "med", "du", "vi", "er", "som",
           "af", "på", "dig", "hos", "vil"},
    "no": {"og", "å", "til", "med", "du", "vi", "er", "som",
           "av", "på", "deg", "hos", "vil"},
    "fi": {"ja", "on", "että", "sekä", "työ", "meillä", "olet",
           "sinä", "joka", "myös", "kanssa"},
    "cs": {"a", "v", "na", "se", "je", "pro", "práce", "nebo",
           "které", "jako", "budete"},
}

# Il requisito esplicito dentro un annuncio inglese: la lingua nominata
# accanto a parole di obbligo o padronanza. Precisione prima di tutto.
_NOMI = {"german": "de", "dutch": "nl", "french": "fr", "italian": "it",
         "spanish": "es", "portuguese": "pt", "polish": "pl",
         "swedish": "sv", "danish": "da", "norwegian": "no",
         "finnish": "fi", "czech": "cs", "japanese": "ja",
         "korean": "ko"}
_RICHIESTA = re.compile(
    r"(?:fluent|native|proficien\w+|business[- ]level|professional)"
    r"(?:\s+\w+){0,2}?\s+(" + "|".join(_NOMI) + r")\b"
    r"|(" + "|".join(_NOMI) + r")\s+(?:language\s+)?(?:is\s+)?"
    r"(?:required|mandatory|essential|a\s+must)", re.I)

_TAG = re.compile(r"<[^>]+>")
_PAROLA = re.compile(r"[a-zà-ÿåäöøœæčřšž]+")


def rileva(testo: str) -> str | None:
    """Il codice lingua del testo, o None se il testo non si sbilancia."""
    if not testo:
        return None
    # scritture non latine: il blocco Unicode basta e avanza
    for ch in testo[:400]:
        cp = ord(ch)
        if 0xAC00 <= cp <= 0xD7AF:
            return "ko"
        if 0x3040 <= cp <= 0x30FF:
            return "ja"
        if 0x4E00 <= cp <= 0x9FFF:
            return "zh"
        if 0x0400 <= cp <= 0x04FF:
            return "ru"
    parole = _PAROLA.findall(_TAG.sub(" ", testo).lower()[:8000])
    if len(parole) < 30:
        return None
    punti = {L: sum(1 for p in parole if p in bag)
             for L, bag in _PAROLE.items()}
    classifica = sorted(punti.items(), key=lambda x: -x[1])
    primo, secondo = classifica[0], classifica[1]
    if primo[1] < 5 or primo[1] < secondo[1] * 1.5:
        return None                  # il dubbio non esclude nessuno
    return primo[0]


def estrai(titolo: str, descrizione: str) -> str | None:
    lingua = rileva(descrizione or "")
    if lingua == "en":
        m = _RICHIESTA.search(descrizione)
        if m:
            nome = (m.group(1) or m.group(2)).lower()
            return _NOMI.get(nome, lingua)
    return lingua


_DESCR_SQL = """COALESCE(raw->>'description', raw->>'externalDescription',
                raw->>'descriptionHtml',
                raw->>'jobDescription', raw->>'job_description',
                raw->>'content', raw->>'descriptionPlain', '')"""


def arricchisci(dsn: str, limite: int = 100000) -> dict:
    stats = {"esaminate": 0, "riempite": 0, "incerte": 0, "requisiti": 0}
    with psycopg.connect(dsn, autocommit=True) as c:
        righe = c.execute(f"""
            SELECT id, title, {_DESCR_SQL}
              FROM ats_jobs
             WHERE expired_at IS NULL AND lang IS NULL
               AND lang_at IS NULL
               AND length({_DESCR_SQL}) >= 200
             ORDER BY posted_at DESC NULLS LAST
             LIMIT %s""", (limite,)).fetchall()
        def scrivi(lotto: list) -> None:
            # in ordine di id e a lotti corti: i demoni toccano le stesse
            # righe, e due scritture in ordini diversi si incastrano
            # (deadlock visto al primo collaudo). Un tentativo di riserva.
            lotto.sort(key=lambda r: r[1])
            for _ in range(2):
                try:
                    c.cursor().executemany(
                        "UPDATE ats_jobs SET lang=%s, lang_at=now() "
                        "WHERE id=%s", lotto)
                    return
                except psycopg.errors.DeadlockDetected:
                    log.warning("deadlock, riprovo il lotto")
            raise RuntimeError("lotto lingua bloccato due volte")

        lotto = []
        for jid, titolo, descr in righe:
            stats["esaminate"] += 1
            lingua = estrai(titolo or "", descr)
            if lingua:
                stats["riempite"] += 1
                if lingua != rileva(descr):
                    stats["requisiti"] += 1
            else:
                stats["incerte"] += 1
            lotto.append((lingua, jid))
            if len(lotto) >= 300:
                scrivi(lotto)
                lotto = []
        if lotto:
            scrivi(lotto)
    log.info("lingua: %s", stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.lingua")
    ap.add_argument("--limite", type=int, default=100000)
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    print(arricchisci(dsn, args.limite))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
