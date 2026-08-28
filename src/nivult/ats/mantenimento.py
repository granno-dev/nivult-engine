"""Il mantenimento del database ATS: expira, normalizza, deduplica.

    python -m nivult.ats.mantenimento --expira
    python -m nivult.ats.mantenimento --normalizza --limite 5000
    python -m nivult.ats.mantenimento --dedup
    python -m nivult.ats.mantenimento --stats

Tre lavori che chiudono i gap di qualità verso Fantastic Jobs:

1. EXPIRA — un'offerta che lo scraper non rivede per GIORNI_SCADENZA
   giorni è scaduta: l'azienda l'ha ritirata. La si marca, non si
   cancella (lo storico serve).

2. NORMALIZZA — il JSON grezzo (raw) che conserviamo da ogni
   piattaforma contiene stipendio e contratto in forme diverse:
   qui si estraggono nei campi comuni. Per piattaforma, il suo
   formato.

3. DEDUP — la stessa posizione esiste spesso su due ATS (vetrina
   Radancy che rimanda a Workday). Chiave: titolo+azienda+luogo
   normalizzati in minuscolo; la prima che resta viva vince, le
   altre restano ma marcate come duplicati.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import unicodedata

import psycopg

log = logging.getLogger("nivult.ats.mantenimento")

ATS_DSN = os.environ.get(
    "ATS_DATABASE_URL",
    "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")

# Quanti giorni senza essere rivista prima di considerarla scaduta.
# Gli scraper girano ogni notte: 21 giorni = tre settimane di
# assenze consecutive.
GIORNI_SCADENZA = 21


# ── 1. EXPIRA ─────────────────────────────────────────────────────

def expira(dsn: str, giorni: int = GIORNI_SCADENZA) -> int:
    """Marca scadute le offerte non più viste dallo scraper."""
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # un'offerta VIVA viene riscritta dal runner a ogni giro
            # (fetched_at si aggiorna): se fetched_at è vecchio e
            # expired_at è NULL, l'azienda l'ha ritirata
            cur.execute("""
                UPDATE ats_jobs
                   SET expired_at = now()
                 WHERE expired_at IS NULL
                   AND fetched_at < now() - make_interval(days => %s)
                RETURNING id
            """, (giorni,))
            n = cur.rowcount
        conn.commit()
    log.info("expira: %d offerte scadute (non viste da %d giorni)", n, giorni)
    return n


# ── 2. NORMALIZZA ─────────────────────────────────────────────────

def _estrai_stipendio(raw: dict, piattaforma: str) -> tuple:
    """(min, max, valuta, contratto) dal raw, per piattaforma."""
    if not isinstance(raw, dict):
        return None, None, None, None

    def num(x):
        try:
            return float(str(x).replace(",", ".").replace(" ", ""))
        except (ValueError, TypeError):
            return None

    if piattaforma == "workday":
        # workday: raw ha postedOn, locationPath, titleText...
        # lo stipendio non c'è quasi mai nell'elenco
        return None, None, None, raw.get("timeType") or raw.get("postedOn")
    if piattaforma == "smartrecruiters":
        # smartrecruiters: compensation, typeOfEmployment
        comp = (raw or {}).get("customField") or []
        comp_d = {}
        for c in comp if isinstance(comp, list) else []:
            if isinstance(c, dict) and c.get("fieldId") in (
                    "compensation", "typeOfEmployment", "department"):
                comp_d[c["fieldId"]] = c.get("valueLabel")
        return None, None, None, comp_d.get("typeOfEmployment")
    if piattaforma == "workable":
        # workable: salary_min/salary_max/salary_currency/employment_type
        return (num(raw.get("salary_min")), num(raw.get("salary_max")),
                raw.get("salary_currency"), raw.get("employment_type"))
    if piattaforma == "bamboohr":
        return None, None, None, raw.get("employmentStatusLabel")
    if piattaforma == "softgarden":
        return None, None, None, None
    if piattaforma == "varbi":
        return None, None, None, None
    if piattaforma == "oracle":
        return None, None, None, raw.get("ContractType") or raw.get("JobSchedule")
    if piattaforma == "phenom":
        return None, None, None, None
    if piattaforma == "join":
        # join: salaryAmountFrom/To con salaryFrequency
        return (num(raw.get("salaryAmountFrom")), num(raw.get("salaryAmountTo")),
                "EUR", raw.get("employmentType", {}).get("name")
                if isinstance(raw.get("employmentType"), dict) else None)
    if piattaforma == "radancy":
        return None, None, None, None
    if piattaforma == "adp":
        return None, None, None, (raw.get("workLevelCode") or {}).get("shortName")
    # ripiego generico: campi con nomi comuni
    return (num(raw.get("salary_min") or raw.get("salaryMin")),
            num(raw.get("salary_max") or raw.get("salaryMax")),
            raw.get("salary_currency") or raw.get("salaryCurrency"),
            raw.get("employment_type") or raw.get("employmentType"))


def normalizza(dsn: str, limite: int = 5000) -> int:
    """Estrae stipendio e contratto dal raw nelle colonne comuni."""
    fatti = 0
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, platform_id, raw FROM ats_jobs
                 WHERE normalized_at IS NULL AND raw IS NOT NULL
                   AND raw::text != '{}'
                 ORDER BY platform_id
                 LIMIT %s
            """, (limite,))
            righe = cur.fetchall()
        for jid, pid, raw in righe:
            smin, smax, valuta, contratto = _estrai_stipendio(raw, pid)
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE ats_jobs SET salary_min = %s, salary_max = %s,
                      salary_currency = %s, employment_type = %s,
                      normalized_at = now()
                    WHERE id = %s
                """, (smin, smax, valuta, contratto, jid))
            fatti += 1
        conn.commit()
    log.info("normalizza: %d offerte elaborate", fatti)
    return fatti


# ── 3. DEDUP ──────────────────────────────────────────────────────

def _chiave(titolo: str, slug: str, citta: str | None) -> str | None:
    """La chiave di dedup: titolo+azienda+luogo, appiattita."""
    def pulisci(s):
        s = unicodedata.normalize("NFKD", s or "")
        s = s.lower()
        s = re.sub(r"[^a-z0-9 ]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s
    t = pulisci(titolo)
    a = pulisci(slug)
    if not t or not a:
        return None
    l = pulisci(citta)[:24] if citta else ""
    return f"{t[:60]}|{a}|{l}"


def dedup(dsn: str, limite: int = 50000) -> int:
    """Calcola la duplicate_key e marca i duplicati."""
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, slug, city FROM ats_jobs
                 WHERE duplicate_key IS NULL
                 LIMIT %s
            """, (limite,))
            righe = cur.fetchall()
        for jid, titolo, slug, citta in righe:
            k = _chiave(titolo, slug, citta)
            with conn.cursor() as cur:
                cur.execute("UPDATE ats_jobs SET duplicate_key = %s WHERE id = %s",
                            (k, jid))
        conn.commit()
    # statistiche: quante chiavi condivise
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT count(*) FROM (
                    SELECT duplicate_key FROM ats_jobs
                     WHERE expired_at IS NULL AND duplicate_key IS NOT NULL
                     GROUP BY duplicate_key HAVING count(*) > 1) s
            """)
            n = cur.fetchone()[0]
    log.info("dedup: %d chiavi con più offerte vive", n)
    return n


# ── statistiche ────────────────────────────────────────────────────

def stats(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ats_jobs")
            tot = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM ats_jobs WHERE expired_at IS NOT NULL")
            scad = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM ats_jobs WHERE salary_min IS NOT NULL")
            stip = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM ats_jobs WHERE employment_type IS NOT NULL")
            contr = cur.fetchone()[0]
            cur.execute("""
                SELECT count(*) FROM (
                    SELECT duplicate_key FROM ats_jobs
                     WHERE expired_at IS NULL AND duplicate_key IS NOT NULL
                     GROUP BY duplicate_key HAVING count(*) > 1) s
            """)
            dup = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL")
            vive = cur.fetchone()[0]
    print(f"\nofferte totali:       {tot}")
    print(f"vive (non scadute):   {vive}")
    print(f"scadute:              {scad}")
    print(f"con stipendio:        {stip}")
    print(f"con tipo contratto:   {contr}")
    print(f"chiavi duplicate:     {dup}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-8s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.mantenimento",
                                 description=__doc__)
    ap.add_argument("--expira", action="store_true")
    ap.add_argument("--normalizza", action="store_true")
    ap.add_argument("--dedup", action="store_true")
    ap.add_argument("--limite", type=int, default=5000)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)

    if args.expira:
        expira(ATS_DSN)
    if args.normalizza:
        normalizza(ATS_DSN, args.limite)
    if args.dedup:
        dedup(ATS_DSN, args.limite * 10)
    if args.stats:
        stats(ATS_DSN)
    if not (args.expira or args.normalizza or args.dedup or args.stats):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
