"""Banco LinkedIn pagine guest (24/09/2026).

Misura, prima di costruire qualunque raccoglitore:
1) per quante delle nostre aziende si TROVA la pagina LinkedIn pubblica
   (slug via SearXNG, mai per nome solo: si conferma col dominio);
2) per quante il dominio dichiarato nella pagina (Organization.sameAs)
   coincide col nostro logo_domain — l'unico match che non prende
   omonimi (il caso Semios: slug giusto, azienda danese sbagliata);
3) quali campi la pagina guest mostra davvero: industry, fascia
   dipendenti dichiarata, founded, tipo, indirizzo, membri-piattaforma.

Condizioni della decisione: solo pagine guest, mai account; mai da
Hetzner in produzione; fonte dichiarata per campo; persone MAI.

Uso: python3 scripts/banco_linkedin.py /tmp/banco_li_aziende.txt
"""
from __future__ import annotations

import html as html_mod
import json
import re
import sys
import time
import urllib.parse
import urllib.request

SEARX = "http://100.119.200.7:8899/search"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def slug_da_ricerca(nome: str, dominio: str) -> str | None:
    q = urllib.parse.urlencode({"q": f'"{dominio}" linkedin company', "format": "json"})
    try:
        d = json.loads(_get(f"{SEARX}?{q}", 15))
    except Exception:
        return None
    for r in d.get("results", []):
        m = re.search(r"linkedin\.com/company/([^/?\"']+)", r.get("url", ""))
        if m:
            return m.group(1)
    return None


def campi_guest(slug: str) -> dict | None:
    try:
        t = html_mod.unescape(_get(f"https://www.linkedin.com/company/{slug}/"))
    except Exception:
        return None
    out = {"bytes": len(t)}
    org = re.search(r'\{"@context":"http://schema\.org","@type":"Organization".{0,4000}?\}\]\}', t)
    blocco = org.group(0) if org else t
    m = re.search(r'"sameAs":"([^"]+)"', blocco)
    out["sito"] = m.group(1) if m else None
    m = re.search(r'"numberOfEmployees":\{"value":(\d+)', blocco)
    out["membri"] = int(m.group(1)) if m else None
    m = re.search(r'"streetAddress":"([^"]*)"', blocco)
    out["via"] = m.group(1) if m else None
    m = re.search(r'"addressCountry":"([^"]*)"', blocco)
    out["paese"] = m.group(1) if m else None
    # la sezione about-us in HTML: dt etichetta, dd valore
    for chiave, nome in (("industry", "settore"), ("companySize", "fascia"),
                         ("foundedOn", "fondata"), ("type", "tipo")):
        m = re.search(rf'about-us__{chiave}".{{0,600}}?<dd[^>]*>\s*(.+?)\s*</dd>', t, re.S)
        out[nome] = re.sub(r"\s+", " ", m.group(1))[:120] if m else None
    return out


def dom(u: str | None) -> str:
    if not u:
        return ""
    u = re.sub(r"^https?://", "", u.lower())
    u = u.split("/")[0]
    return re.sub(r"^www\.", "", u)


def main() -> None:
    righe = [r.split("|") for r in open(sys.argv[1]) if r.strip()][:30]
    trovate = match = 0
    for nome, dominio, _jc in righe:
        slug = slug_da_ricerca(nome, dominio)
        if not slug:
            print(f"{dominio:40s} | slug NON trovato")
            continue
        time.sleep(2)
        c = campi_guest(slug)
        if c is None:
            print(f"{dominio:40s} | {slug:35s} | fetch fallita")
            continue
        trovate += 1
        ok = dom(c["sito"]) == dom(dominio)
        match += ok
        time.sleep(3)
        print(f"{dominio:40s} | {slug:35s} | sito {str(c['sito'])[:30]:30s} | "
              f"{'MATCH' if ok else 'NO  '} | settore {str(c['settore'])[:25]:25s} | "
              f"fascia {str(c['fascia'])[:18]:18s} | fondata {str(c['fondata'])[:8]:8s} | membri {c['membri']}")
    print(f"\n--- pagine trovate {trovate}/30, con dominio coincidente {match}")


if __name__ == "__main__":
    main()
