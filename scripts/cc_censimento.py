"""Censimento delle pagine carriere europee dall'indice colonnare di Common Crawl.
Scorre i 300 file parquet dell'ultimo crawl (predicate pushdown: si leggono
solo i row group dei TLD europei) e scrive dominio, TLD e path delle pagine
carriere vere. Output: /opt/nivult/cc_carriere.csv"""
import duckdb, gzip, urllib.request, time, sys, csv
CRAWL = sys.argv[1] if len(sys.argv) > 1 else "CC-MAIN-2026-34"
paths=[l.strip() for l in gzip.open(urllib.request.urlopen(f"https://data.commoncrawl.org/crawl-data/{CRAWL}/cc-index-table.paths.gz")).read().decode().splitlines() if "subset=warc" in l]
con=duckdb.connect(); con.execute("INSTALL httpfs; LOAD httpfs; SET threads=8;")
TLD = "('it','fr','de','es','nl','be','at','ch','pt','pl','se','dk','no','fi','ie','uk','lu','cz','hu','ro','gr')"
PAROLE = ["lavora-con-noi","lavora_con_noi","lavoraconnoi","posizioni-aperte","/carriere","/careers","/career/","/jobs","/job-","/candidati","opportunita-di-lavoro","entra-in-","recrutement","nous-rejoindre","rejoignez","offres-d-emploi","/carrieres","/emplois","karriere","stellenangebote","/stellen","jobs-und-karriere","trabaja-con-nosotros","/empleo","ofertas-de-empleo","unete","werken-bij","/vacatures","kariera","/praca","ledige-stillinger","/jobb","lediga-jobb","avoimet-tyopaikat","tyopaikat","/vacancies","join-us","joinus","/hiring"]
where_path = " OR ".join(f"url_path ILIKE '%{p}%'" for p in PAROLE)
INIZIO = int(sys.argv[2]) if len(sys.argv) > 2 else 0     # ripresa dopo un'interruzione
out = open("/opt/nivult/cc_carriere.csv", "a" if INIZIO else "w", newline=""); w = csv.writer(out)
t0=time.time(); tot=0
for i, f in enumerate(paths):
    if i < INIZIO:
        continue
    t=time.time()
    try:
        rows=con.execute(f"""SELECT DISTINCT url_host_registered_domain, url_host_tld, url_host_name, url_path
          FROM read_parquet('https://data.commoncrawl.org/{f}')
          WHERE url_host_tld IN {TLD} AND fetch_status=200 AND content_mime_detected='text/html'
            AND length(url_path) < 80 AND ({where_path})""").fetchall()
    except Exception as e:
        print(i, "ERRORE", str(e)[:120], flush=True); continue
    for r in rows: w.writerow(r)
    tot += len(rows); out.flush()
    print(f"{i+1}/300 {len(rows)} righe {int(time.time()-t)}s (tot {tot}, {int(time.time()-t0)}s)", flush=True)
print("FINE", tot, int(time.time()-t0), "s")
