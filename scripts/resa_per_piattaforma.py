"""Il cacciatore rende davvero meno sulle cinque piattaforme «cieche»?

Avevo ragionato come se quelle 18.712 aziende ricevessero zero. Non e' vero: il
logo e' solo UNA delle quattro fonti della cascata, e le altre tre — email
nell'annuncio, nome generato, SearXNG — funzionano su qualunque piattaforma.

Quindi la domanda giusta non e' «quanto rende il logo», e' «quanto rende la
cascata intera, piattaforma per piattaforma, su quelle che ha gia' provato».
"""
import psycopg, pathlib, re

env = dict(re.findall(r"^(\w+)=(.*)$", pathlib.Path("/opt/nivult/engine/.env").read_text(), re.M))
u = env["DATABASE_URL"].strip().strip("\"'").rsplit("/", 1)[0] + "/nivult_ats"
CIECHE = ("BambooHR", "Workable", "Ashby", "Lever", "Workday")

with psycopg.connect(u) as c:
    print("resa della cascata sulle aziende GIA' PROVATE dal cacciatore, per piattaforma:")
    print(f"{'piattaforma':<24}{'provate':>9}{'trovate':>9}{'resa':>7}   {'ancora da provare':>18}")
    tot_p = tot_t = 0
    for piatt, provate, trovate, restano in c.execute("""
        SELECT p.name,
               count(*) FILTER (WHERE a.dominio_cercato_at IS NOT NULL),
               count(*) FILTER (WHERE a.dominio_cercato_at IS NOT NULL AND a.site_domain IS NOT NULL),
               count(*) FILTER (WHERE a.dominio_cercato_at IS NULL AND a.site_domain IS NULL)
          FROM ats_companies a JOIN ats_platforms p ON p.id = a.platform_id
         WHERE EXISTS (SELECT 1 FROM ats_jobs j WHERE j.platform_id=a.platform_id
                        AND j.slug=a.slug AND j.expired_at IS NULL)
         GROUP BY 1 HAVING count(*) FILTER (WHERE a.dominio_cercato_at IS NOT NULL) >= 20
         ORDER BY 2 DESC"""):
        cieca = " (cieca)" if piatt in CIECHE else ""
        tot_p += provate; tot_t += trovate
        print(f"{piatt[:22]+cieca:<24}{provate:>9,}{trovate:>9,}{100*trovate/provate:>6.0f}%{restano:>19,}")
    if tot_p:
        print(f"{'TOTALE':<24}{tot_p:>9,}{tot_t:>9,}{100*tot_t/tot_p:>6.0f}%")

    print("\nda quale fonte vengono i domini trovati dal cacciatore:")
    for fonte, q in c.execute("""
        SELECT site_domain_source, count(*) FROM ats_companies
         WHERE site_domain IS NOT NULL AND site_domain_source NOT IN ('vanity','brandfetch')
         GROUP BY 1 ORDER BY 2 DESC"""):
        print(f"  {fonte or '?':<20}{q:>7,}")
