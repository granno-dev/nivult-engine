"""Costruisce il dataset di marcature per la testa tecnologie di v1.

Il costruttore precedente e' andato perso col pod a noleggio; questo lo rifa' con
dentro la correzione che vale piu' di tutto il resto.

IL GUASTO CHE QUESTO FILE ESISTE PER EVITARE (misurato il 19/09/2026)

Su 1.035 nomi prodotti dal maestro, 226 — il 22% — non si trovavano nel testo
alla lettera. Il costruttore vecchio cercava il nome cosi' com'era scritto: non
lo trovava, non segnava niente, e quel pezzo di testo diventava un esempio
NEGATIVO. Al modello stavamo insegnando che «Excel» non e' una tecnologia,
proprio nelle righe dove lo era.

La causa e' che il maestro normalizza — scrive «Microsoft Excel» dove il testo
dice «Excel», «Amazon Web Services» dove dice «AWS». Per un generativo e' una
virtu'; per chi deve puntare il dito su un pezzo di testo e' veleno.

LE DUE REGOLE

1. Si prova ad ancorare il nome con le sue varianti, non solo alla lettera:
   prefisso del fornitore via, sigla dalle iniziali, punteggiatura ignorata.
2. Se un nome NON si ancora, la riga si marca «parziale». Una riga parziale ha
   etichette incomplete: usarla come esempio pulito insegna a tacere. Chi
   addestra decide se buttarla (--solo-intere) o tenerla, ma lo sa.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import json
import os
import pathlib
import sys

import psycopg

# L'ancoraggio sta in un modulo suo: il demone del 2B usa le stesse funzioni per
# buttare le invenzioni prima di scriverle. Due copie darebbero due confini
# diversi, e il modello imparerebbe su uno mentre la produzione applica l'altro.
import importlib.util as _ilu
_sp = _ilu.spec_from_file_location("ancoraggio", str(pathlib.Path(__file__).parent / "ancoraggio.py"))
ancoraggio = _ilu.module_from_spec(_sp)
_sp.loader.exec_module(ancoraggio)

pulito = ancoraggio.pulito
varianti = ancoraggio.varianti
ancora = ancoraggio.ancora


SQL = """
SELECT j.id, j.title, e.tecnologie,
       coalesce((SELECT v FROM unnest(ARRAY[{campi}]) v WHERE length(v) >= 300 LIMIT 1), '')
FROM estrazioni_v2b e JOIN ats_jobs j ON j.id = e.job_id
WHERE e.modello = ANY(%s) AND e.tecnologie IS NOT NULL
  AND NOT (j.id = ANY(%s))     -- il golden non si addestra: e' il metro
ORDER BY e.creato_at DESC
LIMIT %s
"""
CAMPI_TESTO = ("description", "content", "descriptionHtml", "descriptionPlain", "externalDescription",
               "jobDescription", "job_description", "Job_Description", "body", "content_html",
               "description_html", "descriptionBody", "text", "ShortDescriptionStr")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--righe", type=int, default=200_000)
    ap.add_argument("--maestro", default="nivult-2b,nivult-2b+tec",
                    help="i modelli le cui uscite fanno da etichette")
    ap.add_argument("--out", default="/opt/nivult/tecnologie/tecnologie-train.jsonl.gz")
    ap.add_argument("--solo-intere", action="store_true",
                    help="scarta le righe in cui almeno un nome non si e' ancorato")
    ap.add_argument("--golden", default="/opt/nivult/golden-tec",
                    help="cartella delle etichette a mano: quelle righe NON entrano nel dataset")
    a = ap.parse_args()

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    q = SQL.format(campi=", ".join(f"j.raw->>'{c}'" for c in CAMPI_TESTO))

    fuori: list[str] = []
    for p in sorted(glob.glob(os.path.join(a.golden, "etichette-*.json"))):
        fuori += [o["id"] for o in json.load(open(p))["offerte"]]
    if not fuori:
        print(f"ATTENZIONE: nessuna etichetta a mano trovata in {a.golden}.")
        print("Il dataset conterrebbe anche le righe d'esame. Mi fermo.")
        return 2
    print(f"escluse dall'addestramento: {len(fuori)} righe del golden\n")

    st = {"righe": 0, "scritte": 0, "parziali": 0, "senza_testo": 0, "vuote": 0,
          "nomi": 0, "ancorati_alla_lettera": 0, "ancorati_con_varianti": 0, "persi": 0,
          "sovrapposti": 0}
    persi_esempi: list[str] = []

    with psycopg.connect(os.environ["ATS_DATABASE_URL"]) as c, \
            c.cursor(name="costruisci") as k, gzip.open(a.out, "wt") as f:
        k.itersize = 2000
        k.execute(q, (a.maestro.split(","), fuori, a.righe))
        for jid, titolo, tecs, grezzo in k:
            st["righe"] += 1
            testo = pulito(grezzo)
            if len(testo) < 300:
                st["senza_testo"] += 1
                continue
            nomi = []
            for t in tecs or []:
                n = (t.get("nome") if isinstance(t, dict) else t) or ""
                if n and 1 < len(str(n)) <= 60:
                    nomi.append(str(n))
            if not nomi:
                st["vuote"] += 1
                # una riga senza tecnologie e' un negativo VERO e serve: si scrive
                f.write(json.dumps({"id": str(jid), "titolo": titolo or "", "testo": testo,
                                    "intervalli": [], "parziale": False}, ensure_ascii=False) + "\n")
                st["scritte"] += 1
                continue

            intervalli, perso = [], False
            for n in nomi:
                st["nomi"] += 1
                p = ancora(testo, n)
                if p is None:
                    st["persi"] += 1; perso = True
                    if len(persi_esempi) < 20:
                        persi_esempi.append(n)
                    continue
                if p[2].lower() == n.lower():
                    st["ancorati_alla_lettera"] += 1
                else:
                    st["ancorati_con_varianti"] += 1
                intervalli.append([p[0], p[1], p[2]])

            if perso:
                st["parziali"] += 1
                if a.solo_intere:
                    continue

            # Due nomi sullo stesso pezzo di testo («Microsoft Office» e
            # «Office») darebbero un token che appartiene a due voci: a parita'
            # di posizione tiene la marcatura piu' lunga, che e' la piu'
            # informativa, e butta l'altra.
            tenuti: list = []
            for iv in sorted(intervalli, key=lambda x: (x[0] - x[1], x[0])):
                if all(iv[1] <= t[0] or iv[0] >= t[1] for t in tenuti):
                    tenuti.append(iv)
                else:
                    st["sovrapposti"] += 1
            intervalli = tenuti

            f.write(json.dumps({"id": str(jid), "titolo": titolo or "", "testo": testo,
                                "intervalli": sorted(intervalli), "parziale": perso},
                                ensure_ascii=False) + "\n")
            st["scritte"] += 1

    n = max(st["nomi"], 1)
    print(f"righe lette dal maestro:        {st['righe']:>9,}")
    print(f"  scartate, testo assente:      {st['senza_testo']:>9,}")
    print(f"  senza tecnologie (negativi):  {st['vuote']:>9,}")
    print(f"  scritte nel dataset:          {st['scritte']:>9,}")
    print(f"  di cui parziali:              {st['parziali']:>9,}\n")
    print(f"nomi del maestro:               {st['nomi']:>9,}")
    print(f"  ancorati alla lettera:        {st['ancorati_alla_lettera']:>9,}"
          f"  ({100*st['ancorati_alla_lettera']/n:.1f}%)")
    print(f"  ancorati grazie alle varianti:{st['ancorati_con_varianti']:>9,}"
          f"  ({100*st['ancorati_con_varianti']/n:.1f}%)  <- il guadagno")
    print(f"  persi:                        {st['persi']:>9,}"
          f"  ({100*st['persi']/n:.1f}%)")
    print(f"  scartati perche' sovrapposti: {st['sovrapposti']:>9,}"
          f"  ({100*st['sovrapposti']/n:.1f}%)")
    if persi_esempi:
        print(f"\nesempi di nomi che non si ancorano: {', '.join(persi_esempi)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
