"""Etichette di tecnologie da un maestro MISURATO, per le famiglie dove il 2B taceva.

Il dataset della testa tecnologie viene dal 2B, che sui mestieri vedeva il 7%
delle tecnologie (docs/tecnologie-in-casa.md): ~40.000 righe che insegnano a
tacere. Qui si fa etichettare un campione bilanciato di quelle famiglie da un
maestro misurato sulle 104 righe a mano (DeepSeek: richiamo 79,4%, Trades 28/28;
gpt-oss-120b via Groq era equivalente ma gratis a 8.000 token/minuto, cioe'
giorni). Le risposte passano dal filtro della rubrica come in produzione.

Le etichette vanno in `etichette_tec` (job_id, modello): il costruttore del
dataset le prende da li' con --maestro-tec, e scarta le righe del 2B di quelle
famiglie. Il testo entra INTERO: e' il maestro che deve vedere tutto.

  DEEPSEEK_API_KEY=... python etichetta_tec.py --per-famiglia 2000 --tetto-usd 30
"""
from __future__ import annotations
import argparse
import concurrent.futures as cf
import glob
import importlib.util
import json
import os
import re
import sys
import threading
import time
import urllib.request
import uuid

import psycopg

QUI = os.path.dirname(os.path.abspath(__file__))


def _carica(nome):
    sp = importlib.util.spec_from_file_location(nome, os.path.join(QUI, nome + ".py"))
    m = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


A = _carica("ancoraggio")
PM = _carica("prova_maestro")          # la rubrica: la STESSA misurata sul golden
FR = _carica("filtro_rubrica")

OTTO = ["Agriculture", "Food & Beverage", "Healthcare", "Retail", "Social Services",
        "Sports & Recreation", "Trades", "Transportation"]
CAMPI_TESTO = ("description", "content", "descriptionHtml", "descriptionPlain", "externalDescription",
               "jobDescription", "job_description", "Job_Description", "body", "content_html",
               "description_html", "descriptionBody", "text", "ShortDescriptionStr")
# listino DeepSeek chat (settembre 2026), dollari per milione di token
PREZZO_IN, PREZZO_IN_CACHE, PREZZO_OUT = 0.27, 0.07, 1.10

DDL = """
CREATE TABLE IF NOT EXISTS etichette_tec (
  job_id     uuid NOT NULL REFERENCES ats_jobs(id) ON DELETE CASCADE,
  modello    text NOT NULL,
  tecnologie jsonb NOT NULL,          -- [{"nome": "..."}] dopo il filtro della rubrica
  tolte      jsonb,                   -- [[voce, ragione]] scartate dal filtro
  grezze     jsonb,                   -- la risposta del maestro com'era: se il filtro cambia non si ripaga
  famiglia   text,
  uso        jsonb,                   -- token della chiamata
  creato_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, modello));
ALTER TABLE etichette_tec ADD COLUMN IF NOT EXISTS grezze jsonb"""

# Campione casuale SENZA ordinare per md5: quello costringeva a leggere tutte le
# righe della famiglia (7 minuti a query, e il db di produzione in ginocchio,
# 21/09). Qui si parte da un uuid a caso e si scorre l'indice primario: il
# planner si ferma dopo le prime N righe buone. L'uuid e' casuale, quindi il
# campione lo e' — solo che ogni chiamata prende un «segmento» diverso.
SQL_CAMPIONE = """
SELECT j.id, j.title, coalesce(x.family, x.v1_family) AS fam,
       coalesce((SELECT v FROM unnest(ARRAY[{campi}]) v WHERE length(v) >= 300 LIMIT 1), '')
  FROM job_classifications x JOIN ats_jobs j ON j.id = x.job_id
 WHERE x.job_id >= %s::uuid
   AND (x.family = %s OR (x.family IS NULL AND x.v1_family = %s))
   AND j.expired_at IS NULL
   AND EXISTS (SELECT 1 FROM unnest(ARRAY[{campi}]) v WHERE length(v) >= 300)
   AND NOT (j.id = ANY(%s))
   AND NOT EXISTS (SELECT 1 FROM etichette_tec t WHERE t.job_id = j.id AND t.modello = %s)
 ORDER BY x.job_id LIMIT %s
"""


def dsn() -> str:
    if os.environ.get("ATS_DATABASE_URL"):
        return os.environ["ATS_DATABASE_URL"]
    for f in ("/opt/nivult/.env", "/opt/nivult/engine/.env"):
        try:
            m = re.search(r"^POSTGRES_PASSWORD=(.*)$", open(f).read(), re.M)
            if m:
                return "postgresql://nivult:" + m.group(1).strip() + "@127.0.0.1:5432/nivult_ats"
        except OSError:
            pass
    raise SystemExit("ATS_DATABASE_URL assente")


def chiave_deepseek() -> str:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    for f in ("/opt/nivult/.env", "/opt/nivult/engine/.env"):
        try:
            m = re.search(r"^DEEPSEEK_API_KEY=(.*)$", open(f).read(), re.M)
            if m:
                return m.group(1).strip()
        except OSError:
            pass
    raise SystemExit("DEEPSEEK_API_KEY assente")


def chiedi(url, chiave, modello, testo, tentativi=4):
    body = json.dumps({"model": modello, "temperature": 0, "max_tokens": 1200,
                       "response_format": {"type": "json_object"},
                       "messages": [{"role": "system", "content": PM.SISTEMA},
                                    {"role": "user", "content": testo}]}).encode()
    err = ""
    for k in range(tentativi):
        try:
            rq = urllib.request.Request(url, data=body, method="POST", headers={
                "Content-Type": "application/json", "User-Agent": "nivult/1.0",
                "Authorization": f"Bearer {chiave}"})
            d = json.load(urllib.request.urlopen(rq, timeout=240))
            voci = json.loads(d["choices"][0]["message"]["content"]).get("tecnologie") or []
            voci = [str(v).strip() for v in voci if str(v).strip()]
            return voci, d.get("usage") or {}
        except Exception as e:                                        # noqa: BLE001
            err = str(e)[:200]
            time.sleep(6 * (k + 1))
    return None, {"errore": err}


def costo(uso: dict) -> float:
    hit = uso.get("prompt_cache_hit_tokens", 0) or 0
    miss = uso.get("prompt_cache_miss_tokens", uso.get("prompt_tokens", 0)) or 0
    out = uso.get("completion_tokens", 0) or 0
    return (hit * PREZZO_IN_CACHE + miss * PREZZO_IN + out * PREZZO_OUT) / 1e6


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--famiglie", default=",".join(OTTO))
    ap.add_argument("--per-famiglia", type=int, default=2000)
    ap.add_argument("--modello", default="deepseek-chat")
    ap.add_argument("--url", default="https://api.deepseek.com/chat/completions")
    ap.add_argument("--par", type=int, default=6)
    ap.add_argument("--tetto-usd", type=float, default=30.0)
    ap.add_argument("--golden", default="/opt/nivult/golden-tec,/opt/nivult/golden-tec-famiglie")
    ap.add_argument("--prova", action="store_true", help="10 righe per famiglia, e stampa cosa esce")
    a = ap.parse_args()
    if a.prova:
        a.per_famiglia = 10

    fuori: list[str] = []
    for d in a.golden.split(","):
        for p in sorted(glob.glob(os.path.join(d, "etichette-*.json"))):
            fuori += [o["id"] for o in json.load(open(p))["offerte"]]
    if not fuori:
        print("nessuna etichetta a mano trovata: il golden finirebbe nel dataset. Mi fermo.")
        return 2
    print(f"escluse {len(fuori)} righe del golden", flush=True)

    chiave = chiave_deepseek()
    q = SQL_CAMPIONE.format(campi=", ".join(f"j.raw->>'{c}'" for c in CAMPI_TESTO))
    with psycopg.connect(dsn(), autocommit=True) as c:
        c.execute(DDL)
        lavoro = []
        for fam in a.famiglie.split(","):
            c.execute("SET statement_timeout = '10min'")
            righe = c.execute(q, (str(uuid.uuid4()), fam, fam, fuori, a.modello, a.per_famiglia)).fetchall()
            print(f"  {fam:<22} {len(righe):>5} da etichettare", flush=True)
            lavoro += righe
        print(f"{len(lavoro)} annunci, tetto {a.tetto_usd:.0f} $", flush=True)

        spesa, fatte, vuote, muti, lock = 0.0, 0, 0, 0, threading.Lock()
        stop = threading.Event()

        def una(r):
            if stop.is_set():
                return None
            jid, titolo, fam, grezzo = r
            testo = A.pulito(grezzo)                      # intero: mai tagliare
            voci, uso = chiedi(a.url, chiave, a.modello, f"{titolo}\n\n{testo}")
            return jid, fam, voci, uso

        with cf.ThreadPoolExecutor(a.par) as ex:
            for esito in ex.map(una, lavoro):
                if esito is None:
                    continue
                jid, fam, voci, uso = esito
                with lock:
                    if voci is None:
                        muti += 1
                        continue
                    tenute, tolte = FR.filtra_rubrica(voci)
                    c.execute("INSERT INTO etichette_tec (job_id, modello, tecnologie, tolte, grezze, famiglia, uso) "
                              "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (job_id, modello) DO NOTHING",
                              (jid, a.modello, json.dumps([{"nome": n} for n in tenute]),
                               json.dumps(tolte), json.dumps(voci), fam, json.dumps(uso)))
                    fatte += 1
                    vuote += not tenute
                    spesa += costo(uso)
                    if a.prova:
                        print(f"    [{fam}] {tenute}  tolte={[t[0] for t in tolte]}")
                    if fatte % 100 == 0:
                        print(f"  {fatte}/{len(lavoro)} | vuote {vuote} | muti {muti} | spesa {spesa:.2f} $", flush=True)
                    if spesa >= a.tetto_usd:
                        print(f"TETTO di spesa raggiunto ({spesa:.2f} $): mi fermo", flush=True)
                        stop.set()
        print(f"\nFINE: {fatte} etichettate, {vuote} senza tecnologie ({100*vuote/max(fatte,1):.0f}%), "
              f"{muti} muti, spesa {spesa:.2f} $", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
