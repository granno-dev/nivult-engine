#!/usr/bin/env python3
"""Confronto fra modelli di valutazione, sulle stesse offerte e lo stesso prompt.

UNA OFFERTA PER CHIAMATA, non a lotti: nello stesso prompt il modello confronta
le offerte fra loro invece di misurarle contro il CV, e finisce per distribuire
i voti sulla scala del lotto invece che su quella assoluta.

--filters applica il primo stadio del funnel (filtri deterministici) prima di
chiamare il modello: è il verso in cui girerà la produzione, e il confronto
fra una run con e una senza filtri dice quanto il filtro risparmia.

--from-run riprende il campione esatto di una run precedente: cambiare campione
fra due run renderebbe il confronto rumoroso quanto il dato che si vuole
misurare.
"""
from __future__ import annotations
import argparse, hashlib, json, os, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import httpx, psycopg
from psycopg.rows import dict_row
from nivult.config import load_dotenv, migrator_database_url

BASE_URL = os.environ.get("GLM_BASE_URL", "https://api.z.ai/api/paas/v4")

# Ordine del funnel: paese e lingua tagliano più di tutto, e metterli prima
# rende il conteggio degli stadi successivi più leggibile.
ORDINE_FILTRI = ("countries", "languages", "experience", "work_arrangement",
                 "employment_type", "employer_kind", "headcount")

NOMI_FILTRI = {
    "countries": "paese",
    "languages": "lingua",
    "experience": "esperienza",
    "work_arrangement": "modalità",
    "employment_type": "contratto",
    "employer_kind": "tipo datore",
    "headcount": "dimensione azienda",
}

RUBRICA = """Sei un selezionatore esperto. Valuta quanto UNA offerta di lavoro e
adatta al profilo del candidato che ti viene dato.

Punteggio da 0 a 100:
  90-100  corrispondenza forte: ruolo, livello e competenze coincidono
  70-89   buona: il ruolo e giusto, qualche competenza manca
  50-69   plausibile: settore o livello divergono, ma il passaggio e credibile
  20-49   debole: solo affinita generiche
   0-19   non pertinente

Considera ruolo, seniority, competenze richieste, lingua e sede.
NON premiare un'offerta perche prestigiosa o ben scritta: conta solo l'aderenza
al profilo. NON premiare la genericita: un'offerta vaga che potrebbe adattarsi a
chiunque non e una buona corrispondenza.

Rispondi SOLO con questo JSON, niente altro:
{"score": <0-100>, "reason": "<una frase in italiano, massimo 25 parole>"}"""


def offerta_come_testo(j: dict) -> str:
    righe = [f"Titolo: {j['title']}"]
    if j.get("organization"): righe.append(f"Azienda: {j['organization']}")
    if j.get("cities"): righe.append(f"Sede: {', '.join(j['cities'][:3])}")
    if j.get("ai_experience_level"): righe.append(f"Esperienza richiesta: {j['ai_experience_level']}")
    if j.get("ai_work_arrangement"): righe.append(f"Modalita: {j['ai_work_arrangement']}")
    if j.get("ai_key_skills"): righe.append(f"Competenze: {', '.join(j['ai_key_skills'][:15])}")
    if j.get("ai_requirements_summary"): righe.append(f"Requisiti: {j['ai_requirements_summary'][:700]}")
    return "\n".join(righe)


def applica_filtri(jobs: list[dict], filtri: dict, cur) -> tuple[list[dict], list[tuple[str, int]]]:
    """Il primo stadio del funnel: filtri deterministici prima del modello.

    REGOLA: un campo NULL — o una lista vuota, che è il modo in cui una fonte
    dice "non lo so" — NON esclude mai. Escludere per assenza di dato
    nasconderebbe un'offerta per un motivo che non è una scelta dell'utente.
    Specchia le colonne di user_clusters: quando il funnel vero sarà scritto,
    dovrà comportarsi come qui.
    """
    # La seniority è un intervallo di rank, non un elenco di codici: min e max
    # aperti da un lato funzionano lo stesso.
    livelli: set[str] | None = None
    if filtri.get("min_seniority") or filtri.get("max_seniority"):
        cur.execute(
            "SELECT code FROM experience_levels WHERE rank BETWEEN "
            "COALESCE((SELECT rank FROM experience_levels WHERE code = %s), 0) AND "
            "COALESCE((SELECT rank FROM experience_levels WHERE code = %s), 4)",
            (filtri.get("min_seniority"), filtri.get("max_seniority")))
        livelli = {r[0] for r in cur.fetchall()}

    def f_countries(j):
        acc = filtri.get("countries") or []
        return not acc or not j["countries"] or bool(set(j["countries"]) & set(acc))

    def f_languages(j):
        acc = filtri.get("languages") or []
        return not acc or j["ai_job_language"] is None or j["ai_job_language"] in acc

    def f_experience(j):
        return livelli is None or j["ai_experience_level"] is None \
            or j["ai_experience_level"] in livelli

    def f_arrangement(j):
        acc = filtri.get("work_arrangements") or []
        return not acc or j["ai_work_arrangement"] is None or j["ai_work_arrangement"] in acc

    def f_employment(j):
        acc = filtri.get("employment_types") or []
        return not acc or j["ai_employment_type"] is None or j["ai_employment_type"] in acc

    def f_employer(j):
        acc = filtri.get("accepted_employer_kinds") or []
        return not acc or j["employer_kind"] is None or j["employer_kind"] in acc

    def f_headcount(j):
        lo, hi = filtri.get("min_headcount"), filtri.get("max_headcount")
        h = j["org_headcount"]
        return (lo is None and hi is None) or h is None \
            or ((lo is None or h >= lo) and (hi is None or h <= hi))

    stadi = {"countries": f_countries, "languages": f_languages,
             "experience": f_experience, "work_arrangement": f_arrangement,
             "employment_type": f_employment, "employer_kind": f_employer,
             "headcount": f_headcount}

    superstiti = list(jobs)
    resoconto: list[tuple[str, int]] = []
    for stadio in ORDINE_FILTRI:
        superstiti = [j for j in superstiti if stadi[stadio](j)]
        resoconto.append((stadio, len(superstiti)))
    return superstiti, resoconto


def estrai(testo: str) -> dict:
    t = testo.strip()
    if t.startswith("```"): t = re.sub(r"^```[a-z]*\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m: raise ValueError(f"nessun JSON: {testo[:150]}")
        return json.loads(m.group(0))


class Contatori:
    def __init__(self):
        self.lock = threading.Lock()
        self.input = self.cached = self.output = 0
        self.ok = self.failed = self.limited = 0
    def somma(self, **kw):
        with self.lock:
            for k, v in kw.items(): setattr(self, k, getattr(self, k) + v)


def valuta_una(client, key, model, testa, job, cnt, max_retry=5):
    corpo = testa + [{"role": "user", "content": "OFFERTA\n" + offerta_come_testo(job)}]
    for tentativo in range(max_retry):
        t0 = time.monotonic()
        try:
            r = client.post(f"{BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": corpo, "temperature": 0,
                      "max_tokens": 200, "thinking": {"type": "disabled"}})
        except httpx.HTTPError as exc:
            time.sleep(min(2 ** tentativo, 20))
            if tentativo == max_retry - 1:
                cnt.somma(failed=1)
                return {"job_id": job["id"], "error": f"rete: {exc}"[:200]}
            continue
        lat = int((time.monotonic() - t0) * 1000)
        if r.status_code == 429:
            cnt.somma(limited=1)
            attesa = float(r.headers.get("Retry-After", min(2 ** tentativo, 30)))
            time.sleep(min(attesa, 60)); continue
        if r.status_code != 200:
            cnt.somma(failed=1)
            return {"job_id": job["id"], "error": f"http {r.status_code}: {r.text[:150]}"}
        d = r.json(); u = d.get("usage") or {}
        cnt.somma(input=u.get("prompt_tokens", 0),
                  cached=(u.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
                  output=u.get("completion_tokens", 0), ok=1)
        try:
            p = estrai(d["choices"][0]["message"]["content"])
            return {"job_id": job["id"], "score": max(0, min(100, int(p.get("score", 0)))),
                    "reason": (p.get("reason") or "")[:400], "latency_ms": lat}
        except (ValueError, KeyError, TypeError) as exc:
            cnt.somma(failed=1)
            return {"job_id": job["id"], "error": f"risposta illeggibile: {exc}"[:200]}
    cnt.somma(failed=1)
    return {"job_id": job["id"], "error": "esauriti i tentativi (429 ripetuti)"}


def esegui_modello(key, model, cv, jobs, concorrenza):
    testa = [{"role": "system", "content": RUBRICA},
             {"role": "system", "content": "PROFILO DEL CANDIDATO\n" + cv}]
    cnt = Contatori(); t0 = time.time()
    with httpx.Client(timeout=120) as client:
        with ThreadPoolExecutor(max_workers=concorrenza) as pool:
            risultati = list(pool.map(lambda j: valuta_una(client, key, model, testa, j, cnt), jobs))
    return risultati, cnt, time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cv", required=True)
    ap.add_argument("--sample", type=int, default=200)
    ap.add_argument("--models", default="glm-5.2,glm-4.7-flashx,glm-4.7-flash")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--label", default=None)
    ap.add_argument("--filters", default=None,
                    help="JSON con i filtri deterministici del funnel "
                         "(stessi nomi delle colonne di user_clusters; vedi filtri-esempio.json)")
    ap.add_argument("--from-run", dest="from_run", default=None,
                    help="riprende il campione esatto di questa run: cambiare "
                         "campione rende il confronto rumoroso quanto il dato da misurare")
    args = ap.parse_args()

    load_dotenv()
    key = os.environ.get("GLM_API_KEY", "")
    if not key: raise SystemExit("Serve GLM_API_KEY.")
    cv = Path(args.cv).read_text(encoding="utf-8").strip()
    modelli = [m.strip() for m in args.models.split(",") if m.strip()]
    etichetta = args.label or f"confronto {len(modelli)} modelli"
    filtri = json.loads(Path(args.filters).read_text(encoding="utf-8")) if args.filters else None

    CAMPI = ("j.id::text, j.title, j.organization, j.cities, j.countries, j.source, "
             "j.ai_key_skills, j.ai_experience_level, j.ai_work_arrangement, "
             "j.ai_employment_type, j.ai_job_language, j.employer_kind, "
             "j.org_headcount, j.ai_requirements_summary")

    with psycopg.connect(migrator_database_url()) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            if args.from_run:
                cur.execute(
                    "SELECT DISTINCT s.job_id FROM benchmark_scores s "
                    "JOIN benchmark_models m ON m.id = s.model_run_id "
                    "WHERE m.run_id = %s", (args.from_run,))
                ids = [r["job_id"] for r in cur.fetchall()]
                if not ids:
                    print("la run di partenza non ha offerte."); return 1
                cur.execute(f"SELECT {CAMPI} FROM jobs j WHERE j.id = ANY(%s) "
                            "AND j.status = 'active' AND j.duplicate_of_job_id IS NULL",
                            (ids,))
            else:
                cur.execute(f"SELECT {CAMPI} FROM jobs j "
                            "WHERE j.status = 'active' AND j.duplicate_of_job_id IS NULL "
                            "ORDER BY md5(j.id::text) LIMIT %s", (args.sample,))
            jobs = cur.fetchall()
        if not jobs:
            print("nessuna offerta disponibile."); return 1
        campione = len(jobs)
        per_fonte = {}
        for j in jobs: per_fonte[j["source"]] = per_fonte.get(j["source"], 0) + 1
        print(f"campione: {len(jobs)} offerte  {per_fonte}")
        print(f"profilo: {len(cv)} caratteri (~{len(cv)//4} token)")

        if filtri:
            with conn.cursor() as cur:
                jobs, resoconto = applica_filtri(jobs, filtri, cur)
            print("\nfunnel deterministico (un campo NULL non esclude mai):")
            for stadio, n in resoconto:
                print(f"  dopo {NOMI_FILTRI[stadio]:<18} {n}")
            if not jobs:
                print("\ni filtri non lasciano nulla: niente da valutare."); return 1
            print(f"  -> GLM valuta {len(jobs)} offerte su {campione} "
                  f"({100 - 100*len(jobs)//campione}% tagliate prima di pagare il modello)")

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO benchmark_runs (label, job_count, profile_hash, "
                "  prompt_hash, reference_model, note) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (etichetta, len(jobs), hashlib.sha256(cv.encode()).hexdigest(),
                 hashlib.sha256(RUBRICA.encode()).hexdigest(), modelli[0],
                 (f"campione di {campione}"
                  + (f" dalla run {args.from_run}" if args.from_run else "")
                  + (f", {len(jobs)} superstiti ai filtri deterministici" if filtri else "")
                  or None)))
            run_id = cur.fetchone()[0]
        conn.commit()

        for model in modelli:
            print(f"── {model} " + "─" * (56 - len(model)))
            risultati, cnt, secondi = esegui_modello(key, model, cv, jobs, args.concurrency)
            print(f"   {cnt.ok} valutate, {cnt.failed} fallite, {cnt.limited} volte 429")
            print(f"   {secondi:.0f}s  ({secondi/max(len(jobs),1):.2f}s per offerta)")
            print(f"   token: {cnt.input} in ({cnt.cached} da cache), {cnt.output} out")
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO benchmark_models (run_id, model, input_tokens, "
                    "  cached_tokens, output_tokens, elapsed_s, calls_ok, calls_failed, "
                    "  rate_limited, concurrency) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "RETURNING id",
                    (run_id, model, cnt.input, cnt.cached, cnt.output, round(secondi,1),
                     cnt.ok, cnt.failed, cnt.limited, args.concurrency))
                mr_id = cur.fetchone()[0]
                cur.executemany(
                    "INSERT INTO benchmark_scores (model_run_id, job_id, score, reason, "
                    "  latency_ms, error) VALUES (%s,%s,%s,%s,%s,%s)",
                    [(mr_id, r["job_id"], r.get("score"), r.get("reason"),
                      r.get("latency_ms"), r.get("error")) for r in risultati])
            conn.commit(); print()

        print("── costo misurato " + "─" * 44)
        with conn.cursor() as cur:
            cur.execute("SELECT model, input_tokens, cached_tokens, output_tokens, "
                        "cache_pct, cost_usd, elapsed_s, rate_limited "
                        "FROM benchmark_models_v WHERE run_id = %s ORDER BY model", (run_id,))
            for m, i, c, o, cp, costo, sec, lim in cur.fetchall():
                euro = f"{costo:.5f} $" if costo is not None else "prezzo da confermare"
                print(f"  {m:<18} {i:>7} in ({cp}% cache) {o:>6} out  {sec:>6.0f}s  {euro}"
                      + (f"  429x{lim}" if lim else ""))
        print("\n── recall: top-20 del riferimento dentro le top-30 " + "─" * 12)
        with conn.cursor() as cur:
            cur.execute("SELECT model, trovate, su, recall_pct FROM benchmark_recall(%s,20,30)",
                        (run_id,))
            for m, tr, su, pct in cur.fetchall():
                print(f"  {m:<18} {tr}/{su}  {pct}%")
        print(f"\nrun salvata: {run_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
