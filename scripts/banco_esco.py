"""banco_esco: banco di misura (NON pipeline) per la classificazione ESCO.

Prende 30 titoli di lavoro reali multilingua e li avvicina alle 3.039
occupazioni ESCO v1.2.1 per similarita' di embedding: il titolo giusto
dovrebbe stare nel top-5. E' il mattone 2 del piano ESCO/ISCO: prima di
costruire una pipeline si misura se la similarita' embedding basta.

Modello: mmBERT-base (jhu-clsp/mmBERT-base), la stessa base di nivult-v1
(scripts/addestra_v1.py) — sul N5 e' gia' in cache HuggingFace, non si
scarica niente. Pooling: media mascherata dei token, IDENTICA a quella
della rete v1 (modello_v1.py:_Rete), poi normalizzazione e coseno.

Come si abbina: ogni occupazione porta piu' vettori — le label preferred
delle 17 lingue piu' le alternative (tetto --max-alt per lingua) — e il
suo punteggio e' il MASSIMO sui suoi vettori. I nomi che la gente usa
davvero («magazziniere», «nurse») stanno nelle alt labels, non nella
preferred.

Non gira nel .venv del repo: manca torch (24/09/2026, e per regola non
si installa niente). Gira sul N5, dove torch+transformers ci sono gia'
(ci addestra v1) e mmBERT-base e' in cache:

  rsync -a dati/esco/occupazioni.jsonl N5:/opt/nivult/dati/esco/
  python3 scripts/banco_esco.py            # sul N5, dal root del repo

Gli embedding delle 3.039 occupazioni si calcolano una volta e si
cachano in dati/esco/cache-embedding.pt: i rilanci sono immediati.

Uso:
  python3 scripts/banco_esco.py [--solo-preferred] [--max-alt 6]
                                [--topk 5] [--out dati/esco/banco-risultati.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATI = os.path.join(REPO, "dati", "esco")
BASE = "jhu-clsp/mmBERT-base"

# ── i 30 titoli del banco ────────────────────────────────────────────
# 10 IT, 10 da famiglie non-IT, 10 in altre lingue. `atteso` e' il codice
# ISCO-08 che un umano si aspetta: serve a leggere i risultati, non al
# modello (non entra mai nell'embedding).
TITOLI = [
    # IT (10)
    {"titolo": "Sviluppatore Java senior", "gruppo": "IT", "atteso": "2512"},
    {"titolo": "DevOps Engineer", "gruppo": "IT", "atteso": "2512"},
    {"titolo": "Data Scientist", "gruppo": "IT", "atteso": "2511"},
    {"titolo": "Frontend Developer React", "gruppo": "IT", "atteso": "2513"},
    {"titolo": "Cybersecurity Analyst", "gruppo": "IT", "atteso": "2529"},
    {"titolo": "System administrator", "gruppo": "IT", "atteso": "2522"},
    {"titolo": "Machine Learning Engineer", "gruppo": "IT", "atteso": "2511"},
    {"titolo": "QA test automation engineer", "gruppo": "IT", "atteso": "2519"},
    {"titolo": "Database administrator", "gruppo": "IT", "atteso": "2521"},
    {"titolo": "IT helpdesk technician", "gruppo": "IT", "atteso": "3512"},
    # famiglie non-IT (10)
    {"titolo": "Infermiere di reparto ospedaliero", "gruppo": "non-IT", "atteso": "2221"},
    {"titolo": "Magazziniere con muletto", "gruppo": "non-IT", "atteso": "9333"},
    {"titolo": "Autista di camion patente C", "gruppo": "non-IT", "atteso": "8332"},
    {"titolo": "Elettricista industriale", "gruppo": "non-IT", "atteso": "7411"},
    {"titolo": "Cuoco di ristorante", "gruppo": "non-IT", "atteso": "5120"},
    {"titolo": "Commessa di negozio abbigliamento", "gruppo": "non-IT", "atteso": "5223"},
    {"titolo": "Insegnante di scuola primaria", "gruppo": "non-IT", "atteso": "2341"},
    {"titolo": "Contabile amministrativo", "gruppo": "non-IT", "atteso": "4311"},
    {"titolo": "Idraulico", "gruppo": "non-IT", "atteso": "7126"},
    {"titolo": "Cameriere di sala", "gruppo": "non-IT", "atteso": "5131"},
    # altre lingue (10)
    {"titolo": "Krankenpfleger", "gruppo": "lingue", "atteso": "2221"},
    {"titolo": "Développeur full-stack", "gruppo": "lingue", "atteso": "2512"},
    {"titolo": "Camarero de barra", "gruppo": "lingue", "atteso": "5132"},
    {"titolo": "Lagerarbetare", "gruppo": "lingue", "atteso": "9333"},
    {"titolo": "Elektriker", "gruppo": "lingue", "atteso": "7411"},
    {"titolo": "Magazynier", "gruppo": "lingue", "atteso": "9333"},
    {"titolo": "Sairaanhoitaja", "gruppo": "lingue", "atteso": "2221"},
    {"titolo": "Verpleegkundige", "gruppo": "lingue", "atteso": "2221"},
    {"titolo": "Asistent medical", "gruppo": "lingue", "atteso": "2221"},
    {"titolo": "Νοσηλευτής", "gruppo": "lingue", "atteso": "2221"},
]


def carica_indice(max_alt: int, solo_preferred: bool) -> tuple[list[str], list[dict]]:
    """Da occupazioni.jsonl a (testi, indice): ogni voce d'indice e'
    (occupazione, lingua, tipo) del testo corrispondente."""
    testi: list[str] = []
    indice: list[dict] = []
    occ: list[dict] = []
    with open(os.path.join(DATI, "occupazioni.jsonl"), encoding="utf-8") as f:
        for riga in f:
            occ.append(json.loads(riga))
    for i, r in enumerate(occ):
        for lang, lab in r["preferred"].items():
            testi.append(lab)
            indice.append({"occ": i, "lang": lang, "tipo": "preferred"})
        if not solo_preferred:
            for lang, labs in r["alt"].items():
                for lab in labs[:max_alt]:
                    testi.append(lab)
                    indice.append({"occ": i, "lang": lang, "tipo": "alt"})
    return occ, testi, indice


def embed(testi, tok, modello, device, batch=128, max_len=48):
    """Media mascherata dei token (come _Rete di modello_v1) + L2.
    Le label sono corte: 48 token bastano e tengono il banco veloce."""
    import torch
    import torch.nn.functional as F
    fuori = []
    for i in range(0, len(testi), batch):
        enc = tok(testi[i:i + batch], truncation=True, max_length=max_len,
                  padding=True, return_tensors="pt").to(device)
        with torch.no_grad():
            h = modello(**enc).last_hidden_state
        m = enc["attention_mask"].unsqueeze(-1).to(h.dtype)
        pooled = (h * m).sum(1) / m.sum(1).clamp(min=1)
        fuori.append(F.normalize(pooled.float(), dim=-1).cpu())
        if (i // batch) % 50 == 0:
            print(f"  embedding: {i + len(testi[i:i + batch])}/{len(testi)}", flush=True)
    return torch.cat(fuori)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--solo-preferred", action="store_true",
                    help="ignora le alt labels (ablation)")
    ap.add_argument("--max-alt", type=int, default=6,
                    help="alt labels per lingua per occupazione (default 6)")
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--out", default=os.path.join(DATI, "banco-risultati.json"))
    ap.add_argument("--no-cache", action="store_true")
    a = ap.parse_args()

    try:
        import torch
        from transformers import AutoModel, AutoTokenizer
    except ImportError:
        sys.exit("torch/transformers mancano: questo script gira sul N5, "
                 "non nel .venv del repo. Vedi il docstring.")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"carico {BASE} su {device}…", flush=True)
    tok = AutoTokenizer.from_pretrained(BASE)
    modello = AutoModel.from_pretrained(BASE).to(device).eval()
    if device == "cpu":
        torch.set_num_threads(os.cpu_count() or 4)

    occ, testi, indice = carica_indice(a.max_alt, a.solo_preferred)
    print(f"{len(occ)} occupazioni, {len(testi)} label da embeddare", flush=True)

    # cache: la chiave lega modello, contenuto del jsonl e parametri —
    # cambia uno di questi e gli embedding si rifanno da soli
    h = hashlib.sha256()
    h.update(BASE.encode())
    h.update(f"{a.max_alt}|{a.solo_preferred}".encode())
    with open(os.path.join(DATI, "occupazioni.jsonl"), "rb") as f:
        for pezzo in iter(lambda: f.read(1 << 20), b""):
            h.update(pezzo)
    cache = os.path.join(DATI, f"cache-embedding-{h.hexdigest()[:12]}.pt")

    if os.path.exists(cache) and not a.no_cache:
        print(f"embedding dalla cache {os.path.basename(cache)}", flush=True)
        emb = torch.load(cache, map_location="cpu", weights_only=True)
    else:
        emb = embed(testi, tok, modello, device, a.batch)
        torch.save(emb, cache)
        print(f"cache scritta: {cache}", flush=True)

    titoli = [t["titolo"] for t in TITOLI]
    q = embed(titoli, tok, modello, device, batch=32)

    # punteggio di un'occupazione = max coseno sulle sue label
    import numpy as np
    sim = (q @ emb.T).numpy()
    occ_idx = np.array([d["occ"] for d in indice])
    risultati = []
    for ti, t in enumerate(TITOLI):
        per_occ = np.full(len(occ), -1.0, dtype=np.float32)
        np.maximum.at(per_occ, occ_idx, sim[ti])
        top = np.argsort(-per_occ)[: a.topk]
        voci = []
        for k, oi in enumerate(top):
            r = occ[int(oi)]
            # quale label ha vinto: utile per capire PERCHE' ha matchato
            mask = occ_idx == oi
            j = int(np.argmax(np.where(mask, sim[ti], -1.0)))
            voci.append({
                "rank": k + 1,
                "label_en": r["preferred"].get("en", ""),
                "isco08": r["isco08"],
                "gruppo_isco_en": r["isco_gruppi_en"]["4"],
                "famiglia_nivult": r["famiglia_nivult"],
                "coseno": round(float(per_occ[oi]), 4),
                "label_vincente": testi[j],
                "lingua_vincente": indice[j]["lang"],
                "uri": r["uri"],
            })
        atteso = t["atteso"]
        risultati.append({
            "titolo": t["titolo"], "gruppo": t["gruppo"], "atteso": atteso,
            "top": voci,
            "atteso_in_topk": any(v["isco08"] == atteso for v in voci),
            "gruppo3_in_topk": any(v["isco08"][:3] == atteso[:3] for v in voci),
        })

    # tabella leggibile + metriche grezze: il giudizio fine resta umano
    n4 = sum(r["atteso_in_topk"] for r in risultati)
    n3 = sum(r["gruppo3_in_topk"] for r in risultati)
    print(f"\n{'titolo':38} {'atteso':6} {'top1 (EN)':42} {'isco':5} {'cos':6}")
    for r in risultati:
        v = r["top"][0]
        segno = "OK " if r["atteso_in_topk"] else ("~  " if r["gruppo3_in_topk"] else "   ")
        print(f"{segno}{r['titolo'][:35]:38} {r['atteso']:6} {v['label_en'][:40]:42} "
              f"{v['isco08']:5} {v['coseno']:.3f}")
    print(f"\nunit group (4 cifre) atteso in top-{a.topk}: {n4}/30")
    print(f"minor group (3 cifre) atteso in top-{a.topk}: {n3}/30")

    with open(a.out, "w", encoding="utf-8") as f:
        json.dump({"modello": BASE, "parametri": vars(a), "risultati": risultati},
                  f, ensure_ascii=False, indent=1)
    print(f"dettaglio (top-{a.topk} con label vincenti): {a.out}")


if __name__ == "__main__":
    main()
