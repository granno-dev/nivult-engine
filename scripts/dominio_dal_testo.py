"""Il sito dell'azienda e' dentro il testo dell'annuncio?

Le API delle cinque piattaforme cieche non lo danno — provato: Ashby e Lever
restituiscono solo le offerte, BambooHR solo la lista, e le loro pagine HTML non
contengono nulla. Ma il testo degli annunci ce l'abbiamo GIA' in archivio: se
un'azienda scrive «visita www.esempio.com» o mette un indirizzo email aziendale,
il dominio e' li' e non costa una richiesta di rete.

Questa misura NON scrive niente: dice solo quante aziende avrebbero un candidato,
e mostra i campioni. Il candidato andra' comunque passato dal giudice del
cacciatore (il sito deve rimandare al NOSTRO tenant), esattamente come gli altri.
"""
import psycopg, pathlib, re, collections, html as _html

env = dict(re.findall(r"^(\w+)=(.*)$", pathlib.Path("/opt/nivult/engine/.env").read_text(), re.M))
u = env["DATABASE_URL"].strip().strip("\"'").rsplit("/", 1)[0] + "/nivult_ats"

CAMPI = ("description", "content", "descriptionHtml", "descriptionPlain", "externalDescription",
         "jobDescription", "job_description", "body", "content_html", "description_html", "text")

# chi NON e' l'azienda. La stessa disciplina che ci e' costata 492 domini falsi:
# gli ATS, le reti di distribuzione, i social, i traccianti e i costruttori di siti.
FUORI = re.compile(
    r"(ashbyhq|ashbyprd|bamboohr|lever\.co|workable|myworkday|workday|greenhouse|smartrecruiters|"
    r"personio|teamtailor|recruitee|icims|jobvite|taleo|successfactors|rippling|careerpuck|"
    r"linkedin|facebook|twitter|x\.com|instagram|youtube|youtu\.be|tiktok|glassdoor|indeed|"
    r"google|gstatic|googleapis|gmail|outlook|hotmail|yahoo|schema\.org|w3\.org|ogp\.me|"
    r"cloudfront|amazonaws|akamai|fastly|jsdelivr|unpkg|cdnjs|jquery|bootstrap|fontawesome|"
    r"wixsite|squarespace|wordpress|weebly|webflow|netlify|vercel|herokuapp|rollbar|sentry|"
    r"eeoc\.gov|dol\.gov|adobe|microsoft|apple\.com|mozilla|wikipedia|calendly|zoom\.us|"
    r"bit\.ly|tinyurl|goo\.gl|t\.co)", re.I)

URL = re.compile(r"https?://([a-z0-9][-a-z0-9.]*\.[a-z]{2,})", re.I)
MAIL = re.compile(r"[a-zA-Z0-9._%+-]+@([a-z0-9][-a-z0-9.]*\.[a-z]{2,})", re.I)
TAG = re.compile(r"<[^>]+>")

sql = """
SELECT p.name, a.company_name, a.slug,
       (SELECT string_agg(t, ' ') FROM (
          SELECT coalesce({campi}) AS t FROM ats_jobs j
           WHERE j.platform_id = a.platform_id AND j.slug = a.slug AND j.expired_at IS NULL
           LIMIT 3) z)
  FROM ats_companies a JOIN ats_platforms p ON p.id = a.platform_id
 WHERE p.name = ANY(%s) AND a.site_domain IS NULL
   AND EXISTS (SELECT 1 FROM ats_jobs j WHERE j.platform_id=a.platform_id
                AND j.slug=a.slug AND j.expired_at IS NULL)
 ORDER BY random() LIMIT %s
""".format(campi=", ".join(f"j.raw->>'{c}'" for c in CAMPI))

CIECHE = ["BambooHR", "Workable", "Ashby", "Lever", "Workday"]
N = 400

with psycopg.connect(u) as c:
    righe = c.execute(sql, (CIECHE, N)).fetchall()

per_piatt = collections.Counter()
tot_piatt = collections.Counter()
esempi = []
for piatt, nome, slug, testo in righe:
    tot_piatt[piatt] += 1
    if not testo:
        continue
    t = _html.unescape(TAG.sub(" ", testo))
    cand = []
    for h in URL.findall(t) + MAIL.findall(t):
        h = h.lower().strip(".").removeprefix("www.")
        if h.count(".") <= 2 and not FUORI.search(h) and h not in cand:
            cand.append(h)
    if cand:
        per_piatt[piatt] += 1
        if len(esempi) < 16:
            esempi.append((piatt, nome or slug, cand[:3]))

print(f"campione: {len(righe)} aziende senza dominio sulle cinque piattaforme cieche\n")
print(f"{'piattaforma':<12}{'nel campione':>14}{'con un candidato nel testo':>28}")
for p in CIECHE:
    if tot_piatt[p]:
        print(f"{p:<12}{tot_piatt[p]:>14}{per_piatt[p]:>18} ({100*per_piatt[p]/tot_piatt[p]:>3.0f}%)")
tot = sum(tot_piatt.values()); con = sum(per_piatt.values())
print(f"{'TOTALE':<12}{tot:>14}{con:>18} ({100*con/max(tot,1):>3.0f}%)")

print("\ncandidati trovati (da verificare col giudice, non da scrivere):")
for piatt, nome, cand in esempi:
    print(f"  {piatt:<10}{(nome or '?')[:28]:<30}{', '.join(cand)}")
