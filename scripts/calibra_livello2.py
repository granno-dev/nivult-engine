"""Il nome dell'azienda e il dominio si somigliano abbastanza da fare da prova?

Scaricare il sito non funziona: Cloudflare risponde 403 e le testate da browser
non cambiano niente (misurato). Ma per `Cvshealth -> cvshealth.com`,
`Besix -> besix.com`, `Broadcom -> broadcom.com` la prova e' la corrispondenza
stessa, e non costa una richiesta di rete.

Va CALIBRATA, non decisa a occhio. Si usano i domini che abbiamo gia' e di cui
sappiamo che sono giusti (passati dal giudice, livello 1) per vedere quanto
somigliano davvero; e come contro-prova si accoppiano nomi e domini a CASO, che
devono somigliare molto meno. La soglia si mette dove i due mucchi si separano.
"""
from __future__ import annotations
import difflib
import os
import random
import re

import psycopg

random.seed(20260919)          # la stessa calibrazione deve dare lo stesso numero


def pulisci_nome(n: str) -> str:
    n = (n or "").lower()
    # le forme societarie non aiutano a riconoscere nessuno
    n = re.sub(r"\b(gmbh|ag|inc|llc|ltd|limited|corp|corporation|co|sa|srl|spa|bv|nv|"
               r"plc|group|gruppe|holding|holdings|international|emea|usa|careers?|jobs?)\b", " ", n)
    return re.sub(r"[^a-z0-9]", "", n)


def radice(d: str) -> str:
    d = (d or "").lower().removeprefix("www.")
    parti = d.split(".")
    return re.sub(r"[^a-z0-9]", "", parti[0] if parti else "")


def somiglianza(nome: str, dominio: str) -> float:
    a, b = pulisci_nome(nome), radice(dominio)
    if not a or not b:
        return 0.0
    if a == b or a.startswith(b) or b.startswith(a):
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


with psycopg.connect(os.environ["ATS_DATABASE_URL"]) as c:
    veri = c.execute("""
        SELECT company_name, site_domain FROM ats_companies
         WHERE site_domain IS NOT NULL AND company_name IS NOT NULL
           AND site_domain_source IN ('logo-ats', 'email-annuncio', 'searxng', 'nome-generato')
         LIMIT 2000""").fetchall()

print(f"coppie PROVATE dal giudice (livello 1): {len(veri)}")
punti_veri = [somiglianza(n, d) for n, d in veri]

# contro-prova: gli stessi nomi con domini di ALTRE aziende
domini = [d for _, d in veri]
random.shuffle(domini)
punti_falsi = [somiglianza(n, d) for (n, _), d in zip(veri, domini)]


def quota(punti, soglia):
    return 100 * sum(1 for x in punti if x >= soglia) / max(len(punti), 1)


print(f"\n{'soglia':>7}{'coppie vere sopra':>20}{'coppie a caso sopra':>22}")
for s in (0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 1.00):
    print(f"{s:>7.2f}{quota(punti_veri, s):>19.1f}%{quota(punti_falsi, s):>21.1f}%")

print("\nesempi di coppie vere con somiglianza bassa (il nome non dice il dominio):")
bassi = sorted(zip(veri, punti_veri), key=lambda x: x[1])[:6]
for (n, d), p in bassi:
    print(f"  {p:.2f}  {(n or '')[:32]:<34}{d}")
print("\nesempi di coppie vere con somiglianza piena:")
for (n, d), p in sorted(zip(veri, punti_veri), key=lambda x: -x[1])[:5]:
    print(f"  {p:.2f}  {(n or '')[:32]:<34}{d}")
