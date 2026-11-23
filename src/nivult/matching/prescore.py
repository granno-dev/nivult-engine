"""Il pre-scorer gratuito: match-v1 decide chi merita il giudice.

La bolletta GLM nasce da un'asimmetria: per consegnare 5-10 offerte sopra
soglia, il worker ne valuta una cinquantina a digest — e ogni valutazione
e' una chiamata a pagamento. match-v1 (bge-reranker-v2-m3 distillato da
GLM, rho 0.73, AUC 0.94) legge le stesse coppie profilo-offerta a costo
zero e indovina il voto del maestro bene abbastanza per una domanda sola:
«vale la pena pagare il giudizio di questa?».

IL PRE-SCORER NON SCRIVE UN PUNTEGGIO: decide solo chi entra nel loop delle
valutazioni. Il voto che finisce in matches, nel digest e davanti all'utente
viene sempre dal modello di valutazione effettivamente registrato sul match.

Il modello vive sull'N5 dietro un microservizio HTTP (scripts/
servi_match_v1.py, stesso disegno di MaestroLocale in llm.py: hardware di
casa, rete Tailscale, niente dipendenze nuove sul server che e' gia'
andato in OOM). Qui sta solo il client: PRESCORE_URL valorizzato accende,
assente spegne — e spento il worker fa esattamente cio' che faceva prima.
Con FAMILY_GUARD=1 lo stesso servizio fornisce anche la distribuzione delle
famiglie: il controllo resta limitato a HR e non scarta mai in caso di guasto.
"""

from __future__ import annotations

import logging
import math
import os

import httpx

log = logging.getLogger("nivult.matching.prescore")

# Di quanto il distillato puo' sbagliare in difeso: con soglia 70 si manda
# al giudice configurato tutto cio' che sta sopra 50. Non e' un numero tirato
# a caso: e' la fascia dove l'errore dello studente (misurato a validation)
# non ribalta quasi mai il verdetto — sotto, un 70 vero e' rarissimo.
MARGINE = 20

TIMEOUT = 60.0  # un batch da ~50 coppie sulla GPU dell'N5: pochi secondi


def disponibile() -> bool:
    return bool(os.environ.get("PRESCORE_URL"))


def prescore_batch(profilo_testo: str, offerte_testo: list[str]) -> list[float]:
    """I punteggi previsti (0-100) per le coppie, nello stesso ordine.

    Una chiamata sola per digest: il batch e' il caso normale, non
    l'ottimizzazione. Errori di rete e 5xx risalgono: chi chiama decide se
    valutare tutto con GLM (e lo fa).
    """
    url = os.environ["PRESCORE_URL"].rstrip("/")
    r = httpx.post(f"{url}/prescore", timeout=TIMEOUT, json={
        "pairs": [[profilo_testo, o] for o in offerte_testo],
    })
    if r.status_code != 200:
        raise RuntimeError(f"prescore ({r.status_code}): {r.text[:200]}")
    punteggi = r.json().get("scores")
    if not isinstance(punteggi, list) or len(punteggi) != len(offerte_testo):
        raise RuntimeError("prescore: numero o forma dei punteggi non valido")
    valori: list[float] = []
    for p in punteggi:
        if isinstance(p, bool):
            raise TypeError("prescore: punteggio booleano non ammesso")
        try:
            valore = float(p)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("prescore: punteggio non numerico") from exc
        if not math.isfinite(valore) or not 0 <= valore <= 100:
            raise RuntimeError("prescore: punteggio fuori dalla scala 0-100")
        valori.append(valore)
    return valori


def seleziona_per_glm(punteggi: list[float], soglie: list[int], *,
                      max_items: int, margine: int = MARGINE) -> list[int]:
    """Gli INDICI dei plausibili da mandare a GLM, in ordine di arrivo.

    Passa solo chi ha prescore >= soglia - margine, con soglia PER CANDIDATO.
    In caso di eccesso vincono i punteggi migliori; max_items è un tetto, non
    una quota da riempire con offerte fuori fascia. Un digest vuoto resta
    preferibile a pagare il giudizio di candidati che il distillato esclude.

    Gli indici tornano nell'ordine originale: il budget continua poi a
    privilegiare le offerte più recenti.
    """
    if max_items <= 0:
        return []
    sopra = sorted((i for i, (p, s) in enumerate(zip(punteggi, soglie))
                   if p >= s - margine),
                   key=lambda i: (-punteggi[i], i))
    return sorted(sopra[:max_items])


FAMIGLIA_SOGLIA = 0.90
FAMIGLIE_HR_INCOMPATIBILI = frozenset({
    "Security & Safety", "Environmental & Sustainability"})


def family_guard_disponibile() -> bool:
    return disponibile() and os.environ.get("FAMILY_GUARD") == "1"


def incompatibile_hr(famiglie: list[str] | None,
                     scores: dict[str, float], *,
                     soglia: float = FAMIGLIA_SOGLIA) -> bool:
    """Solo HR puro e prova cumulata forte; le altre famiglie restano fuori."""
    if set(famiglie or []) != {"Human Resources"}:
        return False
    return sum(scores.get(f, 0.0) for f in FAMIGLIE_HR_INCOMPATIBILI) >= soglia


def famiglia_batch(offerte: list[dict]) -> list[dict[str, float]]:
    """Le distribuzioni di famiglia nello stesso ordine delle offerte.

    Il modello e il formatter restano sull'N5: il server applicativo non ha
    bisogno di torch. Un payload non valido risale, mai uno scarto silenzioso.
    """
    if not offerte:
        return []
    url = os.environ["PRESCORE_URL"].rstrip("/")
    jobs = [{"title": o.get("title"),
             "location": ", ".join(o.get("cities") or []),
             "description": o.get("ai_requirements_summary") or ""}
            for o in offerte]
    r = httpx.post(f"{url}/famiglia", timeout=TIMEOUT, json={"jobs": jobs})
    if r.status_code != 200:
        raise RuntimeError(f"famiglia ({r.status_code}): {r.text[:200]}")
    predetti = r.json().get("predictions")
    if not isinstance(predetti, list) or len(predetti) != len(offerte):
        raise RuntimeError("famiglia: numero o forma dei risultati non valido")
    risultati: list[dict[str, float]] = []
    for predetto in predetti:
        scores = predetto.get("scores") if isinstance(predetto, dict) else None
        if not isinstance(scores, dict) or len(scores) != 33:
            raise RuntimeError("famiglia: distribuzione non valida")
        valori: dict[str, float] = {}
        for nome, p in scores.items():
            if not isinstance(nome, str) or not nome:
                raise RuntimeError("famiglia: etichetta vuota")
            if isinstance(p, bool):
                raise TypeError("famiglia: probabilita booleana non ammessa")
            try:
                valore = float(p)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("famiglia: probabilita non numerica") from exc
            if not math.isfinite(valore) or not 0 <= valore <= 1:
                raise RuntimeError("famiglia: probabilita fuori dalla scala 0-1")
            valori[nome] = valore
        if not math.isclose(sum(valori.values()), 1.0, abs_tol=0.001):
            raise RuntimeError("famiglia: le probabilita non sommano a uno")
        risultati.append(valori)
    return risultati
