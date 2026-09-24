"""costruisci_esco: dal dataset ESCO v1.2.1 (CSV per lingua) al file unico
`dati/esco/occupazioni.jsonl`, una riga per occupazione.

Perché JSONL e non parquet: il .venv del repo non ha pyarrow (24/09/2026) e
per regola di progetto non si installa niente senza chiedere. Il JSONL si
indicizza lo stesso (duckdb lo legge nativamente) e si ispeziona a occhio.

Perché ci serve: è il mattone 1 della classificazione ESCO/ISCO delle
offerte — ogni occupazione porta URI, label preferred+alternative in 17
lingue, descrizione dove esiste, codice ISCO-08 a 4 cifre e i rollup
3/2/1 (prefissi: ISCO-08 è gerarchico per costruzione). In più la
famiglia Nivult calcolata con la mappa interna `tassonomie.famiglia_da_isco`,
così il ponte famiglia↔ISCO è già dentro il dato.

Input:  dati/esco/raw/<lang>/occupations_<lang>.csv   (17 lingue)
        dati/esco/raw/en/ISCOGroups_en.csv            (nomi dei gruppi)
Output: dati/esco/occupazioni.jsonl

I CSV si scaricano SENZA email: la pagina ufficiale chiede la mail, ma i
file stanno a URL diretti (scoperto il 24/09/2026 seguendo il link
«direct download» della pagina stessa). Per ogni lingua L:

  curl -O "https://ec.europa.eu/esco/download/ESCO%20dataset%20-%20v1.2.1%20-%20classification%20-%20L%20-%20csv.zip"
  unzip -o esco...zip -d dati/esco/raw/L

(pattern: ESCO dataset - <versione> - classification - <lingua> - <formato>.zip;
la "full version - all languages" ha lo slot lingua vuoto ed esce solo in
rdf/ttl/xml/json-ld, non in csv).

Uso: .venv/bin/python scripts/costruisci_esco.py
"""
from __future__ import annotations

import csv
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(REPO, "dati", "esco", "raw")
OUT = os.path.join(REPO, "dati", "esco", "occupazioni.jsonl")

# le lingue scaricate: en + le 16 chieste dal piano di classificazione
LINGUE = ["en", "it", "de", "fr", "es", "nl", "sv", "pt", "pl",
          "da", "fi", "no", "cs", "sk", "ro", "hu", "el"]

csv.field_size_limit(10**8)

# la mappa interna ISCO→famiglia vive nel pacchetto installato editable
sys.path.insert(0, os.path.join(REPO, "src"))
from nivult.ats.tassonomie import famiglia_da_isco  # noqa: E402


def leggi_occupazioni(lang: str) -> dict[str, dict]:
    """URI → riga CSV per una lingua. altLabels è multi-linea dentro le virgolette."""
    p = os.path.join(RAW, lang, f"occupations_{lang}.csv")
    with open(p, encoding="utf-8") as f:
        return {r["conceptUri"]: r for r in csv.DictReader(f)}


def main() -> None:
    # nomi EN dei 619 gruppi ISCO-08 (livelli 1-4), per rendere leggibili i codici
    gruppi: dict[str, str] = {}
    with open(os.path.join(RAW, "en", "ISCOGroups_en.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            gruppi[r["code"].strip()] = r["preferredLabel"].strip()

    per_lingua = {lang: leggi_occupazioni(lang) for lang in LINGUE}
    base = per_lingua["en"]
    for lang in LINGUE:
        mancanti = set(base) - set(per_lingua[lang])
        if mancanti:
            print(f"ATTENZIONE: {lang} non copre {len(mancanti)} occupazioni", file=sys.stderr)

    # riepilogo copertura: quante occupazioni hanno label/descrizione per lingua
    copertura = {lang: {"label": 0, "descrizione": 0} for lang in LINGUE}

    n = 0
    with open(OUT, "w", encoding="utf-8") as out:
        for uri, r0 in sorted(base.items()):
            isco = r0["iscoGroup"].strip()          # sempre 4 cifre in v1.2.1
            assert len(isco) == 4 and isco.isdigit(), f"iscoGroup inatteso: {isco!r}"
            rec = {
                "uri": uri,
                "codice_esco": r0["code"].strip(),   # codice ESCO puntuale, es. 2512.4
                "isco08": isco,
                "isco08_3": isco[:3],
                "isco08_2": isco[:2],
                "isco08_1": isco[:1],
                "isco_gruppi_en": {
                    "4": gruppi.get(isco, ""),
                    "3": gruppi.get(isco[:3], ""),
                    "2": gruppi.get(isco[:2], ""),
                    "1": gruppi.get(isco[:1], ""),
                },
                "famiglia_nivult": famiglia_da_isco(isco),
                "preferred": {},
                "alt": {},
                "descrizione": {},
            }
            for lang in LINGUE:
                r = per_lingua[lang].get(uri)
                if not r:
                    continue
                pref = r["preferredLabel"].strip()
                if pref:
                    rec["preferred"][lang] = pref
                    copertura[lang]["label"] += 1
                alt = [a.strip() for a in r["altLabels"].split("\n") if a.strip()]
                if alt:
                    rec["alt"][lang] = alt
                desc = r["description"].strip()
                if desc:
                    rec["descrizione"][lang] = desc
                    copertura[lang]["descrizione"] += 1
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1

    size = os.path.getsize(OUT)
    print(f"scritte {n} occupazioni in {OUT} ({size/1e6:.1f} MB)")
    print(f"{'lingua':6} {'label':>6} {'descrizione':>11}")
    for lang in LINGUE:
        c = copertura[lang]
        print(f"{lang:6} {c['label']:>6} {c['descrizione']:>11}")


if __name__ == "__main__":
    main()
