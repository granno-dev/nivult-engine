from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from nivult.matching.micro import feature_vet, guardrail
from nivult.matching.micro_ruoli import RuoliMicro
from nivult.matching.retrieval import documento_ruolo

VERSIONE = "m2v-match-1"


class M2VScorer:
    def __init__(self, percorso: str | Path):
        from model2vec import StaticModel

        self.percorso = Path(percorso)
        self.static = StaticModel.from_pretrained(self.percorso)
        config = json.loads((self.percorso / "scoring.json").read_text())
        if config.get("versione") != VERSIONE:
            raise ValueError("modello M2V incompatibile")
        self.testa = config.get("modello_testa", "lineare")
        self.pesi = np.array(config["weights"], dtype=np.float64)
        self.media = float(config["media"])
        self.bias = float(config["bias"])
        self.blend = float(config.get("blend", 0.0))
        self.media_feature = np.array(config["feature_mean"], dtype=np.float64)
        self.scala_feature = np.array(config["feature_std"], dtype=np.float64)
        if (len(self.media_feature) != len(self.scala_feature)
                or len(self.media_feature) < 22 or (len(self.media_feature) - 21) % 2
                or np.any(self.scala_feature <= 0)
                or not np.all(np.isfinite(self.media_feature))
                or not np.all(np.isfinite(self.scala_feature))
                or not math.isfinite(self.bias)):
            raise ValueError("parametri M2V non validi")
        if self.testa == "hgb":
            import joblib
            self.regressore = joblib.load(self.percorso / "regressor.pkl")
            self.classificatore = joblib.load(self.percorso / "classifier.pkl")
        elif self.testa in {"lineare", "combinato"}:
            self.n_sparse = int(config.get("n_sparse", 0)) if self.testa == "combinato" else 0
            attesi = self.n_sparse + len(self.media_feature)
            if (len(self.pesi) != attesi or not np.all(np.isfinite(self.pesi))
                    or not math.isfinite(self.media)):
                raise ValueError("pesi M2V non validi")
        else:
            raise ValueError("testa M2V non riconosciuta")
        self.ruoli = RuoliMicro.decomprimi(config.get("ruoli_z")) if config.get("ruoli_z") else None

    def score(self, profilo: str, offerta: dict) -> float:
        return self.score_batch(profilo, [offerta])[0]

    def score_batch(self, profilo: str, offerte: list[dict]) -> list[float]:
        if not offerte:
            return []
        testi = [documento_ruolo(o) for o in offerte]
        a = self.static.encode([profilo] * len(offerte))
        b = self.static.encode(testi)
        a = a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-12)
        b = b / np.maximum(np.linalg.norm(b, axis=1, keepdims=True), 1e-12)
        coseno = np.sum(a * b, axis=1, keepdims=True)
        feature_pairs = [feature_vet(profilo, o, self.ruoli) for o in offerte]
        denso = np.array([feature[0] for feature in feature_pairs])
        base = np.column_stack((coseno, a * b, np.abs(a - b), denso))
        if base.shape[1] != len(self.media_feature):
            raise ValueError("feature M2V incompatibili con il modello")
        if self.testa == "hgb":
            scores = ((1 - self.blend) * self.regressore.predict(base)
                      + self.blend * 100 * self.classificatore.predict_proba(base)[:, 1]
                      + self.bias)
        else:
            normalizzato = (base - self.media_feature) / self.scala_feature
            scores = self.media + self.bias + normalizzato @ self.pesi[self.n_sparse:]
            if self.testa == "combinato":
                scores = scores + np.array([
                    sum(self.pesi[indice] * valore for indice, valore in feature[1])
                    for feature in feature_pairs])
        return [guardrail(float(score), profilo, offerta)
                for score, offerta in zip(scores, offerte)]
