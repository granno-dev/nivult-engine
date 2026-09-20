"""La resa si calcola e l'ordine di pesca cambia davvero?"""
import importlib.util, os, psycopg
sp = importlib.util.spec_from_file_location("cd", "/opt/nivult/caccia_domini.py")
m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
with psycopg.connect(os.environ["ATS_DATABASE_URL"], autocommit=True) as c:
    c.execute(m.SQL_RESA)
    media = c.execute(m.MEDIA).fetchone()[0]
    print(f"  media generale: {media:.3f}")
    print("  resa per piattaforma, le prime dieci:")
    for nome, pr, tr, resa in c.execute(
        "select p.name, r.provate, r.trovate, r.resa from caccia_resa r "
        "join ats_platforms p on p.id = r.platform_id where r.resa is not null "
        "order by r.resa desc limit 10"):
        print(f"    {nome[:26]:<28}{pr:>7,} provate{tr:>7,} trovate{100*resa:>6.0f}%")
    print("\n  chi pescherebbe adesso (prime dieci, senza prenotare):")
    sql = m.SQL_CODA.split("RETURNING")[0].replace("UPDATE ats_companies c SET dominio_cercato_at = now()\n WHERE c.id IN (", "").rstrip().rstrip(")")
    sql = sql.replace("FOR UPDATE SKIP LOCKED", "")
    q = ("SELECT p.name, a.company_name, a.job_count FROM ats_companies a "
         "JOIN ats_platforms p ON p.id = a.platform_id WHERE a.id IN (" + sql + ")")
    for nome, az, jc in c.execute(q, (media, 10)):
        print(f"    {nome[:24]:<26}{(az or '?')[:30]:<32}{jc:>5} offerte")
