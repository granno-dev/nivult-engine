"""Quanto rende davvero una GPU a noleggio, sui NOSTRI dati.

Risponde a due domande, e solo a quelle:
  1. quante offerte all'ora, cioe' quante ore al giorno servirebbero;
  2. le tecnologie estratte sono migliori di quelle del 2B?

Il confronto e' appaiato: le stesse 1.981 offerte che il 2B ha gia' letto, con lo
stesso prompt. Due medie su campioni diversi non si confrontano — sbagliato una
volta il 18/09, non si ripete.

Si misura il tempo SOLO della generazione: il caricamento del modello e' un costo
una-tantum e falserebbe il conto delle ore al giorno.

  python banco_gpu.py --modello Qwen/Qwen3-8B --quante 500
"""
from __future__ import annotations
import argparse
import gzip
import json
import re
import time

SISTEMA_TEC = ("Sei l'estrattore di Nivult. Leggi l'annuncio di lavoro e rispondi SOLO con un JSON "
               "con le chiavi: tecnologie. tecnologie = lista di {nome, ruolo} con ruolo in "
               "usata|servizio|gradita. Solo le tecnologie scritte nell'annuncio, niente altro.")


def prompt(r: dict) -> str:
    return (f"Titolo: {r.get('title') or ''}\n"
            f"Sede: {r.get('location') or ''} ({r.get('country') or '-'})\n"
            f"Lingua dell'annuncio: {r.get('lang') or '?'}\n\n{r.get('text') or ''}")


def estrai_json(t: str):
    m = re.search(r"\{.*\}", t or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:                                            # noqa: BLE001
        return None


def nomi(tec) -> set[str]:
    fuori = set()
    for x in tec or []:
        if isinstance(x, dict) and isinstance(x.get("nome"), str):
            n = x["nome"].strip().lower()
            if n:
                fuori.add(n)
    return fuori


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modello", default="Qwen/Qwen3-8B")
    ap.add_argument("--banco", default="/workspace/banco-gpu.jsonl.gz")
    ap.add_argument("--quante", type=int, default=500)
    ap.add_argument("--max-nuovi", type=int, default=200)
    a = ap.parse_args()

    righe = []
    with gzip.open(a.banco, "rt") as f:
        for l in f:
            righe.append(json.loads(l))
            if len(righe) >= a.quante:
                break
    print(f"banco: {len(righe)} offerte", flush=True)

    from vllm import LLM, SamplingParams
    t0 = time.time()
    llm = LLM(model=a.modello, dtype="bfloat16", gpu_memory_utilization=0.90,
              max_model_len=4096, enforce_eager=False)
    print(f"modello caricato in {time.time()-t0:.0f}s (costo una-tantum, fuori dal conto)", flush=True)

    tok = llm.get_tokenizer()
    testi = [tok.apply_chat_template(
        [{"role": "system", "content": SISTEMA_TEC}, {"role": "user", "content": prompt(r)}],
        tokenize=False, add_generation_prompt=True) for r in righe]
    tok_in = sum(len(tok.encode(t)) for t in testi)

    par = SamplingParams(temperature=0, max_tokens=a.max_nuovi)
    t1 = time.time()
    uscite = llm.generate(testi, par)
    dt = time.time() - t1

    tok_out = sum(len(o.outputs[0].token_ids) for o in uscite)
    print(f"\n=== VELOCITA' ===")
    print(f"  {len(righe)} offerte in {dt:.1f}s")
    print(f"  token letti   {tok_in:,}  ->  {tok_in/dt:,.0f}/s")
    print(f"  token scritti {tok_out:,}  ->  {tok_out/dt:,.0f}/s")
    print(f"  {3600*len(righe)/dt:,.0f} offerte/ora")
    # il fabbisogno vero, misurato il 19/09: 47.916 sintesi + 34.404 tecnologie
    ore = (47916 + 34404) / (3600 * len(righe) / dt)
    print(f"  ore al giorno per fare TUTTO il flusso (82.320 chiamate): {ore:.1f}")

    print(f"\n=== QUALITA' DELLE TECNOLOGIE (contro il 2B, stesse offerte) ===")
    uguali = piu = meno = vuote = illeggibili = 0
    esempi = []
    for r, o in zip(righe, uscite):
        d = estrai_json(o.outputs[0].text)
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
            if len(esempi) < 6:
                esempi.append((r["title"], sorted(vecchie)[:5], sorted(nuove)[:6]))
        else:
            meno += 1
    n = len(righe)
    print(f"  identiche al 2B      {uguali:>5}  ({100*uguali/n:.0f}%)")
    print(f"  ne trova DI PIU'     {piu:>5}  ({100*piu/n:.0f}%)")
    print(f"  ne trova di meno     {meno:>5}  ({100*meno/n:.0f}%)")
    print(f"  entrambi vuoti       {vuote:>5}  ({100*vuote/n:.0f}%)")
    print(f"  risposta illeggibile {illeggibili:>5}  ({100*illeggibili/n:.0f}%)")
    print("\n  dove ne trova di piu':")
    for tit, v, nu in esempi:
        print(f"    {(tit or '?')[:44]:<46}2B: {', '.join(v)[:34]:<36}-> {', '.join(nu)[:44]}")

    with open("/workspace/esito-banco.json", "w") as f:
        json.dump({"modello": a.modello, "offerte": n, "secondi": dt,
                   "token_in": tok_in, "token_out": tok_out,
                   "offerte_ora": 3600 * n / dt, "ore_al_giorno": ore,
                   "uguali": uguali, "piu": piu, "meno": meno,
                   "vuote": vuote, "illeggibili": illeggibili}, f, indent=1)
    print("\nesito in /workspace/esito-banco.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
