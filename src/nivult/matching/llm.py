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
    model = "glm-5.2"
    env_key = "GLM_API_KEY"

    # Proprietà e non attributo di classe: l'attributo veniva valutato
    # ALL'IMPORT, prima che load_dotenv() leggesse il .env, e un override di
    # GLM_BASE_URL lì dentro era ignorato in silenzio.
    @property
    def base_url(self) -> str:
        return os.environ.get("GLM_BASE_URL", "https://api.z.ai/api/paas/v4")

    def chat(self, messages, **kw):
        # Thinking OFF: su una valutazione a rubrica il ragionamento esteso
        # moltiplica i token di output senza cambiare il punteggio.
        extra = kw.pop("extra", {}) or {}
        extra.setdefault("thinking", {"type": "disabled"})
        return super().chat(messages, extra=extra, **kw)


class MistralSmall(ChatModel):
    source = "mistral"
    model = "mistral-small-latest"
    env_key = "MISTRAL_API_KEY"

    @property
    def base_url(self) -> str:
        return os.environ.get("MISTRAL_BASE_URL", "https://api.mistral.ai/v1")


RUBRICA = """Sei un selezionatore esperto. Valuta se QUESTA offerta merita il
tempo di QUESTO candidato: potrebbe farla, e sarebbe un passo sensato per lui?

Punteggio da 0 a 100:
  90-100  ruolo e livello centrati: e' esattamente il tipo di posizione che cerca
  70-89   buona: il ruolo e' giusto, con qualche scarto di livello o di ambito
  50-69   plausibile: settore o livello divergono, ma il passaggio e' credibile
  20-49   debole: solo affinita' generiche
   0-19   non pertinente

REGOLA FONDAMENTALE — cio' che l'annuncio NON dice non conta contro di esso.
Gli annunci sono scritti in modi diversissimi: uno elenca dieci strumenti, un
altro dice solo "HR Business Partner, Milano". Il secondo non e' una
corrispondenza peggiore, e' solo scritto piu' corto. Non abbassare mai il
punteggio perche' mancano le competenze specialistiche del candidato: se il
ruolo e il livello sono giusti, quelle competenze sono un VANTAGGIO che il
candidato porta, non un requisito che l'annuncio ha mancato.

Abbassa il punteggio solo per cio' che l'annuncio DICE e che diverge: un
mestiere diverso, un livello lontano, una sede o una lingua incompatibili.

Non premiare un'offerta perche' prestigiosa o ben scritta: conta solo
l'aderenza al profilo.

Rispondi SOLO con questo JSON, niente altro:
{{"score": <0-100>, "reason": "<one sentence in {lingua}, max 10 words>"}}

"""

# Seconda passata: la motivazione che il destinatario del digest legge. Corre
# solo sulle offerte che entrano nel digest (le prime 30), non su tutte: è il
# risparmio progettato nelle decisioni di architettura.
# La lingua è un parametro, non una costante: quella frase E' il prodotto, e
# arriva a utenti in nove lingue. Si genera direttamente nella loro — costa
# un'istruzione, non una seconda chiamata — perché tradotta a valle suonerebbe
# tradotta. Prima diceva "in italiano" per tutti, tedeschi compresi.
RUBRICA_ANALISI = """Sei un selezionatore esperto che spiega un match al
candidato. Rispondi SOLO con JSON:
{{"perche": "...", "pros": ["..."], "cons": ["..."],
  "responsabilita": "...", "requisiti": "...", "benefit": "..."}}
- perche: UNA frase (max 25 parole) che dice perché questa offerta è
  arrivata proprio a questo candidato.
- pros: da 2 a 4 punti in cui il candidato combacia con QUESTA offerta.
  Fatti presi dal profilo e dall'offerta, mai lodi generiche.
- cons: da 1 a 3 punti su cui il colloquio farà domande (requisiti
  scoperti, lingua, sede, livello). Onesti, mai scoraggianti.
- responsabilita, requisiti, benefit: il contenuto dei blocchi omonimi
  dell'offerta, reso fedele e compatto. null se il blocco manca.
- TUTTO in {lingua}, ogni frase breve. Dai del tu.
"""

RUBRICA_MOTIVAZIONE = """Sei un selezionatore esperto. Spiega in UNA frase di
massimo 25 parole, scritta in {lingua}, perché questa offerta è adatta al
profilo del candidato. Sii concreto: ruolo, competenze, livello. Niente
genericità.

Rispondi SOLO con questo JSON, niente altro:
{{"reason": "<la frase>"}}

"""


RUBRICA_CONSEGNA = """Sei un selezionatore esperto che consegna un'offerta
al candidato giusto. Rispondi SOLO con questo JSON:
{{"reason": "...", "perche": "...", "pros": ["..."], "cons": ["..."],
  "responsabilita": "...", "requisiti": "...", "benefit": "..."}}
- reason: la riga che il lettore vede NEL DIGEST. Massimo 25 parole, in
  DUE frasi brevi: la prima dice cosa chiede QUESTA offerta, la seconda
  cosa, nel suo percorso, la soddisfa. Dagli del TU: stai scrivendo a lui,
  non a un selezionatore che valuta una pratica.
  LA FORMA LA SCEGLIE IL CONTENUTO, non il caso. Ogni motivazione nasce
  da una chiamata separata: il modello non sa cosa ha scritto per le altre
  offerte dello stesso digest, quindi «varia» non è un'istruzione che può
  eseguire. Sceglila invece così, e l'alternanza viene dai dati:
    · l'offerta pone una CONDIZIONE netta (patente, certificazione,
      lingua obbligatoria) → parti da quella:
      «L'ADR è obbligatorio e ce l'hai. L'aggiornamento lo pagano loro.»
    · la sovrapposizione è una COMPETENZA precisa che torna nel suo
      percorso → parti da quella:
      «Montaggio quadri e ricerca guasti: le due cose su cui si reggono
      i tuoi ultimi tre lavori.»
    · negli altri casi → parti dal requisito:
      «Vogliono qualcuno che abbia gestito i turni. Tu lo hai fatto per
      quattro anni.»

  ⚠ Gli esempi qui sopra sono in italiano SOLO per mostrare la forma. La
  motivazione va scritta in {lingua}, sempre: copiare la lingua degli
  esempi manderebbe una riga italiana a un lettore inglese.
  VIETATO, e sono i difetti misurati sui digest veri:
    · cominciare con «Questo ruolo…» o col titolo dell'offerta — nove
      motivazioni su nove avevano lo stesso identico attacco, e il lettore
      ne vede tre di fila in un solo messaggio;
    · parlare di lui in terza persona («il candidato», «il suo profilo»);
    · rielencare le sue competenze separate da virgole: le conosce già,
      è venuto a sapere cosa chiede QUESTO annuncio;
    · superlativi — «perfetto», «ideale», «eccellente». La soglia è 80 su
      100, non la perfezione, e su un punteggio di 85 «combacia
      perfettamente» è una bugia piccola ma è una bugia.
  Nessun fatto inventato: solo ciò che sta nell'offerta e nel profilo.
- perche: la stessa cosa distesa, per la finestra di dettaglio del
  pannello: due o tre frasi, sempre dandogli del tu, con un dettaglio in
  più preso dall'offerta che nel digest non c'è entrato.
- pros: da 2 a 4 punti in cui il candidato combacia con QUESTA offerta.
  Fatti presi dal profilo e dall'offerta, mai lodi generiche.
- cons: da 1 a 3 punti su cui il colloquio farà domande (requisiti
  scoperti, lingua, sede, livello). Onesti, mai scoraggianti.
- responsabilita, requisiti, benefit: il contenuto dei blocchi omonimi
  dell'offerta, reso fedele e compatto. null se il blocco manca.
- TUTTO in {lingua}, ogni frase breve.
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


def _coda_offerta(offerta: dict, desiderio: str | None) -> str:
    """Il messaggio dell'offerta, con in fondo le parole dell'utente.

    Il desiderio va QUI e non nel prefisso, ed e' una scelta di costo prima
    che di prompt: il prefisso (CV + rubrica) e' identico a ogni chiamata ed
    e' quello che la cache paga. Cambiarlo per cluster manderebbe la cache a
    vuoto ogni volta che il worker passa da una ricerca all'altra.

    Sta dopo l'offerta anche per una ragione di lettura: il modello prima
    guarda cosa c'e', poi cosa ne pensa chi cerca.
    """
    testo = "OFFERTA\n" + offerta_come_testo(offerta)
    if desiderio:
        testo += ("\n\nCOSA CERCA QUESTA PERSONA, CON PAROLE SUE\n"
                  + desiderio.strip()[:1000]
                  + "\n\nPesalo come una preferenza forte, non come un "
                    "requisito: un'offerta ottima che non la nomina resta "
                    "ottima.")
    return testo


def valuta_offerta(modello: ChatModel, profilo_testo: str, offerta: dict,
                   desiderio: str | None = None,
                   lingua: str = "English") -> tuple[int, str, dict]:
    """UNA offerta per chiamata: punteggio e micro-motivazione.

    Nel lotto il modello confronta le offerte fra loro invece di misurarle
    contro il CV, e distribuisce i voti sulla scala del lotto: è misurato, ed
    è il motivo di questa firma. La micro-motivazione c'è perché
    matches.reason è NOT NULL: lo scarto dev'essere spiegato anche lui, non
    solo ciò che passa.
    """
    corpo = _testa(profilo_testo, RUBRICA.format(lingua=lingua)) + [{
        "role": "user", "content": _coda_offerta(offerta, desiderio)}]
    risposta = modello.chat(corpo, max_tokens=120)
    p = _estrai_json(risposta)
    score = max(0, min(100, int(p.get("score", 0))))
    reason = str(p.get("reason") or "")[:400].strip() or "—"
    return score, reason, dict(modello.last_usage)


def _coda_analisi(offerta: dict, desiderio: str | None) -> str:
    """La coda per le chiamate che riscrivono l'annuncio: la coda standard
    piu' i blocchi che il punteggio non usa ma il lettore si'."""
    def _blocco(v) -> str:
        if isinstance(v, list):
            v = " · ".join(str(x).strip() for x in v if str(x).strip())
        return str(v or "").strip()

    extra = []
    for etichetta, chiave in (("RESPONSABILITÀ", "ai_core_responsibilities"),
                              ("BENEFIT", "ai_benefits"),
                              ("ORARIO", "ai_working_hours")):
        val = _blocco(offerta.get(chiave))
        if val:
            extra.append(f"{etichetta}\n{val[:900]}")
    coda = _coda_offerta(offerta, desiderio)
    if extra:
        coda += "\n\n" + "\n\n".join(extra)
    return coda


def _campi_analisi(p: dict, lingua: str) -> dict:
    pros = [str(x)[:160] for x in (p.get("pros") or []) if str(x).strip()][:4]
    cons = [str(x)[:160] for x in (p.get("cons") or []) if str(x).strip()][:3]
    testo = lambda k, tetto: (str(p.get(k) or "").strip()[:tetto] or None)
    return {"perche": testo("perche", 300), "pros": pros, "cons": cons,
            "responsabilita": testo("responsabilita", 700),
            "requisiti": testo("requisiti", 700),
            "benefit": testo("benefit", 500), "lang": lingua}


def motiva_e_analizza(modello: ChatModel, profilo_testo: str, offerta: dict,
                      desiderio: str | None = None,
                      lingua: str = "English") -> tuple[str, dict, dict]:
    """Seconda passata di consegna: motivazione E analisi in UNA chiamata.

    Nasce dal conto della serva: la finestra di dettaglio del pannello
    chiamava GLM al primo clic, a cache fredda, ripagando tutto il
    prefisso. Qui il prefisso e' gia' caldo (stesso profilo della
    valutazione appena fatta) e la chiamata esisteva comunque per la
    motivazione: l'analisi costa solo l'output in piu'.
    """
    corpo = _testa(profilo_testo, RUBRICA_CONSEGNA.format(lingua=lingua)) + [{
        "role": "user", "content": _coda_analisi(offerta, desiderio)}]
    risposta = modello.chat(corpo, max_tokens=900)
    p = _estrai_json(risposta)
    reason = str(p.get("reason") or "")[:400].strip() or "—"
    return reason, _campi_analisi(p, lingua), dict(modello.last_usage)


def analizza_allineamento(modello: ChatModel, profilo_testo: str,
                          offerta: dict, desiderio: str | None = None,
                          lingua: str = "English") -> dict:
    """Pro e attenzioni del match, per la finestra di dettaglio del pannello.

    Stessa testa in cache delle altre chiamate (profilo + rubrica), stessa
    coda; il tetto e' piu' alto perche' qui si scrivono righe, non una.
    """
    corpo = _testa(profilo_testo, RUBRICA_ANALISI.format(lingua=lingua)) + [{
        "role": "user", "content": _coda_analisi(offerta, desiderio)}]
    risposta = modello.chat(corpo, max_tokens=900)
    return _campi_analisi(_estrai_json(risposta), lingua)


def motiva_offerta(modello: ChatModel, profilo_testo: str, offerta: dict,
                   desiderio: str | None = None,
                   lingua: str = "English") -> tuple[str, dict]:
    """Seconda passata: la motivazione che il destinatario del digest legge."""
    corpo = _testa(profilo_testo, RUBRICA_MOTIVAZIONE.format(lingua=lingua)) + [{
        "role": "user", "content": _coda_offerta(offerta, desiderio)}]
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
