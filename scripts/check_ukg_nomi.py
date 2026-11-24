#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nivult.ats.ukg_nomi import leggi_board, nome_da_slug, nome_sporco, pulisci_nome

PASSED = []
FAILED = []


def check(label, got, expected=True):
    if got == expected:
        PASSED.append(label)
        print(f"  ok    {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}: {got!r} != {expected!r}")


class FakeClient:
    def __init__(self, pagine):
        self.pagine = pagine
        self.chiamate = []

    def get(self, url):
        self.chiamate.append(url)
        testo = self.pagine[url]
        return httpx.Response(200, text=testo, request=httpx.Request("GET", url))


def main() -> int:
    check("path UKG sporco", nome_sporco("Dvc1000/Jobboard/0242Ab93"))
    check("slug UKG non diventa nome", nome_da_slug("dvc1000/jobboard/0242ab93"), None)
    check("UUID non diventa nome", nome_da_slug("0242ab93-599e-ad02-de18-4ee7b0eb85e2"), None)
    check("slug normale conservato", nome_da_slug("acme-robotics"), "Acme Robotics")
    check("prefisso board rimosso", pulisci_nome("Opportunities with The Dixon Group"),
          "The Dixon Group")
    check("suffisso board rimosso", pulisci_nome("Acme Careers"), "Acme")
    check("titolo generico scartato", pulisci_nome("Retail Stores Opportunities"), None)

    board = """<script>jobBoard: {"Id":"0242","Name":"Opportunities with The Dixon Group"};
      var navHeader = { logoHref: 'http://www.dixonvalve.com' };</script>"""
    url = "https://recruiting.ultipro.com/dvc1000/jobboard/0242"
    client = FakeClient({url: board})
    meta = leggi_board(client, "dvc1000/jobboard/0242")
    check("nome reale dalla board", meta["company_name"], "The Dixon Group")
    check("dominio reale dalla board", meta["site_domain"], "dixonvalve.com")
    check("una sola chiamata se nome affidabile", len(client.chiamate), 1)

    url2 = "https://recruiting.ultipro.com/oll1000ollie/jobboard/3559"
    generica = """<script>jobBoard: {"Name":"Retail Stores Opportunities"};
      var navHeader = { logoHref: 'https://www.ollies.com' };</script>"""
    home = '<meta property="og:site_name" content="Ollie\'s Bargain Outlet">'
    client2 = FakeClient({url2: generica, "https://ollies.com": home})
    meta2 = leggi_board(client2, "oll1000ollie/jobboard/3559")
    check("fallback homepage per titolo generico", meta2["company_name"], "Ollie's Bargain Outlet")
    check("fallback conserva dominio", meta2["site_domain"], "ollies.com")

    client3 = FakeClient({url2: generica, "https://ollies.com": "<title>Careers</title>"})
    check("generico ovunque resta NULL", leggi_board(client3, "oll1000ollie/jobboard/3559")[
        "company_name"], None)
    print(f"{len(PASSED)} controlli passati, {len(FAILED)} falliti")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
