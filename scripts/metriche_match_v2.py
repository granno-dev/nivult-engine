"""Metriche condivise del banco match-v2, senza dipendenze di training."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from nivult.matching.prescore import seleziona_per_glm

ROOT = Path(__file__).resolve().parents[1]


def spearman(a: list[float], b: list[float]) -> float:
    def ranghi(valori):
        out = [0.0] * len(valori)
        ordine = sorted(range(len(valori)), key=lambda i: valori[i])
        inizio = 0
        while inizio < len(valori):
            fine = inizio + 1
            while fine < len(valori) and valori[ordine[fine]] == valori[ordine[inizio]]:
                fine += 1
            rango = (inizio + fine - 1) / 2
            for pos in range(inizio, fine):
                out[ordine[pos]] = float(rango)
            inizio = fine
        return out
    if len(a) != len(b):
        raise ValueError("Spearman: vettori di lunghezza diversa")
    ra, rb, n = ranghi(a), ranghi(b), len(a)
    if n < 2:
        return 0.0
    media = (n - 1) / 2
    num = sum((x - media) * (y - media) for x, y in zip(ra, rb))
    den = (sum((x - media) ** 2 for x in ra) * sum((y - media) ** 2 for y in rb)) ** 0.5
    return num / den if den else 0.0


def _ndcg(ordinati: list[dict], k: int) -> float:
    import math
    rilevanti = [2 if r["label"] == "buona" else 1 if r["label"] == "borderline" else 0
                 for r in ordinati[:k]]
    dcg = sum((2 ** v - 1) / math.log2(i + 2) for i, v in enumerate(rilevanti))
    ideali = sorted((2 if r["label"] == "buona" else 1 if r["label"] == "borderline" else 0
                     for r in ordinati), reverse=True)[:k]
    idcg = sum((2 ** v - 1) / math.log2(i + 2) for i, v in enumerate(ideali))
    return dcg / idcg if idcg else 0.0


def metriche_ranking(righe: list[dict], punteggi: dict[str, float],
                     tagli: tuple[int, ...] = (30, 60, 100, 300, 500)) -> dict:
    """Metriche di retrieval/ranking per query, senza il gate di calibrazione."""
    per_profilo = {}
    for profilo in sorted({r["profile_key"] for r in righe}):
        gruppo = [r for r in righe if r["profile_key"] == profilo]
        ordinati = sorted(gruppo, key=lambda r: (-punteggi[r["pair_id"]], r["pair_id"]))
        buone_totali = sum(r["label"] == "buona" for r in gruppo)
        misure = {}
        for k in tagli:
            scelti = ordinati[:k]
            buone = sum(r["label"] == "buona" for r in scelti)
            misure[f"recall@{k}"] = buone / buone_totali if buone_totali else None
            misure[f"precision@{k}"] = buone / len(scelti) if scelti else None
            misure[f"ndcg@{k}"] = _ndcg(ordinati, k) if scelti else None
        per_profilo[profilo] = misure
    complessivo = {}
    for k in tagli:
        for metrica in ("recall", "precision", "ndcg"):
            chiave = f"{metrica}@{k}"
            valori = [m[chiave] for m in per_profilo.values() if m[chiave] is not None]
            complessivo[chiave] = sum(valori) / len(valori) if valori else None
    return {"per_profilo": per_profilo, "complessivo": complessivo}


def metriche_slice(righe: list[dict], punteggi: dict[str, float]) -> dict:
    """Diagnostica per fette; i paesi multi-valore contribuiscono a ciascuno."""
    campi = {
        "per_paese": lambda r: r.get("countries") or ([r["country"]] if r.get("country") else ["unknown"]),
        "per_fonte": lambda r: [r.get("source") or "unknown"],
        "per_lingua": lambda r: [r.get("ai_job_language") or "unknown"],
        "per_seniority": lambda r: [r.get("ai_experience_level") or "unknown"],
    }
    out = {}
    for nome, valori in campi.items():
        chiavi = sorted({v for r in righe for v in valori(r)})
        out[nome] = {v: _una([r for r in righe if v in valori(r)], punteggi)
                     for v in chiavi}
    return out


def carica_banco(radice: Path = ROOT) -> list[dict]:
    from nivult.matching.llm import offerta_per_prompt, profilo_come_testo

    base = radice / "dati/match-v2"
    profili = json.loads((base / "profili.json").read_text())
    hrbp = json.loads((radice / "profilo-esempio.json").read_text())
    testi = {"hrbp": profilo_come_testo(hrbp)}
    testi.update({k: profilo_come_testo(v["profilo"]) for k, v in profili.items()})
    etichette = {}
    vecchie = radice / "dati/match-benchmark/etichette-manuali.jsonl"
    for e in map(json.loads, vecchie.read_text().splitlines()):
        etichette[f"hrbp:{e['id']}"] = e
    for nuove in sorted(base.glob("etichette-*.jsonl")):
        for e in map(json.loads, nuove.read_text().splitlines()):
            etichette[e["pair_id"]] = e
    righe = []
    for c in map(json.loads, (base / "candidati.jsonl").read_text().splitlines()):
        pair_id = c["pair_id"]
        if pair_id not in etichette:
            continue
        e = etichette[pair_id]
        if "prompt" in c:
            offerta = c["prompt"]
        else:
            offerta = offerta_per_prompt(dict(c, _wants=profili[c["profile_key"]]["wants"]))
        righe.append(dict(c, profilo=testi[c["profile_key"]], offerta=offerta,
                          score=e["score_manuale"], label=e["label"], motivo=e["motivo"]))
    return righe


def carica_banco_v3(radice: Path = ROOT) -> list[dict]:
    from nivult.matching.llm import offerta_per_prompt, profilo_come_testo

    base = radice / "dati/match-v3"
    congelato = base / "holdout.json"
    if congelato.exists():
        manifest = json.loads(congelato.read_text())
        for nome, atteso in manifest["sha256"].items():
            attuale = hashlib.sha256((base / nome).read_bytes()).hexdigest()
            if attuale != atteso:
                raise ValueError(f"holdout match-v3 modificato dopo il congelamento: {nome}")
    profili = json.loads((base / "profili.json").read_text())
    testi = {k: profilo_come_testo(v["profilo"]) for k, v in profili.items()}
    etichette = {}
    for path in sorted(base.glob("etichette-*.jsonl")):
        for e in map(json.loads, path.read_text().splitlines()):
            if e["pair_id"] in etichette:
                raise ValueError(f"etichetta duplicata: {e['pair_id']}")
            etichette[e["pair_id"]] = e
    righe = []
    for c in map(json.loads, (base / "candidati.jsonl").read_text().splitlines()):
        pair_id = c["pair_id"]
        if pair_id not in etichette:
            continue
        e = etichette[pair_id]
        offerta = offerta_per_prompt(dict(c, _wants=profili[c["profile_key"]]["wants"]))
        righe.append(dict(c, profilo=testi[c["profile_key"]], offerta=offerta,
                          score=e["score_manuale"], label=e["label"], motivo=e["motivo"]))
    if len(righe) != len(etichette):
        raise ValueError("etichette senza candidato corrispondente")
    return righe


def _una(righe: list[dict], punteggi: dict[str, float]) -> dict:
    if not righe:
        return {"n": 0, "selezionate": 0, "buone_totali": 0, "buone": 0,
                "borderline": 0, "scartate": 0, "precisione": None,
                "recall": None, "spearman": None, "mae": None}
    predetti = [punteggi[r["pair_id"]] for r in righe]
    indici = seleziona_per_glm(predetti, [70] * len(righe), max_items=30)
    scelti = [righe[i] for i in indici]
    conteggi = Counter(r["label"] for r in scelti)
    buone_totali = sum(r["label"] == "buona" for r in righe)
    mae = sum(abs(p - r["score"]) for p, r in zip(predetti, righe)) / len(righe)
    return {"n": len(righe), "selezionate": len(scelti), "buone_totali": buone_totali,
            "buone": conteggi["buona"], "borderline": conteggi["borderline"],
            "scartate": conteggi["scartata"],
            "precisione": conteggi["buona"] / len(scelti) if scelti else None,
            "recall": conteggi["buona"] / buone_totali if buone_totali else None,
            "spearman": spearman([r["score"] for r in righe], predetti), "mae": mae}


def metriche(righe: list[dict], punteggi: dict[str, float]) -> dict:
    out = {"per_profilo": {}}
    for profilo in sorted({r["profile_key"] for r in righe}):
        gruppo = [r for r in righe if r["profile_key"] == profilo]
        out["per_profilo"][profilo] = _una(gruppo, punteggi)
    gruppi = list(out["per_profilo"].values())
    totali = {k: sum(g[k] for g in gruppi) for k in
              ("n", "selezionate", "buone_totali", "buone", "borderline", "scartate")}
    out["complessivo"] = dict(totali)
    out["complessivo"].update({
        "precisione": totali["buone"] / totali["selezionate"] if totali["selezionate"] else None,
        "recall": totali["buone"] / totali["buone_totali"] if totali["buone_totali"] else None,
        "spearman": spearman([r["score"] for r in righe], [punteggi[r["pair_id"]] for r in righe]),
        "mae": sum(abs(punteggi[r["pair_id"]] - r["score"]) for r in righe) / len(righe),
    })
    return out


def cancello(baseline: dict, candidato: dict) -> tuple[bool, list[str]]:
    errori = []
    for profilo, b in baseline["per_profilo"].items():
        c = candidato["per_profilo"][profilo]
        if c["buone"] < b["buone"]:
            errori.append(f"{profilo}: buone {c['buone']} < baseline {b['buone']}")
    if candidato["complessivo"]["scartate"] >= baseline["complessivo"]["scartate"]:
        errori.append("le scartate complessive non diminuiscono")
    b_hrbp = baseline["per_profilo"].get("hrbp")
    c_hrbp = candidato["per_profilo"].get("hrbp")
    if b_hrbp and c_hrbp and c_hrbp["scartate"] > b_hrbp["scartate"]:
        errori.append("peggioramento sul test HRBP")
    return not errori, errori
