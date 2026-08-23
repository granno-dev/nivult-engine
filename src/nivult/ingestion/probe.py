"""Sonda in sola lettura: interroga una fonte e mostra cosa tornerebbe.

    python -m nivult.ingestion.probe france_travail --query "ressources humaines" --limit 5
    python -m nivult.ingestion.probe france_travail --query "..." --json > fixture.json

NON TOCCA IL DATABASE. Serve a confermare sul campo il contratto di una API
prima che una sola riga venga scritta, e a registrare fixture per i test
offline.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict

from nivult.config import load_dotenv

SOURCES = {
    "france_travail": ("nivult.ingestion.sources.france_travail", "FranceTravailClient"),
    "arbetsformedlingen": ("nivult.ingestion.sources.arbetsformedlingen",
                           "ArbetsformedlingenClient"),
    "fantastic": ("nivult.ingestion.sources.fantastic", "FantasticClient"),
}


def load(name: str):
    if name not in SOURCES:
        raise SystemExit(f"fonte sconosciuta: {name}. Disponibili: {', '.join(SOURCES)}")
    module, cls = SOURCES[name]
    return getattr(__import__(module, fromlist=[cls]), cls)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.ingestion.probe", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", choices=sorted(SOURCES))
    ap.add_argument("--query", required=True)
    ap.add_argument("--country", default=None)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--json", action="store_true", help="stampa i RawJob grezzi, per le fixture")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    # Il probe non tocca il database, quindi nessuno avrebbe caricato .env:
    # le credenziali delle fonti stanno lì insieme a tutto il resto.
    load_dotenv()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s", stream=sys.stderr)

    with load(args.source)() as client:
        country = args.country or sorted(client.countries)[0]
        result = client.fetch(query=args.query, country=country, limit=args.limit)

    if args.json:
        print(json.dumps([asdict(j) for j in result.jobs], default=str, ensure_ascii=False, indent=2))
        return 0

    print(f"\nfonte: {args.source}   query: {args.query!r}")
    print(f"ricevute: {len(result.jobs)}   disponibili: {result.total_available}   "
          f"fetch completa: {result.complete}")
    print(f"richieste: {result.requests_made}   crediti: {result.credits_used}")

    kinds: dict[str, int] = {}
    for j in result.jobs:
        kinds[j.link_kind] = kinds.get(j.link_kind, 0) + 1
    print(f"tipi di link: {kinds or '—'}\n")

    for j in result.jobs:
        print(f"  {j.title}")
        print(f"    {j.organization}  ·  {', '.join(j.cities) or '—'}  ·  "
              f"{j.date_posted:%Y-%m-%d}")
        print(f"    [{j.link_kind}] {j.canonical_url}")
        gaps = [k for k in ("ai_experience_level", "ai_employment_type", "ai_work_arrangement")
                if getattr(j, k) is None]
        if gaps:
            print(f"    non mappati: {', '.join(gaps)}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
