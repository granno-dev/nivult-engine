#!/usr/bin/env python3
"""Dal dataset v2 alle coppie di addestramento «annuncio → JSON» per nivult-v2.

Il modello generativo impara con etichette PARZIALI: ogni riga porta solo i
campi che ha davvero (una francese ha famiglia da codice ROME, contratto e
seniority dichiarati; una Lever ha il remoto dichiarato e la famiglia
GLM+v1). Nel prompt si scrive quali campi si chiedono e la risposta li
contiene tutti e solo quelli: cosi' nessuna riga insegna un «unknown» che
in realta' e' solo «non dichiarato». In produzione si chiedono tutti.

La STIMA si insegna in due modi, entrambi con verita' dichiarata dal datore:
  - le righe dove il testo NON nomina il valore (menzione = False) sono
    stima per costruzione: il modello deve dedurlo da titolo, ruolo,
    azienda, sede;
  - una quota delle righe con menzione riceve una variante MASCHERATA:
    la frase che dichiara il valore viene tolta dal testo e la riga si
    duplica. Il campo `stimato` nella risposta dice al modello (e a chi
    legge) quando sta stimando.

    python scripts/formatta_v2.py --in /opt/nivult/v2 --out /opt/nivult/v2/sft [--maschera 0.25]

Scrive sft-train.jsonl.gz e sft-esame.jsonl.gz (messages in formato chat),
con `firma` (righe + hash) in rapporto-sft.json.
"""
from __future__ import annotations

import argparse
import collections
import gzip
import hashlib
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from prompt_v2 import FAMIGLIE, SENIORITY, CONTRATTO, REMOTO, SISTEMA, utente, _RX_MENZIONE  # noqa: E402,F401


def maschera(campo: str, valore: str, testo: str) -> str | None:
    """Toglie dal testo la frase che dichiara il valore (quella riconosciuta
    dalla stessa regex della menzione). None se non trova niente da togliere."""
    rx = _RX_MENZIONE.get(campo, {}).get(valore)
    if not rx or not rx.search(testo):
        return None
    # via la frase intera (dal separatore precedente al successivo) che contiene la menzione
    parti = re.split(r"(?<=[.;:!?\n])\s+|\s{2,}|\s[|•·]\s", testo)
    tenute = [p for p in parti if not rx.search(p)]
    nuovo = " ".join(tenute).strip()
    return nuovo if len(nuovo) >= 60 else None


def riga_sft(r: dict, testo: str, mascherato: bool, rnd: random.Random,
             tieni: dict | None = None) -> dict | None:
    """`tieni`: probabilita' di TENERE un campo per (campo, valore, provenienza).
    Dove manca vale 1. E' il riequilibrio: si toglie il campo dalla richiesta,
    non la riga dal dataset."""
    campi, risposta = [], {}
    if r.get("family"):
        campi.append("family")
        risposta["family"] = r["family"]
    for campo in ("seniority", "employment_type", "remote"):
        v = r.get(campo)
        if not v:
            continue
        if tieni and rnd.random() >= tieni.get((campo, v, r.get(f"{campo}_prov")), 1.0):
            continue
        campi.append(campo)
        risposta[campo] = v
        menz = r.get(f"{campo}_menzione")
        risposta[f"{campo}_stimato"] = bool(mascherato or not menz)
    if r.get("languages_required"):
        campi.append("languages_required")
        risposta["languages_required"] = sorted(set(r["languages_required"]))
    if not campi:
        return None
    return {"id": r["id"], "mascherato": mascherato, "campi": campi,
            "messages": [{"role": "system", "content": SISTEMA},
                         {"role": "user", "content": utente(r, campi, testo)},
                         {"role": "assistant", "content": json.dumps(risposta, ensure_ascii=False)}]}


# ── il riequilibrio ────────────────────────────────────────────────
# I campi dichiarati non mancano a caso: chi assume in sede non lo scrive,
# chi cerca un senior lo scrive sempre. Addestrare sulla distribuzione dei
# DICHIARATI insegna un prior sbagliato: l'esame dell'11/09 ha mostrato che
# l'84% degli errori di seniority e il 90% di quelli del contratto erano
# frecce verso la classe maggioritaria del train (senior 41,7%, full_time
# 85,6%) mentre il reale — le 280 a mano — dice 21,5% e 60,9%. Qui si porta
# la classe maggioritaria al prior delle 280 a mano sottocampionandola; per
# il remoto si fa il contrario, si AGGIUNGONO le righe «onsite per assenza»
# (estrai_dataset_v2) fino all'80,5%. Si tocca solo il lato train.
PRIOR_A_MANO = {"seniority": ("senior", 0.215), "employment_type": ("full_time", 0.609),
                "remote": ("onsite", 0.805)}


def conta_classi(src: str) -> dict:
    c: dict = collections.defaultdict(collections.Counter)
    with gzip.open(src, "rt") as f:
        for l in f:
            r = json.loads(l)
            for campo in PRIOR_A_MANO:
                v = r.get(campo)
                if v:
                    c[campo][(v, r.get(f"{campo}_prov"))] += 1
    return c


def calcola_tieni(conteggi: dict) -> dict:
    """Le probabilita' di tenere, per (campo, valore, provenienza)."""
    tieni: dict = {}
    for campo, (magg, t) in PRIOR_A_MANO.items():
        cnt = conteggi.get(campo) or {}
        tot = sum(cnt.values())
        if not tot:
            continue
        if campo == "remote":
            on_d = cnt.get(("onsite", "dichiarato"), 0)
            on_a = cnt.get(("onsite", "assenza"), 0)
            altri = tot - on_d - on_a
            voluti = t / (1 - t) * altri            # onsite totali per stare a t
            if on_d >= voluti:                      # i dichiarati bastano gia': niente assenza
                tieni[("remote", "onsite", "assenza")] = 0.0
                tieni[("remote", "onsite", "dichiarato")] = round(voluti / on_d, 4)
            else:
                tieni[("remote", "onsite", "assenza")] = round(min(1.0, (voluti - on_d) / max(on_a, 1)), 4)
            continue
        n = sum(v for (val, _), v in cnt.items() if val == magg)
        s = n / tot
        if s > t:                                   # k = t(1-s) / (s(1-t)): la quota che porta s a t
            tieni[(campo, magg, "dichiarato")] = round(t * (1 - s) / (s * (1 - t)), 4)
    return tieni


def converti(src: str, dst: str, quota_maschera: float, rnd: random.Random, max_testo: int,
             tieni_sporche: bool = False, tieni: dict | None = None) -> dict:
    st = collections.Counter()
    h = hashlib.sha1()
    with gzip.open(src, "rt") as f, gzip.open(dst, "wt") as out:
        for l in f:
            r = json.loads(l)
            testo = (r.get("text") or "")[:max_testo]
            if r.get("family_prov") == "glm_senza_audit":
                # GLM senza il controllo incrociato di v1 e' la parte piu' sporca del
                # dataset. Il 09/09/2026, col corpus cresciuto di 300k offerte e
                # l'audit fermo all'08, era salita dal 16% al 41% delle famiglie:
                # addestrare il cancello piu' alto (famiglia >= 92%) su quelle
                # etichette e' un autogol. Si toglie SOLO la famiglia; i campi
                # dichiarati dal datore restano, perche' sono verita' della fonte
                # e non dipendono da GLM. Con --tieni-glm-senza-audit tornano.
                st["glm_senza_audit"] += 1
                if not tieni_sporche:
                    r = dict(r)
                    r["family"] = None
            x = riga_sft(r, testo, False, rnd, tieni)
            if x:
                s = json.dumps(x, ensure_ascii=False)
                out.write(s + "\n")
                h.update(s.encode()[:400])
                st["righe"] += 1
                risp = json.loads(x["messages"][2]["content"])
                for c in x["campi"]:
                    st[f"campo:{c}"] += 1
                    if c != "family" and c != "languages_required" and x["messages"][2]["content"].find(f'"{c}_stimato": true') >= 0:
                        st[f"stima:{c}"] += 1
                    if c in PRIOR_A_MANO:            # la distribuzione DOPO il riequilibrio, per verificarlo
                        st[f"classe:{c}:{risp[c]}"] += 1
            # variante mascherata: solo dove il testo nomina il valore
            if quota_maschera > 0 and rnd.random() < quota_maschera:
                for campo in ("seniority", "employment_type", "remote"):
                    v = r.get(campo)
                    if v and r.get(f"{campo}_menzione"):
                        t2 = maschera(campo, v, testo)
                        if t2:
                            r2 = dict(r)
                            r2[f"{campo}_menzione"] = False
                            # nella variante mascherata si chiede SOLO il campo mascherato (+ famiglia)
                            for altro in ("seniority", "employment_type", "remote"):
                                if altro != campo:
                                    r2[altro] = None
                            r2["languages_required"] = None
                            x2 = riga_sft(r2, t2, True, rnd, tieni)
                            if x2:
                                out.write(json.dumps(x2, ensure_ascii=False) + "\n")
                                st["righe"] += 1
                                st[f"mascherate:{campo}"] += 1
                            break
    st["firma"] = f"{st['righe']}:{h.hexdigest()[:12]}"
    return dict(st)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default="/opt/nivult/v2")
    ap.add_argument("--out", default="/opt/nivult/v2/sft")
    ap.add_argument("--maschera", type=float, default=0.25)
    ap.add_argument("--max-testo", type=int, default=1000)
    ap.add_argument("--seme", type=int, default=7)
    ap.add_argument("--tieni-glm-senza-audit", action="store_true",
                    help="tiene la famiglia anche dove GLM non e' stato verificato da v1")
    ap.add_argument("--giudicati", default=None, help="giudicati.jsonl dal giudice su GPU: le famiglie decise entrano nel train")
    ap.add_argument("--senza-riequilibrio", action="store_true",
                    help="NON porta seniority/contratto/remoto al prior delle 280 a mano (solo per confronti)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    rnd = random.Random(a.seme)
    rapporto = {}
    if a.giudicati:
        n = 0
        with open(a.giudicati) as f, gzip.open(os.path.join(a.out, "sft-giudicati.jsonl.gz"), "wt") as out:
            for l in f:
                r = json.loads(l)
                if not r.get("family"):
                    continue
                x = riga_sft({"id": r["id"], "title": r.get("title"), "location": r.get("location"), "country": None,
                              "azienda": r.get("azienda"), "family": r["family"]}, (r.get("text") or "")[:a.max_testo], False, rnd)
                if x:
                    out.write(json.dumps(x, ensure_ascii=False) + "\n")
                    n += 1
        rapporto["giudicati"] = {"righe": n}
        print("giudicati:", n, flush=True)
    for nome in ("train", "esame"):
        src = os.path.join(a.src, f"dataset-{nome}-v2.jsonl.gz")
        dst = os.path.join(a.out, f"sft-{nome}.jsonl.gz")
        tieni = None
        if nome == "train" and not a.senza_riequilibrio:
            conteggi = conta_classi(src)
            tieni = calcola_tieni(conteggi)
            rapporto["riequilibrio"] = {"prima": {c: {f"{v}@{p}": n for (v, p), n in cnt.most_common()}
                                                  for c, cnt in conteggi.items()},
                                        "tieni": {f"{c}:{v}@{p}": k for (c, v, p), k in tieni.items()}}
            print("riequilibrio, probabilita' di tenere:", rapporto["riequilibrio"]["tieni"], flush=True)
        rapporto[nome] = converti(src, dst, a.maschera if nome == "train" else 0.0, rnd, a.max_testo,
                                  a.tieni_glm_senza_audit, tieni)
        print(nome, json.dumps(rapporto[nome], indent=1), flush=True)
    json.dump(rapporto, open(os.path.join(a.out, "rapporto-sft.json"), "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
