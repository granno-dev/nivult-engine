"""L'esame della testa tecnologie contro le 200 righe etichettate a mano.

Il numero che conta, e l'unico confrontabile con gli altri: il 2B e l'8B sono
stati misurati sulle stesse 200 righe, con lo stesso confronto morbido. Un voto
calcolato in un altro modo non si puo' mettere accanto ai loro, ed e' l'errore
che ha reso inutile l'esame di settembre (47,3% contro le etichette del 2B, cioe'
contro il maestro invece che contro la verita').

  MODELLO_TEC=/opt/nivult/gpu/tecnologie-v1 python esame_golden.py

Le 200 righe NON sono nel dataset di addestramento: lo garantisce la query del
costruttore, che le esclude per id.
"""
from __future__ import annotations
import argparse
import glob
import gzip
import importlib.util
import json
import os
import sys

import torch
from transformers import AutoTokenizer, AutoModelForTokenClassification

sp = importlib.util.spec_from_file_location("a", "/opt/nivult/engine/scripts/ancoraggio.py")
A = importlib.util.module_from_spec(sp)
sp.loader.exec_module(A)

GOLDEN = os.environ.get("GOLDEN_TEC", "/opt/nivult/golden-tec")
BANCO = os.environ.get("BANCO_TEC", "/opt/nivult/banco-gpu.jsonl.gz")


def combacia(a: set[str], b: set[str]) -> int:
    """Quante voci di a hanno una gemella in b, a meno di come sono scritte.

    Identica a quella usata per misurare 2B e 8B: se cambia qui, i voti non si
    possono piu' confrontare.
    """
    bb = [y.lower() for y in b]
    return sum(1 for x in (s.lower() for s in a) if any(x in y or y in x for y in bb))


def voci(testo: str, pred, offsets, soglia: float) -> set[str]:
    """Da BIO a nomi. L'etichetta 1 (inizio) APRE una voce nuova anche se la
    precedente non si e' chiusa: senza questo, due tecnologie vicine si
    incollano in una sola parola inesistente."""
    fuori, ini, fin = set(), None, None
    for k, (oi, of) in enumerate(offsets):
        if of <= oi:                                   # token speciale o riempimento
            if ini is not None:
                fuori.add(testo[ini:fin]); ini = None
            continue
        inizio = float(pred[k][1]) >= soglia and float(pred[k][1]) >= float(pred[k][2])
        dentro = float(pred[k][1] + pred[k][2]) >= soglia
        if inizio and ini is not None:
            fuori.add(testo[ini:fin]); ini, fin = oi, of
        elif inizio or (dentro and ini is not None):
            if ini is None:
                ini = oi
            fin = of
        elif ini is not None:
            fuori.add(testo[ini:fin]); ini = None
    if ini is not None:
        fuori.add(testo[ini:fin])
    return {s for s in (x.strip(" ,;:.()[]/\n\t") for x in fuori) if 1 < len(s) <= 40}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modello", default=os.environ.get("MODELLO_TEC", "/opt/nivult/gpu/tecnologie-v1"))
    ap.add_argument("--max-len", type=int, default=1024)
    ap.add_argument("--bs", type=int, default=4)
    ap.add_argument("--soglie", default="0.3,0.4,0.5,0.6,0.7",
                    help="la soglia si sceglie SUL GOLDEN, e poi si usa quella in produzione")
    ap.add_argument("--uscita", default="/opt/nivult/esame-golden.json")
    a = ap.parse_args()

    mano: dict[str, set[str]] = {}
    for p in sorted(glob.glob(os.path.join(GOLDEN, "etichette-*.json"))):
        for o in json.load(open(p))["offerte"]:
            mano[o["id"]] = {t["nome"] for t in o["tecnologie"]}
    if len(mano) < 100:
        print(f"ERRORE: solo {len(mano)} righe a mano trovate in {GOLDEN}"); return 2

    righe = []
    for linea in gzip.open(BANCO, "rt"):
        r = json.loads(linea)
        if r["id"] in mano:
            righe.append((r["id"], r.get("title") or "", A.pulito(r.get("text"))))
    print(f"esame su {len(righe)} righe a mano, {sum(len(v) for v in mano.values())} nomi\n")

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(a.modello)
    mod = AutoModelForTokenClassification.from_pretrained(a.modello).to(dev).eval()

    # una passata sola sul modello, poi si prova ogni soglia sulle stesse uscite
    grezze = []
    with torch.inference_mode():
        for i in range(0, len(righe), a.bs):
            lotto = righe[i:i + a.bs]
            testi = [f"{t}\n{x}"[:20000] for _, t, x in lotto]
            enc = tok(testi, truncation=True, max_length=a.max_len, padding=True,
                      return_offsets_mapping=True, return_tensors="pt")
            off = enc.pop("offset_mapping")
            pr = torch.softmax(mod(**{k: v.to(dev) for k, v in enc.items()}).logits, -1).cpu()
            for j, (jid, tit, _) in enumerate(lotto):
                grezze.append((jid, testi[j], pr[j], off[j].tolist()))
            if i % 40 == 0:
                print(f"  {i}/{len(righe)}", flush=True)

    print(f"\n{'soglia':>8}{'precisione':>13}{'richiamo':>11}{'F1':>8}{'Jaccard':>10}{'vuoti ok':>10}")
    migliore, uscite = None, {}
    for s in (float(x) for x in a.soglie.split(",")):
        P = nP = R = nR = vuoti = 0
        jac, dettaglio = [], []
        for jid, testo, pr, off in grezze:
            veri = mano[jid]
            trovati = voci(testo, pr, off, s)
            dettaglio.append({"id": jid, "trovati": sorted(trovati), "veri": sorted(veri)})
            if not veri and not trovati:
                vuoti += 1
                continue
            c, c2 = combacia(trovati, veri), combacia(veri, trovati)
            P += c; nP += len(trovati); R += c2; nR += len(veri)
            jac.append(min(c, c2) / max(len(trovati) + len(veri) - min(c, c2), 1))
        prec = 100 * P / max(nP, 1)
        ric = 100 * R / max(nR, 1)
        f1 = 2 * prec * ric / max(prec + ric, 1e-9)
        print(f"{s:>8.2f}{prec:>12.1f}%{ric:>10.1f}%{f1:>7.1f}%"
              f"{100*sum(jac)/max(len(jac),1):>9.1f}%{vuoti:>10}")
        if migliore is None or f1 > migliore[1]:
            migliore, uscite = (s, f1), {"soglia": s, "dettaglio": dettaglio}

    print(f"\nmigliore: soglia {migliore[0]:.2f}, F1 {migliore[1]:.1f}%")
    # I voti di 2B e 8B valgono SOLO sulle 200 righe del primo golden. Stampati
    # accanto a un esame fatto su altre righe sarebbero un confronto falso, ed e'
    # esattamente il modo in cui una misura inganna (visto il 20/09/2026 girando
    # questo stesso esame sulle 104 righe delle otto famiglie).
    if len(mano) == 200:
        print(f"{'':>8}{'per confronto sulle stesse 200 righe:':>50}")
        print(f"{'':>8}{'2B in produzione   precisione 93,6%  richiamo 50,3%  F1 65,4%':>62}")
        print(f"{'':>8}{'8B + filtro        precisione 58,2%  richiamo 69,5%  F1 63,4%':>62}")
    else:
        print(f"{'':>8}nessun confronto: 2B e 8B sono stati misurati su altre righe "
              f"(le 200 del primo golden), e i voti non si mettono accanto.")
    json.dump(uscite, open(a.uscita, "w"), ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
