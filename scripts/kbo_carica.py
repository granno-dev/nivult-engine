"""KBO/BCE open data (Belgio) -> una tabella compatta per il registro (22/09/2026).

Il registro belga non ha un'API: e' uno zip mensile (~300 MB, 2,2 GB di CSV)
che Giuseppe scarica con il suo account su kbopub.economie.fgov.be. Qui si
legge lo zip in streaming e si scrive UN TSV con una riga per impresa
(non per unita' di stabilimento): numero, stato, forma giuridica (codice e
etichetta francese), data d'inizio, denominazione sociale, tutte le
denominazioni (per il confronto dei nomi), sede legale (REGO) e NACE
principale. Il TSV va su Hetzner e entra in `kbo_imprese` con COPY
(`--carica` sul server); `registri_imprese._be` la interroga per nome.

    python scripts/kbo_carica.py --zip ~/Downloads/KboOpenData_..._Full.zip --out kbo.tsv.gz
    python scripts/kbo_carica.py --carica kbo.tsv.gz        # sul server
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

COLONNE = ("numero", "stato", "forma_codice", "forma", "inizio", "nome", "nomi", "via", "civico", "cap", "comune", "nace", "nome_norm")


def _norm(s: str) -> str:
    # copia di registri_imprese._norm (lo script gira anche dove httpx manca)
    s = re.sub(r"\b(srl|spa|s\.p\.a\.|gmbh|ag|bv|b\.v\.|inc|llc|ltd|limited|llp|sa|"
               r"s\.a\.|sas|sasu|oy|oyj|ab|as|asa|aps|a/s|plc|co|corp|"
               r"s\.r\.o\.|a\.s\.|spol|nv|n\.v\.|sprl|bvba|cvba|scrl|vzw|asbl|comm\.v|scs|"
               r"group|groupe|holding)\b\.?", " ", s.lower())
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _righe(z: zipfile.ZipFile, nome: str):
    with z.open(nome) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", newline=""))


def _data(d: str) -> str | None:
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", d or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def estrai(percorso_zip: str, out: str) -> int:
    z = zipfile.ZipFile(percorso_zip)
    forme = {}
    for r in _righe(z, "code.csv"):
        if r["Category"] == "JuridicalForm" and r["Language"] == "FR":
            forme[r["Code"]] = r["Description"]
    imprese: dict[str, dict] = {}
    for r in _righe(z, "enterprise.csv"):
        imprese[r["EnterpriseNumber"]] = {"numero": r["EnterpriseNumber"], "stato": r["Status"],
                                          "forma_codice": r["JuridicalForm"] or None,
                                          "forma": forme.get(r["JuridicalForm"] or ""), "inizio": _data(r["StartDate"]),
                                          "nome": None, "nomi": [], "via": None, "civico": None, "cap": None,
                                          "comune": None, "nace": None, "_nace_v": ""}
    print("imprese:", len(imprese), file=sys.stderr)
    for r in _righe(z, "denomination.csv"):
        e = imprese.get(r["EntityNumber"])
        if not e:
            continue
        d = (r["Denomination"] or "").strip()
        if not d:
            continue
        e["nomi"].append(d)
        # 001 = denominazione sociale; la prima vista vince, il francese/olandese non contano
        if r["TypeOfDenomination"] == "001" and not e["nome"]:
            e["nome"] = d
    for r in _righe(z, "address.csv"):
        e = imprese.get(r["EntityNumber"])
        if not e or r["TypeOfAddress"] != "REGO" or r["DateStrikingOff"]:
            continue
        e["via"] = r["StreetFR"] or r["StreetNL"] or None
        e["civico"] = r["HouseNumber"] or None
        e["cap"] = r["Zipcode"] or None
        e["comune"] = r["MunicipalityFR"] or r["MunicipalityNL"] or None
    for r in _righe(z, "activity.csv"):
        if r["Classification"] != "MAIN":
            continue
        e = imprese.get(r["EntityNumber"])
        if not e:
            continue
        # la versione NACE piu' recente vince; a parita' il gruppo 001 (IVA)
        chiave = r["NaceVersion"] + ("1" if r["ActivityGroup"] == "001" else "0")
        if chiave > e["_nace_v"]:
            e["_nace_v"], e["nace"] = chiave, r["NaceCode"]
    n = 0
    with gzip.open(out, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_NONE, escapechar="\\")
        for e in imprese.values():
            if not e["nome"] and not e["nomi"]:
                continue
            nome = e["nome"] or e["nomi"][0]
            w.writerow([e["numero"], e["stato"], e["forma_codice"] or "", e["forma"] or "", e["inizio"] or "", nome,
                        " | ".join(dict.fromkeys(e["nomi"]))[:1000], e["via"] or "", e["civico"] or "", e["cap"] or "",
                        e["comune"] or "", e["nace"] or "", _norm(nome)])
            n += 1
    print("scritte:", n, file=sys.stderr)
    return n


DDL = """
CREATE TABLE IF NOT EXISTS kbo_imprese (
  numero text PRIMARY KEY, stato text, forma_codice text, forma text, inizio date, nome text, nomi text,
  via text, civico text, cap text, comune text, nace text, nome_norm text);
CREATE INDEX IF NOT EXISTS kbo_imprese_nome_norm_idx ON kbo_imprese (nome_norm);
"""


def carica(dsn: str, tsv: str) -> int:
    import psycopg
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(DDL)
        c.execute("TRUNCATE kbo_imprese")
        with c.cursor() as cur, cur.copy("COPY kbo_imprese (" + ", ".join(COLONNE) + ") FROM STDIN "
                                         "(FORMAT text, NULL '')") as cp:
            with gzip.open(tsv, "rb") as f:
                while blocco := f.read(1 << 20):
                    cp.write(blocco)
        c.execute("ANALYZE kbo_imprese")
        return c.execute("SELECT count(*) FROM kbo_imprese").fetchone()[0]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip")
    ap.add_argument("--out", default="kbo.tsv.gz")
    ap.add_argument("--carica", help="TSV gz da caricare in kbo_imprese (sul server)")
    a = ap.parse_args(argv)
    if a.carica:
        dsn = os.environ.get("ATS_DATABASE_URL")
        if not dsn:
            for f in ("/opt/nivult/.env", "/opt/nivult/engine/.env"):
                try:
                    m = re.search(r"^POSTGRES_PASSWORD=(.*)$", open(f).read(), re.M)
                    if m:
                        dsn = "postgresql://nivult:" + m.group(1).strip() + "@127.0.0.1:5432/nivult_ats"
                        break
                except OSError:
                    pass
        print("kbo_imprese:", carica(dsn, a.carica))
        return 0
    if not a.zip:
        ap.error("--zip o --carica")
    estrai(a.zip, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
