"""Estrazione dei salari: da 0% a feature da grande player.

Migliaia di offerte portano il salario nel `raw` (breezy come stringa
«$20 – $22 / hour», lever come `salaryRange` strutturato) ma le colonne
salary_min/max/currency non venivano mai popolate. Qui le riempiamo:
prima le forme strutturate, poi il parser di stringhe (valute, range,
suffisso k, separatori EU/US, periodo). Regola d'onesta': il periodo si
salva SOLO se dichiarato; niente numeri inventati, e le forme ambigue si
scartano — meglio un salario mancante che uno sbagliato.
"""
from __future__ import annotations

import logging
import os
import re

import psycopg

log = logging.getLogger("nivult.ats.salari")

_VALUTE = {
    "$": "USD", "us$": "USD", "usd": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
    "chf": "CHF", "sek": "SEK", "nok": "NOK", "dkk": "DKK",
    "pln": "PLN", "zł": "PLN", "czk": "CZK", "huf": "HUF",
    "cad": "CAD", "c$": "CAD", "aud": "AUD", "a$": "AUD",
    "inr": "INR", "₹": "INR", "brl": "BRL", "r$": "BRL",
}
_PERIODI = [
    (r"hour|/\s*hr\b|hourly|all'ora|ora\b|heure|stunde", "hour"),
    (r"\bday\b|daily|giorno|jour|tag\b", "day"),
    (r"week|settiman|semaine|woche", "week"),
    (r"month|mese|mois|monat|mensil", "month"),
    (r"year|/\s*yr\b|annum|annual|anno|an\b|jahr|annuo", "year"),
]
_NUM = re.compile(r"(\d[\d.,  ]*\d|\d)\s*([kK])?")


def _numero(txt: str, kappa: str | None) -> float | None:
    """'40,000' -> 40000; '40.000' -> 40000; '40.5' -> 40.5; '40'+'k' -> 40000."""
    t = txt.replace(" ", "").replace(" ", "")
    # separatore seguito da esattamente 3 cifre = migliaia; altrimenti decimale
    t = re.sub(r"[.,](?=\d{3}(\D|$))", "", t)
    t = t.replace(",", ".")
    try:
        v = float(t)
    except ValueError:
        return None
    if kappa:
        v *= 1000
    return v


def parse_stringa(s: str):
    """«$20 – $22 / hour» -> (20, 22, 'USD', 'hour'). None se ambigua."""
    if not s or len(s) > 200:
        return None
    basso = s.lower()
    valuta = None
    for sym, code in _VALUTE.items():
        if sym in basso:
            valuta = code
            break
    periodo = None
    for rx, p in _PERIODI:
        if re.search(rx, basso):
            periodo = p
            break
    numeri = []
    for m in _NUM.finditer(s):
        v = _numero(m.group(1), m.group(2))
        if v is not None and 0 < v < 10_000_000:
            numeri.append(v)
    if not numeri or not valuta:
        return None            # senza valuta e' troppo ambiguo: scarta
    if len(numeri) == 1:
        mn = mx = numeri[0]
    else:
        mn, mx = min(numeri[:2]), max(numeri[:2])
    if mx > 0 and mn / mx < 0.01:
        return None            # range assurdo (es. «$1 - $500000»): scarta
    return mn, mx, valuta, periodo


def _sano(v) -> float | None:
    """Un importo credibile, dentro i limiti della colonna numeric(12,2)."""
    if not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if 0 < v < 10_000_000 else None


def estrai(raw: dict):
    """Dal raw di un'offerta ritorna (min, max, valuta, periodo) o None."""
    if not isinstance(raw, dict):
        return None
    # 1) lever e simili: salaryRange strutturato
    sr = raw.get("salaryRange") or raw.get("salary_range")
    if isinstance(sr, dict):
        mn, mx = sr.get("min"), sr.get("max")
        if isinstance(mn, (int, float)) or isinstance(mx, (int, float)):
            cur = (sr.get("currency") or "").upper() or None
            per = (sr.get("interval") or "").lower() or None
            per = {"per-year-salary": "year", "per-hour-wage": "hour",
                   "yearly": "year", "hourly": "hour", "annual": "year",
                   "monthly": "month"}.get(per, per if per in
                   ("hour", "day", "month", "year") else None)
            mn, mx = _sano(mn), _sano(mx)
            if cur and (mn or mx):
                return (mn or mx), (mx or mn), cur, per
    # 2) salary come oggetto {min,max,currency}
    sal = raw.get("salary")
    if isinstance(sal, dict):
        mn, mx = sal.get("min"), sal.get("max")
        if isinstance(mn, (int, float)) or isinstance(mx, (int, float)):
            cur = (sal.get("currency") or "").upper() or None
            mn, mx = _sano(mn), _sano(mx)
            if cur and (mn or mx):
                return (mn or mx), (mx or mn), cur, None
    # 3) salary come stringa (breezy: «$20 – $22 / hour»)
    if isinstance(sal, str):
        return parse_stringa(sal)
    # 4) compensation stringa (varie)
    comp = raw.get("compensation")
    if isinstance(comp, str):
        return parse_stringa(comp)
    return None


# ── Il salario scritto NEL TESTO dell'annuncio ──────────────────────
# Misurato il 09/09/2026: 85.401 offerte hanno il campo strutturato
# (4,5%), ma 1.056.044 nominano il salario nel testo (56%) e su un
# campione il 46% porta una cifra: ~480.000 offerte, cinque volte.
#
# PRIMA VERSIONE, BOCCIATA A 67% di precisione. Cercava una parola-
# salario e poi qualunque numero nella finestra: leggeva «36 hours,
# 7a-7p» come 7-36 all'ora, e prendeva il periodo da una parola
# qualsiasi vicina («risposo settimanale» -> a settimana).
#
# Questa versione pretende un IMPORTO COMPLETO attaccato: valuta e
# cifra insieme, e il periodo entro pochi caratteri dall'importo, non
# in un raggio di trecento. Piu' tre difese:
#   - la valuta ambigua la scioglie il PAESE ($ e' USD in US, AUD in
#     AU, CAD in CA; kr e' SEK in SE, NOK in NO, DKK in DK);
#   - le finestre che parlano di orari di lavoro o di bonus si buttano;
#   - l'importo dev'essere plausibile per il suo periodo.
# In dubbio si scarta: meglio un salario mancante che uno inventato.

_VAL_PAESE = {
    "$": {"US": "USD", "CA": "CAD", "AU": "AUD", "NZ": "NZD", "SG": "SGD", "HK": "HKD"},
    "kr": {"SE": "SEK", "NO": "NOK", "DK": "DKK", "IS": "ISK"},
    "£": {"GB": "GBP", "IE": "EUR"},
}
_SIMBOLO = r"(?:€|£|\$|kr|zł|EUR|USD|GBP|CHF|SEK|NOK|DKK|PLN|CZK|HUF|CAD|AUD|RON|BGN)"
# la «k» di «45k» va presa solo se sta da sola: senza la guardia si
# mangiava la k di «35 000 kr per manad» e la corona spariva
_IMPORTO = r"\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?(?:\s*[kK](?![A-Za-z]))?"
# valuta prima o dopo la cifra, ed eventuale secondo estremo del range
_RX_SOLDI = re.compile(
    rf"(?P<pre>{_SIMBOLO})?\s*(?P<a>{_IMPORTO})\s*(?P<post>{_SIMBOLO})?"
    # «USD $16.10 - USD $19.25»: il secondo estremo puo' portare DUE
    # marcatori di valuta, il codice e il simbolo. Ammettendone uno solo
    # il range si spezzava e restava il massimo, cioe' il numero sbagliato.
    # il «e» del range cambia lingua: und, en, tot, och, og, til, y.
    # Senza il tedesco e l'olandese il range si spezzava e restava il
    # secondo estremo, cioe' sempre il numero piu' alto.
    rf"(?:\s*(?:-|–|—|/|\bto\b|\bbis\b|\bund\b|\ben\b|\btot\b|\boch\b|\bog\b|"
    rf"\btil\b|\ba\b|\bà\b|\be\b|\by\b)\s*(?:{_SIMBOLO})?\s*(?:{_SIMBOLO})?\s*"
    rf"(?P<b>{_IMPORTO})\s*(?:{_SIMBOLO})?)?",
    re.I)
_RX_PERIODO = re.compile(
    # fra la cifra e il periodo l'annuncio infila spesso una parola:
    # «12,02 € brut de l'heure», «45.000 € lordi all'anno», «$60k gross
    # per year». Senza questo pezzo quelle offerte non venivano lette
    # affatto, e al loro posto vinceva un'altra cifra piu' avanti nel
    # testo — il premio (misurato il 09/09/2026).
    r"^[^\w]{0,4}(?:(?:brut\w*|net\w*|lord[oi]|gross|brutto|netto|circa|about)\s+)?"
    r"(?:per\s+|/\s*|al\s+|all'|alla\s+|au\s+|aux\s+|a\s+|pro\s+|par\s+|the\s+|"
    r"de\s+l['’]|de\s+la\s+|dell['’]|di\s+|l['’])?"
    r"(hourly|hour|hr\b|ora\b|ore\b|orari[ea]|heure|stunde|st[uü]ndlich|time\b|"
    r"daily|day|giorno|giornalier\w*|jour|tag\b|t[aä]glich|dag\b|"
    r"weekly|week|settiman\w*|semaine|hebdomadaire|woche|w[oö]chentlich|vecka|uke|"
    r"monthly|month|mensil\w*|mese|mesi|mois|mensuel\w*|monat\w*|monatlich|"
    r"m[åa]nad\w*|maand\w*|"
    r"yearly|year|yr\b|annum|annual\w*|annuel\w*|anno|annui?|an\b|jahr\w*|"
    r"j[aä]hrlich|[åa]r\b|jaar\w*)", re.I)
_PER_NORM = {"hour": "hour", "hr": "hour", "ora": "hour", "ore": "hour", "heure": "hour",
             "stunde": "hour", "time": "hour",
             "day": "day", "giorno": "day", "jour": "day", "tag": "day", "dag": "day",
             "week": "week", "semaine": "week", "woche": "week", "vecka": "week", "uke": "week",
             "month": "month", "mese": "month", "mesi": "month", "mois": "month",
             "monat": "month", "maand": "month",
             "year": "year", "yr": "year", "annum": "year", "anno": "year", "annui": "year",
             "annuo": "year", "an": "year", "jahr": "year", "jaar": "year"}
# Il regex cattura le declinazioni («annually», «monatlich»), il dizionario
# no: «$80,000 CAD annually» si perdeva qui, con la cifra letta bene e il
# periodo buttato. La radice e' piu' robusta dell'elenco.
_PER_RADICI = (
    ("hour", "hour"), ("hr", "hour"), ("orari", "hour"), ("ora", "hour"),
    ("ore", "hour"), ("heure", "hour"), ("stund", "hour"), ("stünd", "hour"),
    ("time", "hour"),
    ("dai", "day"), ("day", "day"), ("giorn", "day"), ("jour", "day"),
    ("tag", "day"), ("tägl", "day"), ("dag", "day"),
    ("week", "week"), ("settiman", "week"), ("semaine", "week"),
    ("hebdo", "week"), ("woch", "week"), ("wöch", "week"), ("veck", "week"),
    ("uke", "week"),
    ("month", "month"), ("mens", "month"), ("mese", "month"), ("mesi", "month"),
    ("mois", "month"), ("monat", "month"), ("månad", "month"),
    ("manad", "month"), ("maand", "month"),
    ("year", "year"), ("yr", "year"), ("annu", "year"), ("anno", "year"),
    ("jahr", "year"), ("jähr", "year"), ("jaar", "year"), ("år", "year"),
    ("ar", "year"), ("an", "year"),
)


def _periodo(parola: str) -> str | None:
    p = parola.lower().rstrip(".")
    if p in _PER_NORM:
        return _PER_NORM[p]
    for radice, val in _PER_RADICI:
        if p.startswith(radice):
            return val
    return None


_RX_PAROLA_SAL = re.compile(
    r"\b(salar\w+|stipendi\w+|retribuzion\w+|remunerazion\w+|compenso|RAL\b|"
    r"gehalt\w*|verg[uü]tung|lohn\b|salaire\w*|r[ée]mun[ée]ration\w*|"
    r"l[oö]n\b|lønn\w*|sueldo\w*|salario\w*|sal[aá]rio\w*|"
    r"wage\w*|pay\b|pay range|compensation|base pay|CCNL)\b", re.I)
_RX_NON_SALARIO = re.compile(
    r"\b(bonus|premio|fatturat\w+|revenue|turnover|budget|investiment\w+|"
    r"clienti|customers|dipendenti|employees|founded|fondat\w+|"
    r"hours?\s+(?:per|a|/)\s*(?:week|settimana)|ore\s+settimanal\w+|"
    r"shift|turn[oi]\b|orario di lavoro|working hours|schedule)\b", re.I)
_PLAUSIBILE = {"hour": (4, 400), "day": (30, 3000), "week": (150, 20000),
               "month": (400, 60000), "year": (8000, 1000000)}
# Il rialzo: l'annuncio dice la base e POI quanto si arriva a fare con
# premi, commissioni o ferie incluse. E' l'errore piu' frequente del
# lettore (120 su 165 disaccordi, misurato il 09/09/2026 su 6.152
# offerte): «12,02 € brut de l'heure ... soit jusqu'a 16 €» diventava 16.
# Quello che va nel digest e' la base, che e' anche cio' che la fonte
# scrive nel campo strutturato.
_RX_RIALZO = re.compile(
    r"(jusqu'?\s*[àa]|up\s+to|fino\s+a|bis\s+zu|hasta|soit\b|"
    r"prime\w*|premi\w*|bonus|commission\w*|OTE\b|on[- ]target|"
    r"incl\w*\s+(?:holiday|ferie|urlaub)|including\s+holiday|"
    r"umbrella|inclusive\s+of|potential\w*|earn\s+up|can\s+earn)", re.I)


def _valuta(sym: str | None, paese: str | None) -> str | None:
    if not sym:
        return None
    s = sym.lower().strip()
    if s in _VAL_PAESE:
        m = _VAL_PAESE[s]
        return m.get((paese or "").upper()) or (None if s == "$" else m.get("SE"))
    return _VALUTE.get(s) or (s.upper() if len(s) == 3 else None)


def _plausibile(mn: float, mx: float, periodo: str) -> bool:
    lo, hi = _PLAUSIBILE[periodo]
    return lo <= mn <= hi and lo <= mx <= hi and mx / max(mn, 0.01) <= 20


def parse_testo(testo: str, paese: str | None = None):
    """Il salario dichiarato nel testo, o None.

    Ritorna (min, max, valuta, periodo, frase): la frase e' la prova,
    da mostrare quando si vuole controllare a occhio cosa ha letto."""
    if not testo:
        return None
    t = re.sub(r"<[^>]+>", " ", testo)
    t = re.sub(r"&[a-z]+;|&#x?\w+;", " ", t)
    t = " ".join(t.split())
    # le posizioni delle parole-salario: il candidato buono e' quello
    # ATTACCATO a una di esse, non il primo numero che capita nel testo
    # (a 72% di precisione l'errore tipico era «12,02 € brut de l'heure»
    # letto come 16, perche' 16 compariva prima in un'altra frase).
    ancore = [x.start() for x in _RX_PAROLA_SAL.finditer(t)]
    if not ancore:
        return None
    candidati = []
    periodi_visti = set()
    for m in _RX_SOLDI.finditer(t):
        sym = m.group("pre") or m.group("post")
        if not sym:
            continue                     # senza valuta e' troppo ambiguo
        coda = t[m.end(): m.end() + 32]
        mp = _RX_PERIODO.match(coda)
        if not mp:
            continue                     # senza periodo attaccato non si sa cosa sia
        per = _periodo(mp.group(1))
        if not per:
            continue
        fin = t[max(0, m.start() - 130): m.end() + 60]
        if _RX_NON_SALARIO.search(fin):
            continue                     # parla d'orari o di bonus, non di paga
        if not _RX_PAROLA_SAL.search(fin):
            continue                     # nessuna parola-salario intorno
        a = _numero(m.group("a"), "k" if m.group("a").rstrip()[-1:] in "kK" else None)
        b = _numero(m.group("b"), "k" if (m.group("b") or "").rstrip()[-1:] in "kK" else None) if m.group("b") else None
        if a is None:
            continue
        mn, mx = (min(a, b), max(a, b)) if b else (a, a)
        if not _plausibile(mn, mx, per):
            continue
        val = _valuta(sym, paese)
        if not val:
            continue
        periodi_visti.add(per)
        dist = min(abs(m.start() - x) for x in ancore)
        if dist > 110:
            continue
        # bonus a chi dice esplicitamente «lordo»: e' il salario vero
        lordo = 1 if re.search(r"\b(brut\w*|lord[oi]|gross|brutto)\b", fin, re.I) else 0
        # e malus a chi e' introdotto da una formula di rialzo, in una
        # finestra STRETTA: larga, «bonus» in fondo all'annuncio bocciava
        # anche la base
        vicino = t[max(0, m.start() - 45): m.end() + 25]
        rialzo = 1 if _RX_RIALZO.search(vicino) else 0
        candidati.append((dist - 40 * lordo + 200 * rialzo, mn, mx, val, per, fin[:170]))
    if not candidati:
        return None
    _, mn, mx, val, per, fin = min(candidati)
    # Fissati valuta e periodo dal candidato migliore, fra TUTTI quelli che
    # parlano della stessa valuta e dello stesso periodo vince l'importo
    # piu' basso: due cifre nello stesso annuncio sono quasi sempre la base
    # e la base piu' i premi, e la base e' quella che la fonte dichiara.
    stessi = [c for c in candidati if c[3] == val and c[4] == per]
    if stessi:
        _, mn, mx, val, per, fin = min(stessi, key=lambda c: (c[1], c[0]))
    # Se l'annuncio dichiara la paga su DUE periodi diversi — «$20/ora» e
    # «$75.000 l'anno», o l'oraria e il totale settimanale del camionista —
    # non si sceglie: si tace. Qui un errore si vede nel digest, una
    # casella vuota no, e il rapporto fra i due sbagli non e' alla pari.
    # Misurato: allargare il sospetto a TUTTE le cifre del testo, anche
    # lontane da una parola-salario, non guadagna precisione (92,3 contro
    # 92,8) e perde copertura. Si guarda solo fra i candidati veri.
    if len({c[4] for c in candidati}) > 1:
        return None
    return mn, mx, val, per, fin


def arricchisci_salari(dsn: str, limite: int = 50000) -> dict:
    stats = {"esaminate": 0, "riempite": 0}
    with psycopg.connect(dsn, autocommit=True) as c:
        righe = c.execute("""
            SELECT id, raw FROM ats_jobs
             WHERE salary_min IS NULL AND expired_at IS NULL
               AND salary_checked_at IS NULL
               AND (raw ? 'salary' OR raw ? 'salaryRange'
                    OR raw ? 'salary_range' OR raw ? 'compensation')
             LIMIT %s""", (limite,)).fetchall()
        for jid, raw in righe:
            stats["esaminate"] += 1
            r = estrai(raw)
            if not r:
                # imparsabile («competitive salary»): si MARCA comunque,
                # o ogni ciclo lo ri-esaminava per niente (spreco noto).
                c.execute("UPDATE ats_jobs SET salary_checked_at=now() "
                          "WHERE id=%s", (jid,))
                continue
            mn, mx, cur, per = r
            c.execute("""UPDATE ats_jobs SET salary_min=%s, salary_max=%s,
                         salary_currency=%s, salary_period=%s,
                         salary_da='dichiarato',
                         salary_checked_at=now() WHERE id=%s""",
                      (mn, mx, cur, per, jid))
            stats["riempite"] += 1
    return stats


def arricchisci_dal_testo(dsn: str, limite: int = 200000, lotto: int = 1000,
                          dry: bool = False) -> dict:
    """Il salario letto NEL TESTO, dove la fonte non l'ha dichiarato.

    Precisione misurata il 10/09/2026 su 11.858 offerte che hanno
    entrambe le cose — il campo strutturato fa da verita' gratis, senza
    etichette a mano: **92,8% severa**, 94,7% contando come giuste le
    letture che dicono la stessa paga in un'altra unita' e quelle dove a
    sbagliare la valuta e' la fonte. La provenienza finisce in
    `salary_da`, perche' un salario letto dal testo e uno dichiarato non
    valgono uguale e chi li usera' dopo deve poterli distinguere.

    Ogni offerta si guarda UNA volta: `salary_testo_at` la marca anche
    quando non si trova niente, o ogni giro ripasserebbe il milione di
    annunci che parlano di stipendio senza scrivere una cifra."""
    from nivult.ats.testo import SQL_HA_TESTO, SQL_TESTO
    st = {"esaminate": 0, "scritte": 0}
    with psycopg.connect(dsn) as c:
        if c.info.transaction_status:
            raise RuntimeError("connessione con una transazione gia' aperta")
        while st["esaminate"] < limite:
            righe = c.execute(f"""
                SELECT id, country, left({SQL_TESTO}, 12000)
                  FROM ats_jobs
                 WHERE expired_at IS NULL AND salary_min IS NULL
                   AND salary_testo_at IS NULL AND {SQL_HA_TESTO}
                 LIMIT %s""", (min(lotto, limite - st["esaminate"]),)).fetchall()
            if not righe:
                break
            scritte, viste = [], []
            for jid, paese, testo in righe:
                st["esaminate"] += 1
                viste.append(jid)
                r = parse_testo(testo, paese)
                if r:
                    mn, mx, cur, per, _ = r
                    scritte.append((jid, mn, mx, cur, per))
            if not dry:
                if scritte:
                    c.execute("""
                        UPDATE ats_jobs j
                           SET salary_min = v.mn, salary_max = v.mx,
                               salary_currency = v.cur, salary_period = v.per,
                               salary_da = 'testo', salary_testo_at = now()
                          FROM unnest(%s::uuid[], %s::numeric[], %s::numeric[],
                                      %s::text[], %s::text[]) AS v(id, mn, mx, cur, per)
                         WHERE j.id = v.id AND j.salary_min IS NULL""",
                        ([r[0] for r in scritte], [r[1] for r in scritte],
                         [r[2] for r in scritte], [r[3] for r in scritte],
                         [r[4] for r in scritte]))
                c.execute("UPDATE ats_jobs SET salary_testo_at = now() "
                          "WHERE id = ANY(%s::uuid[]) AND salary_testo_at IS NULL",
                          (viste,))
                c.commit()
            st["scritte"] += len(scritte)
            log.info("%s esaminate, %s con salario (%.1f%%)", st["esaminate"],
                     st["scritte"], 100 * st["scritte"] / max(st["esaminate"], 1))
            if dry:
                break
    return st


def main(argv: list[str] | None = None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.salari")
    ap.add_argument("--limite", type=int, default=50000)
    ap.add_argument("--testo", action="store_true",
                    help="legge il salario dal testo dove la fonte non lo dichiara")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    if args.testo:
        print(arricchisci_dal_testo(dsn, args.limite, dry=args.dry_run))
    else:
        print(arricchisci_salari(dsn, args.limite))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
