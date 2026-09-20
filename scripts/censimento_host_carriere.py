#!/usr/bin/env python3
"""Censimento degli host dietro le career page «senza ATS».

Il detector ha lasciato 38.000 domini con una pagina carriere ma nessuna
impronta nota (`company_domains.status = 'no_ats'`). Molte di quelle
pagine rimandano a un ATS che NON conosciamo: un host esterno con la
lista delle offerte, un iframe, un form. Qui si rifà il giro su un
campione, si raccolgono tutti gli host esterni citati dalla pagina
carriere (e dalla pagina «cerca offerte» un clic più in là), e si
contano per quante aziende diverse compaiono. Un host citato da
cinquanta aziende e' un fornitore: quello e' un adapter da scrivere.

    ATS_DATABASE_URL=... python scripts/censimento_host_carriere.py --limite 2000 --thread 16 \
        --out /opt/nivult/censimento-host.jsonl

Il riepilogo (host per numero di aziende, con esempi) si stampa alla
fine e si puo' rigenerare con --riepilogo <file>.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from nivult.ats.detector import FINGERPRINT, PAROLE_CARRIERE, _link_carriere  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0 (compatible; nivult-ats/1.0; +https://nivult.com)"}
# host che non sono fornitori di ATS: social, CDN, motori, servizi generici
RUMORE = re.compile(r"(facebook|instagram|linkedin|twitter|x\.com|youtube|tiktok|google|gstatic|googleapis|"
                    r"cloudflare|cloudfront|amazonaws|akamai|jsdelivr|unpkg|bootstrapcdn|fontawesome|typekit|"
                    r"wp\.com|wordpress|gravatar|vimeo|apple\.com|microsoft|office\.com|adobe|hubspot|mailchimp|"
                    r"cookiebot|onetrust|iubenda|trustpilot|whatsapp|telegram|glassdoor|indeed|kununu|xing|"
                    r"vimeo|spotify|paypal|shopify|w3\.org|schema\.org|creativecommons|wikipedia|maps\.|"
                    r"bing\.com|yahoo|yandex|baidu|pinterest|snapchat|flickr|tumblr|reddit|medium\.com|"
                    r"cdn\.|static\.|assets\.|fonts\.|analytics|tagmanager|doubleclick|hotjar|matomo|"
                    r"recaptcha|hcaptcha|gov\.it|europa\.eu)", re.I)
NOTI = [m for marche in FINGERPRINT.values() for m in marche if "." in m]
PAROLE_RICERCA = re.compile(r"search[-_]?jobs?|job[-_]?search|vacanc|stellenangebote|offres?-d|job-listings|"
                            r"open[-_]?positions?|find-a-job|job-openings|current[-_]?openings|posizioni|offerte|"
                            r"lavora-con-noi|candidat|apply|bewerb|postul", re.I)


def _host(u: str) -> str:
    return urlparse(u).netloc.lower().split(":")[0]


def _esterni(html: str, base: str, dominio: str) -> collections.Counter:
    """Gli host esterni citati: href, iframe/frame src, form action, script src."""
    out: collections.Counter = collections.Counter()
    for m in re.finditer(r'(?:href|src|action)\s*=\s*["\']([^"\']+)["\']', html, re.I):
        u = urljoin(base, m.group(1).strip())
        h = _host(u)
        if not h or h == dominio or h.endswith("." + dominio) or RUMORE.search(h):
            continue
        out[h] += 1
    return out


def censisci(dominio: str) -> dict:
    esito = {"dominio": dominio, "carriere": None, "host": {}, "noto": None, "errore": None}
    try:
        with httpx.Client(timeout=15, headers=UA, follow_redirects=True) as cl:
            r = cl.get(f"https://{dominio}")
            if r.status_code >= 400:
                esito["errore"] = f"http {r.status_code}"; return esito
            dom = dominio.lower().removeprefix("www.")
            link = _link_carriere(str(r.url), r.text)
            visti = 0
            for url in link[:3]:
                try:
                    rc = cl.get(url)
                except httpx.HTTPError:
                    continue
                if rc.status_code != 200 or len(rc.text) < 300:
                    continue
                visti += 1
                esito["carriere"] = esito["carriere"] or str(rc.url)
                # la pagina carriere stessa puo' stare su un host esterno (jobs.fornitore.com/azienda)
                hc = _host(str(rc.url))
                if hc and hc != dom and not hc.endswith("." + dom) and not RUMORE.search(hc):
                    esito["host"][hc] = esito["host"].get(hc, 0) + 50
                for h, n in _esterni(rc.text, str(rc.url), dom).items():
                    esito["host"][h] = esito["host"].get(h, 0) + n
                # un clic piu' in la': la pagina «cerca offerte»
                for m in re.finditer(r'href\s*=\s*["\']([^"\']+)["\']', rc.text, re.I):
                    u2 = urljoin(str(rc.url), m.group(1))
                    if PAROLE_RICERCA.search(u2) and _host(u2) not in ("", ):
                        try:
                            r2 = cl.get(u2)
                        except httpx.HTTPError:
                            continue
                        if r2.status_code == 200:
                            h2 = _host(str(r2.url))
                            if h2 and h2 != dom and not h2.endswith("." + dom) and not RUMORE.search(h2):
                                esito["host"][h2] = esito["host"].get(h2, 0) + 50
                            for h, n in _esterni(r2.text, str(r2.url), dom).items():
                                esito["host"][h] = esito["host"].get(h, 0) + n
                        break
                if visti >= 2:
                    break
            # gia' noto al registro? (allora e' il detector che non l'ha visto)
            for h in esito["host"]:
                for m in NOTI:
                    if m in h:
                        esito["noto"] = m
    except Exception as exc:  # noqa: BLE001
        esito["errore"] = type(exc).__name__
    return esito


def riepilogo(percorso: str, minimo: int = 3) -> None:
    per_host: dict[str, set] = collections.defaultdict(set)
    esempi: dict[str, str] = {}
    n = con_carriere = noti = 0
    for line in open(percorso):
        e = json.loads(line); n += 1
        if e.get("carriere"):
            con_carriere += 1
        if e.get("noto"):
            noti += 1
        for h in e.get("host", {}):
            per_host[h].add(e["dominio"]); esempi.setdefault(h, e["dominio"])
    print(f"domini censiti {n}, con pagina carriere {con_carriere}, con un ATS gia' noto sfuggito al detector {noti}")
    print(f"\nHOST ESTERNI per numero di aziende che li citano (>= {minimo}):")
    righe = sorted(per_host.items(), key=lambda kv: -len(kv[1]))
    for h, doms in righe:
        if len(doms) < minimo:
            break
        noto = next((m for m in NOTI if m in h), "")
        print(f"  {len(doms):4d}  {h:50s} {'[NOTO: ' + noto + ']' if noto else ''}  es. {esempi[h]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=2000)
    ap.add_argument("--thread", type=int, default=16)
    ap.add_argument("--out", default="/opt/nivult/censimento-host.jsonl")
    ap.add_argument("--riepilogo", help="solo il riepilogo di un file gia' scritto")
    ap.add_argument("--minimo", type=int, default=3)
    a = ap.parse_args()
    if a.riepilogo:
        riepilogo(a.riepilogo, a.minimo); return 0
    import psycopg
    c = psycopg.connect(os.environ["ATS_DATABASE_URL"])
    domini = [r[0] for r in c.execute("""
        SELECT domain FROM company_domains WHERE status = 'no_ats'
         ORDER BY (country IN ('IT','DE','FR')) DESC, employees DESC NULLS LAST LIMIT %s""", (a.limite,))]
    t0 = time.time(); fatti = 0
    with open(a.out, "w") as f, ThreadPoolExecutor(max_workers=a.thread) as pool:
        for e in pool.map(censisci, domini):
            f.write(json.dumps(e, ensure_ascii=False) + "\n"); f.flush()
            fatti += 1
            if fatti % 200 == 0:
                print(f"  {fatti}/{len(domini)} in {int(time.time()-t0)}s", flush=True)
    print(f"scritto {a.out}: {fatti} domini in {int(time.time()-t0)}s")
    riepilogo(a.out, a.minimo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
