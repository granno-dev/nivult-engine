"""La lettura a pezzi su annunci VERI: quante tecnologie in piu' trova?

Il demone non ha una prova a secco, e non voglio scoprire in produzione se
funziona. Qui si rifa' esattamente quello che fa lui — stesso modello, stessa
soglia, stessa funzione `voci` — su annunci presi dal magazzino, e si
confronta:

  PRIMA   una sola finestra da 1024 token, il resto buttato
  DOPO    tutte le finestre, marcature unite

Si guardano apposta gli annunci LUNGHI: su quelli corti le due strade
coincidono per costruzione, e mescolarli annacqua la differenza fino a
nasconderla.
"""
from __future__ import annotations
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import torch
from transformers import AutoModelForTokenClassification, AutoTokenizer

QUI = pathlib.Path(__file__).resolve().parent


def carica(nome):
    sp = importlib.util.spec_from_file_location(nome, str(QUI / f"{nome}.py"))
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


ancoraggio = carica("ancoraggio")
finestre = carica("finestre").finestre
# `voci` sta dentro il demone: si carica quello, cosi' non c'e' una seconda
# copia della funzione che puo' divergere da quella vera
dem = carica("tec_v1_demone")

MOD = os.environ.get("MODELLO_TEC", "/opt/nivult/gpu/tec-v1-ck00500")
SOGLIA = float(os.environ.get("SOGLIA_TEC", "0.30"))
MAXLEN = int(os.environ.get("MAXLEN", "1024"))
QUANTE = int(sys.argv[1]) if len(sys.argv) > 1 else 60

CAMPI = ("description", "content", "descriptionHtml", "descriptionPlain", "externalDescription",
         "jobDescription", "job_description", "Job_Description", "body", "content_html",
         "description_html", "descriptionBody", "text", "ShortDescriptionStr")


def prendi(n):
    campi = ", ".join(f"j.raw->>'{c}'" for c in CAMPI)
    sql = f"""
SET max_parallel_workers_per_gather = 0;
WITH s AS MATERIALIZED (SELECT id, title FROM ats_jobs WHERE expired_at IS NULL
   ORDER BY md5(id::text) LIMIT 4000)
SELECT row_to_json(t) FROM (
 SELECT s.title, coalesce((SELECT v FROM unnest(ARRAY[{campi}]) v
        WHERE length(v) >= 6000 LIMIT 1), '') AS testo
   FROM s JOIN ats_jobs j ON j.id = s.id) t;
"""
    SEP = "§§§"
    p = subprocess.run(["docker", "exec", "-i", "nivult-db-1", "psql", "-U", "nivult",
                        "-d", "nivult_ats", "-tA", "-R", SEP, "-v", "ON_ERROR_STOP=1"],
                       input=sql, capture_output=True, text=True)
    if p.returncode:
        raise SystemExit(f"psql: {p.stderr[:300]}")
    fuori = []
    for r in p.stdout.split(SEP):
        r = r.strip()
        if not r.startswith("{"):
            continue
        try:
            d = json.loads(r)
        except Exception:                                          # noqa: BLE001
            continue
        t = ancoraggio.pulito(d.get("testo"))
        if len(t) < 300:
            continue
        fuori.append(f"{d.get('title') or ''}\n{t}")
        if len(fuori) >= n:
            break
    return fuori


def main() -> None:
    tok = AutoTokenizer.from_pretrained(MOD)
    mod = AutoModelForTokenClassification.from_pretrained(MOD).eval()
    testi = prendi(QUANTE)
    print(f"{len(testi)} annunci lunghi presi dal magazzino\n")

    def marca(pezzo: str) -> set[str]:
        enc = tok([pezzo], truncation=True, max_length=MAXLEN,
                  return_offsets_mapping=True, return_tensors="pt")
        off = enc.pop("offset_mapping")
        with torch.inference_mode():
            pr = torch.softmax(mod(**enc).logits, -1)
        return set(dem.voci(pezzo, pr[0], off[0].tolist(), SOGLIA))

    prima_tot = dopo_tot = 0
    guadagno = []
    n_pezzi = 0
    for t in testi:
        pezzi = finestre(tok, t, MAXLEN)
        n_pezzi += len(pezzi)
        prima = marca(t)                       # una finestra sola: il resto si perde
        dopo = set()
        for p in pezzi:
            dopo |= marca(p)
        prima_tot += len(prima)
        dopo_tot += len(dopo)
        nuove = dopo - prima
        if nuove:
            guadagno.append((len(pezzi), sorted(nuove)[:6]))

    n = len(testi) or 1
    print(f"  finestre per annuncio: {n_pezzi / n:.1f} in media")
    print(f"  tecnologie PRIMA (una finestra):  {prima_tot}  ({prima_tot / n:.2f} per annuncio)")
    print(f"  tecnologie DOPO  (tutte):         {dopo_tot}  ({dopo_tot / n:.2f} per annuncio)")
    if prima_tot:
        print(f"  guadagno: +{(dopo_tot - prima_tot) * 100 / prima_tot:.0f}%")
    print(f"\n  annunci in cui la coda conteneva qualcosa: {len(guadagno)}/{n}")
    for q, nomi in guadagno[:10]:
        print(f"     {q} finestre → {nomi}")


if __name__ == "__main__":
    main()
