#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LD_RX = re.compile(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', re.DOTALL)
PRE_RX = re.compile(r"<pre[^>]*>(.*?)</pre>", re.DOTALL)
REST_RX = re.compile(r"(?<![^\W_])REST(?:\s+API|ful)?(?![^\W_])")
PLACEHOLDER_RX = re.compile(
    r"(?:\d+\s+locations?|multiple locations|various locations|several locations)",
    re.IGNORECASE,
)


def _records(text: str) -> list[dict]:
    records = []
    for block in PRE_RX.findall(text):
        plain = html.unescape(re.sub(r"<[^>]+>", "", block)).strip()
        if not plain.startswith("{"):
            continue
        try:
            value = json.loads(plain)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and {"title", "company", "ats"} <= value.keys():
            records.append(value)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit statico delle pagine ATS")
    parser.add_argument("--vetrina", type=Path, default=ROOT / "vetrina")
    args = parser.parse_args()
    root = args.vetrina
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    files = sorted((root / "ats").glob("*/index.html"))
    check(bool(files), "nessuna pagina ATS trovata")
    hub = (root / "ats/index.html").read_text(encoding="utf-8")
    check(not re.search(r"(?<![\d,])1 companies\b", hub),
          "hub: plurale 1 companies")
    check(hub.count("1 public portal") == 3, "hub: attesi 3 public portal")
    check("export-campione" not in hub, "hub: endpoint legacy")

    slugs = []
    for file in files:
        slug = file.parent.name
        slugs.append(slug)
        text = file.read_text(encoding="utf-8")
        canonical = f"https://nivult.com/ats/{slug}/"
        check(f'<link rel="canonical" href="{canonical}">' in text,
              f"{slug}: canonical")
        check("unknown ·" not in text and ">?? ·" not in text,
              f"{slug}: unknown nei Top countries")
        check("export-campione" not in text, f"{slug}: endpoint legacy")
        check("sample-export?type=jobs" in text, f"{slug}: sample-export mancante")
        check("Get the feed on Apify" not in text, f"{slug}: CTA Apify primaria")
        check("Location as posted" not in text, f"{slug}: location placeholder")
        check(not re.search(r">Q\d+<", text), f"{slug}: Wikidata Q-ID")
        check("/ats/bundesanstellung/" not in text, f"{slug}: vecchio slug Bundesagentur")
        check("v1/jobs?ats=bundesanstellung" not in text,
              f"{slug}: vecchio slug nello snippet curl")

        header = text.split("<header", 1)[-1].split("</header>", 1)[0]
        top = header.split("Top countries", 1)[-1]
        for code in re.findall(r">([^<>]+) · [\d,]+</span>", top):
            check(bool(re.fullmatch(r"[A-Z]{2}", code)),
                  f"{slug}: country non ISO-2 {code!r}")

        sample = text.split("Real rows, real links", 1)[-1].split("For developers", 1)[0]
        check(not PLACEHOLDER_RX.search(sample), f"{slug}: location multi-sede")

        scripts = LD_RX.findall(text)
        check(len(scripts) == 1, f"{slug}: {len(scripts)} blocchi JSON-LD")
        if len(scripts) == 1:
            try:
                graph = {x["@type"]: x for x in json.loads(scripts[0])["@graph"]}
                check({"BreadcrumbList", "Dataset", "FAQPage"} <= graph.keys(),
                      f"{slug}: grafo JSON-LD incompleto")
            except (ValueError, KeyError, TypeError) as exc:
                failures.append(f"{slug}: JSON-LD invalido {exc}")

        records = _records(text)
        check(len(records) <= 1, f"{slug}: troppi record JSON di esempio")
        for record in records:
            skills = record.get("skills") or []
            keys = [s.casefold() for s in skills if isinstance(s, str)]
            check(len(keys) == len(set(keys)), f"{slug}: skills duplicate {skills}")
            check("rest" not in keys or REST_RX.search(record["title"]),
                  f"{slug}: REST senza contesto tecnico")

        card = re.search(rf'<a href="/ats/{slug}/".*?</a>', hub, re.DOTALL)
        check(card is not None, f"{slug}: card hub mancante")
        if card:
            card_text = card.group(0)
            detail_count = re.search(r"([\d,]+) live postings right now", text)
            card_count = re.search(r"([\d,]+) live", card_text)
            check(detail_count and card_count
                  and detail_count.group(1) == card_count.group(1),
                  f"{slug}: conteggio hub/scheda diverso")
            detail_date = re.search(
                r'<b[^>]*>(\d+ [A-Za-z]+ \d{4})</b>\s*<span[^>]*>last refresh', text)
            card_date = re.search(r"last refresh ([^<]+)", card_text)
            check(detail_date and card_date
                  and detail_date.group(1) == card_date.group(1),
                  f"{slug}: data hub/scheda diversa")

    homepage = (root / "index.html").read_text(encoding="utf-8")
    check("export-campione" not in homepage, "homepage: endpoint legacy")
    check(homepage.count("sample-export?type=jobs") >= 1,
          "homepage: sample jobs mancante")
    check("sample-export?type=companies" in homepage,
          "homepage: sample companies mancante")
    check("65 ATS platforms within 77+ total employer and public sources" in homepage,
          "homepage: dicitura fonti non allineata")

    sitemap = ET.parse(root / "sitemap-ats.xml")
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls = {node.text for node in sitemap.findall("s:url/s:loc", ns)}
    expected = {f"https://nivult.com/ats/{slug}/" for slug in slugs}
    expected.add("https://nivult.com/ats/")
    check(urls == expected, "sitemap ATS non allineata alle pagine")
    check("https://nivult.com/ats/bundesanstellung/" not in urls,
          "sitemap: vecchio slug Bundesagentur")

    redirects = (root / "_redirects").read_text(encoding="utf-8")
    check("/ats/bundesanstellung/* /ats/bundesagentur/ 301" in redirects,
          "redirect Bundesagentur mancante")

    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    print(f"OK: {len(files)} pagine ATS, hub, homepage, sitemap e redirect")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
