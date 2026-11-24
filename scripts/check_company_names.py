#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nivult.ats.company_names import (
    board_url,
    dominio,
    nome_da_html,
    nome_unico,
    nomi_da_raw,
    pulisci,
)

PASSED = []
FAILED = []


def check(label, got, expected=True):
    if got == expected:
        PASSED.append(label)
        print(f"  ok    {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}: {got!r} != {expected!r}")


def main() -> int:
    check("France Travail", nomi_da_raw({"entreprise": {"nom": "VIVANDA"}}), ["VIVANDA"])
    check("Bundesagentur", nomi_da_raw({"firma": "KONDI GmbH"}), ["KONDI GmbH"])
    check("Arbetsformedlingen", nomi_da_raw(
        {"employer": {"name": "Feni Sushi AB"}}), ["Feni Sushi AB"])
    check("Teamtailor", nomi_da_raw(
        {"_jobposting": {"hiringOrganization": {"name": "Team Tailor AB"}}}),
        ["Team Tailor AB"])
    check("slash legale accettato", pulisci("Demant A/S"), "Demant A/S")
    check("path UKG rifiutato", pulisci("Dvc1000/Jobboard/0242"), None)
    check("nome univoco", nome_unico([{"firma": "Acme"}, {"firma": "Acme"}]), "Acme")
    check("nomi ambigui non scelti", nome_unico([{"firma": "A"}, {"firma": "B"}]), None)
    check("board Lever", board_url("lever", "acme"), "https://jobs.lever.co/acme")
    check("dominio board", dominio("https://www.acme.com/jobs"), "acme.com")
    check("title board", nome_da_html("<title>Jobs at Acme Robotics</title>"), "Acme Robotics")
    check("title con prefisso generico", nome_da_html("<title>Careers - Acme Robotics</title>"),
          "Acme Robotics")
    check("og con apostrofo", nome_da_html(
        '<meta property="og:site_name" content="Ollie\'s Bargain Outlet">'),
        "Ollie's Bargain Outlet")
    print(f"{len(PASSED)} controlli passati, {len(FAILED)} falliti")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
