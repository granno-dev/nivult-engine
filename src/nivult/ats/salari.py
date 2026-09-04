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
            mn = float(mn) if isinstance(mn, (int, float)) else None
            mx = float(mx) if isinstance(mx, (int, float)) else None
            if cur and (mn or mx):
                return (mn or mx), (mx or mn), cur, per
    # 2) salary come oggetto {min,max,currency}
    sal = raw.get("salary")
    if isinstance(sal, dict):
        mn, mx = sal.get("min"), sal.get("max")
        if isinstance(mn, (int, float)) or isinstance(mx, (int, float)):
            cur = (sal.get("currency") or "").upper() or None
            if cur:
                mn = float(mn) if isinstance(mn, (int, float)) else None
                mx = float(mx) if isinstance(mx, (int, float)) else None
                return (mn or mx), (mx or mn), cur, None
    # 3) salary come stringa (breezy: «$20 – $22 / hour»)
    if isinstance(sal, str):
        return parse_stringa(sal)
    # 4) compensation stringa (varie)
    comp = raw.get("compensation")
    if isinstance(comp, str):
        return parse_stringa(comp)
    return None


def arricchisci_salari(dsn: str, limite: int = 50000) -> dict:
    stats = {"esaminate": 0, "riempite": 0}
    with psycopg.connect(dsn, autocommit=True) as c:
        righe = c.execute("""
            SELECT id, raw FROM ats_jobs
             WHERE salary_min IS NULL AND expired_at IS NULL
               AND (raw ? 'salary' OR raw ? 'salaryRange'
                    OR raw ? 'salary_range' OR raw ? 'compensation')
             LIMIT %s""", (limite,)).fetchall()
        for jid, raw in righe:
            stats["esaminate"] += 1
            r = estrai(raw)
            if not r:
                continue
            mn, mx, cur, per = r
            c.execute("""UPDATE ats_jobs SET salary_min=%s, salary_max=%s,
                         salary_currency=%s, salary_period=%s WHERE id=%s""",
                      (mn, mx, cur, per, jid))
            stats["riempite"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.salari")
    ap.add_argument("--limite", type=int, default=50000)
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    print(arricchisci_salari(dsn, args.limite))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
