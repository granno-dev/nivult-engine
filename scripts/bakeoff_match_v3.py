#!/usr/bin/env python3
"""Bake-off zero-shot di matching dense e BM25 sulle viste full, role e skills."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import resource
import sys
import time
from collections import Counter
from pathlib import Path

from metriche_match_v2 import carica_banco, carica_banco_v3, metriche_ranking
from nivult.matching.retrieval import BM25
from nivult.matching.retrieval import documento_ruolo as _documento_ruolo
from nivult.matching.retrieval import fatti_profilo as _fatti_profilo

ROOT = Path(__file__).resolve().parents[1]
VISTE = ("full", "role", "skills")
MODEL_ID = {
    "bge-m3": "BAAI/bge-m3",
    "e5": "intfloat/multilingual-e5-large-instruct",
    "qwen3": "Qwen/Qwen3-Embedding-0.6B",
    "jobbert-v3": "TechWolf/JobBERT-v3",
    "bm25": None,
}
DEFAULT_MAX_LENGTH = {"bge-m3": 1024, "e5": 512, "qwen3": 512, "jobbert-v3": 64}
TETTO_MAX_LENGTH = {"bge-m3": 1024, "e5": 512, "jobbert-v3": 64}
QWEN_QUERY_PROMPT = (
    "Instruct: Given a candidate profile, retrieve relevant job offers\nQuery: "
)


def _intero_positivo(valore: str) -> int:
    numero = int(valore)
    if numero <= 0:
        raise argparse.ArgumentTypeError("deve essere un intero positivo")
    return numero


def _limita_per_profilo(righe: list[dict], limite: int | None) -> list[dict]:
    if limite is None:
        return righe
    selezionati = set()
    for profilo in sorted({r["profile_key"] for r in righe}):
        gruppo = [r for r in righe if r["profile_key"] == profilo]
        if len(gruppo) <= limite:
            selezionati.update(r["pair_id"] for r in gruppo)
            continue
        per_split = {}
        for r in gruppo:
            per_split.setdefault(r["split"], []).append(r)
        for candidati in per_split.values():
            candidati.sort(key=lambda r: (
                hashlib.sha256(r["pair_id"].encode("utf-8")).hexdigest(), r["pair_id"]
            ))
        posizioni = dict.fromkeys(per_split, 0)
        scelti = []
        while len(scelti) < limite:
            aggiunto = False
            for split in sorted(per_split):
                posizione = posizioni[split]
                if posizione < len(per_split[split]):
                    scelti.append(per_split[split][posizione]["pair_id"])
                    posizioni[split] += 1
                    aggiunto = True
                    if len(scelti) == limite:
                        break
            if not aggiunto:
                break
        selezionati.update(scelti)
    return [r for r in righe if r["pair_id"] in selezionati]


def _documento_ridotto(r: dict, vista: str) -> str:
    if vista == "role":
        return _documento_ruolo(r)
    pezzi = [f"titolo: {r.get('title') or ''}"]
    if r.get("ai_key_skills"):
        pezzi.append(f"competenze: {', '.join(r['ai_key_skills'])}")
    if r.get("ai_requirements_summary"):
        pezzi.append(f"requisiti: {r['ai_requirements_summary']}")
    return "\n".join(pezzi)


def _testi_vista(r: dict, vista: str) -> tuple[str, str]:
    if vista == "full":
        return r["profilo"], r["offerta"]
    if vista == "role":
        query = _fatti_profilo(r["profilo"], ("Ruolo cercato", "Seniority", "Note"))
    else:
        competenze = _fatti_profilo(r["profilo"], ("Competenza",))
        query = "\n".join(x for x in (competenze, (r.get("_wants") or "").strip()) if x)
    return query, _documento_ridotto(r, vista)


def _costruisci_viste(righe: list[dict]) -> dict[str, dict[str, dict[str, str]]]:
    viste = {}
    for vista in VISTE:
        query = {}
        documenti = {}
        for r in righe:
            testo_query, documento = _testi_vista(r, vista)
            profilo = r["profile_key"]
            if profilo in query and query[profilo] != testo_query:
                raise ValueError(f"query non deterministica per {profilo}, vista {vista}")
            query[profilo] = testo_query
            documenti[r["pair_id"]] = documento
        viste[vista] = {"query": query, "documenti": documenti}
    return viste


def _gruppi_profilo(righe: list[dict]) -> dict[str, list[dict]]:
    gruppi = {}
    for r in righe:
        gruppi.setdefault(r["profile_key"], []).append(r)
    return {profilo: gruppi[profilo] for profilo in sorted(gruppi)}


def _indici_bm25(
    righe: list[dict], viste: dict[str, dict[str, dict[str, str]]]
) -> dict[tuple[str, str], BM25]:
    indici = {}
    for profilo, gruppo in _gruppi_profilo(righe).items():
        for vista in VISTE:
            testi = viste[vista]
            documenti = {r["pair_id"]: testi["documenti"][r["pair_id"]] for r in gruppo}
            indici[vista, profilo] = BM25(documenti)
    return indici


def _scores_bm25(
    righe: list[dict],
    viste: dict[str, dict[str, dict[str, str]]],
    indici: dict[tuple[str, str], BM25],
) -> dict[str, dict[str, float]]:
    scores = {vista: {} for vista in VISTE}
    for profilo, gruppo in _gruppi_profilo(righe).items():
        for vista in VISTE:
            punteggi = indici[vista, profilo].punteggi(viste[vista]["query"][profilo])
            scores[vista].update({r["pair_id"]: float(punteggi[r["pair_id"]]) for r in gruppo})
    return {vista: dict(sorted(valori.items())) for vista, valori in scores.items()}


def _encode_standard(
    modello,
    testi: list[str],
    *,
    nome_modello: str,
    query: bool,
    batch_size: int,
    device: str,
):
    if nome_modello == "e5":
        prefisso = "query: " if query else "passage: "
        testi = [prefisso + testo for testo in testi]
    opzioni = {
        "batch_size": batch_size,
        "convert_to_numpy": True,
        "device": device,
        "normalize_embeddings": True,
        "show_progress_bar": False,
    }
    if nome_modello == "qwen3" and query:
        prompts = getattr(modello, "prompts", {}) or {}
        if "query" in prompts:
            opzioni["prompt_name"] = "query"
        else:
            opzioni["prompt"] = QWEN_QUERY_PROMPT
    return modello.encode(testi, **opzioni)


def _encode_jobbert(modello, testi: list[str], batch_size: int):
    import numpy as np
    import torch
    from sentence_transformers.util import batch_to_device

    blocchi = []
    for inizio in range(0, len(testi), batch_size):
        features = modello.tokenize(testi[inizio:inizio + batch_size])
        features = batch_to_device(features, modello.device)
        features["text_keys"] = ["anchor"]
        with torch.inference_mode():
            output = modello.forward(features)
        blocchi.append(output["sentence_embedding"].detach().cpu().numpy())
    return np.concatenate(blocchi) if blocchi else np.empty((0, 0), dtype=np.float32)


def _cosine_scores(
    righe: list[dict],
    query_ids: list[str],
    documento_ids: list[str],
    query_embeddings,
    documento_embeddings,
) -> dict[str, float]:
    import numpy as np

    query = np.asarray(query_embeddings, dtype=np.float32)
    documenti = np.asarray(documento_embeddings, dtype=np.float32)
    query = query / np.maximum(np.linalg.norm(query, axis=1, keepdims=True), 1e-12)
    documenti = documenti / np.maximum(
        np.linalg.norm(documenti, axis=1, keepdims=True), 1e-12
    )
    indici_query = {profilo: i for i, profilo in enumerate(query_ids)}
    indici_documenti = {pair_id: i for i, pair_id in enumerate(documento_ids)}
    scores = {}
    for profilo, gruppo in _gruppi_profilo(righe).items():
        colonne = [indici_documenti[r["pair_id"]] for r in gruppo]
        vettore = query[indici_query[profilo]]
        valori = documenti[colonne] @ vettore
        scores.update({r["pair_id"]: float(valore) for r, valore in zip(gruppo, valori)})
    return dict(sorted(scores.items()))


def _sincronizza(torch, device: str) -> None:
    if device == "cuda":
        torch.cuda.synchronize()


def _scores_dense(
    modello,
    nome_modello: str,
    righe: list[dict],
    viste: dict[str, dict[str, dict[str, str]]],
    batch_size: int,
    device: str,
    torch,
) -> tuple[dict[str, dict[str, float]], float, float]:
    scores = {}
    tempo_encoding = 0.0
    tempo_inferenza = 0.0
    for vista in VISTE:
        testi = viste[vista]
        query_ids = sorted(testi["query"])
        documento_ids = sorted(testi["documenti"])
        query = [testi["query"][profilo] for profilo in query_ids]
        documenti = [testi["documenti"][pair_id] for pair_id in documento_ids]
        inizio = time.perf_counter()
        if nome_modello == "jobbert-v3":
            query_embeddings = _encode_jobbert(modello, query, batch_size)
            documento_embeddings = _encode_jobbert(modello, documenti, batch_size)
        else:
            query_embeddings = _encode_standard(
                modello, query, nome_modello=nome_modello, query=True,
                batch_size=batch_size, device=device
            )
            documento_embeddings = _encode_standard(
                modello, documenti, nome_modello=nome_modello, query=False,
                batch_size=batch_size, device=device
            )
        _sincronizza(torch, device)
        tempo_encoding += time.perf_counter() - inizio
        inizio = time.perf_counter()
        scores[vista] = _cosine_scores(
            righe, query_ids, documento_ids, query_embeddings, documento_embeddings
        )
        tempo_inferenza += time.perf_counter() - inizio
    return scores, tempo_encoding, tempo_inferenza


def _memoria_ram() -> dict[str, int | None]:
    valori = {"ram_rss_bytes": None, "ram_total_bytes": None, "ram_available_bytes": None}
    try:
        for linea in Path("/proc/self/status").read_text().splitlines():
            if linea.startswith("VmRSS:"):
                valori["ram_rss_bytes"] = int(linea.split()[1]) * 1024
                break
        for linea in Path("/proc/meminfo").read_text().splitlines():
            if linea.startswith("MemTotal:"):
                valori["ram_total_bytes"] = int(linea.split()[1]) * 1024
            elif linea.startswith("MemAvailable:"):
                valori["ram_available_bytes"] = int(linea.split()[1]) * 1024
    except OSError:
        pass
    picco = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    valori["ram_peak_rss_bytes"] = int(picco) * (1 if sys.platform == "darwin" else 1024)
    if valori["ram_rss_bytes"] is None:
        valori["ram_rss_bytes"] = valori["ram_peak_rss_bytes"]
    return valori


def _memoria_gpu(torch, device: str) -> dict[str, int | str] | None:
    if torch is None or device != "cuda" or not torch.cuda.is_available():
        return None
    proprieta = torch.cuda.get_device_properties(device)
    return {
        "name": proprieta.name,
        "total_bytes": proprieta.total_memory,
        "allocated_bytes": torch.cuda.memory_allocated(device),
        "peak_allocated_bytes": torch.cuda.max_memory_allocated(device),
        "reserved_bytes": torch.cuda.memory_reserved(device),
        "peak_reserved_bytes": torch.cuda.max_memory_reserved(device),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, choices=list(MODEL_ID))
    ap.add_argument("--dataset", choices=("v2", "v3"), default="v2")
    ap.add_argument("--out", required=True, help="rapporto JSON di destinazione")
    ap.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    ap.add_argument("--batch-size", type=_intero_positivo, default=8)
    ap.add_argument("--max-length", type=_intero_positivo)
    ap.add_argument("--limit-per-profilo", type=_intero_positivo)
    args = ap.parse_args()
    inizio_totale = time.perf_counter()

    max_length = args.max_length or DEFAULT_MAX_LENGTH.get(args.model)
    tetto = TETTO_MAX_LENGTH.get(args.model)
    if max_length is not None and tetto is not None and max_length > tetto:
        ap.error(f"--max-length per {args.model} deve essere al massimo {tetto}")

    carica = carica_banco_v3 if args.dataset == "v3" else carica_banco
    righe = _limita_per_profilo(carica(ROOT), args.limit_per_profilo)
    if not righe:
        raise SystemExit("il banco selezionato è vuoto")
    viste = _costruisci_viste(righe)
    torch = None
    modello = None
    tempo_caricamento = 0.0
    if args.model == "bm25":
        device = "cpu"
        inizio = time.perf_counter()
        indici = _indici_bm25(righe, viste)
        tempo_encoding = time.perf_counter() - inizio
        inizio = time.perf_counter()
        scores = _scores_bm25(righe, viste, indici)
        tempo_inferenza = time.perf_counter() - inizio
    else:
        import torch

        if args.device == "cuda" and not torch.cuda.is_available():
            raise SystemExit("--device cuda richiesto, ma CUDA non è disponibile")
        device = "cuda" if args.device == "cuda" or (
            args.device == "auto" and torch.cuda.is_available()
        ) else "cpu"
        if device == "cuda":
            torch.cuda.reset_peak_memory_stats(device)
        inizio = time.perf_counter()
        from sentence_transformers import SentenceTransformer

        modello = SentenceTransformer(MODEL_ID[args.model], device=device,
                                      local_files_only=True)
        modello.max_seq_length = max_length
        modello.eval()
        _sincronizza(torch, device)
        tempo_caricamento = time.perf_counter() - inizio
        scores, tempo_encoding, tempo_inferenza = _scores_dense(
            modello, args.model, righe, viste, args.batch_size, device, torch
        )

    if any(not math.isfinite(valore) for per_vista in scores.values()
           for valore in per_vista.values()):
        raise SystemExit("sono stati prodotti punteggi non finiti")
    ranking = {
        vista: {
            split: metriche_ranking(
                [r for r in righe if r["split"] == split], scores[vista]
            )
            for split in ("validation", "test")
        }
        for vista in VISTE
    }
    memoria = _memoria_ram()
    memoria["gpu"] = _memoria_gpu(torch, device)
    rapporto = {
        "schema_version": "match-v3-bakeoff-1",
        "model": args.model,
        "model_id": MODEL_ID[args.model],
        "dataset": args.dataset,
        "device": device,
        "max_length": max_length,
        "batch_size": args.batch_size,
        "limit_per_profilo": args.limit_per_profilo,
        "rows": len(righe),
        "rows_per_split": dict(sorted(Counter(r["split"] for r in righe).items())),
        "timings_seconds": {
            "model_load": tempo_caricamento,
            "encoding": tempo_encoding,
            "inference": tempo_inferenza,
            "total": time.perf_counter() - inizio_totale,
        },
        "memory": memoria,
        "scores": scores,
        "ranking": ranking,
    }
    testo = json.dumps(rapporto, ensure_ascii=False, indent=2, allow_nan=False)
    Path(args.out).write_text(testo + "\n")
    print(f"Rapporto scritto in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
