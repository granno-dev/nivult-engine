"""Il benchmark salariale: stime oneste dove l'annuncio tace.

Il timore di Giuseppe: offerte senza salario che non si possono vendere.
La risposta dell'industria seria (Glassdoor, Indeed) non e' inventare:
e' STIMARE DICHIARANDO — campi separati, mai mescolati con l'osservato,
col metodo e la base campionaria scritti accanto. La stima onesta e' un
prodotto; la stima spacciata per dato e' veleno.

La fonte migliore ce l'abbiamo in casa: i nostri annunci CON salario.
Mediana e quartili per (paese, valuta, famiglia, seniority), con
ripiego a (paese, valuta, famiglia) quando la cella e' magra. Guardie
di sanita' imparate dai dati: percentili e non medie (un contractor da
100$/ora non deve piegare il gruppo), normalizzazione al lordo annuo
(ora x2080, settimana x52, mese x12), forchette di plausibilita' per
periodo, e la cella entra SOLO con almeno 20 osservazioni.
"""
from __future__ import annotations

import logging
import os

import psycopg

log = logging.getLogger("nivult.ats.benchmark")

MIN_CAMPIONE = 20

# (fattore verso l'anno, minimo plausibile, massimo plausibile)
_PERIODI = {
    "year":  (1,    8000, 900000),
    "month": (12,    500,  60000),
    "week":  (52,    150,  15000),
    "hour":  (2080,    5,    400),
    "day":   (240,    40,   3000),
}


def aggiorna(dsn: str) -> dict:
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS stipendi_benchmark (
                country   text NOT NULL,
                currency  text NOT NULL,
                family    text NOT NULL,
                seniority text NOT NULL DEFAULT '',
                p25 int NOT NULL, p50 int NOT NULL, p75 int NOT NULL,
                n int NOT NULL,
                aggiornato timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (country, currency, family, seniority))""")
        casi = " ".join(
            f"WHEN j.salary_period = '{p}' THEN j.salary_min * {f}"
            for p, (f, _, _) in _PERIODI.items())
        guardie = " OR ".join(
            f"(j.salary_period = '{p}' AND j.salary_min BETWEEN {lo} AND {hi})"
            for p, (_, lo, hi) in _PERIODI.items())
        base = f"""
            SELECT j.country, j.salary_currency,
                   x.family, {{sen}} AS seniority,
                   percentile_cont(0.25) WITHIN GROUP (ORDER BY annuo),
                   percentile_cont(0.50) WITHIN GROUP (ORDER BY annuo),
                   percentile_cont(0.75) WITHIN GROUP (ORDER BY annuo),
                   count(*)
              FROM (SELECT j.*, CASE {casi} END AS annuo
                      FROM ats_jobs j
                     WHERE j.expired_at IS NULL
                       AND j.salary_min IS NOT NULL
                       AND j.country IS NOT NULL
                       AND j.salary_currency IS NOT NULL
                       AND ({guardie})) j
              JOIN job_classifications x ON x.job_id = j.id
             GROUP BY 1, 2, 3, 4
            HAVING count(*) >= {MIN_CAMPIONE}"""
        c.execute("TRUNCATE stipendi_benchmark")
        n_tot = 0
        # due livelli: con seniority (quando c'e') e senza (ripiego '')
        for sen in ("COALESCE(j.seniority, '')", "''"):
            for r in c.execute(base.format(sen=sen)).fetchall():
                c.execute("""
                    INSERT INTO stipendi_benchmark
                        (country, currency, family, seniority,
                         p25, p50, p75, n)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (country, currency, family, seniority)
                    DO NOTHING""",
                    (r[0], r[1], r[2], r[3], int(r[4]), int(r[5]),
                     int(r[6]), r[7]))
                n_tot += 1
        celle = c.execute(
            "SELECT count(*) FROM stipendi_benchmark").fetchone()[0]
    log.info("benchmark: %d celle con base >= %d", celle, MIN_CAMPIONE)
    return {"celle": celle}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    print(aggiorna(dsn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
