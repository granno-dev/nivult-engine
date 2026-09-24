"""Raccoglitore GRATUITO dei dati aziendali da pagine LinkedIn pubbliche
via Wayback Machine. Mai toccare linkedin.com: solo web.archive.org.

Per ogni riga `dominio|nome` del file in ingresso:

1. genera fino a 3 candidati slug dal nome (suffissi societari via,
   spazi -> trattini, punteggiatura via, variante senza «and/the»):
   «Dave and Busters» -> dave-and-busters, daveandbusters, dave-busters;
2. interroga il CDX della Wayback: vince il PRIMO candidato con uno
   snapshot 200 dell'anno in corso; se nessuno vince -> «no_wayback»;
3. scarica lo snapshot in formato `id_` (HTML originale senza toolbar)
   e legge i campi dal blocco <dt>etichetta</dt><dd>valore</dd> della
   sezione overview (il JSON-LD degli snapshot NON porta Organization)
   piu' i follower dalla meta description; snapshot pre-2013 scartati;
4. match anti-omonimo: il Website dichiarato nello snapshot, ridotto a
   dominio nudo, DEVE coincidere col nostro — altrimenti
   «no_match_domain». Se il Website manca, si accetta solo se il
   dominio nostro (senza TLD) compare nello slug o nel nome pagina, e
   il record e' marcato `match: "debole"`.

Uso:
    python3 scripts/wayback_aziende.py <file_domini> [--limite N] [--da-indice N]

Output jsonl su stdout; progressi (ogni 50) e riepilogo su stderr.
Ogni eccezione di rete/parse diventa una riga "errore" e si prosegue.

Misurato il 24/09/2026: CDX ~5,5s di mediana (IA a intermittenza: 6
tentativi con backoff 2-5s x tentativo), snapshot ~1,3-1,8s e ~430 KB.

NOTA sul CDX, verificata in corsa: il filtro `timestamp:>20260101` del
primo appunto torna SEMPRE vuoto — il «>» non e' un operatore, il CDX
lo cerca alla lettera. Si usa `from=<anno>` + `limit=-5`: i 5 snapshot
200 piu' recenti dell'anno.
"""
from __future__ import annotations

import argparse
import html as html_mod
import json
import random
import re
import sys
import time
import unicodedata

import httpx

CDX = "https://web.archive.org/cdx/search/cdx"
WEB = "https://web.archive.org/web/{ts}id_/https://www.linkedin.com/company/{slug}/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
ANNO_MIN = "2026"      # solo snapshot dell'anno: layout guest moderno, dt/dd presente
PASSO = 1.0            # ritmo gentile: ~1 richiesta/s aggregato verso l'archivio
TENTATIVI = 6          # IA va a intermittenza: si insiste con backoff

CLIENTE = httpx.Client(headers={"User-Agent": UA}, follow_redirects=True,
                       timeout=httpx.Timeout(90.0, connect=20.0))

SUFFISSI_SOCIETARI = {
    "inc", "llc", "ltd", "gmbh", "srl", "spa", "sarl", "bv", "ag", "se",
    "co", "company", "corp", "corporation", "group", "holdings",
}
RIEMPITIVO = {"and", "&", "the"}
# i suffissi brevi anche in forma puntata (s.p.a., s.r.l., g.m.b.h.):
# il punto spezzerebbe le sigle in lettere sciolte prima del filtro
_PUNTI = r"\.?"
SUFFISSI_PUNTI = re.compile(
    r"\b(?:" + "|".join(_PUNTI.join(s) + _PUNTI for s in
                        ("sarl", "gmbh", "corp", "srl", "spa", "llc", "ltd",
                         "inc", "bv", "ag", "se", "co")) + r")\b")
ETICHETTE = {
    "website": "website",
    "industry": "settore",
    "company size": "fascia",
    "headquarters": "hq",
    "type": "tipo",
    "founded": "fondata",
    "specialties": "specialties",
}

_ultima_richiesta = 0.0


def _calma() -> None:
    """Mai piu' di ~1 richiesta al secondo verso web.archive.org."""
    global _ultima_richiesta
    attesa = PASSO - (time.monotonic() - _ultima_richiesta)
    if attesa > 0:
        time.sleep(attesa)
    _ultima_richiesta = time.monotonic()


def _get(url: str, params: list | None = None) -> str:
    """GET con retry: fino a 6 tentativi, pausa 2-5s moltiplicata per il tentativo."""
    ultimo = "?"
    for tentativo in range(1, TENTATIVI + 1):
        _calma()
        try:
            r = CLIENTE.get(url, params=params)
            if r.status_code == 200:
                return r.text
            ultimo = f"HTTP {r.status_code}"
        except Exception as exc:
            ultimo = f"{type(exc).__name__}: {exc}"
        if tentativo < TENTATIVI:
            time.sleep(random.uniform(2, 5) * tentativo)
    raise RuntimeError(f"archivio irraggiungibile ({ultimo})")


def candidati_slug(nome: str) -> list[str]:
    """Fino a 3 slug dal nome, senza motore di ricerca."""
    base = unicodedata.normalize("NFKD", nome.lower()).encode("ascii", "ignore").decode()
    base = SUFFISSI_PUNTI.sub(" ", base)
    parole = [p for p in re.sub(r"[^a-z0-9]+", " ", base).split()
              if p not in SUFFISSI_SOCIETARI]
    if not parole:
        return []
    varianti = ["-".join(parole), "".join(parole)]
    snelle = [p for p in parole if p not in RIEMPITIVO]
    if snelle != parole:
        varianti.append("-".join(snelle))
    out = []
    for v in varianti:
        if v and v not in out:
            out.append(v)
    return out[:3]


def snapshot_cdx(slug: str) -> str | None:
    """Il timestamp dello snapshot 200 piu' recente dell'anno, o None."""
    params = [
        ("url", f"linkedin.com/company/{slug}/"),
        ("output", "json"),
        ("filter", "statuscode:200"),
        ("from", ANNO_MIN),
        ("limit", "-5"),
    ]
    grezzo = _get(CDX, params).strip()
    if not grezzo:
        return None
    righe = json.loads(grezzo)          # prima riga = intestazione
    catture = [r[1] for r in righe[1:] if len(r) > 1 and r[1] >= "20130101"]
    return max(catture) if catture else None


def _testo(frammento: str) -> str:
    """Tag via, entita' decodificate (gli snapshot le codificano anche due volte)."""
    s = html_mod.unescape(re.sub(r"<[^>]+>", " ", frammento))
    return re.sub(r"\s+", " ", html_mod.unescape(s)).strip()


def _website_dal_dd(dd: str) -> str | None:
    """Nel dd il sito e' il testo del link; dopo c'e' uno span nascosto
    «External link for X» che non deve finire nel valore."""
    m = re.search(r"(?is)<a\b[^>]*>(.*?)</a>", dd)
    candidato = _testo(m.group(1)) if m else _testo(dd)
    for pezzo in candidato.split():
        if "." in pezzo:
            return pezzo
    return None


def leggi_snapshot(pagina: str) -> dict:
    """Campi dal blocco <dt>/<dd> dell'overview + follower dalla meta description."""
    campi: dict = {}
    coppie = re.findall(r"(?is)<dt\b[^>]*>(.*?)</dt>\s*<dd\b[^>]*>(.*?)</dd>", pagina)
    if not coppie:
        raise ValueError("snapshot senza blocco overview (layout alieno o authwall)")
    for dt, dd in coppie:
        campo = ETICHETTE.get(_testo(dt).lower().rstrip(":"))
        if not campo or campo in campi:
            continue
        valore = _website_dal_dd(dd) if campo == "website" else _testo(dd)
        if valore:
            campi[campo] = valore
    follower = None
    for tag in re.findall(r"(?is)<meta\b[^>]*>", pagina):
        if re.search(r'(?i)\bname="description"', tag):
            c = re.search(r'(?is)\bcontent="([^"]*)"', tag)
            f = c and re.search(r"([\d][\d.,]*)\s+followers?\s+on\s+LinkedIn",
                                html_mod.unescape(c.group(1)))
            if f:
                follower = int(re.sub(r"\D", "", f.group(1)))
            break
    campi["follower"] = follower
    nome_pagina = None
    m = re.search(r"(?is)<h1\b[^>]*>(.*?)</h1>", pagina)
    if m:
        nome_pagina = _testo(m.group(1)) or None
    if not nome_pagina:
        m = re.search(r"(?is)<title\b[^>]*>(.*?)</title>", pagina)
        if m:
            nome_pagina = re.sub(r"\s*\|\s*LinkedIn.*$", "", _testo(m.group(1))) or None
    campi["nome_pagina"] = nome_pagina
    return campi


def _norm_dominio(u: str | None) -> str:
    """Riduce un URL o un dominio all'host nudo: via schema, www, path e porta."""
    if not u:
        return ""
    u = u.strip().lower()
    u = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u)
    u = u.split("/", 1)[0].split(":", 1)[0]
    if u.startswith("www."):
        u = u[4:]
    return u.strip(".")


def _solo_alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def raccogli(dominio: str, nome: str) -> dict:
    """Un record jsonl per azienda: campi pieni, oppure l'esito negativo."""
    try:
        slug = ts = None
        for candidato in candidati_slug(nome):
            ts = snapshot_cdx(candidato)
            if ts:
                slug = candidato
                break
        if not slug:
            return {"dominio": dominio, "esito": "no_wayback"}
        campi = leggi_snapshot(_get(WEB.format(ts=ts, slug=slug)))
        website = campi.get("website")
        if website and _norm_dominio(website) == _norm_dominio(dominio):
            match = "forte"
        elif not website:
            radice = _solo_alnum(_norm_dominio(dominio).rsplit(".", 1)[0])
            if not (radice and (radice in _solo_alnum(slug)
                                or radice in _solo_alnum(campi.get("nome_pagina") or ""))):
                return {"dominio": dominio, "slug": slug, "wayback_ts": ts,
                        "esito": "no_match_domain"}
            match = "debole"
        else:
            return {"dominio": dominio, "slug": slug, "wayback_ts": ts,
                    "website": website, "esito": "no_match_domain"}
        return {
            "dominio": dominio, "slug": slug, "wayback_ts": ts, "match": match,
            "settore": campi.get("settore"), "fascia": campi.get("fascia"),
            "hq": campi.get("hq"), "tipo": campi.get("tipo"),
            "fondata": campi.get("fondata"), "specialties": campi.get("specialties"),
            "website": website, "follower": campi.get("follower"),
        }
    except Exception as exc:
        return {"dominio": dominio, "esito": "errore",
                "dettaglio": f"{type(exc).__name__}: {exc}"[:200]}


def _leggi_voci(percorso: str) -> list[tuple[str, str]]:
    """Righe `dominio|nome`; vuote e commenti saltati. Senza «|» il dominio vale da nome."""
    voci = []
    with open(percorso, encoding="utf-8") as f:
        for riga in f:
            riga = riga.strip()
            if not riga or riga.startswith("#"):
                continue
            dominio, _, nome = riga.partition("|")
            if dominio.strip():
                voci.append((dominio.strip(), nome.strip() or dominio.strip()))
    return voci


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Dati aziendali da pagine LinkedIn pubbliche via Wayback Machine "
                    "(mai toccare linkedin.com).")
    ap.add_argument("file_domini", help="file con righe `dominio|nome`")
    ap.add_argument("--limite", type=int, default=None, help="al massimo N aziende")
    ap.add_argument("--da-indice", type=int, default=0,
                    help="salta le prime N righe utili (per le riprese)")
    args = ap.parse_args()
    voci = _leggi_voci(args.file_domini)[args.da_indice:]
    if args.limite is not None:
        voci = voci[:args.limite]
    conteggi: dict[str, int] = {}
    for i, (dominio, nome) in enumerate(voci, 1):
        record = raccogli(dominio, nome)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        chiave = record.get("match") or record["esito"]
        conteggi[chiave] = conteggi.get(chiave, 0) + 1
        if i % 50 == 0:
            print(f"[wayback] {i}/{len(voci)}: {conteggi}", file=sys.stderr, flush=True)
    print(f"[wayback] fine: {len(voci)} aziende, {conteggi}", file=sys.stderr)


if __name__ == "__main__":
    main()
