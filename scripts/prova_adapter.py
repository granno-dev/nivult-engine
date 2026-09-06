#!/usr/bin/env python3
"""Il banco di prova dell'officina: un adapter contro una pagina salvata.

    prova_adapter.py <piattaforma> --repo <clone> --campione pagina.html --attese attese.json [--vivo]

Niente database, niente rete (salvo --vivo): la pagina arriva da un file
e il client HTTP dell'adapter viene sostituito da uno finto che la
restituisce a qualunque richiesta. Il verdetto e' sulla riga finale:

    PROVA: OK   ...      exit 0
    PROVA: FAIL ...      exit 1

Cosa deve essere vero perche' passi:
  1. l'adapter trova ALMENO UNA offerta, entro 60 secondi;
  2. ritrova almeno l'80% delle offerte d'esempio (attese.json) per URL,
     e per QUELLE l'external_id e' IDENTICO a quello in archivio — se
     cambia, alla prossima lettura ogni offerta diventerebbe un doppione
     e le vecchie scadrebbero: e' il controllo piu' importante;
  3. titoli puliti: non vuoti, senza tag HTML, senza entita' (&amp;);
  4. URL assoluti (http…);
  5. con --vivo: la lettura vera del tenant torna almeno un'offerta.
"""
from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import time


def _norma(u: str) -> str:
    u = (u or "").strip().split("#", 1)[0]
    u = re.sub(r"^https?://(www\.)?", "", u)
    return u.rstrip("/").lower()


def _norma_senza_query(u: str) -> str:
    return _norma(u).split("?", 1)[0]


class _Scaduto(Exception):
    pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("piattaforma")
    ap.add_argument("--repo", required=True, help="clone del repo da cui importare gli adapter")
    ap.add_argument("--campione", required=True)
    ap.add_argument("--attese", required=True)
    ap.add_argument("--vivo", action="store_true", help="in piu', una lettura vera del tenant")
    ap.add_argument("--secondi", type=int, default=60)
    a = ap.parse_args()

    sys.path.insert(0, f"{a.repo}/src")
    import httpx
    from nivult.ats import adapters as mod
    assert mod.__file__.startswith(a.repo), f"importato l'adapter sbagliato: {mod.__file__}"
    ADAPTERS = mod.ADAPTERS
    if a.piattaforma not in ADAPTERS:
        print(f"PROVA: FAIL piattaforma sconosciuta: {a.piattaforma}"); return 1

    attese = json.load(open(a.attese))
    pagina = open(a.campione, errors="replace").read()
    ct = "application/json" if pagina.lstrip()[:1] in "{[" else "text/html"
    slug = attese["slug"]
    problemi, avvisi = [], []

    def finto(req):
        return httpx.Response(200, text=pagina, headers={"content-type": ct}, request=req)

    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(_Scaduto()))
    t0 = time.time()
    try:
        signal.alarm(a.secondi)
        with ADAPTERS[a.piattaforma]() as ad:
            ad.client = httpx.Client(transport=httpx.MockTransport(finto), follow_redirects=True)
            jobs = ad.jobs(slug)
        signal.alarm(0)
    except _Scaduto:
        print(f"PROVA: FAIL l'adapter non ha finito in {a.secondi}s (backtracking del regex?)"); return 1
    except Exception as exc:  # noqa: BLE001
        signal.alarm(0)
        print(f"PROVA: FAIL eccezione nell'adapter: {type(exc).__name__}: {str(exc)[:200]}"); return 1
    ms = int((time.time() - t0) * 1000)

    n = len(jobs)
    print(f"trovate {n} offerte in {ms} ms (attese circa {attese.get('attese', '?')})")
    if n == 0:
        problemi.append("zero offerte dalla pagina")
    att = attese.get("attese") or 0
    if att and n and n < att * 0.5:
        avvisi.append(f"trovate {n}, attese ~{att}: meno della meta'")
    if att and n > att * 3:
        avvisi.append(f"trovate {n}, attese ~{att}: piu' del triplo (righe doppie? link non-offerta?)")

    # 2. gli esempi: URL ritrovato ed external_id identico
    per_url = {}
    for j in jobs:
        per_url.setdefault(_norma(j.url), j)
        per_url.setdefault(_norma_senza_query(j.url), j)
    esempi = attese.get("esempi", [])
    ritrovati = 0
    for e in esempi:
        j = per_url.get(_norma(e["url"])) or per_url.get(_norma_senza_query(e["url"]))
        if not j:
            continue
        ritrovati += 1
        if str(j.external_id) != str(e["external_id"]):
            problemi.append(f"external_id CAMBIATO per {e['url'][:70]}: archivio {e['external_id']!r}, adapter {j.external_id!r}")
        if e.get("title") and j.title and _norma(e["title"])[:20] != _norma(j.title)[:20]:
            avvisi.append(f"titolo diverso per {e['url'][:60]}: archivio {e['title'][:40]!r}, adapter {j.title[:40]!r}")
    if esempi:
        q = ritrovati / len(esempi)
        print(f"esempi ritrovati: {ritrovati}/{len(esempi)}")
        if q < 0.8:
            problemi.append(f"ritrovati solo {ritrovati} esempi su {len(esempi)} (serve l'80%)")

    # 3-4. igiene
    for j in jobs[:500]:
        t = j.title or ""
        if not t.strip():
            problemi.append(f"titolo vuoto su {j.url[:70]}"); break
        if "<" in t or "&amp;" in t or "&#" in t or len(t) > 300:
            problemi.append(f"titolo sporco: {t[:80]!r}"); break
        if not str(j.url).startswith("http"):
            problemi.append(f"URL non assoluto: {j.url[:80]!r}"); break
        if not j.external_id:
            problemi.append(f"external_id vuoto su {j.url[:70]}"); break

    # 5. dal vivo
    if a.vivo:
        try:
            signal.alarm(a.secondi)
            with ADAPTERS[a.piattaforma]() as ad:
                vivi = ad.jobs(slug)
            signal.alarm(0)
            print(f"dal vivo: {len(vivi)} offerte")
            if not vivi:
                problemi.append("dal vivo: zero offerte")
        except Exception as exc:  # noqa: BLE001
            signal.alarm(0)
            problemi.append(f"dal vivo: {type(exc).__name__}: {str(exc)[:120]}")

    for w in avvisi:
        print("avviso:", w)
    for p in problemi:
        print("PROBLEMA:", p)
    if jobs:
        j = jobs[0]
        print(f"esempio: {j.external_id!r} | {j.title[:60]!r} | {j.location!r} | {j.url[:80]}")
    if problemi:
        print(f"PROVA: FAIL {len(problemi)} problemi, {len(avvisi)} avvisi"); return 1
    print(f"PROVA: OK {n} offerte, {ritrovati}/{len(esempi)} esempi, {len(avvisi)} avvisi"); return 0


if __name__ == "__main__":
    raise SystemExit(main())
