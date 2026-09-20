"""Quanto e' vendibile davvero `aziende_vendibili`?

Due difetti visti a occhio sulla prima scheda, da misurare:
  1. DOPPIONI — «Groupement Mousquetaires» compare due volte con lo stesso
     dominio. Su un prodotto che si paga per azienda rivelata, vendere due volte
     la stessa azienda e' il difetto peggiore che ci sia.
  2. DOMINIO DELLE CARRIERE — `carrieres-mousquetaires.com` invece di
     `mousquetaires.com`, `accorhotels-ausbildung.de` invece di `accor.com`. La
     prova dell'aggancio regge (quel sito rimanda davvero al nostro tenant), ma
     chi compra vuole il dominio aziendale per incrociarlo col proprio CRM.
"""
import psycopg, pathlib, re

env = dict(re.findall(r"^(\w+)=(.*)$", pathlib.Path("/opt/nivult/engine/.env").read_text(), re.M))
u = env["DATABASE_URL"].strip().strip("\"'").rsplit("/", 1)[0] + "/nivult_ats"

# parole che tradiscono un sito di reclutamento, non la sede dell'azienda
CARRIERE = r"(carrier|career|jobs?|lavora|ausbildung|recruit|hiring|talent|werken|empleo|karriere|stage)"

with psycopg.connect(u) as c:
    tot = c.execute("select count(*) from aziende_vendibili").fetchone()[0]
    con = c.execute("select count(*) from aziende_vendibili where dominio is not null").fetchone()[0]

    print("=== 1. DOPPIONI ===")
    gruppi, righe = c.execute("""
        SELECT count(*), coalesce(sum(n), 0) FROM (
          SELECT dominio, count(*) AS n FROM aziende_vendibili
           WHERE dominio IS NOT NULL GROUP BY 1 HAVING count(*) > 1) g""").fetchone()
    print(f"domini posseduti da piu' aziende: {gruppi:,} domini, {righe:,} righe "
          f"({100*righe/max(con,1):.1f}% di quelle con dominio)")
    print("i peggiori:")
    for d, n, nomi in c.execute("""
        SELECT dominio, count(*), string_agg(DISTINCT nome, ' | ') FROM aziende_vendibili
         WHERE dominio IS NOT NULL GROUP BY 1 HAVING count(*) > 1
         ORDER BY 2 DESC LIMIT 5"""):
        print(f"  {d:<40}{n:>3} righe   {(nomi or '')[:70]}")

    print("\nstesso NOME, righe diverse (anche senza dominio):")
    gn, rn = c.execute("""
        SELECT count(*), coalesce(sum(n), 0) FROM (
          SELECT lower(nome) AS x, count(*) AS n FROM aziende_vendibili
           WHERE nome IS NOT NULL GROUP BY 1 HAVING count(*) > 1) g""").fetchone()
    print(f"  {gn:,} nomi ripetuti su {rn:,} righe ({100*rn/max(tot,1):.1f}% della tabella)")

    print("\n=== 2. DOMINIO DELLE CARRIERE ===")
    n_car = c.execute("select count(*) from aziende_vendibili where dominio is not null and dominio ~* %s",
                      (CARRIERE,)).fetchone()[0]
    print(f"domini che contengono una parola da reclutamento: {n_car:,} su {con:,} "
          f"({100*n_car/max(con,1):.1f}%)")
    for d, nome in c.execute("select dominio, nome from aziende_vendibili "
                             "where dominio is not null and dominio ~* %s order by offerte_attive desc limit 6",
                             (CARRIERE,)):
        print(f"  {d:<44}{(nome or '')[:40]}")

    print("\n=== 3. COMPLETEZZA ===")
    for k, s in [
        ("righe totali",                 "select count(*) from aziende_vendibili"),
        ("scheda COMPLETA (dominio+tec+settore)",
         "select count(*) from aziende_vendibili where dominio is not null and n_tecnologie > 0 and settore is not null"),
        ("dominio + tecnologie",         "select count(*) from aziende_vendibili where dominio is not null and n_tecnologie > 0"),
        ("lette dal 2B ma zero tecnologie",
         "select count(*) from aziende_vendibili where offerte_lette > 0 and n_tecnologie = 0"),
        ("mai lette dal 2B",             "select count(*) from aziende_vendibili where offerte_lette = 0"),
    ]:
        print(f"  {k:<40}{c.execute(s).fetchone()[0]:>9,}")
