"""Passa il filtro su tutte le sintesi gia' scritte.

L'originale non si tocca: la versione ripulita va in `sintesi_pulita`, e chi
legge (la vista `sintesi_finali`) passera' a quella. Cosi' se domani il filtro
si rivela troppo severo, si torna indietro cambiando una vista invece di
rigenerare 85.000 sintesi.

Il giro non usa psycopg: il DSN dal file di shell si rompe sulla password, e
psql dentro il container entra senza. Quindi si lavora a lotti, ognuno un
viaggio di andata (psql tira fuori il lotto in JSON) e uno di ritorno (psql
applica gli UPDATE da un file).

Dal database NON si porta via il raw intero — sarebbero piu' di un gigabyte su
un disco all'83% — ma solo il suo flusso di cifre, che e' quello che al filtro
serve: `regexp_replace(raw::text, '\\D', '', 'g')`.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sintesi_ancorata import ripulisci                            # noqa: E402

LOTTO = int(os.environ.get("LOTTO", "4000"))
PSQL = ["docker", "exec", "-i", "nivult-db-1", "psql", "-U", "nivult", "-d", "nivult_ats"]
TMP = "/tmp/ripulisci"


def psql(sql: str, uscita: str | None = None) -> str:
    cmd = PSQL + (["-tA"] if uscita else ["-q"])
    p = subprocess.run(cmd, input=sql, capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"psql: {p.stderr[:400]}")
    if uscita:
        with open(uscita, "w", encoding="utf-8") as f:
            f.write(p.stdout)
    return p.stdout


def virgoletta(t: str) -> str:
    """Una stringa per psql: si raddoppiano gli apici, e basta — il testo va
    dentro un dollar-quote nel file, non qui."""
    return t.replace("'", "''")


def main() -> None:
    os.makedirs(TMP, exist_ok=True)
    tot = int(psql("SELECT count(*) FROM sintesi_mt5 WHERE sintesi IS NOT NULL "
                   "AND pulita_at IS NULL;", "x").strip() or 0)
    print(f"da ripulire: {tot}", flush=True)
    fatte = intatte = accorciate = morte = 0

    while True:
        # DUE TEMPI, non uno. Scritta come una join sola, questa query ci mette
        # piu' di tre minuti a lotto: il pianificatore stima tante righe, scarta
        # la chiave primaria e scansiona tutta ats_jobs in parallelo. Con la CTE
        # MATERIALIZED il lotto di 4.000 id si forma per primo, e la join che
        # segue ha nient'altro da guardare.
        sql = f"""
WITH lotto AS MATERIALIZED (
  SELECT job_id, sintesi FROM sintesi_mt5
   WHERE sintesi IS NOT NULL AND pulita_at IS NULL
   ORDER BY job_id LIMIT {LOTTO})
SELECT row_to_json(t) FROM (
  SELECT l.job_id::text AS id, l.sintesi,
         regexp_replace(coalesce(j.title,'') || ' ' || coalesce(j.raw::text,''), '\\D', '', 'g') AS cifre
    FROM lotto l JOIN ats_jobs j ON j.id = l.job_id) t;
"""
        psql(sql, f"{TMP}/lotto.jsonl")
        righe = [json.loads(r) for r in open(f"{TMP}/lotto.jsonl", encoding="utf-8") if r.strip()]
        if not righe:
            break

        with open(f"{TMP}/scrivi.sql", "w", encoding="utf-8") as f:
            f.write("BEGIN;\n")
            for d in righe:
                # al filtro si passa come «fonte» il flusso di cifre gia' pronto:
                # e' esattamente cio' che guarda, e schiacciarlo di nuovo non cambia
                pulita, tolte, _ = ripulisci(d["sintesi"], d.get("cifre") or "")
                if tolte == 0:
                    intatte += 1
                elif pulita is None:
                    morte += 1
                else:
                    accorciate += 1
                val = "NULL" if pulita is None else "$q$" + pulita.replace("$q$", "") + "$q$"
                f.write(f"UPDATE sintesi_mt5 SET sintesi_pulita={val}, frasi_tolte={tolte}, "
                        f"pulita_at=now() WHERE job_id='{virgoletta(d['id'])}';\n")
            f.write("COMMIT;\n")
        subprocess.run(PSQL + ["-q", "-f", "-"], stdin=open(f"{TMP}/scrivi.sql"),
                       capture_output=True, text=True, check=True)
        fatte += len(righe)
        print(f"  {fatte}/{tot}  intatte {intatte}  accorciate {accorciate}  buttate {morte}", flush=True)

    print(f"\nfinito: {fatte} sintesi")
    print(f"  intatte     {intatte:7d}  ({intatte*100/max(fatte,1):.1f}%)")
    print(f"  accorciate  {accorciate:7d}  ({accorciate*100/max(fatte,1):.1f}%)")
    print(f"  buttate     {morte:7d}  ({morte*100/max(fatte,1):.1f}%)")


if __name__ == "__main__":
    main()
