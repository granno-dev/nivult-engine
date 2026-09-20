"""Fin dove si puo' arrivare davvero coi domini?

Il cacciatore rende il 24%. La domanda e' cosa sia il restante 76%:
  (a) aziende che un sito NON CE L'HANNO — e allora il 95% e' irraggiungibile;
  (b) aziende che il sito ce l'hanno ma il nostro giudice lo rifiuta — e allora
      il limite e' nostro, non del mondo.

Sono due cose molto diverse e si distinguono guardando. Qui si prendono aziende
su cui il cacciatore ha GIA' fallito e si chiede: esiste almeno un candidato
plausibile? E quel candidato regge a un controllo piu' largo del nostro giudice
(il sito esiste, e il suo testo nomina l'azienda)?

Il giudice del cacciatore pretende che il sito rimandi al NOSTRO tenant ATS. E'
una prova fortissima — ed e' quella che ci ha salvati dai 492 domini falsi — ma
un'azienda puo' benissimo avere un sito che alla sua pagina lavora-con-noi non
mette il link, o lo mette dietro JavaScript.
"""
from __future__ import annotations
import json
import os
import re
import time
import urllib.parse
import urllib.request

import httpx
import psycopg

SEARX = "http://100.119.200.7:8899/search"
MOTORI = "google,yahoo"
UA = "Mozilla/5.0 (compatible; NivultBot/1.0; +https://nivult.com/bot)"
FUORI = re.compile(
    r"(ashbyhq|bamboohr|lever\.co|workable|myworkday|workday|greenhouse|smartrecruiters|"
    r"personio|teamtailor|recruitee|icims|jobvite|taleo|successfactors|rippling|catsone|"
    r"breezy|jazzhr|applytojob|vincere|zoho|cornerstone|werecruit|"
    r"linkedin|facebook|twitter|instagram|youtube|glassdoor|indeed|crunchbase|bloomberg|"
    r"zoominfo|wikipedia|google|yahoo|bing|pitchbook|dnb\.com|apollo\.io|rocketreach|"
    r"trustpilot|yelp|mapquest|bbb\.org|manta\.com|zippia|jooble|neuvoo|talent\.com)", re.I)

N = int(os.environ.get("N", "30"))
cli = httpx.Client(headers={"User-Agent": UA}, verify=False, follow_redirects=True, timeout=12)


def candidati(nome, paese):
    q = urllib.parse.quote(f"{nome} {paese} official website".strip()[:120])
    try:
        rq = urllib.request.Request(f"{SEARX}?q={q}&format=json&engines={MOTORI}",
                                    headers={"User-Agent": "nivult/1.0"})
        with urllib.request.urlopen(rq, timeout=35) as r:
            d = json.load(r)
    except Exception:                                            # noqa: BLE001
        return []
    fuori = []
    for x in (d.get("results") or [])[:12]:
        h = re.sub(r"^https?://(www\.)?", "", x.get("url") or "").split("/")[0].lower()
        if h and "." in h and h.count(".") <= 3 and not FUORI.search(h) and h not in fuori:
            fuori.append(h)
    return fuori[:3]


def nomina_azienda(dominio, nome):
    """Controllo largo: il sito esiste e il suo testo nomina l'azienda?"""
    parole = [w for w in re.split(r"[^a-z0-9]+", (nome or "").lower()) if len(w) >= 4][:3]
    if not parole:
        return None
    for schema in ("https://", "http://"):
        try:
            r = cli.get(schema + dominio)
            if r.status_code >= 400:
                continue
            t = r.text.lower()
            return sum(1 for w in parole if w in t) >= max(1, len(parole) - 1)
        except Exception:                                        # noqa: BLE001
            continue
    return None


with psycopg.connect(os.environ["ATS_DATABASE_URL"]) as c:
    righe = c.execute("""
        SELECT a.company_name, coalesce(a.country, ''), p.name
          FROM ats_companies a JOIN ats_platforms p ON p.id = a.platform_id
         WHERE a.dominio_cercato_at IS NOT NULL AND a.site_domain IS NULL
           AND a.company_name IS NOT NULL AND length(a.company_name) >= 4
           AND EXISTS (SELECT 1 FROM ats_jobs j WHERE j.platform_id=a.platform_id
                        AND j.slug=a.slug AND j.expired_at IS NULL)
         ORDER BY random() LIMIT %s""", (N,)).fetchall()

print(f"aziende su cui il cacciatore ha GIA' fallito: {len(righe)}\n")
print(f"{'azienda':<30}{'candidato':<34}{'il sito la nomina?'}")
senza_cand = regge = non_regge = irraggiungibile = 0
for nome, paese, piatt in righe:
    cand = candidati(nome, paese)
    if not cand:
        senza_cand += 1
        print(f"  {nome[:28]:<30}{'(nessun candidato)':<34}")
        continue
    d = cand[0]
    ok = nomina_azienda(d, nome)
    if ok is True:
        regge += 1; esito = "si'"
    elif ok is False:
        non_regge += 1; esito = "no"
    else:
        irraggiungibile += 1; esito = "sito irraggiungibile"
    print(f"  {nome[:28]:<30}{d[:32]:<34}{esito}")
    time.sleep(1)

t = len(righe)
print(f"\nsu {t} fallimenti del cacciatore:")
print(f"  nessun candidato trovato      {senza_cand:>4}  ({100*senza_cand/t:.0f}%)")
print(f"  candidato che NOMINA l'azienda{regge:>4}  ({100*regge/t:.0f}%)  <- domini che stiamo perdendo")
print(f"  candidato che non la nomina   {non_regge:>4}  ({100*non_regge/t:.0f}%)")
print(f"  candidato irraggiungibile     {irraggiungibile:>4}  ({100*irraggiungibile/t:.0f}%)")
