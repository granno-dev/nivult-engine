"""Un maestro si misura PRIMA di addestrarci sopra.

Il 2B, sulle otto famiglie che il buttafuori escludeva, trovava 0,16 tecnologie
per annuncio nei mestieri contro le 2,15 che ci sono davvero: ne vedeva il 7%,
e lasciava vuoto il 90,7% degli annunci. La testa v1 ha imparato da li' e ha
copiato il silenzio (richiamo 31,9%).

Prima di rifare il dataset con un altro maestro bisogna sapere se quel maestro
ci vede: e' la lezione del giro C, dove un addestramento perfetto ha prodotto
un modello bocciato perche' nessuno aveva misurato chi dettava le etichette.

Qui si prende un candidato, gli si da' la rubrica nel messaggio di sistema (la
rubrica nel prompt vale molti punti: gia' misurato su v2) e lo si interroga
sulle 104 righe etichettate a mano delle otto famiglie. Stesso confronto
morbido di `esame_golden.py`, o i voti non si possono mettere accanto.

Le voci che il maestro restituisce passano dallo STESSO filtro d'ancoraggio
della produzione: un nome che nel testo non si puo' puntare col dito non
conta, ne' a favore ne' contro. E' come verrebbe usato davvero.

  GROQ_API_KEY=... python prova_maestro.py --modello openai/gpt-oss-120b
"""
from __future__ import annotations
import argparse
import collections
import concurrent.futures as cf
import contextlib
import glob
import gzip
import importlib.util
import json
import os
import re
import sys
import time
import urllib.request

sp = importlib.util.spec_from_file_location("a", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ancoraggio.py"))
A = importlib.util.module_from_spec(sp)
sp.loader.exec_module(A)

SISTEMA = """Estrai dall'annuncio di lavoro le TECNOLOGIE che nomina.

La domanda non e' «questa parola e' tecnica?» ma: l'annuncio nomina uno
strumento con un'identita' propria, che chi legge potrebbe cercare per nome?

SONO tecnologie:
- software e applicazioni con un nome proprio (Excel, SAP, Salesforce, AutoCAD, Opera PMS)
- linguaggi, framework, librerie (Python, C++, React)
- basi di dati, piattaforme, sistemi operativi (PostgreSQL, AWS, Kubernetes, Linux)
- protocolli, standard e norme tecniche con una sigla d'uso (ISO 9001, ISO 27001, HACCP,
  OWASP, NFPA 72, NEC, D1.1 GMAW): sono impianti su cui ci si certifica, non discipline
- MACCHINARI E APPARATI con un nome o un tipo riconoscibile: pressa piegatrice, tornio
  CNC, muletto, saldatrice TIG, MIG welding, videosorveglianza, Brandmeldeanlagen,
  trattori, mietitrebbie, pompe di sollevamento, quadri elettrici, apparecchi a raggi X,
  ecografo, defibrillatore, motoslitta. QUI STA LA PARTE CHE I MODELLI SBAGLIANO DI PIU':
  un elettricista, un saldatore, un camionista e un infermiere HANNO tecnologie, si
  chiamano solo in un altro modo. Non dare per scontato che un annuncio manuale non ne abbia.
- sigle di categoria di software: ERP, MES, CRM, HCM, PLM, POS, EMR, IMD
- metodi con un nome proprio e un corpo definito: Scrum, Kanban, Six Sigma, ITIL, GAAP

NON sono tecnologie:
- discipline e settori (ingegneria civile, project management, contabilita')
- processi e attivita' (pianificazione, budgeting, manutenzione, installazione)
- qualita' personali (problem solving, leadership, precisione)
- benefit e piattaforme di welfare (401k, buoni pasto, Wagestream, DailyPay, Blue Light Card)
- materiali e prodotti (rame, acciaio, additivi, cemento)
- titoli e abilitazioni della persona (laurea, patente B, primo soccorso, BLS, CPR, FIMO)
- lingue naturali
- reparti, mansioni, e il marchio del DATORE (BMW se e' il concessionario che assume)
- leggi e regimi di approvazione (GDPR, OSHA, HIPAA, Care Act)
- il software per CANDIDARSI (Workday, Chrome, Firefox nominati per mandare la domanda)

Un marchio vale se e' il sistema su cui si lavora («impianti antincendio Notifier,
Simplex, Honeywell»), non se e' chi assume.

COME SI SCRIVE IL NOME: nella forma esatta in cui l'annuncio lo scrive, non nella
forma commerciale completa. Se il testo dice «Excel», scrivi «Excel». Non tradurre.

Un annuncio senza tecnologie e' un esito normale: rispondi con una lista vuota.
Un elenco vuoto e' piu' utile di un elenco inventato.

Rispondi SOLO con JSON: {"tecnologie": ["...", "..."]}"""


def combacia(a: set[str], b: set[str]) -> int:
    bb = [y.lower() for y in b]
    return sum(1 for x in (s.lower() for s in a) if any(x in y or y in x for y in bb))


def chiedi(url: str, chiave: str, modello: str, testo: str, tentativi: int = 4):
    body = json.dumps({"model": modello,
                       "messages": [{"role": "system", "content": SISTEMA},
                                    {"role": "user", "content": testo}],
                       "temperature": 0, "max_tokens": 1200,
                       "response_format": {"type": "json_object"}}).encode()
    for k in range(tentativi):
        try:
            # SENZA User-Agent Groq risponde 403 e basta. urllib ne manda uno
            # («Python-urllib/3.12») che viene rifiutato, mentre curl passa: e'
            # costato un giro intero di 104 chiamate tutte fallite (20/09/2026).
            rq = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json", "User-Agent": "nivult/1.0",
                "Authorization": f"Bearer {chiave}"})
            d = json.load(urllib.request.urlopen(rq, timeout=180))
            return d["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            # IL TETTO E' SUI TOKEN AL MINUTO, non sulle richieste: il piano
            # gratuito di Groq ne da' 8.000, e un annuncio ne pesa ~2.000. Con
            # quattro richieste in parallelo il 46% e' caduto in 429 e il voto
            # e' uscito su mezzo campione (20/09/2026). Qui si aspetta quello
            # che il server stesso dice di aspettare, invece di indovinare.
            attesa = 0.0
            if e.code == 429:
                with contextlib.suppress(Exception):
                    attesa = float(e.headers.get("retry-after") or 0)
                attesa = max(attesa, 20.0)
            if k == tentativi - 1:
                return f"__ERRORE__ {e}"
            time.sleep(attesa or 4 * (k + 1))
        except Exception as e:                                     # noqa: BLE001
            if k == tentativi - 1:
                return f"__ERRORE__ {e}"
            time.sleep(4 * (k + 1))
    return "__ERRORE__"


def voci(grezza: str) -> list[str] | None:
    """None = il modello non ha risposto: non e' un «nessuna tecnologia»."""
    if not grezza or grezza.startswith("__ERRORE__"):
        return None
    m = re.search(r"\{.*\}", grezza, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:                                              # noqa: BLE001
        return None
    v = d.get("tecnologie")
    if not isinstance(v, list):
        return None
    return [str(x).strip() for x in v if str(x).strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modello", default="openai/gpt-oss-120b")
    ap.add_argument("--url", default="https://api.groq.com/openai/v1/chat/completions")
    ap.add_argument("--chiave", default=os.environ.get("GROQ_API_KEY", ""))
    ap.add_argument("--golden", default="/opt/nivult/golden-tec-famiglie")
    ap.add_argument("--banco", default="/opt/nivult/banco-famiglie.jsonl.gz")
    ap.add_argument("--par", type=int, default=2,
                    help="oltre 2 si sfonda il tetto dei token al minuto")
    ap.add_argument("--uscita", default="/opt/nivult/maestro-famiglie.json")
    a = ap.parse_args()
    if not a.chiave:
        print("manca la chiave"); return 2

    mano, fam = {}, {}
    for p in sorted(glob.glob(os.path.join(a.golden, "etichette-*.json"))):
        j = json.load(open(p))
        for o in j["offerte"]:
            mano[o["id"]] = {t["nome"] for t in o["tecnologie"]}
            fam[o["id"]] = j["famiglia"]
    testi = {}
    for linea in gzip.open(a.banco, "rt"):
        d = json.loads(linea)
        if d["id"] in mano:
            testi[d["id"]] = f"Titolo: {d.get('title') or ''}\n\n{A.pulito(d.get('text'))}"
    print(f"{len(mano)} righe a mano, {len(testi)} testi, maestro {a.modello}", flush=True)

    def una(jid):
        return jid, chiedi(a.url, a.chiave, a.modello, testi[jid][:12000])

    risposte = {}
    with cf.ThreadPoolExecutor(a.par) as ex:
        for i, (jid, g) in enumerate(ex.map(una, list(testi)), 1):
            risposte[jid] = g
            if i % 20 == 0:
                print(f"  {i}/{len(testi)}", flush=True)

    # un errore non deve sparire dentro «muto»: la prima volta si stampa, o si
    # scambia un guasto di rete per un maestro che non trova niente
    for jid, g in risposte.items():
        if str(g).startswith("__ERRORE__"):
            print(f"  esempio di errore: {g[:200]}", flush=True)
            break

    tp = fp = fn = muti = 0
    vuoti_ok = vuoti = 0
    per_fam = collections.defaultdict(lambda: [0, 0, 0])
    dettaglio = []
    for jid, veri in mano.items():
        v = voci(risposte.get(jid, ""))
        if v is None:
            muti += 1
            continue
        tenuti, _ = A.filtra(testi.get(jid, ""), v)
        t = set(tenuti)
        if not veri:
            vuoti += 1
            vuoti_ok += not t
        ok = combacia(veri, t)
        tp += ok
        fn += len(veri) - ok
        fp += len(t) - combacia(t, veri)
        f = fam.get(jid, "?")
        per_fam[f][0] += ok
        per_fam[f][1] += len(veri) - ok
        per_fam[f][2] += len(t) - combacia(t, veri)
        dettaglio.append({"id": jid, "famiglia": f, "veri": sorted(veri), "trovati": sorted(t)})

    p = tp / (tp + fp) * 100 if tp + fp else 0.0
    r = tp / (tp + fn) * 100 if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    print(f"\n=== maestro {a.modello}   (muti: {muti})")
    print(f"  precisione {p:.1f}%   richiamo {r:.1f}%   F1 {f1:.1f}%")
    print(f"  annunci senza tecnologie azzeccati: {vuoti_ok}/{vuoti}")
    print(f"\n  {'famiglia':<22} {'presi':>6} {'persi':>6} {'falsi':>6}   richiamo")
    for k in sorted(per_fam):
        ok, persi, falsi = per_fam[k]
        rr = ok / (ok + persi) * 100 if ok + persi else float("nan")
        print(f"  {k:<22} {ok:>6} {persi:>6} {falsi:>6}   {rr:>7.1f}%")
    print("\n  per confronto sulle STESSE 104 righe:")
    print("    testa v1 ck00500   precisione 91,7%  richiamo 31,9%  F1 47,3%")
    json.dump({"modello": a.modello, "dettaglio": dettaglio},
              open(a.uscita, "w"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
