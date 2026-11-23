from __future__ import annotations

import logging
import re

import httpx

from nivult.matching.llm import (
    ChatModel,
    RispostaModelloNonValida,
    _analisi_inventata,
    _estrai_json,
    offerta_per_prompt,
    profilo_come_testo,
)

VERSIONE_LETTERA = "2026-11-02.l4"

log = logging.getLogger("nivult.matching.lettera")

_RUBRICA = """Scrivi una lettera di presentazione pronta da rileggere, non una scaletta.
Usa esclusivamente i fatti presenti nel profilo e nell'annuncio forniti: non
inventare mansioni svolte, risultati, numeri, settori, lingue, certificazioni o
motivazioni personali. Non dedurre che una competenza sia stata usata in un
incarico specifico se i dati non lo dicono. Non attribuire al candidato le
attività richieste dall'annuncio: se un'esperienza non è nel profilo, puoi solo
parlare di ciò che potrà fare, senza scrivere che «trova riscontro» nel passato.

La lettera deve essere specifica per QUESTA azienda e QUESTO ruolo:
- apri collegando una responsabilità concreta dell'offerta a una o due evidenze
  realmente pertinenti del profilo; non iniziare con formule di rito;
- cita il titolo ufficiale del ruolo e l'azienda, quando è indicata;
- sviluppa un solo filo narrativo, con tono sobrio e naturale, senza elenchi;
- non usare «la mia esperienza include», «my experience includes» o equivalenti;
- non copiare l'annuncio e non trasformare le competenze in un elenco;
- non presentare una trasferta come un trasferimento di residenza: se l'annuncio
  richiede spostamenti periodici, chiamali trasferte;
- 140-180 parole, saluto iniziale e firma col nome fornito, se presente;
- scrivi TUTTO in {lingua}; restano invariati soltanto nomi propri, titolo
  ufficiale del ruolo e nomi di tecnologie o certificazioni. Se la lingua è
  italiana, traduci anche le competenze: «gestione del cambiamento», non
  «change management»; «miglioramento dei processi», non «process improvement»;
  «interlocutori aziendali», non «business stakeholder».

Rispondi SOLO con JSON: {{"lettera": "testo completo"}}.
"""

_FORMULE_GENERICHE = re.compile(
    r"\b(?:la mia esperienza include|my experience includes|"
    r"mi experiencia incluye|mon expérience comprend)\b", re.IGNORECASE)
_ANGLOCISMI = re.compile(
    r"\b(?:business stakeholders?|change management|process improvement|"
    r"change impact assessment|continuous improvement|workforce planning|"
    r"talent management|stakeholder management|people management|service delivery)\b",
    re.IGNORECASE)
_PAROLE = {
    "Italian": ("il", "lo", "la", "di", "del", "della", "che", "per", "con",
                "una", "un", "mio", "mia", "questo", "questa", "gentile", "grazie"),
    "English": ("the", "and", "with", "for", "this", "your", "my", "you",
                "dear", "would", "experience", "role", "team", "thank"),
    "Spanish": ("el", "la", "de", "del", "que", "para", "con", "una", "un",
                "mi", "esta", "este", "estimado", "gracias", "experiencia"),
}


def _pulisci(valore) -> str:
    return re.sub(r"\s+", " ", str(valore or "")).strip()


def profilo_per_lettera(profilo: dict, estrazione: dict | None,
                        nome: str | None = None) -> str:
    estrazione = estrazione if isinstance(estrazione, dict) else {}
    blocchi = [profilo_come_testo(profilo)]
    if _pulisci(nome):
        blocchi.append(f"Nome per la firma: {_pulisci(nome)}")
    if _pulisci(estrazione.get("headline")):
        blocchi.append(f"Presentazione professionale nel CV: {_pulisci(estrazione['headline'])}")
    incarichi = []
    for ruolo in (estrazione.get("roles") or [])[:8]:
        if not isinstance(ruolo, dict):
            continue
        riga = " · ".join(_pulisci(ruolo.get(chiave)) for chiave in
                          ("title", "employer", "period") if _pulisci(ruolo.get(chiave)))
        if riga:
            incarichi.append(riga)
    if incarichi:
        blocchi.append("Incarichi dichiarati nel CV:\n" + "\n".join(incarichi))
    certificazioni = [_pulisci(x) for x in (estrazione.get("certifications") or [])[:8]
                      if _pulisci(x)]
    if certificazioni:
        blocchi.append("Certificazioni dichiarate nel CV: " + ", ".join(certificazioni))
    formazione = []
    for titolo in (estrazione.get("education") or [])[:4]:
        if not isinstance(titolo, dict):
            continue
        riga = " · ".join(_pulisci(titolo.get(chiave)) for chiave in
                          ("title", "school") if _pulisci(titolo.get(chiave)))
        if riga:
            formazione.append(riga)
    if formazione:
        blocchi.append("Formazione dichiarata nel CV:\n" + "\n".join(formazione))
    return "\n\n".join(blocchi)[:8000]


def _lingua_coerente(testo: str, lingua: str) -> bool:
    parole = _PAROLE.get(lingua)
    if not parole:
        return True
    minuscolo = testo.casefold()
    conteggio = lambda voci: sum(len(re.findall(rf"\b{re.escape(voce)}\b", minuscolo))
                                 for voce in voci)
    proprie = conteggio(parole)
    if proprie < 5:
        return False
    if lingua != "English":
        inglese = conteggio(_PAROLE["English"])
        if inglese > max(3, int(proprie * 0.65)):
            return False
    return True


def _normalizza_riferimento(valore) -> str:
    return _pulisci(re.sub(r"[^\w]+", " ", str(valore or "").casefold()))


def _riferimenti_presenti(testo: str, offerta: dict) -> bool:
    normalizzato = _normalizza_riferimento(testo)
    titolo = _pulisci(offerta.get("title"))
    principale = re.split(r"\s+(?:-|—|\|)\s+", titolo, maxsplit=1)[0]
    varianti_titolo = {titolo, principale}
    if titolo and not any(_normalizza_riferimento(v) in normalizzato for v in varianti_titolo):
        escluse = {"di", "e", "del", "della", "il", "la", "lo", "per", "the",
                   "and", "of", "de", "des", "und", "der", "die", "en"}
        distintive = set(_normalizza_riferimento(principale).split()) - escluse
        presenti = sum(voce in set(normalizzato.split()) for voce in distintive)
        if len(distintive) < 3 or presenti < (3 * len(distintive) + 3) // 4:
            return False
    azienda = _pulisci(offerta.get("organization"))
    varianti_azienda = {azienda, re.split(r"\s+(?:-|—|\|)\s+", azienda, maxsplit=1)[0]}
    for valore in varianti_azienda:
        normalizzata = _normalizza_riferimento(valore)
        normalizzata = re.sub(
            r"\s+(?:s p a|s r l|gmbh|ltd|llc|inc|limited|ag|bv|b v|s a s|s a)$",
            "", normalizzata).strip()
        if normalizzata and normalizzata in normalizzato:
            return True
    return not azienda


def _errore_lettera(testo: str, offerta: dict, nome: str | None,
                    lingua: str) -> str | None:
    parole = re.findall(r"\b[\w’'-]+\b", testo, re.UNICODE)
    if not 110 <= len(parole) <= 210:
        return f"aveva {len(parole)} parole invece di 140-180"
    if _FORMULE_GENERICHE.search(testo):
        return "usava una formula generica vietata"
    anglicismo = _ANGLOCISMI.search(testo) if lingua == "Italian" else None
    if anglicismo:
        return f"conteneva l'anglicismo evitabile «{anglicismo.group(0)}»: traducilo"
    if lingua == "Italian" and re.search(
            r"\btrasferiment\w*\s+periodic\w*\b", testo, re.IGNORECASE):
        return "chiamava trasferimento quella che nell'annuncio è una trasferta"
    if any(re.match(r"^\s*[-*•]", riga) for riga in testo.splitlines()):
        return "conteneva un elenco invece di una lettera discorsiva"
    if not _riferimenti_presenti(testo, offerta):
        return ("non citava in modo riconoscibile il ruolo e l'azienda: nel testo "
                f"devi includere il ruolo «{_pulisci(offerta.get('title'))}» e "
                f"l'azienda «{_pulisci(offerta.get('organization'))}», anche con "
                "una variante naturale ma riconoscibile")
    if _pulisci(nome) and _pulisci(nome).casefold() not in testo[-120:].casefold():
        return "non riportava la firma fornita"
    if not _lingua_coerente(testo, lingua):
        return f"non era scritta interamente in {lingua}"
    return None


def _lettera_valida(testo: str, offerta: dict, nome: str | None,
                    lingua: str) -> bool:
    return _errore_lettera(testo, offerta, nome, lingua) is None


_VERIFICA_FEDELTA = """Sei un revisore severo di lettere di presentazione.

LETTERA
{lettera}

PROFILO DEL CANDIDATO (unica fonte sul suo passato)
{profilo}

OFFERTA (fonte solo su ruolo e attività richieste)
{offerta}

La lettera attribuisce al candidato un'attività, un'esperienza, una responsabilità,
un risultato o un ambito NON presente nel profilo? Controlla anche affermazioni
implicite come «trova riscontro nel mio percorso», «ho maturato» o «ho operato»:
un'attività richiesta dall'offerta NON diventa un'esperienza del candidato.
Non segnalare contributi futuri o intenzioni formulate chiaramente al condizionale.
Segnala anche se una trasferta viene presentata come un trasferimento di residenza.

Rispondi SOLO con JSON: {{"inventato": true}} oppure {{"inventato": false}}.
"""


def _lettera_non_fedele(modello: ChatModel, testo: str, profilo_testo: str,
                        offerta: dict) -> bool:
    offerta_testo = offerta_per_prompt(offerta)
    offerta_testo += "\n\nTESTO DELL'ANNUNCIO\n" + _pulisci(
        offerta.get("ai_core_responsibilities"))[:6000]
    try:
        risposta = modello.chat([{"role": "user", "content": _VERIFICA_FEDELTA.format(
            lettera=testo, profilo=profilo_testo, offerta=offerta_testo)}], max_tokens=80)
        return _estrai_json(risposta).get("inventato") is not False
    except (httpx.HTTPError, RuntimeError, ValueError, TypeError) as exc:
        log.warning("verificatore della lettera non disponibile: %s", type(exc).__name__)
        return True


def genera_lettera(modello: ChatModel, profilo_testo: str, offerta: dict, *,
                   nome: str | None = None, lingua: str = "English") -> str:
    nota = ""
    for tentativo in range(2):
        descrizione = _pulisci(offerta.get("ai_core_responsibilities"))[:6000]
        requisiti = _pulisci(offerta.get("ai_requirements_summary"))[:3000]
        coda = offerta_per_prompt(offerta)
        coda += f"\n\nTESTO DELL'ANNUNCIO\n{descrizione}"
        if requisiti:
            coda += f"\n\nREQUISITI\n{requisiti}"
        coda += f"\n\nSCRIVI LA LETTERA ESCLUSIVAMENTE IN {lingua.upper()}."
        if nota:
            coda += f"\n\nLA VERSIONE PRECEDENTE È STATA RIFIUTATA: {nota}"
        risposta = modello.chat([
            {"role": "system", "content": _RUBRICA.format(lingua=lingua)},
            {"role": "system", "content": "PROFILO DEL CANDIDATO\n" + profilo_testo},
            {"role": "user", "content": coda},
        ], max_tokens=750, temperature=0.35)
        try:
            testo = str(_estrai_json(risposta).get("lettera") or "").strip()
        except (TypeError, ValueError) as exc:
            raise RispostaModelloNonValida(
                f"{modello.model}: JSON della lettera non valido") from exc
        errore = _errore_lettera(testo, offerta, nome, lingua)
        if errore:
            nota = errore
            log.warning("lettera rifiutata (%s): %s", offerta.get("id"), errore)
            continue
        if _analisi_inventata(modello, "", {"perche": testo}, profilo_testo, offerta):
            nota = "conteneva fatti non presenti nel profilo o nell'annuncio"
            log.warning("lettera rifiutata dal verificatore (%s)", offerta.get("id"))
            continue
        if _lettera_non_fedele(modello, testo, profilo_testo, offerta):
            nota = ("attribuiva al passato del candidato attività dell'annuncio "
                    "non documentate dal profilo: distingui ciò che ha fatto da "
                    "ciò che potrà fare")
            log.warning("lettera rifiutata dal controllo specifico (%s)", offerta.get("id"))
            continue
        return testo
    raise RispostaModelloNonValida(
        f"{modello.model}: lettera rifiutata dai controlli di qualità")
