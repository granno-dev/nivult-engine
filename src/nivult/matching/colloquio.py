from __future__ import annotations

import json
import logging
import re

import httpx

from nivult.matching.lettera import _lingua_coerente, _pulisci
from nivult.matching.llm import (
    ChatModel,
    RispostaModelloNonValida,
    _estrai_json,
    offerta_per_prompt,
)

VERSIONE_COLLOQUIO = "2026-11-02.c4"

log = logging.getLogger("nivult.matching.colloquio")

_RUBRICA = """Prepari una persona a un colloquio specifico. Scrivi in {lingua},
con italiano naturale e professionale se la lingua richiesta è Italian.
Usa solo fatti presenti nel profilo e nell'annuncio. Non attribuire al candidato
attività dell'offerta che non risultano dal suo profilo: se manca un'esperienza,
aiutalo a dichiararlo e a presentare un esempio vicino senza fingere equivalenze.

Restituisci SOLO JSON con questa forma:
{{"spunti": ["...", "...", "..."], "domande": [
  {{"domanda": "...", "preparazione": "...", "risposta": "..."}},
  {{"domanda": "...", "preparazione": "...", "risposta": "..."}},
  {{"domanda": "...", "preparazione": "...", "risposta": "..."}}
]}}

SPUNTI: 3 paragrafi discorsivi di 30-65 parole. Trasforma i punti di forza in
consigli concreti su cosa raccontare e come collegarlo al ruolo. Non copiare le
frasi dei punti di forza. Non usare «collega questo punto», «spiega il tuo
contributo specifico», «chiudi con un risultato misurabile» o altre formule
ricorrenti. Varia l'attacco di ogni paragrafo. Non inventare esempi o risultati:
chiedi alla persona di scegliere un caso reale compatibile col CV.

DOMANDE: 3 quesiti che un selezionatore potrebbe davvero porre, brevi e naturali,
non il testo dell'annuncio o degli aspetti da chiarire travestito da domanda.
- preparazione: 25-55 parole con un'indicazione pratica e specifica sul caso da
  selezionare dal proprio percorso, senza istruzioni generiche ripetibili;
- risposta: 30-65 parole con una struttura concreta di risposta, non una risposta
  inventata in prima persona. Usa i fatti disponibili e indica come affrontare
  onestamente eventuali gap. Non scrivere «rispondi senza venderti troppo».
Parla di benefici realmente osservati, mai di risparmi o risultati da inventare.
Se la lingua è Italian, evita anglicismi come hands-on, end-to-end, scope,
go-live, stakeholder, process improvement e lesson learned; usa equivalenti
italiani naturali, mantenendo solo nomi di moduli, tecnologie e certificazioni.
Rivolgiti sempre al candidato con tu, senza alternare seconda e terza persona.
Non usare «advisory» e controlla gli articoli: davanti a vocale usa «dell'»,
mai «del». Niente citazioni testuali dell'annuncio, elenchi Markdown o virgolette.
Rileggi ogni paragrafo: zero refusi, niente frasi incomplete o incoerenti.
"""

_VERIFICA = """Controlla questa preparazione al colloquio.

CONTENUTO
{contenuto}

PROFILO DEL CANDIDATO (unica fonte sul suo passato)
{profilo}

OFFERTA (fonte solo su ruolo e requisiti)
{offerta}

Segnala come inventato qualsiasi esempio, esperienza, risultato o attività del
candidato che non sia presente nel profilo, anche se richiesto dall'offerta o
nascosto fra consigli come «racconta quando» e «trova riscontro nel tuo percorso».
Le domande possono esplorare un gap dichiarato, ma preparazione e risposta non
devono suggerire di fingere competenze. Non segnalare intenzioni future oneste.
Controlla anche refusi, frasi incomplete, incoerenze grammaticali e periodi che
non scorrono naturalmente. Rispondi SOLO con JSON:
{{"inventato": true|false, "forma_scorretta": true|false}}.
"""

_FORMULE_VETUSTE = re.compile(
    r"collega questo punto|spiega il tuo contributo specifico|"
    r"chiudi con un risultato misurabile|rispondi senza venderti troppo|"
    r"prepara un esempio reale su|quale esempio pertinente puoi portare", re.IGNORECASE)
_ANGLOCISMI = re.compile(
    r"\b(?:hands-on|end-to-end|scope|go-live|stakeholders?|process improvement|"
    r"lesson learned|business process configuration|advisory)\b", re.IGNORECASE)


def _parole(testo: str) -> int:
    return len(re.findall(r"\b[\w’'-]+\b", testo, re.UNICODE))


def _normalizzato(testo: str) -> str:
    return _pulisci(testo).casefold()


def _copia_fonte(testo: str, analisi: dict) -> bool:
    candidato = _normalizzato(testo)
    for chiave in ("pros", "cons"):
        for punto in analisi.get(chiave) or []:
            sorgente = _normalizzato(punto)
            if len(sorgente) >= 60 and sorgente in candidato:
                return True
    return False


def _errore_contenuto(data: dict, lingua: str, analisi: dict) -> str | None:
    spunti = data.get("spunti")
    domande = data.get("domande")
    if not isinstance(spunti, list) or not 2 <= len(spunti) <= 4:
        return "servono da 2 a 4 spunti"
    if not isinstance(domande, list) or not 2 <= len(domande) <= 4:
        return "servono da 2 a 4 domande"
    testi = []
    attacchi = set()
    for spunto in spunti:
        if not isinstance(spunto, str) or not 25 <= _parole(spunto) <= 75:
            return "ogni spunto deve essere un paragrafo di 25-75 parole"
        attacchi.add(" ".join(_normalizzato(spunto).split()[:5]))
        testi.append(spunto)
    if len(attacchi) < len(spunti):
        return "gli spunti iniziano tutti con la stessa formula"
    for voce in domande:
        if not isinstance(voce, dict):
            return "ogni domanda deve avere la struttura richiesta"
        domanda, preparazione, risposta = (voce.get(k) for k in
                                           ("domanda", "preparazione", "risposta"))
        if not isinstance(domanda, str) or not domanda.strip().endswith("?") \
                or not 7 <= _parole(domanda) <= 40:
            return "ogni quesito deve essere naturale e di 7-40 parole"
        if not isinstance(preparazione, str) or not 20 <= _parole(preparazione) <= 65:
            return "ogni preparazione deve contenere 20-65 parole"
        if not isinstance(risposta, str) or not 25 <= _parole(risposta) <= 75:
            return "ogni risposta deve contenere 25-75 parole"
        testi.extend((domanda, preparazione, risposta))
    testo = "\n".join(testi)
    if _FORMULE_VETUSTE.search(testo):
        return "contiene ancora una formula precompilata"
    if lingua == "Italian" and _ANGLOCISMI.search(testo):
        return "contiene anglicismi evitabili: usa un italiano naturale"
    if lingua == "Italian" and re.search(
            r"\bdel\s+[aeiouàèéìíòóùú]", testo, re.IGNORECASE):
        return "contiene un errore d’articolo: davanti a vocale usa «dell'»"
    if lingua == "Italian" and re.search(
            r"\b(?:ha mai|come gestisce|ci descriva|mi racconti)\b", testo, re.IGNORECASE):
        return "alterna seconda e terza persona: rivolgiti al candidato solo con tu"
    if _copia_fonte(testo, analisi):
        return "incolla il testo dell'analisi invece di riformularlo"
    if not _lingua_coerente(testo, lingua):
        return f"non è scritto interamente in {lingua}"
    return None


def _problema_verifica(modello: ChatModel, data: dict, profilo_testo: str,
                       offerta: dict) -> str | None:
    contenuto = json.dumps(data, ensure_ascii=False)
    offerta_testo = offerta_per_prompt(offerta) + "\n\nTESTO DELL'ANNUNCIO\n"
    offerta_testo += _pulisci(offerta.get("ai_core_responsibilities"))[:6000]
    try:
        risposta = modello.chat([{"role": "user", "content": _VERIFICA.format(
            contenuto=contenuto, profilo=profilo_testo, offerta=offerta_testo)}],
            max_tokens=80)
        esito = _estrai_json(risposta)
        if esito.get("inventato") is not False:
            return "conteneva esperienze o risultati non presenti nel profilo"
        if esito.get("forma_scorretta") is True:
            return "conteneva refusi, frasi incomplete o incoerenze grammaticali"
        return None
    except (httpx.HTTPError, RuntimeError, ValueError, TypeError) as exc:
        log.warning("verificatore del colloquio non disponibile: %s", type(exc).__name__)
        return "il controllo di qualità non era disponibile"


def genera_colloquio(modello: ChatModel, profilo_testo: str, offerta: dict, *,
                     analisi: dict | None = None, lingua: str = "English") -> dict:
    analisi = analisi or {}
    nota = ""
    for _ in range(2):
        coda = offerta_per_prompt(offerta)
        coda += "\n\nTESTO DELL'ANNUNCIO\n" + _pulisci(
            offerta.get("ai_core_responsibilities"))[:6000]
        coda += "\n\nPUNTI DI FORZA DA RIFORMULARE\n" + "\n".join(
            f"- {_pulisci(x)}" for x in (analisi.get("pros") or [])[:4])
        coda += "\n\nASPETTI DA CHIARIRE DA TRASFORMARE IN QUESITI NATURALI\n"
        coda += "\n".join(f"- {_pulisci(x)}" for x in (analisi.get("cons") or [])[:3])
        coda += f"\n\nSCRIVI TUTTO IN {lingua.upper()}."
        if nota:
            coda += f"\n\nLA VERSIONE PRECEDENTE È STATA RIFIUTATA: {nota}"
        risposta = modello.chat([
            {"role": "system", "content": _RUBRICA.format(lingua=lingua)},
            {"role": "system", "content": "PROFILO DEL CANDIDATO\n" + profilo_testo},
            {"role": "user", "content": coda},
        ], max_tokens=1800, temperature=0.45)
        try:
            data = _estrai_json(risposta)
        except (TypeError, ValueError) as exc:
            raise RispostaModelloNonValida(
                f"{modello.model}: JSON del colloquio non valido") from exc
        errore = _errore_contenuto(data, lingua, analisi)
        if errore:
            nota = errore
            log.warning("preparazione colloquio rifiutata (%s): %s", offerta.get("id"), errore)
            continue
        problema = _problema_verifica(modello, data, profilo_testo, offerta)
        if problema:
            nota = problema
            log.warning("colloquio rifiutato dal verificatore (%s): %s",
                        offerta.get("id"), problema)
            continue
        return data
    raise RispostaModelloNonValida(
        f"{modello.model}: preparazione colloquio rifiutata dai controlli")
