"""I segnali tecnografici: quando un'azienda INIZIA a chiedere una competenza.

Il prodotto premium di TheirStack e' l'alert «l'azienda X ha iniziato a
menzionare Snowflake»: vale perche' e' un segnale d'acquisto (chi adotta
una tecnologia compra formazione, consulenza, strumenti). Noi abbiamo
tutto il necessario — le competenze estratte per offerta — ci manca solo
la MEMORIA: quando ogni competenza e' apparsa per la prima volta in
ciascuna azienda.

`azienda_skill` e' quella memoria: (azienda, competenza) -> prima volta,
ultima volta, quante menzioni. Il refresh e' incrementale e onesto:
first_seen si conserva per sempre, la storia si costruisce da oggi in
avanti (retrodatarla sarebbe inventare). La fiducia a tre livelli come
la loro: alta >= 3 menzioni, media 2, bassa 1.

`segnali()` estrae le ADOZIONI: competenze apparse negli ultimi N giorni
in aziende che conoscevamo gia' da prima — il filtro che separa «azienda
nuova nel censimento» (rumore) da «azienda nota che cambia stack»
(segnale).
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import os

import psycopg

log = logging.getLogger("nivult.ats.segnali")


def prepara(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS azienda_skill (
                platform_id text NOT NULL,
                slug        text NOT NULL,
                skill       text NOT NULL,
                mentions    int  NOT NULL,
                first_seen  timestamptz NOT NULL DEFAULT now(),
                last_seen   timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (platform_id, slug, skill))""")


def aggiorna(dsn: str) -> dict:
    """Un refresh: le competenze delle offerte attive entrano in memoria.
    Una INSERT..SELECT sola: niente lotti, niente deadlock — la tabella
    e' nostra e nessun demone la tocca."""
    prepara(dsn)
    with psycopg.connect(dsn, autocommit=True) as c:
        n = c.execute("""
            INSERT INTO azienda_skill (platform_id, slug, skill,
                                       mentions, first_seen, last_seen)
            SELECT j.platform_id, j.slug, s.skill, count(*),
                   now(), now()
              FROM ats_jobs j, LATERAL unnest(j.skills) AS s(skill)
             WHERE j.expired_at IS NULL
             GROUP BY 1, 2, 3
            ON CONFLICT (platform_id, slug, skill) DO UPDATE
               SET mentions = EXCLUDED.mentions,
                   last_seen = now()""").rowcount
        tot = c.execute("SELECT count(*) FROM azienda_skill").fetchone()[0]
    log.info("azienda_skill: %d toccate, %d totali", n, tot)
    return {"toccate": n, "totali": tot}


def segnali(dsn: str, giorni: int = 30) -> int:
    """Esporta le adozioni: competenze nuove in aziende gia' note."""
    from nivult.ats.esporta import CARTELLA, _riga
    os.makedirs(CARTELLA, exist_ok=True)
    oggi = dt.date.today().isoformat()
    percorso = f"{CARTELLA}/segnali-tecnografici-{oggi}.jsonl.gz"
    n = 0
    with psycopg.connect(dsn) as conn, \
            gzip.open(percorso + ".tmp", "wt", encoding="utf-8") as f:
        with conn.cursor(name="segnali") as cur:
            cur.itersize = 2000
            cur.execute("""
                SELECT a.platform_id, a.slug, ac.company_name, ac.country,
                       ac.logo_domain, a.skill, a.mentions, a.first_seen,
                       CASE WHEN a.mentions >= 3 THEN 'high'
                            WHEN a.mentions = 2 THEN 'medium'
                            ELSE 'low' END
                  FROM azienda_skill a
                  JOIN ats_companies ac ON ac.platform_id = a.platform_id
                                       AND ac.slug = a.slug
                 WHERE a.first_seen > now() - make_interval(days => %s)
                   -- azienda gia' nota PRIMA: e' un cambio di stack,
                   -- non un'azienda nuova nel censimento
                   AND EXISTS (SELECT 1 FROM azienda_skill v
                                WHERE v.platform_id = a.platform_id
                                  AND v.slug = a.slug
                                  AND v.first_seen < a.first_seen
                                        - interval '14 days')
                 ORDER BY a.first_seen DESC""", (giorni,))
            for r in cur:
                f.write(_riga(
                    event="skill_adopted", ats=r[0], company_slug=r[1],
                    company=r[2], country=r[3], domain=r[4], skill=r[5],
                    mentions=r[6], first_seen=r[7], confidence=r[8]))
                n += 1
    os.replace(percorso + ".tmp", percorso)
    stabile = f"{CARTELLA}/segnali-tecnografici-ultimo.jsonl.gz"
    try:
        os.remove(stabile + ".tmp")
    except OSError:
        pass
    os.symlink(os.path.basename(percorso), stabile + ".tmp")
    os.replace(stabile + ".tmp", stabile)
    log.info("segnali: %d adozioni negli ultimi %d giorni", n, giorni)
    return n


def main(argv: list[str] | None = None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.segnali")
    ap.add_argument("--aggiorna", action="store_true")
    ap.add_argument("--segnali", action="store_true")
    ap.add_argument("--giorni", type=int, default=30)
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    if args.aggiorna or not args.segnali:
        print("aggiorna:", aggiorna(dsn))
    if args.segnali:
        print("segnali:", segnali(dsn, args.giorni))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
