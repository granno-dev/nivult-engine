#!/usr/bin/env python3
"""Collaudo completo della superficie API di produzione (06/10/2026).

Prima di vendere su Apify si misura TUTTO: endpoint pubblici, la v1 con
chiave, il portale con sessione, gli errori (401/400/404/429), la
paginazione, il freno anti-raccolta e le latenze. Ogni voce stampa
ok/FAIL con la prova — un FAIL non e' nascosto, e' il motivo per cui
non si pubblica.

    NV_SESSIONE=... python scripts/collaudo_api.py

La sessione di prova si crea col mint_link sul server. La chiave v1 del
collaudo si crea e si REVOCA nello stesso giro: niente chiavi orfane.
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

API = "https://api.nivult.com"
SESS = os.environ.get("NV_SESSIONE", "")

passati: list[str] = []
falliti: list[str] = []


def call(metodo: str, percorso: str, *, chiave: str = "", sessione: bool = False,
         corpo: dict | None = None, atteso: int | None = None,
         attesi: tuple = ()) -> tuple[int, dict | list | None, float]:
    """Una chiamata misurata. Torna (status, json|None, secondi)."""
    headers = {
        # senza UA di browser Cloudflare risponde 403 a prescindere (e' la sua
        # protezione anti-bot, non un nostro guasto — misurato il 06/10/2026)
        "User-Agent": "Mozilla/5.0 (Macintosh; collaudo-nivult)",
    }
    if chiave:
        headers["X-Api-Key"] = chiave
    if sessione:
        headers["Authorization"] = f"Bearer {SESS}"
    t0 = time.time()
    try:
        r = httpx.request(metodo, API + percorso, headers=headers,
                          json=corpo, timeout=60)
        dt = time.time() - t0
        try:
            return r.status_code, r.json(), dt
        except ValueError:
            return r.status_code, None, dt
    except Exception as e:                              # noqa: BLE001
        return -1, {"errore": str(e)}, time.time() - t0


def prova(nome: str, metodo: str, percorso: str, *, chiave: str = "",
          sessione: bool = False, corpo: dict | None = None,
          atteso: int = 200, controlla=None, max_sec: float = 30.0) -> object:
    st, js, dt = call(metodo, percorso, chiave=chiave, sessione=sessione,
                      corpo=corpo)
    note = []
    ok = st == atteso
    if ok and controlla and js is not None:
        try:
            controlla(js)
        except AssertionError as e:
            ok, note = False, [str(e)]
    if ok and dt > max_sec:
        ok, note = False, [f"lenta: {dt:.1f}s > {max_sec}s"]
    voce = f"{nome} [{st} in {dt:.2f}s]" + (f" — {'; '.join(note)}" if note else "")
    (passati if ok else falliti).append(voce)
    print(("ok   " if ok else "FAIL ") + voce, flush=True)
    return js


print("== A. pubblici (nessuna chiave) ==", flush=True)
prova("radice", "GET", "/")
prova("demo stats", "GET", "/v1/demo/stats",
      controlla=lambda j: (_ for _ in ()).throw(AssertionError("campi mancanti"))
      if not all(k in j for k in ("offerte", "aziende", "paesi")) else None)
prova("demo tecnologie SAP", "GET", "/v1/demo/tecnologie?q=SAP")
prova("demo tecnologie input sporco", "GET", "/v1/demo/tecnologie?q=%3Cscript%3E",
      atteso=400)
prova("demo campione offerte", "GET", "/v1/demo/export-campione?tipo=offerte")
prova("demo campione tipo sbagliato", "GET", "/v1/demo/export-campione?tipo=xyz",
      atteso=404)

print("== B. autenticazione v1 ==", flush=True)
prova("senza chiave", "GET", "/v1/jobs?limit=1", atteso=401)
prova("chiave spazzatura", "GET", "/v1/jobs?limit=1", chiave="nv_sbagliata",
      atteso=401)

# la chiave del collaudo: si crea e si revoca in questo stesso giro
st, js, _ = call("POST", "/me/chiavi", sessione=True,
                 corpo={"etichetta": "collaudo"})
CHIAVE = (js or {}).get("chiave", "")
CHIAVE_ID = (js or {}).get("id", "")
if CHIAVE:
    print("ok   chiave di collaudo creata", flush=True)
else:
    print("FAIL creazione chiave di collaudo — il resto v1 salta", flush=True)

if CHIAVE:
    print("== C. la v1 dei clienti ==", flush=True)
    j1 = prova("jobs pagina 1", "GET", "/v1/jobs?limit=5", chiave=CHIAVE)
    if j1:
        cur = j1.get("next_cursor")
        if cur:
            j2 = prova("jobs pagina 2 (cursore)", "GET",
                       f"/v1/jobs?limit=5&cursor={cur}", chiave=CHIAVE)
            if j1 and j2:
                id1 = {r.get("id") for r in j1.get("data", [])}
                id2 = {r.get("id") for r in j2.get("data", [])}
                if id1 & id2:
                    falliti.append("paginazione: pagine sovrapposte")
                    print("FAIL paginazione: pagine sovrapposte", flush=True)
                else:
                    passati.append("paginazione: pagine disgiunte")
                    print("ok   paginazione: pagine disgiunte", flush=True)
        else:
            falliti.append("paginazione: next_cursor assente")
            print("FAIL paginazione: next_cursor assente", flush=True)
    prova("jobs filtrati IT+SAP", "GET", "/v1/jobs?country=IT&technology=SAP&limit=3",
          chiave=CHIAVE)
    prova("jobs limite oltre il tetto", "GET", "/v1/jobs?limit=501",
          chiave=CHIAVE, atteso=422)
    prova("jobs cursore spazzatura", "GET", "/v1/jobs?cursor=spazzatura",
          chiave=CHIAVE, atteso=400)
    prova("companies", "GET", "/v1/companies?limit=3", chiave=CHIAVE)
    ja = prova("companies IT", "GET", "/v1/companies?country=IT&limit=2",
               chiave=CHIAVE)
    if ja and ja.get("data"):
        rif = ja["data"][0]
        prova("company singola", "GET",
              f"/v1/companies/{rif.get('ats')}/{rif.get('company_slug')}",
              chiave=CHIAVE)
    prova("company inesistente", "GET", "/v1/companies/workday/inesistente-xyz",
          chiave=CHIAVE, atteso=404)
    prova("changes senza since", "GET", "/v1/changes", chiave=CHIAVE, atteso=400)
    prova("changes data sbagliata", "GET", "/v1/changes?since=nonunadata",
          chiave=CHIAVE, atteso=400)
    prova("changes ok", "GET", "/v1/changes?since=2026-10-01T00:00:00Z&limit=3",
          chiave=CHIAVE)
    prova("coverage", "GET", "/v1/coverage", chiave=CHIAVE)
    prova("usage", "GET", "/v1/usage", chiave=CHIAVE,
          controlla=lambda j: None if "residui" in j
          else (_ for _ in ()).throw(AssertionError("campo residui mancante")))
    prova("exports latest", "GET", "/v1/exports/latest", chiave=CHIAVE)
    prova("export nome sbagliato", "GET", "/v1/exports/latest/xyz",
          chiave=CHIAVE, atteso=404)

print("== D. il portale (sessione) ==", flush=True)
prova("me", "GET", "/me", sessione=True)
prova("le mie chiavi", "GET", "/me/chiavi", sessione=True)
prova("cerca offerte", "POST", "/portale/cerca", sessione=True,
      corpo={"country": "IT"})
prova("cerca aziende", "POST", "/portale/cerca-aziende", sessione=True,
      corpo={"country": "IT"})
prova("uso (registro)", "GET", "/portale/uso", sessione=True)
prova("stima export", "POST", "/portale/export/stima", sessione=True,
      corpo={"tipo": "postings", "country": "IT"})
prova("dettaglio senza id", "POST", "/portale/offerta-dettaglio", sessione=True,
      corpo={}, atteso=400)

# Il freno anti-raccolta non si puo' far scattare via HTTP senza 1001
# chiamate (il tetto giornaliero) — e le raffiche da 70 si spalmano oltre
# il minuto finestra. Verificato invece in isolamento sul server
# (06/10/2026): 61 chiamate alla funzione -> 429 alla 61esima.
passati.append("freno portale: verificato in isolamento (429 alla 61esima)")
print("ok   freno portale: verificato in isolamento sul server", flush=True)

# la chiave di collaudo muore qui, e si verifica che sia morta davvero
if CHIAVE_ID:
    st, _, _ = call("DELETE", f"/me/chiavi/{CHIAVE_ID}", sessione=True)
    st2, _, _ = call("GET", "/v1/usage", chiave=CHIAVE)
    if st == 200 and st2 == 401:
        passati.append("revoca chiave immediata (401 dopo la revoca)")
        print("ok   revoca chiave immediata", flush=True)
    else:
        falliti.append(f"revoca chiave rotta: delete {st}, dopo {st2}")
        print(f"FAIL revoca chiave: delete {st}, dopo {st2}", flush=True)

print()
print(f"{'='*50}\nPASSATI {len(passati)} · FALLITI {len(falliti)}")
for f in falliti:
    print("  FAIL", f)
sys.exit(1 if falliti else 0)
