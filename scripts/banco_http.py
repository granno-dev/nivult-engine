"""Quanto rende una GPU a noleggio, sui NOSTRI dati e come la useremmo davvero.

Interroga il server HTTP di vLLM — lo stesso modo in cui gia' usiamo llama-server
in produzione. Solo libreria standard: niente da installare sul pod.

Risponde a due domande:
  1. quante offerte all'ora, cioe' quante ore al giorno servirebbero;
  2. le tecnologie sono migliori di quelle del 2B, sulle STESSE offerte?

Il confronto e' appaiato: le stesse offerte che il 2B ha gia' letto. Due medie su
campioni diversi non si confrontano.

  python banco_http.py --url http://127.0.0.1:8000 --quante 400 --par 64
"""
from __future__ import annotations
import argparse
import concurrent.futures as cf
import gzip
import json
import re
import time
import urllib.error
import urllib.request

SISTEMA_TEC = ("Sei l'estrattore di Nivult. Leggi l'annuncio di lavoro e rispondi SOLO con un JSON "
               "con le chiavi: tecnologie. tecnologie = lista di {nome, ruolo} con ruolo in "
               "usata|servizio|gradita. Solo le tecnologie scritte nell'annuncio, niente altro.")

# Lo STESSO vincolo della grammatica che usa la produzione (scripts/tec.gbnf),
# scritto come schema JSON perche' e' cosi' che lo vuole vLLM. Con questo il
# modello non PUO' scrivere altro. Il 28% di risposte illeggibili della prima
# prova era colpa mia, non sua: l'avevo lasciato libero mentre il 2B in
# produzione e' vincolato dalla grammatica.
SCHEMA_TEC = {
    "type": "object",
    "properties": {
        "tecnologie": {
            "type": "array",
            "maxItems": 40,
            "items": {
                "type": "object",
                "properties": {
                    "nome": {"type": "string", "maxLength": 60},
                    "ruolo": {"type": "string", "enum": ["usata", "servizio", "gradita"]},
                },
                "required": ["nome", "ruolo"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tecnologie"],
    "additionalProperties": False,
}


def prompt(r: dict) -> str:
    return (f"Titolo: {r.get('title') or ''}\n"
            f"Sede: {r.get('location') or ''} ({r.get('country') or '-'})\n"
            f"Lingua dell'annuncio: {r.get('lang') or '?'}\n\n{r.get('text') or ''}")


def chiedi(url: str, modello: str, testo: str, max_nuovi: int, guidato: bool = True):
    """Una chiamata. Ritorna (testo, token_letti, token_scritti)."""
    d = {
        "model": modello,
        "messages": [{"role": "system", "content": SISTEMA_TEC},
                     {"role": "user", "content": testo}],
        "max_tokens": max_nuovi, "temperature": 0,
        # Qwen3 ragiona a voce alta se non glielo si vieta, e qui non serve
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if guidato:
        d["response_format"] = {"type": "json_schema",
                                "json_schema": {"name": "tecnologie",
                                                "schema": SCHEMA_TEC, "strict": True}}
    corpo = json.dumps(d).encode()
    # Il proxy di RunPod sta dietro Cloudflare, che rifiuta `Python-urllib` con un
    # 403 codice 1010 («banned based on your browser's signature»). Senza questa
    # riga tutte le chiamate falliscono in un decimo di secondo — e il conto delle
    # offerte/ora diventa un numero enorme e falso.
    rq = urllib.request.Request(url + "/v1/chat/completions", data=corpo,
                                headers={"Content-Type": "application/json",
                                         "User-Agent": "curl/8.5.0"})
    try:
        with urllib.request.urlopen(rq, timeout=300) as r:
            risp = json.load(r)
        u = risp.get("usage") or {}
        return (risp["choices"][0]["message"]["content"],
                u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
    except urllib.error.HTTPError as e:
        return f"__ERRORE__ HTTP{e.code} {e.read()[:120]!r}", 0, 0
    except Exception as e:                                       # noqa: BLE001
        return f"__ERRORE__ {type(e).__name__}", 0, 0


def estrai_json(t: str):
    m = re.search(r"\{.*\}", t or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:                                            # noqa: BLE001
        return None


def nomi(tec) -> set[str]:
    return {x["nome"].strip().lower() for x in (tec or [])
            if isinstance(x, dict) and isinstance(x.get("nome"), str) and x["nome"].strip()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--modello", default="Qwen/Qwen3-8B")
    ap.add_argument("--banco", default="/workspace/banco-gpu.jsonl.gz")
    ap.add_argument("--quante", type=int, default=400)
    ap.add_argument("--par", type=int, default=64)
    ap.add_argument("--max-nuovi", type=int, default=200)
    ap.add_argument("--libero", action="store_true",
                    help="senza decodifica guidata, com era la prima prova")
    a = ap.parse_args()
    guidato = not a.libero

    righe = []
    with gzip.open(a.banco, "rt") as f:
        for l in f:
            righe.append(json.loads(l))
            if len(righe) >= a.quante:
                break
    modo = "libera" if a.libero else "GUIDATA dallo schema"
    print(f"banco: {len(righe)} offerte | parallelismo {a.par} | decodifica {modo}", flush=True)

    # una chiamata a vuoto per svegliare il server: non deve entrare nel conto.
    # E se fallisce, si smette subito invece di misurare 400 fallimenti.
    prova = chiedi(a.url, a.modello, "prova", 8, guidato)
    if str(prova[0]).startswith("__ERRORE__"):
        print(f"  la prima chiamata e' fallita: {prova[0][:140]}")
        print("  mi fermo: non ha senso misurare su chiamate che non arrivano.")
        return 2
    print(f"  chiamata di prova riuscita: {str(prova[0])[:60]}", flush=True)

    t0 = time.time()
    with cf.ThreadPoolExecutor(a.par) as ex:
        esiti = list(ex.map(lambda r: chiedi(a.url, a.modello, prompt(r),
                                             a.max_nuovi, guidato), righe))
    dt = time.time() - t0

    tok_in = sum(e[1] for e in esiti)
    tok_out = sum(e[2] for e in esiti)
    muti = [e[0] for e in esiti if str(e[0]).startswith("__ERRORE__")]

    print("\n=== VELOCITA' ===")
    print(f"  {len(righe)} offerte in {dt:.1f}s   (errori di rete: {len(muti)})")
    if len(muti) > len(righe) * 0.05:
        print(f"  NON MISURABILE: {len(muti)} chiamate su {len(righe)} sono fallite.")
        print(f"  primo errore: {muti[0][:140]}")
        print("  Un numero calcolato su chiamate fallite sarebbe inventato.")
        return 2
    print(f"  token letti   {tok_in:>9,}  ->  {tok_in/dt:>7,.0f}/s")
    print(f"  token scritti {tok_out:>9,}  ->  {tok_out/dt:>7,.0f}/s")
    ora = 3600 * len(righe) / dt
    print(f"  {ora:,.0f} offerte/ora")
    # il fabbisogno misurato il 19/09: 47.916 sintesi + 34.404 tecnologie
    print(f"  ore al giorno per TUTTO il flusso (82.320 chiamate): {82320/ora:.1f}")
    print(f"  su un 4090 a 0,34 $/h: {82320/ora*0.34:.2f} $/giorno = {82320/ora*0.34*30:.0f} $/mese")

    print("\n=== QUALITA' DELLE TECNOLOGIE (contro il 2B, stesse offerte) ===")
    uguali = piu = meno = vuote = illeggibili = 0
    esempi = []
    for r, (t, _, _) in zip(righe, esiti):
        if str(t).startswith("__ERRORE__"):
            continue
        d = estrai_json(t)
        if d is None:
            illeggibili += 1
            continue
        nuove, vecchie = nomi(d.get("tecnologie")), nomi(r.get("tecnologie_2b"))
        if not nuove and not vecchie:
            vuote += 1
        elif nuove == vecchie:
            uguali += 1
        elif len(nuove) > len(vecchie):
            piu += 1
            if len(esempi) < 8:
                esempi.append((r.get("title"), sorted(vecchie)[:4], sorted(nuove)[:7]))
        else:
            meno += 1
    n = max(len(righe) - len(muti), 1)
    for et, q in (("identiche al 2B", uguali), ("ne trova DI PIU'", piu),
                  ("ne trova di meno", meno), ("entrambi vuoti", vuote),
                  ("risposta illeggibile", illeggibili)):
        print(f"  {et:<22}{q:>5}  ({100*q/n:>3.0f}%)")

    print("\n  dove ne trova di piu' (2B -> modello nuovo):")
    for tit, v, nu in esempi:
        print(f"    {(tit or '?')[:38]:<40}{', '.join(v)[:28]:<30}-> {', '.join(nu)[:46]}")

    esito = a.banco.rsplit("/", 1)[0] + "/esito-banco.json"
    with open(esito, "w") as f:
        json.dump({"modello": a.modello, "guidato": guidato, "offerte": len(righe),
                   "secondi": dt, "par": a.par, "token_in": tok_in, "token_out": tok_out,
                   "offerte_ora": ora, "ore_al_giorno": 82320 / ora, "muti": len(muti),
                   "uguali": uguali, "piu": piu, "meno": meno,
                   "vuote": vuote, "illeggibili": illeggibili}, f, indent=1)
    print(f"\nesito in {esito}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
