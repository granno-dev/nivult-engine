"""Seconda strada: le API pubbliche non danno il sito, quindi dove sta?

La prima sonda ha escluso l'ipotesi comoda — `posting-api` di Ashby, `careers/list`
di BambooHR, `v0/postings` di Lever e il widget di Workable restituiscono le
offerte e basta. Qui si provano i posti dove il dato PUO' stare:

  Ashby     il GraphQL che usa la bacheca stessa, che carica l'organizzazione
  Workable  la `description` dell'account, che e' HTML e puo' contenere il link
  BambooHR  la pagina /careers, che e' JavaScript ma spesso porta uno stato JSON
  Lever     la pagina della bacheca, dove il logo rimanda al sito

E si guarda anche quello che NON cercavamo ma serve: Workable regala il NOME
dell'azienda, e di nomi ce ne mancano 7.071.
"""
from __future__ import annotations
import json
import re
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; NivultBot/1.0; +https://nivult.com/bot)"
NON_AZIENDA = re.compile(
    r"(ashbyhq|ashbyprd|bamboohr|lever\.co|workable|myworkday|greenhouse|w3\.org|"
    r"cloudfront|amazonaws|googleapis|gstatic|google|facebook|twitter|linkedin|"
    r"youtube|instagram|schema\.org|jsdelivr|unpkg|jquery|gravatar|licdn)", re.I)


def prendi(url, dati=None, tipo="application/json"):
    h = {"User-Agent": UA, "Accept": "application/json, text/html, */*"}
    if dati is not None:
        h["Content-Type"] = tipo
    rq = urllib.request.Request(url, data=dati, headers=h)
    try:
        with urllib.request.urlopen(rq, timeout=30) as r:
            return r.status, r.read(2_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, (e.read(4000).decode("utf-8", "replace") if e.fp else "")
    except Exception as e:                                           # noqa: BLE001
        return 0, str(e)[:80]


def host_utili(t, limite=5):
    fuori, visti = [], set()
    for h in re.findall(r"https?://([a-z0-9][-a-z0-9.]*\.[a-z]{2,})", t or "", re.I):
        h = h.lower().strip(".")
        if h in visti or NON_AZIENDA.search(h) or h.count(".") > 3:
            continue
        visti.add(h); fuori.append(h)
    return fuori[:limite]


print("=" * 78, "\nASHBY — il GraphQL della bacheca")
Q = ("query ApiJobBoardWithTeams($organizationHostedJobsPageName: String!) {"
     " jobBoard: jobBoardWithTeams(organizationHostedJobsPageName: $organizationHostedJobsPageName)"
     " { organizationName teams { id name } } }")
for s in ("airwallex", "bjakcareer", "airapps"):
    corpo = json.dumps({"operationName": "ApiJobBoardWithTeams",
                        "variables": {"organizationHostedJobsPageName": s},
                        "query": Q}).encode()
    st, t = prendi("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobBoardWithTeams", corpo)
    print(f"  {s:<16}{st:<5}{len(t):>7} byte   {t[:110].replace(chr(10), ' ')}")
    time.sleep(2)

print("\n" + "=" * 78, "\nWORKABLE — nome e descrizione dell'account")
for s in ("cxg", "kreyco", "afg"):
    st, t = prendi(f"https://apply.workable.com/api/v1/widget/accounts/{s}")
    if st == 200:
        try:
            d = json.loads(t)
            desc = d.get("description") or ""
            print(f"  {s:<16}nome = {str(d.get('name'))[:40]:<42}siti nella descrizione: {host_utili(desc)}")
        except Exception:                                            # noqa: BLE001
            print(f"  {s:<16}risposta non leggibile")
    else:
        print(f"  {s:<16}stato {st}")
    time.sleep(2)

print("\n" + "=" * 78, "\nBAMBOOHR — la pagina /careers")
for s in ("theweitzcompany", "armstrongfluidtechnology", "quiktrakcontractor"):
    st, t = prendi(f"https://{s}.bamboohr.com/careers")
    nome = re.search(r'"companyName"\s*:\s*"([^"]{2,80})"', t or "")
    sito = re.search(r'"(?:companyWebsite|websiteUrl|website)"\s*:\s*"([^"]{4,90})"', t or "")
    print(f"  {s:<26}{st:<5}{len(t):>7} byte  nome={nome.group(1)[:26] if nome else '-':<28}"
          f"sito={sito.group(1)[:34] if sito else '-'}")
    if st == 200 and not sito:
        print(f"      host nella pagina: {host_utili(t)}")
    time.sleep(2)

print("\n" + "=" * 78, "\nLEVER — la pagina della bacheca")
for s in ("boxlunch", "globalelitecareers", "aogarciaagency"):
    st, t = prendi(f"https://jobs.lever.co/{s}")
    print(f"  {s:<22}{st:<5}{len(t):>7} byte  host: {host_utili(t)}")
    time.sleep(2)
