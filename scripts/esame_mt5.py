"""Esame del modello ENCODER-DECODER della sintesi sulle 280 righe a mano del golden.

Stessa misura di esame_sintesi.py (che pero' serve i modelli decoder-only): qui il
modello legge l'annuncio con l'encoder e scrive col decoder, quindi niente modello
di conversazione e niente taglio del prompt dall'uscita.

Confronta con la sintesi di DeepSeek (il maestro) su quattro misure, non solo il
vocabolario in comune che da solo e' ingannevole (premia chi ricopia):
  - vocabolario in comune, per continuita' coi numeri precedenti (270M 54%, 2B 64%)
  - LINGUA: la sintesi e' nella stessa lingua dell'annuncio?
  - FEDELTA': i numeri citati nella sintesi compaiono davvero nell'annuncio?
  - lunghezza e quante sintesi mancano del tutto

  MODELLO=/workspace/mt5/modello python esame_mt5.py --uscita esame-mt5.json
"""
from __future__ import annotations
import argparse, gzip, json, os, re, sys, time, collections
import torch
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

GOLDEN = "/opt/nivult/etichette/dataset-golden-arbitrato.jsonl.gz"
DEEPSEEK = "/opt/nivult/etichette/cancello-uscite.jsonl"
SISTEMA = ("Riassumi l'annuncio di lavoro in 40-120 parole, nella lingua dell'annuncio. "
           "Tre parti in un solo paragrafo: ruolo e mansioni, requisiti, condizioni. "
           "Solo fatti presenti nel testo, nessun giudizio e nessuna aggiunta.")
PAROLE_LINGUA = {
    "it": (" di ", " che ", " per ", " con ", " sono "), "en": (" the ", " and ", " for ", " with ", " you "),
    "de": (" und ", " der ", " die ", " mit ", " für "), "fr": (" et ", " le ", " la ", " pour ", " avec "),
    "es": (" el ", " la ", " para ", " con ", " que "), "nl": (" de ", " het ", " en ", " voor ", " met "),
    "pt": (" de ", " para ", " com ", " que ", " uma "), "sv": (" och ", " att ", " för ", " med ", " som "),
    "pl": (" i ", " w ", " na ", " z ", " do "), "no": (" og ", " for ", " med ", " som ", " til "),
    "da": (" og ", " for ", " med ", " som ", " til "), "fi": (" ja ", " on ", " sekä ", " tai ", " että "),
}
def lingua_di(t):
    b = " " + re.sub(r"\s+", " ", (t or "").lower()) + " "
    p = {k: sum(b.count(w) for w in ws) for k, ws in PAROLE_LINGUA.items()}
    k = max(p, key=p.get)
    return k if p[k] >= 3 else None

def numeri(t): return set(re.findall(r"\d[\d.,]{1,}", t or ""))

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uscita", default="/workspace/esame-sintesi.json")
    ap.add_argument("--max-nuovi", type=int, default=220)
    ap.add_argument("--bs", type=int, default=8)
    a = ap.parse_args()
    base = os.environ["MODELLO"]
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    # niente padding a sinistra: qui il prompt sta nell'encoder, non nel decoder
    tok = AutoTokenizer.from_pretrained(base)
    model = AutoModelForSeq2SeqLM.from_pretrained(base, dtype=torch.bfloat16).to(dev); model.eval()
    # XLSum si porta dietro num_beams=4 e no_repeat_ngram_size=2 dal riassunto di
    # notizie. Il secondo fa danno qui: un annuncio ripete «esperienza in», «anni di»
    # e vietarlo costringe il modello a girare intorno. Si generano come in produzione.
    model.generation_config.no_repeat_ngram_size = 0
    model.generation_config.num_beams = 1
    model.generation_config.length_penalty = 1.0
    model.generation_config.max_length = a.max_nuovi
    ds = {}
    for l in open(DEEPSEEK):
        u = json.loads(l)
        if isinstance(u.get("uscita"), dict) and u["uscita"].get("sintesi"): ds[u["id"]] = u["uscita"]["sintesi"]
    righe = [json.loads(l) for l in gzip.open(GOLDEN, "rt")]
    righe = [r for r in righe if r.get("fonte") == "mano" and r["id"] in ds]
    print(f"modello {base} | righe da esaminare: {len(righe)}", flush=True)
    usc = []; t0 = time.time()
    for i in range(0, len(righe), a.bs):
        lotto = righe[i:i+a.bs]
        prompt = []
        for r in lotto:
            # ESATTAMENTE l'ingresso su cui e' stato addestrato: nient'altro.
            prompt.append(f"Titolo: {r.get('title') or ''}\nSede: {r.get('location') or ''} "
                          f"({r.get('country') or '-'})\n"
                          f"Lingua dell'annuncio: {r.get('lang') or lingua_di(r.get('text')) or '?'}\n\n"
                          f"{(r.get('text') or '')[:6000]}")
        enc = tok(prompt, return_tensors="pt", padding=True, truncation=True, max_length=1536).to(dev)
        with torch.no_grad():
            out = model.generate(**enc, max_new_tokens=a.max_nuovi, do_sample=False, num_beams=1)
        for r, o in zip(lotto, out):
            usc.append({"id": r["id"], "sintesi": tok.decode(o, skip_special_tokens=True).strip()})
        if (i // a.bs) % 5 == 0: print(f"  {min(i+a.bs, len(righe))}/{len(righe)} in {time.time()-t0:.0f}s", flush=True)
    c = collections.Counter(); voc = 0; nvoc = 0; lung = []
    for r, u in zip(righe, usc):
        s = u["sintesi"]; d = ds[r["id"]]; testo = r.get("text") or ""
        if not s or len(s.split()) < 20: c["mancanti o troppo corte"] += 1; continue
        lung.append(len(s.split()))
        lm, la = lingua_di(s), lingua_di(testo)
        if la and lm == la: c["lingua giusta"] += 1
        elif la: c["lingua sbagliata"] += 1
        n_s, n_t = numeri(s), numeri(testo)
        if n_s: c["con numeri"] += 1; c["numeri tutti nell'annuncio"] += (1 if n_s <= n_t else 0)
        ws, wd = {w.lower() for w in s.split()}, {w.lower() for w in d.split()}
        voc += len(ws & wd) / max(len(wd), 1); nvoc += 1
    n = len(usc)
    rap = {"modello": base, "righe": n, "secondi": round(time.time()-t0),
           "vocabolario_in_comune": round(100*voc/max(nvoc,1), 1),
           "lingua_giusta_pc": round(100*c["lingua giusta"]/max(c["lingua giusta"]+c["lingua sbagliata"],1), 1),
           "numeri_fedeli_pc": round(100*c["numeri tutti nell'annuncio"]/max(c["con numeri"],1), 1),
           "parole_mediana": sorted(lung)[len(lung)//2] if lung else None,
           "mancanti": c["mancanti o troppo corte"]}
    json.dump({"rapporto": rap, "uscite": usc}, open(a.uscita, "w"), ensure_ascii=False, indent=1)
    print(json.dumps(rap, ensure_ascii=False, indent=1))
    for u in usc[:3]: print("•", u["sintesi"][:220].replace("\n", " "))
    return 0

if __name__ == "__main__":
    sys.exit(main())
