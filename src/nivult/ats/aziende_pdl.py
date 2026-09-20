"""People Data Labs, Free Company Dataset (CC BY 4.0): ~22 milioni di
aziende con dominio, LinkedIn, settore, fascia di dipendenti, anno di
fondazione, sede. Gira sul N5, dove sta il lago dati; su Hetzner arriva
solo cio' che si aggancia ai datori che seguiamo.

Due passi:
  --lago      il JSON-lines gzip (2,6 GB, 6,4 GB decompresso) diventa un
              Parquet tipizzato in /opt/nivult/lago/pdl/aziende.parquet,
              col dominio normalizzato (senza www, minuscolo). DuckDB lo
              fa in streaming: non serve tenerlo in RAM.
  --aggancia  prende da Hetzner i nostri domini (company_domains, e
              site_domain/logo_domain dei tenant), li incrocia col Parquet
              e scrive su Hetzner la tabella `aziende_pdl` (una riga per
              dominio). Il match e' SOLO per dominio: il 35% delle righe
              PDL non ha website e non si aggancia — meglio niente che
              un omonimo fra 22 milioni.

Il paese in PDL e' un nome esteso in inglese («italy»): si traduce in ISO2
con geonamescache (gia' dipendenza del motore). Attribuzione richiesta
dalla licenza: «People Data Labs, Free Company Dataset, CC BY 4.0» —
sta in `aziende_pdl.fonte`.

    python -m nivult.ats.aziende_pdl --lago
    python -m nivult.ats.aziende_pdl --aggancia
    python -m nivult.ats.aziende_pdl --stato
"""
from __future__ import annotations

import argparse
import logging
import os
import time

import psycopg

from nivult.ats.jsonld import _dsn

log = logging.getLogger("nivult.ats.aziende_pdl")

LAGO = os.environ.get("NIVULT_LAGO", "/opt/nivult/lago")
GREZZO = os.path.join(LAGO, "pdl", "free_company_dataset.json.gz")
PARQUET = os.path.join(LAGO, "pdl", "aziende.parquet")
FONTE = "People Data Labs, Free Company Dataset, CC BY 4.0"


def _duck(memoria: str = "6GB", thread: int = 4):
    import duckdb
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{memoria}'"); con.execute(f"SET threads={thread}")
    con.execute(f"SET temp_directory='{os.path.join(LAGO, 'tmp')}'")
    os.makedirs(os.path.join(LAGO, "tmp"), exist_ok=True)
    return con


def lago(memoria: str = "6GB", thread: int = 4) -> dict:
    """JSON-lines gzip -> Parquet tipizzato, dominio normalizzato."""
    t0 = time.time()
    con = _duck(memoria, thread)
    con.execute(f"""
        COPY (
          SELECT id, name AS nome, founded::INTEGER AS fondata, size AS fascia,
                 locality AS localita, region AS regione, country AS paese_nome,
                 industry AS settore, linkedin_url,
                 website AS sito,
                 CASE WHEN website IS NULL THEN NULL
                      ELSE regexp_replace(regexp_replace(lower(trim(website)), '^(https?://)?(www\\.)?', ''), '[/:?#].*$', '')
                 END AS dominio
          FROM read_json('{GREZZO}', format='newline_delimited', compression='gzip',
                         columns={{'id':'VARCHAR','website':'VARCHAR','name':'VARCHAR','founded':'VARCHAR','size':'VARCHAR',
                                  'locality':'VARCHAR','region':'VARCHAR','country':'VARCHAR','industry':'VARCHAR','linkedin_url':'VARCHAR'}},
                         ignore_errors=true, maximum_object_size=1048576)
        ) TO '{PARQUET}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 500000)
    """)
    righe, con_dominio = con.execute(f"SELECT count(*), count(dominio) FROM '{PARQUET}'").fetchone()
    return {"righe": righe, "con_dominio": con_dominio, "parquet_mb": os.path.getsize(PARQUET) // 1048576,
            "minuti": round((time.time() - t0) / 60, 1)}


def _iso2() -> dict[str, str]:
    """nome inglese minuscolo -> ISO2, con qualche alias che PDL usa."""
    import geonamescache
    gc = geonamescache.GeonamesCache()
    m = {v["name"].lower(): k for k, v in gc.get_countries().items()}
    m.update({"united states": "US", "united kingdom": "GB", "russia": "RU", "south korea": "KR", "czechia": "CZ",
              "czech republic": "CZ", "vietnam": "VN", "iran": "IR", "taiwan": "TW", "macedonia": "MK",
              "north macedonia": "MK", "moldova": "MD", "bolivia": "BO", "venezuela": "VE", "syria": "SY",
              "laos": "LA", "brunei": "BN", "tanzania": "TZ", "côte d'ivoire": "CI", "ivory coast": "CI",
              "hong kong": "HK", "macau": "MO", "palestine": "PS", "kosovo": "XK", "türkiye": "TR", "turkey": "TR",
              "the netherlands": "NL", "netherlands": "NL", "cape verde": "CV", "swaziland": "SZ", "eswatini": "SZ"})
    return m


def prepara(c) -> None:
    # Il ruolo dell'operaio sul N5 scrive ma non crea: `IF NOT EXISTS` chiede
    # comunque CREATE sullo schema. La tabella la crea Hetzner (ruolo nivult)
    # con --stato; qui si salta il DDL se c'e' gia'.
    if c.execute("SELECT to_regclass('aziende_pdl')").fetchone()[0] is not None:
        return
    c.execute("""CREATE TABLE IF NOT EXISTS aziende_pdl (
        dominio text PRIMARY KEY, pdl_id text, nome text, fondata integer, fascia text,
        localita text, regione text, paese text, paese_nome text, settore text, linkedin_url text,
        fonte text NOT NULL, aggiornato timestamptz NOT NULL DEFAULT now())""")
    c.execute("CREATE INDEX IF NOT EXISTS aziende_pdl_paese_idx ON aziende_pdl (paese)")


def aggancia(dsn: str, memoria: str = "6GB", thread: int = 4) -> dict:
    t0 = time.time()
    con = _duck(memoria, thread)
    with psycopg.connect(dsn) as c:
        prepara(c); c.commit()
        nostri = {r[0] for r in c.execute("SELECT domain FROM company_domains")}
        nostri |= {r[0] for r in c.execute("SELECT site_domain FROM ats_companies WHERE site_domain IS NOT NULL")}
        nostri |= {r[0] for r in c.execute("SELECT logo_domain FROM ats_companies WHERE logo_domain IS NOT NULL")}
        nostri |= {r[0] for r in c.execute("SELECT dominio FROM bilanci WHERE dominio IS NOT NULL")}
    nostri = {d.lower().removeprefix("www.") for d in nostri if d and "." in d}
    con.execute("CREATE TABLE nostri (dominio VARCHAR)")
    con.executemany("INSERT INTO nostri VALUES (?)", [(d,) for d in nostri])
    # un dominio PDL puo' comparire piu' volte (filiali, doppioni): si tiene la
    # riga piu' completa (piu' campi valorizzati), poi la piu' grande
    righe = con.execute(f"""
        WITH p AS (
          SELECT a.*, (fondata IS NOT NULL)::INT + (settore IS NOT NULL)::INT + (localita IS NOT NULL)::INT
                      + (paese_nome IS NOT NULL)::INT AS pieni
          FROM '{PARQUET}' a JOIN nostri n USING (dominio)
        )
        SELECT dominio, id, nome, fondata, fascia, localita, regione, paese_nome, settore, linkedin_url
        FROM (SELECT *, row_number() OVER (PARTITION BY dominio ORDER BY pieni DESC, fascia DESC) AS rn FROM p)
        WHERE rn = 1
    """).fetchall()
    iso = _iso2()
    with psycopg.connect(dsn) as c, c.cursor() as cur:
        # si svuota e si riscrive per intero: idempotente, e il Parquet e' la verita'
        cur.execute("TRUNCATE aziende_pdl")
        with cur.copy("COPY aziende_pdl (dominio, pdl_id, nome, fondata, fascia, localita, regione, paese, paese_nome, "
                      "settore, linkedin_url, fonte) FROM STDIN") as cp:
            for dom, pid, nome, fondata, fascia, loc, reg, paese_nome, settore, li in righe:
                cp.write_row((dom, pid, nome, fondata, fascia, loc, reg, iso.get((paese_nome or "").lower()),
                              paese_nome, settore, li, FONTE))
        # il nome vero e il paese dove ci mancano: PDL li ha per tutti i suoi
        cur.execute("""UPDATE company_domains d SET company_name = COALESCE(d.company_name, p.nome),
                                                    country = COALESCE(d.country, p.paese)
                       FROM aziende_pdl p WHERE p.dominio = d.domain AND (d.company_name IS NULL OR d.country IS NULL)""")
        riempiti = cur.rowcount
        c.commit()
    return {"nostri_domini": len(nostri), "agganciati": len(righe), "company_domains_riempiti": riempiti,
            "minuti": round((time.time() - t0) / 60, 1)}


def stato(dsn: str) -> None:
    with psycopg.connect(dsn) as c:
        prepara(c)
        for r in c.execute("SELECT paese, count(*), count(settore), count(fondata) FROM aziende_pdl "
                           "GROUP BY 1 ORDER BY 2 DESC LIMIT 15"):
            print(f"  {str(r[0]):4} {r[1]:7} settore={r[2]:7} fondata={r[3]:7}")
        print("  totale:", c.execute("SELECT count(*) FROM aziende_pdl").fetchone()[0])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.ats.aziende_pdl", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lago", action="store_true"); ap.add_argument("--aggancia", action="store_true")
    ap.add_argument("--stato", action="store_true")
    ap.add_argument("--memoria", default="6GB"); ap.add_argument("--thread", type=int, default=4)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    if a.lago:
        print("lago pdl:", lago(a.memoria, a.thread))
    if a.aggancia:
        print("aggancio pdl:", aggancia(_dsn(), a.memoria, a.thread))
    if a.stato:
        stato(_dsn())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
