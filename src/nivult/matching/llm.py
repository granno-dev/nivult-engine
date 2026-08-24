"""Client per i modelli di valutazione.

Due modelli, due ruoli:

  GLM 5.2       il giudizio vero. Punteggio 0-100 e motivazione.
  Mistral Small il pre-screening, solo sui cluster sopra soglia: un ordinamento
                grossolano per passare a GLM solo le migliori.

NON ANCORA VERIFICATO SUL CAMPO: mancano le chiavi. Forma delle richieste presa
dalla documentazione, non da una risposta vista.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from nivult.ingestion.base import HttpSource


@dataclass(slots=True)
class Punteggio:
    job_id: str
    score: int
    reason: str | None = None


def _estrai_json(testo: str) -> dict:
    """Il modello a volte avvolge il JSON in prosa o in un blocco markdown.

    Meglio recuperarlo che far fallire una chiamata già pagata.
    """
    testo = testo.strip()
    if testo.startswith("```"):
        testo = re.sub(r"^```[a-z]*\n|\n```$", "", testo)
    try:
        d = json.loads(testo)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", testo, re.S)
        if not m:
            raise ValueError(f"nessun JSON nella risposta: {testo[:180]}")
        d = json.loads(m.group(0))
    if not isinstance(d, dict):
        raise ValueError(f"atteso un oggetto JSON, ricevuto: {str(d)[:120]}")
    return d


class ChatModel(HttpSource):
    """Base per un endpoint compatibile con l'API chat completions."""

    base_url = ""
    model = ""
    env_key = ""

    def __init__(self, api_key: str | None = None, **kw):
        super().__init__(rate_per_second=kw.pop("rate_per_second", 2.0),
                         timeout=kw.pop("timeout", 120.0), **kw)
        self.api_key = api_key or os.environ.get(self.env_key, "")
        if not self.api_key:
            raise SystemExit(f"Serve {self.env_key}.")
        self.input_tokens = 0
        self.output_tokens = 0
        # Uso dell'ULTIMA chiamata: chi valuta un'offerta alla volta lo registra
        # su ogni riga di matches, non in aggregato.
        self.last_usage: dict = {}

    def chat(self, messages: list[dict], *, temperature: float = 0.0,
             max_tokens: int = 4000, extra: dict | None = None) -> str:
        payload = {"model": self.model, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
        if extra:
            payload.update(extra)
        r = self.request("POST", f"{self.base_url}/chat/completions",
                         headers={"Authorization": f"Bearer {self.api_key}",
                                  "Content-Type": "application/json"},
                         json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"{self.model} ({r.status_code}): {r.text[:300]}")
        d = r.json()
        uso = d.get("usage") or {}
        self.input_tokens += uso.get("prompt_tokens", 0)
        self.output_tokens += uso.get("completion_tokens", 0)
        self.last_usage = {
            "input": uso.get("prompt_tokens", 0),
            "cached": (uso.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
            "output": uso.get("completion_tokens", 0)}
        return d["choices"][0]["message"]["content"]


class GLM(ChatModel):
    source = "glm"
    base_url = os.environ.get("GLM_BASE_URL", "https://api.z.ai/api/paas/v4")
    model = "glm-5.2"
    env_key = "GLM_API_KEY"

    def chat(self, messages, **kw):
        # Thinking OFF: su una valutazione a rubrica il ragionamento esteso
        # moltiplica i token di output senza cambiare il punteggio.
        extra = kw.pop("extra", {}) or {}
        extra.setdefault("thinking", {"type": "disabled"})
        return super().chat(messages, extra=extra, **kw)


class MistralSmall(ChatModel):
    source = "mistral"
    base_url = os.environ.get("MISTRAL_BASE_URL", "https://api.mistral.ai/v1")
    model = "mistral-small-latest"
    env_key = "MISTRAL_API_KEY"


RUBRICA = """Sei un selezionatore esperto. Valuta quanto UNA offerta di lavoro è
adatta al profilo del candidato che ti viene dato.

Punteggio da 0 a 100:
  90-100  corrispondenza forte: ruolo, livello e competenze coincidono
  70-89   buona: il ruolo è giusto, qualche competenza manca
  50-69   plausibile: settore o livello divergono, ma il passaggio è credibile
  20-49   debole: solo affinità generiche
   0-19   non pertinente

Considera ruolo, seniority, competenze richieste, lingua e sede.
NON premiare un'offerta perché prestigiosa o ben scritta: conta solo l'aderenza
al profilo. NON premiare la genericità: un'offerta vaga che potrebbe adattarsi a
chiunque non è una buona corrispondenza.

Rispondi SOLO con questo JSON, niente altro:
{"score": <0-100>, "reason": "<una frase in italiano, massimo 10 parole>"}

"""

# Seconda passata: la motivazione che il destinatario del digest legge. Corre
# solo sulle offerte che entrano nel digest (le prime 30), non su tutte: è il
# risparmio progettato nelle decisioni di architettura.
RUBRICA_MOTIVAZIONE = """Sei un selezionatore esperto. Spiega in UNA frase
italiana di massimo 25 parole perché questa offerta è adatta al profilo del
candidato. Sii concreto: ruolo, competenze, livello. Niente genericità.

Rispondi SOLO con questo JSON, niente altro:
{"reason": "<la frase>"}

"""


def profilo_come_testo(profilo: dict) -> str:
    parti = [f"Ruolo cercato: {profilo.get('ruolo','—')}",
             f"Seniority: {profilo.get('seniority','—')}",
             f"Competenze: {', '.join(profilo.get('competenze', []))}",
             f"Lingue: {', '.join(profilo.get('lingue', []))}",
             f"Sedi accettate: {', '.join(profilo.get('sedi', []))}"]
    if profilo.get("note"):
        parti.append(f"Note: {profilo['note']}")
    return "\n".join(parti)


def offerta_come_testo(job: dict) -> str:
    """Riassunto e competenze, non la descrizione integrale.

    La descrizione integrale moltiplica i token per un contenuto che è in gran
    parte boilerplate legale e di employer branding.
    """
    pezzi = [f"id: {job['id']}", f"titolo: {job['title']}"]
    if job.get("organization"):
        pezzi.append(f"azienda: {job['organization']}")
    if job.get("ai_experience_level"):
        pezzi.append(f"esperienza: {job['ai_experience_level']}")
    if job.get("ai_work_arrangement"):
        pezzi.append(f"modalità: {job['ai_work_arrangement']}")
    if job.get("cities"):
        pezzi.append(f"sede: {', '.join(job['cities'][:2])}")
    if job.get("ai_key_skills"):
        pezzi.append(f"competenze: {', '.join(job['ai_key_skills'][:12])}")
    if job.get("ai_requirements_summary"):
        pezzi.append(f"requisiti: {job['ai_requirements_summary'][:400]}")
    return " | ".join(pezzi)


def _testa(profilo_testo: str, rubrica: str) -> list[dict]:
    # Il profilo va in testa e identico a ogni chiamata: è la parte che il
    # fornitore mette in cache, e cambiarla fra un'offerta e l'altra
    # annullerebbe il risparmio (misurato: prefissi in cache al 90%).
    return [{"role": "system", "content": rubrica},
            {"role": "system", "content": "PROFILO DEL CANDIDATO\n" + profilo_testo}]


def valuta_offerta(modello: ChatModel, profilo_testo: str, offerta: dict
                   ) -> tuple[int, str, dict]:
    """UNA offerta per chiamata: punteggio e micro-motivazione.

    Nel lotto il modello confronta le offerte fra loro invece di misurarle
    contro il CV, e distribuisce i voti sulla scala del lotto: è misurato, ed
    è il motivo di questa firma. La micro-motivazione c'è perché
    matches.reason è NOT NULL: lo scarto dev'essere spiegato anche lui, non
    solo ciò che passa.
    """
    corpo = _testa(profilo_testo, RUBRICA) + [{
        "role": "user", "content": "OFFERTA\n" + offerta_come_testo(offerta)}]
    risposta = modello.chat(corpo, max_tokens=120)
    p = _estrai_json(risposta)
    score = max(0, min(100, int(p.get("score", 0))))
    reason = str(p.get("reason") or "")[:400].strip() or "—"
    return score, reason, dict(modello.last_usage)


def motiva_offerta(modello: ChatModel, profilo_testo: str, offerta: dict
                   ) -> tuple[str, dict]:
    """Seconda passata: la motivazione che il destinatario del digest legge."""
    corpo = _testa(profilo_testo, RUBRICA_MOTIVAZIONE) + [{
        "role": "user", "content": "OFFERTA\n" + offerta_come_testo(offerta)}]
    risposta = modello.chat(corpo, max_tokens=120)
    p = _estrai_json(risposta)
    reason = str(p.get("reason") or "")[:400].strip() or "—"
    return reason, dict(modello.last_usage)


def valuta(modello: ChatModel, profilo: dict, jobs: list[dict], *,
           con_motivazione: bool = False) -> list[Punteggio]:
    """Comodità per gli esperimenti: la serie di valutazioni a offerta singola."""
    testo = profilo_come_testo(profilo)
    out: list[Punteggio] = []
    for j in jobs:
        score, reason, _ = valuta_offerta(modello, testo, j)
        if con_motivazione:
            reason, _ = motiva_offerta(modello, testo, j)
        out.append(Punteggio(str(j["id"]), score, reason))
    return out
