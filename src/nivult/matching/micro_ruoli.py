from __future__ import annotations

import base64
import json
import math
import zlib
from collections import Counter
from pathlib import Path

from nivult.matching.retrieval import tokenizza

LINGUE = ("en", "it", "de", "fr", "sv")
_DROP = {"senior", "junior", "sr", "jr", "lead", "head", "principal", "entry",
         "m", "w", "d", "f", "h", "nb", "in", "innen"}


def _token(testo: str) -> list[str]:
    out = []
    for token in tokenizza(testo):
        if token in _DROP:
            continue
        out.extend(("hr", "human", "resources") if token == "hr" else (token,))
    return out


class RuoliMicro:
    def __init__(self, dati: dict):
        self.alias = {k: frozenset(v) for k, v in dati.get("a", {}).items()}
        self.posts = {k: tuple(sorted(v)) for k, v in dati.get("p", {}).items()}
        self.isco = dati.get("i", [])
        self.famiglie = dati.get("f", [])
        n = len(self.isco)
        self.idf = {t: math.log((n + 1) / (len(v) + 1)) + 1
                    for t, v in self.posts.items()}
        self._cache_ids = {}
        self._cache_vettori = {}

    @classmethod
    def da_esco(cls, percorso: str | Path) -> RuoliMicro:
        alias: dict[str, set[int]] = {}
        posts: dict[str, set[int]] = {}
        isco = []
        famiglie = []
        with Path(percorso).open(encoding="utf-8") as f:
            for linea in f:
                riga = json.loads(linea)
                indice = len(isco)
                isco.append(riga["isco08"])
                famiglie.append(riga["famiglia_nivult"])
                for lingua in LINGUE:
                    etichette = [riga["preferred"].get(lingua, ""),
                                 *riga["alt"].get(lingua, [])]
                    for etichetta in etichette:
                        for parte in (etichetta, *etichetta.split("/")):
                            token = _token(parte)
                            chiave = " ".join(token)
                            if 2 <= len(token) <= 10:
                                alias.setdefault(chiave, set()).add(indice)
                            for t in set(token):
                                posts.setdefault(t, set()).add(indice)
        return cls({"a": alias, "p": posts, "i": isco, "f": famiglie})

    @classmethod
    def decomprimi(cls, valore: str | None) -> RuoliMicro:
        if not valore:
            return cls({})
        raw = zlib.decompress(base64.b85decode(valore.encode("ascii")))
        return cls(json.loads(raw))

    def comprimi(self) -> str:
        dati = {"a": {k: sorted(v) for k, v in self.alias.items()},
                "p": {k: sorted(v) for k, v in self.posts.items()},
                "i": self.isco, "f": self.famiglie}
        raw = json.dumps(dati, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.b85encode(zlib.compress(raw, 9)).decode("ascii")

    def _ids(self, testo: str) -> frozenset[int]:
        if testo in self._cache_ids:
            return self._cache_ids[testo]
        token = _token(testo)
        out = set()
        i = 0
        while i < len(token):
            trovato = None
            for j in range(min(len(token), i + 10), i + 1, -1):
                chiave = " ".join(token[i:j])
                if chiave in self.alias:
                    trovato = j, self.alias[chiave]
                    break
            if trovato:
                j, ids = trovato
                out.update(ids)
                i = j
            else:
                i += 1
        risultato = frozenset(out)
        if len(self._cache_ids) >= 1024:
            self._cache_ids.clear()
        self._cache_ids[testo] = risultato
        return risultato

    def _vettore(self, testo: str) -> tuple[dict[int, float], float]:
        if testo in self._cache_vettori:
            return self._cache_vettori[testo]
        vettore = Counter()
        for token in set(_token(testo)):
            peso = self.idf.get(token)
            if peso is None:
                continue
            for indice in self.posts[token]:
                vettore[indice] += peso
        risultato = dict(vettore), sum(v * v for v in vettore.values())
        if len(self._cache_vettori) >= 1024:
            self._cache_vettori.clear()
        self._cache_vettori[testo] = risultato
        return risultato

    def _struttura(self, a: frozenset[int], b: frozenset[int]) -> float:
        if not a or not b:
            return 0.0
        if a & b:
            return 1.0
        migliore = 0.0
        for i in a:
            for j in b:
                if self.famiglie[i] == self.famiglie[j]:
                    migliore = max(migliore, 0.05)
                if self.isco[i][:2] == self.isco[j][:2]:
                    migliore = max(migliore, 0.2)
                if self.isco[i][:3] == self.isco[j][:3]:
                    migliore = max(migliore, 0.5)
                if self.isco[i] == self.isco[j]:
                    return 0.8
        return migliore

    def feature(self, ruolo: str, titolo: str) -> list[float]:
        va, na = self._vettore(ruolo)
        vb, nb = self._vettore(titolo)
        if len(va) > len(vb):
            va, vb = vb, va
        dot = sum(valore * vb.get(indice, 0.0) for indice, valore in va.items())
        coseno = dot / math.sqrt(na * nb) if dot and na and nb else 0.0
        copertura_ruolo = dot / na if dot and na else 0.0
        copertura_titolo = dot / nb if dot and nb else 0.0
        return [coseno, copertura_ruolo, copertura_titolo,
                self._struttura(self._ids(ruolo), self._ids(titolo))]
