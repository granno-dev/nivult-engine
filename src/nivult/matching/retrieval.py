"""Primitive deterministiche per retrieval ibrido e fusione dei ranking."""
from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence

_TOKEN = re.compile(r"[^\W_]+(?:[+#]+)?(?:\.[^\W_]+(?:[+#]+)?)*", re.UNICODE)


def tokenizza(testo: str) -> list[str]:
    return _TOKEN.findall(testo.lower())


def fatti_profilo(profilo: str, etichette: tuple[str, ...]) -> str:
    fatti = []
    for linea in profilo.splitlines():
        fatto = re.sub(r"^\[\d+\]\s*", "", linea).strip()
        if any(fatto.startswith(f"{etichetta}:") for etichetta in etichette):
            fatti.append(fatto)
    return "\n".join(fatti)


def documento_ruolo(offerta: dict) -> str:
    pezzi = [f"titolo: {offerta.get('title') or ''}"]
    if offerta.get("ai_core_responsibilities"):
        pezzi.append(f"responsabilità: {offerta['ai_core_responsibilities']}")
    if offerta.get("ai_requirements_summary"):
        pezzi.append(f"requisiti: {offerta['ai_requirements_summary']}")
    return "\n".join(pezzi)


DOCUMENTO_RUOLO_SQL = """concat_ws(chr(10),
  'titolo: ' || coalesce(j.title, ''),
  CASE WHEN coalesce(j.ai_core_responsibilities, '') <> ''
       THEN 'responsabilità: ' || j.ai_core_responsibilities END,
  CASE WHEN coalesce(j.ai_requirements_summary, '') <> ''
       THEN 'requisiti: ' || j.ai_requirements_summary END)"""
HASH_DOCUMENTO_SQL = (
    f"encode(sha256(convert_to({DOCUMENTO_RUOLO_SQL}, 'UTF8')), 'hex')")


def rrf(rankings: Mapping[str, Sequence[str]], *, k: int = 60,
        pesi: Mapping[str, float] | None = None) -> dict[str, float]:
    """Reciprocal Rank Fusion; ogni ID conta una sola volta per vista."""
    if isinstance(k, bool) or k < 0:
        raise ValueError("k deve essere non negativo")
    pesi = pesi or {}
    out: dict[str, float] = {}
    for vista, ordinati in rankings.items():
        peso = pesi.get(vista, 1.0)
        if isinstance(peso, bool) or not math.isfinite(peso) or peso < 0:
            raise ValueError(f"peso non valido per {vista}")
        visti = set()
        for posizione, identificativo in enumerate(ordinati, 1):
            if not isinstance(identificativo, str) or not identificativo:
                raise ValueError("gli ID devono essere stringhe non vuote")
            if identificativo in visti:
                raise ValueError(f"ID duplicato nella vista {vista}: {identificativo}")
            visti.add(identificativo)
            out[identificativo] = out.get(identificativo, 0.0) + peso / (k + posizione)
    return out


def ordina(punteggi: Mapping[str, float], *, limite: int | None = None) -> list[str]:
    if limite is not None and limite < 0:
        raise ValueError("limite deve essere non negativo")
    for valore in punteggi.values():
        if isinstance(valore, bool) or not math.isfinite(valore):
            raise ValueError("punteggio non finito")
    ordinati = sorted(punteggi, key=lambda i: (-punteggi[i], i))
    return ordinati if limite is None else ordinati[:limite]


def min_max(punteggi: Mapping[str, float]) -> dict[str, float]:
    """Normalizza per query; una vista costante non aggiunge evidenza."""
    if not punteggi:
        return {}
    if any(isinstance(v, bool) or not math.isfinite(v) for v in punteggi.values()):
        raise ValueError("punteggio non finito")
    basso, alto = min(punteggi.values()), max(punteggi.values())
    if alto == basso:
        return dict.fromkeys(punteggi, 0.0)
    return {i: (v - basso) / (alto - basso) for i, v in punteggi.items()}


def media_geometrica(viste: Mapping[str, Mapping[str, float]], *,
                     epsilon: float = 1e-6) -> dict[str, float]:
    """Fonde punteggi già normalizzati, con peso uguale e tie deterministico altrove."""
    if not 0 < epsilon <= 1:
        raise ValueError("epsilon deve essere in (0, 1]")
    if not viste:
        return {}
    normalizzate = {nome: min_max(valori) for nome, valori in viste.items()}
    ids = set().union(*(set(v) for v in normalizzate.values()))
    out = {}
    for identificativo in ids:
        valori = [v.get(identificativo, 0.0) for v in normalizzate.values()]
        out[identificativo] = math.exp(
            sum(math.log(max(v, epsilon)) for v in valori) / len(valori))
    return out


class BM25:
    """BM25 Okapi per piccoli banchi; l'indice di produzione resta esterno."""

    def __init__(self, documenti: Mapping[str, str], *, k1: float = 1.5, b: float = 0.75):
        if not 0 <= b <= 1 or k1 < 0:
            raise ValueError("parametri BM25 non validi")
        self.k1, self.b = k1, b
        self.documenti = {i: tokenizza(testo) for i, testo in documenti.items()}
        if len(self.documenti) != len(set(self.documenti)):
            raise ValueError("ID documento duplicati")
        self.df = Counter()
        for token in self.documenti.values():
            self.df.update(set(token))
        self.media = (sum(map(len, self.documenti.values())) / len(self.documenti)
                      if self.documenti else 0.0)

    def punteggi(self, query: str) -> dict[str, float]:
        n = len(self.documenti)
        out = {}
        for identificativo, documento in self.documenti.items():
            frequenze = Counter(documento)
            norma = self.k1 * (1 - self.b + self.b * len(documento) / self.media) \
                if self.media else 0.0
            score = 0.0
            for termine in tokenizza(query):
                df = self.df.get(termine, 0)
                if not df:
                    continue
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                tf = frequenze.get(termine, 0)
                score += idf * tf * (self.k1 + 1) / (tf + norma) if tf + norma else 0.0
            out[identificativo] = score
        return out


_LINGUE = {
    "english": "English", "inglese": "English", "englisch": "English",
    "italian": "Italian", "italiano": "Italian", "italienisch": "Italian",
    "german": "German", "tedesco": "German", "deutsch": "German",
    "french": "French", "francese": "French", "französisch": "French",
    "spanish": "Spanish", "spagnolo": "Spanish", "spanisch": "Spanish",
    "swedish": "Swedish", "svedese": "Swedish", "schwedisch": "Swedish",
    "dutch": "Dutch", "olandese": "Dutch", "niederländisch": "Dutch",
}
_RE_LINGUA = re.compile(
    r"\b(" + "|".join(map(re.escape, _LINGUE)) + r")(?:kenntnisse|sprache)?\b", re.IGNORECASE)
_RE_REQUISITO_LINGUA = re.compile(
    r"languages?\s+(?:required|requirement)|\bmandatory\b|\brequired\b|"
    r"\bobbligatori|\brichiest|fluent|fluency|proficien|sehr gute|"
    r"kommunikationsstark|ability to communicate|ottima conoscenza|"
    r"eccellente conoscenza|very good", re.IGNORECASE)


def lingue_obbligatorie(testo: str) -> set[str]:
    """Lingue richieste esplicitamente, non semplici riferimenti geografici."""
    out = set()
    for frase in re.split(r"[\n.!?;]+", testo):
        if _RE_REQUISITO_LINGUA.search(frase):
            out.update(_LINGUE[m.group(1).lower()] for m in _RE_LINGUA.finditer(frase))
    return out


def lingue_mancanti(testo: str, lingue_profilo: set[str]) -> set[str]:
    return lingue_obbligatorie(testo) - set(lingue_profilo)


def prompt_reranker(sistema: str, istruzione: str, query: str, documento: str) -> str:
    return (f"<|im_start|>system\n{sistema}<|im_end|>\n"
            f"<|im_start|>user\n<Instruct>: {istruzione}\n"
            f"<Query>: {query}\n<Document>: {documento}<|im_end|>\n"
            "<|im_start|>assistant\n<think>\n\n</think>\n\n")
