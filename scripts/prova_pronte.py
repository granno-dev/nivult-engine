"""Le righe di `aziende_pronte` reggono davvero?

Non basta che i filtri siano passati: si guarda il contenuto. E si controlla che
i tre filtri facciano quello che dicono — niente bacheche, niente senza nome,
niente doppioni.
"""
import psycopg, pathlib, re

env = dict(re.findall(r"^(\w+)=(.*)$", pathlib.Path("/opt/nivult/engine/.env").read_text(), re.M))
u = env["DATABASE_URL"].strip().strip("\"'").rsplit("/", 1)[0] + "/nivult_ats"

with psycopg.connect(u) as c:
    def n(s):
        return c.execute(s).fetchone()[0]

    print("i filtri mordono?")
    for t, s in [
        # la vista non espone e_bacheca (le bacheche le toglie): si verifica risalendo
        ("bacheche rimaste in aziende_pronte",
         """SELECT count(*) FROM aziende_pronte p JOIN aziende_vendibili v USING (company_id)
             WHERE v.e_bacheca"""),
        ("righe senza nome rimaste",             "SELECT count(*) FROM aziende_pronte WHERE nome IS NULL"),
        ("doppioni rimasti (non capofila)",      "SELECT count(*) FROM aziende_pronte WHERE canonico_id IS NOT NULL AND canonico_id <> company_id"),
        ("capofila che non sono in aziende_pronte",
         """SELECT count(DISTINCT canonico_id) FROM aziende_vendibili v
             WHERE canonico_id IS NOT NULL AND canonico_id <> company_id
               AND NOT EXISTS (SELECT 1 FROM aziende_pronte p WHERE p.company_id = v.canonico_id)"""),
    ]:
        q = n(s)
        print(f"  [{'ok' if q == 0 else 'DA GUARDARE'}] {t}: {q:,}")

    print("\nle bacheche piu' grosse, escluse dal prodotto:")
    for nome, slug, nomi, off in c.execute("""
        SELECT nome, slug, nomi_dichiarati, offerte_attive FROM aziende_vendibili
         WHERE e_bacheca ORDER BY offerte_attive DESC LIMIT 5"""):
        print(f"  {(nome or slug)[:30]:<32}{nomi:>5} datori diversi{off:>8,} offerte")

    print("\nun gruppo di doppioni, come viene rappresentato:")
    capo = c.execute("""SELECT canonico_id FROM aziende_vendibili
                         WHERE canonico_id IS NOT NULL AND canonico_id <> company_id
                         GROUP BY 1 ORDER BY count(*) DESC LIMIT 1""").fetchone()[0]
    for nome, slug, att, cid, capoid in c.execute("""
        SELECT nome, slug, offerte_attive, company_id, canonico_id FROM aziende_vendibili
         WHERE canonico_id = %s ORDER BY offerte_attive DESC""", (capo,)):
        print(f"  {'CAPOFILA' if cid == capoid else '        '} {(nome or '?')[:28]:<30}{slug[:26]:<28}{att:>6} offerte")

    print("\nquattro schede pronte e complete:")
    for nome, dom, pae, sett, att, tec in c.execute("""
        SELECT nome, dominio, paese, settore, offerte_attive, tecnologie FROM aziende_pronte
         WHERE dominio IS NOT NULL AND n_tecnologie >= 3 AND NOT dominio_e_carriere
         ORDER BY offerte_attive DESC LIMIT 4"""):
        t = ", ".join(f"{x['nome']}({x['offerte']})" for x in (tec or [])[:6])
        print(f"  {nome[:34]:<36}{dom[:28]:<30}{pae or '?':<4}{att:>5} off.")
        print(f"      settore: {(sett or '?')[:50]}")
        print(f"      stack:   {t[:88]}")
