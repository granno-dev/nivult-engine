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
from estrai_dataset_v2 import _RX_MENZIONE  # noqa: E402

FAMIGLIE = ['Administrative', 'Agriculture', 'Art & Design', 'Construction', 'Consulting', 'Creative & Media',
            'Customer Service & Support', 'Data & Analytics', 'Education', 'Energy', 'Engineering',
            'Environmental & Sustainability', 'Finance & Accounting', 'Food & Beverage', 'Government & Public Sector',
            'Healthcare', 'Hospitality', 'Human Resources', 'Legal', 'Logistics', 'Management & Leadership',
            'Manufacturing', 'Marketing', 'Retail', 'Sales', 'Science & Research', 'Security & Safety',
            'Social Services', 'Software', 'Sports & Recreation', 'Technology', 'Trades', 'Transportation', 'none']
SENIORITY = ["intern", "junior", "mid", "senior", "lead", "head"]
CONTRATTO = ["full_time", "part_time", "contract", "temporary", "internship", "apprenticeship"]
REMOTO = ["remote", "hybrid", "onsite"]

SISTEMA = (
    "Sei nivult, il classificatore di annunci di lavoro di Nivult. Leggi l'annuncio e rispondi SOLO con un JSON con i campi richiesti.\n"
    "family: la famiglia del RUOLO (non del settore dell'azienda), una di: " + ", ".join(FAMIGLIE) + ". "
    "\"none\" se non e' un annuncio di lavoro (candidatura spontanea, talent pool, pagina di prova).\n"
    "seniority: " + ", ".join(SENIORITY) + ". employment_type: " + ", ".join(CONTRATTO) + ". remote: " + ", ".join(REMOTO) + ".\n"
    "languages_required: codici ISO-639-1 delle lingue richieste esplicitamente (lista vuota se nessuna).\n"
    "Per seniority, employment_type e remote aggiungi \"<campo>_stimato\": true quando il testo non lo dichiara e lo stai deducendo dal ruolo, "
    "dal titolo, dall'azienda o dalla sede; false quando il testo lo dice."
)


def utente(r: dict, campi: list[str], testo: str) -> str:
    return (f"Campi richiesti: {', '.join(campi)}\n\n"
            f"Titolo: {r.get('title') or ''}\nSede: {r.get('location') or ''} ({r.get('country') or '-'})\n"
            f"Azienda: {(r.get('azienda') or '').split('/')[-1]}\n\n{testo}")


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


def riga_sft(r: dict, testo: str, mascherato: bool, rnd: random.Random) -> dict | None:
    campi, risposta = [], {}
    if r.get("family"):
        campi.append("family")
        risposta["family"] = r["family"]
    for campo in ("seniority", "employment_type", "remote"):
        v = r.get(campo)
        if not v:
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


def converti(src: str, dst: str, quota_maschera: float, rnd: random.Random, max_testo: int) -> dict:
    st = collections.Counter()
    h = hashlib.sha1()
    with gzip.open(src, "rt") as f, gzip.open(dst, "wt") as out:
        for l in f:
            r = json.loads(l)
            testo = (r.get("text") or "")[:max_testo]
            if r.get("family_prov") == "glm_senza_audit":
                # GLM senza il controllo di v1: si tiene solo se conferma un campo dichiarato
                # (la famiglia resta, ma pesa meno: e' la parte piu' sporca del dataset)
                st["glm_senza_audit"] += 1
            x = riga_sft(r, testo, False, rnd)
            if x:
                s = json.dumps(x, ensure_ascii=False)
                out.write(s + "\n")
                h.update(s.encode()[:400])
                st["righe"] += 1
                for c in x["campi"]:
                    st[f"campo:{c}"] += 1
                    if c != "family" and c != "languages_required" and x["messages"][2]["content"].find(f'"{c}_stimato": true') >= 0:
                        st[f"stima:{c}"] += 1
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
                            x2 = riga_sft(r2, t2, True, rnd)
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
    ap.add_argument("--max-testo", type=int, default=1500)
    ap.add_argument("--seme", type=int, default=7)
    ap.add_argument("--giudicati", default=None, help="giudicati.jsonl dal giudice su GPU: le famiglie decise entrano nel train")
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
        rapporto[nome] = converti(src, dst, a.maschera if nome == "train" else 0.0, rnd, a.max_testo)
        print(nome, json.dumps(rapporto[nome], indent=1), flush=True)
    json.dump(rapporto, open(os.path.join(a.out, "rapporto-sft.json"), "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
