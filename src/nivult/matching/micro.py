from __future__ import annotations

import hashlib
import json
import math
import re
from itertools import pairwise
from pathlib import Path

from nivult.matching.micro_ruoli import RuoliMicro
from nivult.matching.retrieval import lingue_mancanti, tokenizza

N_FEATURES = 2 ** 14
VERSIONE = "match-micro-3"
_DENSO = 20
_INTERNSHIP = re.compile(
    r"\b(?:internship|intern\b|alternance|apprenticeship|apprentice|stage|tirocinio|"
    r"praktikum|praktika|becario|prácticas|trainee)", re.IGNORECASE)
_EUROPA = {
    "IT", "DE", "FR", "ES", "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE",
    "FI", "GR", "HU", "IE", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO",
    "SK", "SI", "SE", "GB", "UK", "CH", "NO", "IS", "LI", "MC", "SM", "VA",
    "AL", "BA", "MD", "ME", "MK", "RS", "UA", "XK",
}


def _testo(valore) -> str:
    if isinstance(valore, list):
        return " ".join(str(v) for v in valore if v)
    return str(valore or "")


def _campo(profilo: str, etichette: tuple[str, ...]) -> str:
    for riga in profilo.splitlines():
        pulita = re.sub(r"^\[\d+\]\s*", "", riga).strip()
        if any(pulita.startswith(f"{etichetta}:") for etichetta in etichette):
            return pulita.split(":", 1)[1].strip()
    return ""


def _lista(profilo: str, etichetta: str) -> list[str]:
    valori = []
    for riga in profilo.splitlines():
        pulita = re.sub(r"^\[\d+\]\s*", "", riga).strip()
        if pulita.startswith(f"{etichetta}:"):
            valori.extend(v.strip() for v in pulita.split(":", 1)[1].split(",") if v.strip())
    return valori


def _rank(valore: str) -> int:
    testo = valore.strip().lower()
    if testo in {"intern", "internship", "entry", "0-2", "junior", "entry / junior"}:
        return 1 if testo not in {"0-2", "entry / junior"} else 2
    if testo in {"mid", "2-5", "mid-level", "mid level"}:
        return 2
    if testo in {"senior", "5-10", "lead / principal"}:
        return 3
    if testo in {"lead", "head", "10+", "principal", "director", "executive"}:
        return 4
    return 0


def _ngrams(testo: str) -> list[str]:
    token = tokenizza(testo)
    return token + [f"{a}_{b}" for a, b in pairwise(token)]


def _hash(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=4).digest(),
                          "little") % N_FEATURES


def _overlap(a: set[str], b: set[str]) -> tuple[float, float]:
    if not a or not b:
        return 0.0, 0.0
    inter = len(a & b)
    return inter / len(a | b), inter / min(len(a), len(b))


def _mismatch_paese(profilo: str, offerta: dict) -> float:
    sedi = _campo(profilo, ("Sedi accettate",))
    paesi = offerta.get("countries") or ([offerta["country"]] if offerta.get("country") else [])
    if isinstance(paesi, str):
        paesi = [paesi]
    if not sedi or not paesi:
        return 0.0
    sedi_norm = {v.lower().replace(" ", "") for v in _lista(profilo, "Sedi accettate")}
    if "disponibile a trasferirsi" in profilo.lower():
        return 0.0
    nomi = {"IT": "italia", "DE": "germania", "FR": "francia", "SE": "svezia",
            "ES": "spagna", "AT": "austria", "CH": "svizzera", "NL": "olanda",
            "GB": "regno unito", "UK": "regno unito", "IE": "irlanda",
            "PT": "portogallo", "BE": "belgio", "PL": "polonia"}
    paesi_dichiarati = bool(sedi_norm & (set(nomi.values()) | {v.lower() for v in _EUROPA}))
    modalita = _testo(offerta.get("ai_work_arrangement")).lower()
    for paese in paesi:
        codice = str(paese).upper()
        if codice.lower() in sedi_norm or nomi.get(codice) in sedi_norm:
            continue
        if ("europa" in sedi_norm or "europe" in sedi_norm) and codice in _EUROPA:
            continue
        if ({"remoto", "remote"} & sedi_norm) and modalita in {"remote solely", "remote ok", "remote"}:
            continue
        return float(paesi_dichiarati)
    return 0.0


def _sparse(testo_ruolo: str, titolo: str, testo_offerta: str) -> list[tuple[int, float]]:
    conteggi: dict[int, float] = {}
    for prefisso, testo in (("r", testo_ruolo), ("t", titolo), ("q", testo_offerta)):
        for token in _ngrams(testo):
            indice = _hash(f"{prefisso}:{token}")
            conteggi[indice] = conteggi.get(indice, 0.0) + 1.0
    for ruolo in _ngrams(testo_ruolo):
        for token_titolo in _ngrams(titolo):
            indice = _hash(f"x:{ruolo}|{token_titolo}")
            conteggi[indice] = conteggi.get(indice, 0.0) + 1.0
    norma = math.sqrt(sum(v * v for v in conteggi.values())) or 1.0
    return [(indice, valore / norma) for indice, valore in sorted(conteggi.items())]


def feature_vet(profilo: str, offerta: dict, ruoli: RuoliMicro | None = None,
                ) -> tuple[list[float], list[tuple[int, float]]]:
    ruolo = _campo(profilo, ("Ruolo cercato", "Ruolo"))
    seniority = _campo(profilo, ("Seniority",))
    rank_cv = _rank(seniority)
    rank_job = _rank(_testo(offerta.get("ai_experience_level")))
    competenze = _lista(profilo, "Competenza")
    testo_offerta = " ".join((
        _testo(offerta.get("title")),
        _testo(offerta.get("ai_requirements_summary")),
        _testo(offerta.get("ai_core_responsibilities")),
    ))
    testo_ruolo = " ".join((ruolo, " ".join(competenze)))
    token_ruolo = set(_ngrams(ruolo))
    token_titolo = set(_ngrams(_testo(offerta.get("title"))))
    token_competenze = set(_ngrams(" ".join(competenze)))
    token_testo = set(_ngrams(testo_offerta))
    role_jaccard, role_cover = _overlap(token_ruolo, token_titolo)
    skill_jaccard, skill_cover = _overlap(token_competenze, token_testo)
    lingue = set(_lista(profilo, "Lingua"))
    mancanti = lingue_mancanti(testo_offerta, lingue)
    internship = bool(rank_cv >= 3 and (
        _INTERNSHIP.search(_testo(offerta.get("title")))
        or _testo(offerta.get("ai_experience_level")).lower() in {"intern", "internship"}))
    agenzia = ("non in agenzia" in profilo.lower()
               and offerta.get("employer_kind") == "staffing_agency")
    denso = [
        rank_cv / 5, rank_job / 5, abs(rank_cv - rank_job) / 5,
        float(rank_job < rank_cv - 1), float(rank_job > rank_cv),
        float(not rank_cv), float(not rank_job),
        role_jaccard, role_cover, skill_jaccard, skill_cover,
        len(mancanti) / 3, float(bool(mancanti)),
        _mismatch_paese(profilo, offerta), float(agenzia), float(internship),
    ]
    denso += ruoli.feature(ruolo, _testo(offerta.get("title"))) if ruoli else [0.0] * 4
    sparse = dict(_sparse(testo_ruolo, _testo(offerta.get("title")), testo_offerta))
    for i, primo in enumerate(denso):
        for j in range(i, len(denso)):
            indice = _hash(f"d:{i}:{j}")
            sparse[indice] = sparse.get(indice, 0.0) + primo * denso[j]
    return denso, sorted(sparse.items())


def guardrail(score: float, profilo: str, offerta: dict) -> float:
    testo_offerta = " ".join((
        _testo(offerta.get("title")),
        _testo(offerta.get("ai_requirements_summary")),
        _testo(offerta.get("ai_core_responsibilities")),
    ))
    if lingue_mancanti(testo_offerta, set(_lista(profilo, "Lingua"))):
        score = min(score, 45.0)
    rank_cv = _rank(_campo(profilo, ("Seniority",)))
    if rank_cv >= 3 and (_INTERNSHIP.search(_testo(offerta.get("title")))
                         or _testo(offerta.get("ai_experience_level")).lower() in {"intern", "internship"}):
        score = min(score, 45.0)
    return max(0.0, min(100.0, score))


class MicroScorer:
    def __init__(self, percorso: str | Path):
        modello = json.loads(Path(percorso).read_text())
        if modello.get("versione") != VERSIONE or modello.get("n_features") != N_FEATURES:
            raise ValueError("modello match-micro incompatibile")
        self.pesi = [float(v) for v in modello["weights"]]
        self.media = float(modello["media"])
        self.bias = float(modello["bias"])
        self.media_denso = [float(v) for v in modello["dense_mean"]]
        self.scala_denso = [float(v) for v in modello["dense_std"]]
        if len(self.pesi) != N_FEATURES + _DENSO:
            raise ValueError("pesi match-micro non validi")
        if len(self.media_denso) != _DENSO or len(self.scala_denso) != _DENSO:
            raise ValueError("normalizzazione match-micro non valida")
        if any(not math.isfinite(v) for v in self.pesi + self.media_denso):
            raise ValueError("pesi match-micro non finiti")
        if any(not math.isfinite(v) or v <= 0 for v in self.scala_denso):
            raise ValueError("scala match-micro non valida")
        if not math.isfinite(self.media) or not math.isfinite(self.bias):
            raise ValueError("calibrazione match-micro non valida")
        self.ruoli = RuoliMicro.decomprimi(modello.get("ruoli_z"))

    def score(self, profilo: str, offerta: dict) -> float:
        denso, sparse = feature_vet(profilo, offerta, self.ruoli)
        normalizzato = [(v - m) / s for v, m, s in zip(denso, self.media_denso, self.scala_denso)]
        score = self.media + self.bias
        score += sum(v * w for v, w in zip(normalizzato, self.pesi[:_DENSO]))
        score += sum(self.pesi[_DENSO + indice] * valore for indice, valore in sparse)
        return guardrail(score, profilo, offerta)
