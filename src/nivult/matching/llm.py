"""Client OpenAI-compatibili per valutazione, motivazione e analisi.

Il worker sceglie soltanto gli endpoint configurati e registra modello, provider
e uso reale di ogni chiamata. Le risposte di score e tutti i campi dell'analisi
sono validati prima di poter alimentare un match.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass

from nivult.ingestion.base import HttpSource

log = logging.getLogger("nivult.matching.llm")


class RispostaModelloNonValida(RuntimeError):
    """Risposta HTTP riuscita ma contenuto inutilizzabile: si può ripiegare."""

# La versione delle rubriche di punteggio. SI ALZA A MANO, e si alza ogni
# volta che una modifica sposta i punteggi: soglie, regole di valutazione,
# pesi. Non per una correzione di refuso.
#
# Finisce in `matches.rubric_version` e risponde a una domanda che prima
# non aveva risposta: con quale metro e' stato misurato QUESTO candidato
# contro QUESTA offerta. Il 30/08/2026 la rubrica e' cambiata due volte in
# un giorno, e senza questa stringa i punteggi di ieri e quelli di oggi
# sembrano confrontabili mentre non lo sono.
VERSIONE_RUBRICA = "2026-11-02.r1"
VERSIONE_ANALISI = "2026-11-02.a2"


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

    @classmethod
    def disponibile(cls) -> bool:
        return bool(os.environ.get(cls.env_key))

    def __init__(self, api_key: str | None = None, **kw):
        super().__init__(rate_per_second=kw.pop("rate_per_second", 2.0),
                         timeout=kw.pop("timeout", 120.0), **kw)
        self.api_key = api_key or os.environ.get(self.env_key, "")
        if not self.api_key:
            raise SystemExit(f"Serve {self.env_key}.")
        self.input_tokens = 0
        self.output_tokens = 0
        self.last_usage: dict = {}

    def chat(self, messages: list[dict], *, temperature: float = 0.0,
             max_tokens: int = 4000, extra: dict | None = None) -> str:
        self.last_usage = {}
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
        try:
            d = r.json()
        except (TypeError, ValueError) as exc:
            raise RispostaModelloNonValida(
                f"{self.model}: risposta non JSON") from exc
        uso = d.get("usage") or {}
        dettagli = uso.get("prompt_tokens_details") or {}
        inp = uso.get("prompt_tokens")
        out = uso.get("completion_tokens")
        cached = dettagli.get("cached_tokens", 0)
        validi = all(isinstance(v, int) and not isinstance(v, bool) and v >= 0
                     for v in (inp, out, cached))
        self.input_tokens += inp if isinstance(inp, int) else 0
        self.output_tokens += out if isinstance(out, int) else 0
        self.last_usage = {"input": inp or 0, "cached": cached or 0,
                           "output": out or 0, "complete": validi}
        try:
            msg = d["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RispostaModelloNonValida(
                f"{self.model}: risposta senza choices[0].message") from exc
        contenuto = msg.get("content")
        if contenuto is None:
            contenuto = msg.get("reasoning_content")
        if contenuto is None:
            raise RispostaModelloNonValida(
                f"{self.model}: risposta senza content "
                f"(finish={d['choices'][0].get('finish_reason')})")
        return contenuto


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


class Groq(ChatModel):
    source = "groq"
    model = "qwen/qwen3.8-27b"          # multilingua pulito, JSON al primo colpo
    env_key = "GROQ_API_KEY"

    @property
    def base_url(self) -> str:
        return os.environ.get("GROQ_BASE_URL",
                              "https://api.groq.com/openai/v1")


class MaestroLocale(ChatModel):
    """Il giudice in casa: llama.cpp sull'N5, via Tailscale (12/10/2026).

    Misurato contro GLM sulle 1.454 coppie vere: rho 0.73, AUC 0.94.
    Nasce per il digest gratis: GLM costa e il credito puo' stare a zero.
    Niente chiave da pagare: la base di ChatModel la esige, qui e' finta
    di proposito — l'autenticazione e' la rete (solo Tailscale)."""
    source = "maestro-locale"
    model = "qwen3-4b-locale"
    env_key = "MAESTRO_DUMMY"           # mai letta davvero: vedi __init__

    def __init__(self, **kw):
        # la base esige una chiave non vuota: quella locale e' finta di
        # proposito — l'autenticazione e' la rete (solo Tailscale)
        os.environ.setdefault("MAESTRO_DUMMY", "locale")
        super().__init__(**kw)

    @property
    def base_url(self) -> str:
        return os.environ.get("MAESTRO_URL", "")

    @classmethod
    def disponibile(cls) -> bool:
        return bool(os.environ.get("MAESTRO_URL"))


class Scrittore(ChatModel):
    """Chi SCRIVE le frasi del digest (14/10/2026).

    Separato dal giudice: il punteggio resta sul 4B misurato (rho 0.73),
    la prosa va al modello piu' grande di casa (Qwen3.6-35B-A3B, MoE,
    porta 8091). Il 4B giudicava bene ma scriveva male: 18 ripieghi su 30
    nel digest vero, e le 12 passate si somigliavano tutte. Giudicare e
    scrivere sono due mestieri; un modello solo li faceva entrambi male a
    meta'."""
    source = "scrittore-locale"
    model = "qwen3.6-35b-locale"
    env_key = "SCRITTORE_DUMMY"

    def __init__(self, **kw):
        os.environ.setdefault("SCRITTORE_DUMMY", "locale")
        super().__init__(**kw)

    @property
    def base_url(self) -> str:
        return os.environ.get("SCRITTORE_URL", "")

    @classmethod
    def disponibile(cls) -> bool:
        return bool(os.environ.get("SCRITTORE_URL"))

    def chat(self, messages, *, temperature=0.0, max_tokens=4000,
             extra=None):
        # Qwen3.6 PENSA di default: col pensiero acceso il budget di token
        # lo mangia il ragionamento e content torna vuoto (misurato il
        # 14/10: il digest intero al ripiego per risposte vuote). Per
        # scrivere JSON su rubrica il pensiero non serve: si spegne, come
        # gia' per nemotron.
        payload_extra = dict(extra or {})
        payload_extra["chat_template_kwargs"] = {"enable_thinking": False}
        return super().chat(messages, temperature=temperature,
                            max_tokens=max_tokens, extra=payload_extra)


class Nvidia(ChatModel):
    source = "nvidia"
    # solo nemotron-3-super risponde pulito su questo account (gli altri
    # danno 404 "not found for account"): un membro in piu', non sei
    model = "nvidia/nemotron-3-super-120b-a12b"
    env_key = "NVIDIA_API_KEY"

    @property
    def base_url(self) -> str:
        return os.environ.get("NVIDIA_BASE_URL",
                              "https://integrate.api.nvidia.com/v1")

    def chat(self, messages, *, temperature=0.0, max_tokens=4000,
             extra=None):
        # nemotron e' un modello che RAGIONA: col pensiero acceso il
        # budget di token lo mangia il reasoning e content torna None
        # (10/10/2026: digest morto su «'NoneType' has no strip»). Per
        # l'estrazione JSON il pensiero non serve: si spegne.
        payload_extra = dict(extra or {})
        payload_extra["chat_template_kwargs"] = {"enable_thinking": False}
        return super().chat(messages, temperature=temperature,
                            max_tokens=max_tokens, extra=payload_extra)


class PoolModello:
    """Un client unico che ruota su piu' modelli GRATUITI: quando uno e'
    saturo (429) passa al successivo, cosi' la somma delle quote gratuite
    da' throughput da tier a pagamento. Espone .chat() identico a
    ChatModel, quindi i passi esistenti non cambiano.

    Ogni provider ha un 'disponibile_da': un 429 lo mette in pausa breve
    e il giro prosegue sugli altri. Se sono tutti in pausa si aspetta il
    piu' vicino. NON e' la catena del matching del digest, che ha propri
    contatori per modello: e' per l'arricchimento ad alto volume."""

    # (classe, modello, pausa dopo un 429 in secondi). Ordine = preferenza:
    # prima i piu' generosi/puliti, Mistral per ultimo (free tier severo).
    _RICETTA = [
        (Groq, "qwen/qwen3.8-27b", 3.0),
        (Nvidia, "nvidia/nemotron-3-super-120b-a12b", 120.0),
        ("GLM_FLASH", "glm-4.5-flash", 2.0),
        (MistralSmall, "mistral-small-latest", 8.0),
    ]

    def __init__(self):
        import time as _t
        self._t = _t
        self.membri = []           # [{"m":client,"pausa":s,"pronto":ts,"nome":str}]
        for classe, modello, pausa in self._RICETTA:
            try:
                if classe == "GLM_FLASH":
                    m = GLM(max_retries=1)
                    m.model = modello
                else:
                    m = classe(max_retries=1)
                    m.model = modello
            except SystemExit:
                continue           # chiave assente: si salta, non e' un errore
            m.fail_fast = True     # il 429 rimbalza subito: il pool ruota
            self.membri.append({"m": m, "pausa": pausa,
                                "pronto": 0.0, "nome": m.source})
        if not self.membri:
            raise SystemExit("nessun modello gratuito configurato")
        self._giro = 0

    def quanti(self) -> int:
        return len(self.membri)

    def chat(self, messages, **kw) -> str:
        n = len(self.membri)
        ultimo_errore = None
        for _ in range(n * 2):
            adesso = self._t.monotonic()
            # scelta a rotazione, saltando chi e' in pausa
            scelto = None
            for k in range(n):
                cand = self.membri[(self._giro + k) % n]
                if cand["pronto"] <= adesso:
                    scelto = cand
                    self._giro = (self._giro + k + 1) % n
                    break
            if scelto is None:
                # tutti in pausa: aspetta il piu' vicino
                attesa = min(x["pronto"] for x in self.membri) - adesso
                if attesa > 0:
                    self._t.sleep(min(attesa, 10))
                continue
            try:
                return scelto["m"].chat(messages, **kw)
            except RuntimeError as e:
                ultimo_errore = e
                testo = str(e)
                # 429 / rate / quota: metti in pausa questo, prova un altro
                if "429" in testo or "rate" in testo.lower() \
                        or "quota" in testo.lower() or "1300" in testo:
                    scelto["pronto"] = self._t.monotonic() + scelto["pausa"]
                    continue
                # errore diverso (modello, rete): pausa breve e prova altrove
                scelto["pronto"] = self._t.monotonic() + 1.0
                continue
        raise RuntimeError(f"pool: tutti i modelli falliti ({ultimo_errore})")


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
mestiere diverso, un livello lontano, una lingua che il candidato non parla.

SEDE E PAESE. La ricerca del candidato su un paese viene prima: se l'offerta
e' in quel paese, una citta' diversa nello stesso paese NON e' un gap e non
abbassa il punteggio. Se il CV dichiara citta' e paese, usa quei dati: non
segnalare MAI come problema una sede che coincide con citta' e paese del CV.
Se il paese dell'offerta e' diverso da quello della ricerca o dal paese
dichiarato dal CV, valuta il cambio reale: la disponibilita' al trasferimento
lo rende una sfida gestibile, non una ragione automatica per scartare; una
sede on-site in un altro paese resta pero' un vincolo concreto. Non inventare
residenza, visto o disponibilita' a trasferirsi.

IL RUOLO A CUI PUNTA viene PRIMA della sua storia, quando c'e': dice cosa
vuole fare adesso, non cosa ha gia' fatto. E se nomina una FAMIGLIA
professionale intera invece di un ruolo preciso — «Risorse umane»,
«Ingegneria», «Finanza» — sta dicendo che gli va bene QUALUNQUE ruolo di
quella famiglia al livello giusto. In quel caso non restringere alla
specializzazione che leggi nel CV: una ricerca larga e' una scelta, non una
distrazione.

ANCHE LA PROFESSIONE E' GIA' SCELTA. La ricerca e' su una famiglia
professionale intera: dentro quella famiglia, una specializzazione diversa da
quella del CV e' un passaggio laterale credibile — fascia 50-69, non 20-49.
Un consulente di trasformazione HR che guarda una posizione di recruiting o
di payroll resta nel suo mestiere: cambia il taglio, non il campo.

Non premiare un'offerta perche' prestigiosa o ben scritta: conta solo
l'aderenza al profilo.

MAI attribuire al candidato un requisito dell'OFFERTA che il profilo non
ha. Se l'annuncio chiede il cinese e il suo profilo non ce l'ha, il motivo
e' «chiedono il cinese» — non «parli cinese». Misurato sul digest del
13/10/2026: il modello aveva regalato al candidato la patente A e il
cinese perche' l'offerta li nominava.

Rispondi SOLO con questo JSON, niente altro:
{{"score": <0-100>, "reason": "<one sentence in {lingua}, max 10 words>"}}

Quando {lingua} = Italian, la frase deve essere italiano naturale da prodotto
professionale, non una traduzione letterale. Non usare «combacia»,
«profilatura», «passaggio laterale plausibile» o «punti da presidiare».
Scrivi invece «è in linea», «cambiamento di ruolo credibile», «aspetti da
chiarire». Per CBA o Collective Bargaining Agreement usa «contrattazione
collettiva». I titoli di lavoro restano nella lingua originale; le attività si
spiegano con verbi concreti.
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
- perche: UNA frase (max 30 parole) che spiega il fattore decisivo del
  punteggio: ruolo, livello o requisito specifico. Evita formule standard come
  «livello operativo» senza il perché, generalizzazioni e semplice ripetizione
  del titolo o dell'azienda.
- pros: da 2 a 4 punti in cui il candidato è in linea con QUESTA offerta.
  Fatti presi dal profilo e dall'offerta, mai lodi generiche.
- cons: da 0 a 3 divergenze concrete tra profilo e offerta che il colloquio
  potrebbe approfondire. Se non ce ne sono, restituisci []: non inventare
  un requisito o un problema di sede per riempire il campo.
- SEDE: confronta sempre città E paese. «Rome, US» non è «Roma, IT». Se
  coincidono, non citare la sede fra i cons. Se il paese non è noto, non
  dedurlo dal solo nome della città. La disponibilità al trasferimento non
  elimina il cambio di paese, ma lo rende una scelta già dichiarata.
- responsabilita: massimo 3 frasi sulle attività concrete che la persona
  svolgerà, usando i verbi dell'annuncio. Non ripetere datore, sede o employer
  branding: quelli non sono mansioni. Non limitarti al ruolo generico.
- requisiti, benefit: il contenuto dei blocchi omonimi dell'offerta, reso
  fedele e compatto. null se il blocco manca.
- TUTTO in {lingua}, ogni frase breve. Dai del tu.
- Se {lingua} = Italian, scrivi in italiano naturale professionale, non
  tradurre letteralmente né rendere idiomi parola per parola. Usa «punti di
  forza» e «aspetti da chiarire», mai «punti da presidiare», «combacia» o
  «profilatura». Per HR Business Partner usa una sola volta la forma «HR
  Business Partner (HRBP)». Per HR Generalist usa «ruolo HR generalista». Per
  labor relations usa «relazioni sindacali», per employee relations «relazioni
  con i dipendenti», per Permanent Placement «inserimento diretto». I titoli
  ufficiali restano invariati; le attività si descrivono con verbi concreti.
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
{{"reason": "...", "prova": "...", "perche": "...", "pros": ["..."],
  "cons": ["..."], "responsabilita": "...", "requisiti": "...",
  "benefit": "..."}}
- reason: la riga che il lettore vede NEL DIGEST. Massimo 25 parole, in
  DUE frasi brevi: la prima dice cosa chiede QUESTA offerta, la seconda
  cosa, nel suo percorso, la soddisfa. Dagli del TU: stai scrivendo a lui,
  non a un selezionatore che valuta una pratica.
  MAI attribuire al candidato un requisito dell'OFFERTA che il profilo non
  ha. Se l'annuncio chiede il cinese e il suo profilo non ce l'ha, si
  scrive «chiedono il cinese» — non «parli cinese». Un'invenzione qui
  brucia la fiducia in tutto il messaggio.
- prova: i NUMERI dei fatti del profilo su cui si regge la seconda frase,
  come lista: [3, 7]. Ogni affermazione sul candidato deve avere il suo
  numero; se la frase parla solo dell'offerta, []. Se nessun numero regge
  la frase che vorresti scrivere, quella frase non si puo' scrivere.
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
- pros: da 2 a 4 punti in cui il candidato è in linea con QUESTA offerta.
  Fatti presi dal profilo e dall'offerta, mai lodi generiche.
- cons: da 0 a 3 divergenze concrete tra profilo e offerta che il colloquio
  potrebbe approfondire. Se non ce ne sono, restituisci []: non inventare
  un requisito o un problema di sede per riempire il campo.
- SEDE: confronta sempre città E paese. «Rome, US» non è «Roma, IT». Se
  coincidono, non citare la sede fra i cons. Se il paese non è noto, non
  dedurlo dal solo nome della città. La disponibilità al trasferimento non
  elimina il cambio di paese, ma lo rende una scelta già dichiarata.
- responsabilita: massimo 3 frasi sulle attività concrete che la persona
  svolgerà, usando i verbi dell'annuncio. Non ripetere datore, sede o employer
  branding: quelli non sono mansioni. Non limitarti al ruolo generico.
- requisiti, benefit: il contenuto dei blocchi omonimi dell'offerta, reso
  fedele e compatto. null se il blocco manca.
- TUTTO in {lingua}, ogni frase breve.
- Se {lingua} = Italian, scrivi in italiano naturale professionale, non
  tradurre letteralmente né rendere idiomi parola per parola. Usa «aspetti da
  chiarire», «è in linea» e «cambiamento di ruolo credibile», non «punti da
  presidiare», «combacia» o «passaggio laterale plausibile». Per labor relations
  usa «relazioni sindacali», per employee relations «relazioni con i dipendenti»
  e per Permanent Placement «inserimento diretto». I titoli ufficiali restano
  invariati; le attività si descrivono con verbi concreti.
"""


def profilo_come_testo(profilo: dict) -> str:
    """Il profilo come elenco NUMERATO di fatti (13/10/2026).

    La numerazione e' il contratto anti-invenzione: la rubrica di consegna
    cita i fatti per numero ([3], [7]) e la verifica li controlla con un
    confronto fra interi. La copia letterale era troppo difficile per un
    modello da 4B — parafrasava, e quattordici motivazioni su quattordici
    cadevano al controllo della sottostringa."""
    fatti = [f"Ruolo cercato: {profilo.get('ruolo','—')}",
             f"Seniority: {profilo.get('seniority','—')}"]
    fatti += [f"Competenza: {c}" for c in profilo.get("competenze", [])]
    fatti += [f"Lingua: {l}" for l in profilo.get("lingue", [])]
    if profilo.get("residenza"):
        fatti.append(f"Città e paese dichiarati nel CV: {profilo['residenza']}")
    if profilo.get("sedi"):
        fatti.append(f"Sedi accettate: {', '.join(profilo['sedi'])}")
    if profilo.get("note"):
        fatti.append(f"Note: {profilo['note']}")
    return "\n".join(f"[{i + 1}] {f}" for i, f in enumerate(fatti))


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
    if job.get("countries"):
        pezzi.append(f"paese: {', '.join(job['countries'][:2])}")
    # Solo quando e' true: il false di Fantastic significa «l'annuncio non
    # ne parla», e scriverlo qui insegnerebbe al modello una bugia.
    if job.get("ai_visa_sponsorship"):
        pezzi.append("sponsorizzazione visto: dichiarata dall'annuncio")
    if job.get("ai_key_skills"):
        pezzi.append(f"competenze: {', '.join(job['ai_key_skills'][:12])}")
    if job.get("ai_requirements_summary"):
        pezzi.append(f"requisiti: {job['ai_requirements_summary'][:2000]}")
    elif job.get("ai_core_responsibilities"):
        pezzi.append(f"descrizione: {job['ai_core_responsibilities'][:2000]}")
    return " | ".join(pezzi)


def _testa(profilo_testo: str, rubrica: str) -> list[dict]:
    # Il profilo va in testa e identico a ogni chiamata: è la parte che il
    # fornitore mette in cache, e cambiarla fra un'offerta e l'altra
    # annullerebbe il risparmio (misurato: prefissi in cache al 90%).
    return [{"role": "system", "content": rubrica},
            {"role": "system", "content": "PROFILO DEL CANDIDATO\n" + profilo_testo}]


def offerta_per_prompt(offerta: dict, desiderio: str | None = None) -> str:
    """Offerta e preferenze nello stesso formato per ogni passo del matching."""
    if desiderio is None:
        desiderio = offerta.get("_wants")
    testo = "OFFERTA\n" + offerta_come_testo(offerta)
    if desiderio:
        testo += ("\n\nCOSA CERCA QUESTA PERSONA, CON PAROLE SUE\n"
                  + desiderio.strip()[:1000]
                  + "\n\nPesalo come una preferenza forte, non come un "
                    "requisito: un'offerta ottima che non la nomina resta "
                    "ottima.")
    return testo


def _coda_offerta(offerta: dict, desiderio: str | None = None) -> str:
    return offerta_per_prompt(offerta, desiderio)


def _uso_modello(modello: ChatModel) -> dict:
    uso = dict(getattr(modello, "last_usage", {}) or {})
    uso.update({"model": modello.model, "provider": modello.source})
    uso.setdefault("complete", True)
    return uso


def _accumula_uso(totale: dict, uso: dict) -> None:
    for chiave in ("input", "cached", "output"):
        valore = uso.get(chiave, 0)
        totale[chiave] = totale.get(chiave, 0) + (
            valore if isinstance(valore, (int, float))
            and not isinstance(valore, bool) else 0)
    totale["complete"] = (totale.get("complete", True)
                          and uso.get("complete", True))


def _score_valido(p: dict, modello: ChatModel) -> int:
    if "score" not in p or p["score"] is None or isinstance(p["score"], bool):
        raise RispostaModelloNonValida(
            f"{modello.model}: score assente, null o booleano")
    try:
        numero = float(p["score"])
    except (TypeError, ValueError, OverflowError) as exc:
        raise RispostaModelloNonValida(
            f"{modello.model}: score non numerico") from exc
    if not math.isfinite(numero):
        raise RispostaModelloNonValida(f"{modello.model}: score non finito")
    return max(0, min(100, int(numero)))


def valuta_offerta(modello: ChatModel, profilo_testo: str, offerta: dict,
                   desiderio: str | None = None, lingua: str = "English",
                   on_usage=None) -> tuple[int, str, dict]:
    """UNA offerta per chiamata: punteggio e micro-motivazione."""
    corpo = _testa(profilo_testo, RUBRICA.format(lingua=lingua)) + [{
        "role": "user", "content": offerta_per_prompt(offerta, desiderio)}]
    risposta = modello.chat(corpo, max_tokens=120)
    uso = _uso_modello(modello)
    if on_usage:
        on_usage(uso)
    try:
        p = _estrai_json(risposta)
    except (TypeError, ValueError) as exc:
        raise RispostaModelloNonValida(
            f"{modello.model}: JSON di valutazione non valido") from exc
    score = _score_valido(p, modello)
    reason = str(p.get("reason") or "")[:400].strip() or "—"
    return score, reason, uso


def _coda_analisi(offerta: dict, desiderio: str | None,
                  lingua: str | None = None) -> str:
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
    # La lingua torna QUI, in fondo al messaggio dell'utente, e non solo
    # nella rubrica di sistema. L'ultima cosa che il modello legge e' il
    # testo dell'annuncio, che e' quasi sempre nella lingua del paese: su
    # una richiesta in tedesco, con un annuncio di Torino, ha risposto in
    # italiano. E' un errore che non si ripara da solo, perche' `lang`
    # registra la lingua CHIESTA e non quella ottenuta: la risposta fuori
    # lingua resta in cache e la finestra non la rigenera piu'.
    if lingua:
        coda += (f"\n\nSCRIVI OGNI CAMPO IN {lingua.upper()}. L'annuncio "
                 "qui sopra e' probabilmente in un'altra lingua: traducine "
                 "il contenuto, non copiarlo.")
    return coda


def _testo_limitato(valore, limite: int) -> str | None:
    testo = str(valore or "").strip()
    if len(testo) <= limite:
        return testo or None
    taglio = testo[:limite + 1]
    frase = max(taglio.rfind(". "), taglio.rfind("? "), taglio.rfind("! "),
                taglio.rfind("\n"))
    if frase >= int(limite * 0.65):
        return taglio[:frase + 1].strip()
    parola = taglio.rfind(" ")
    if parola >= int(limite * 0.65):
        return taglio[:parola].strip() + "…"
    return taglio[:limite].strip() + "…"


def _campi_analisi(p: dict, lingua: str) -> dict:
    pros = [_testo_limitato(x, 400) for x in (p.get("pros") or []) if str(x).strip()][:4]
    cons = [_testo_limitato(x, 400) for x in (p.get("cons") or []) if str(x).strip()][:3]
    testo = lambda k, tetto: _testo_limitato(p.get(k), tetto)
    return {"perche": testo("perche", 300), "pros": pros, "cons": cons,
            "responsabilita": testo("responsabilita", 700),
            "requisiti": testo("requisiti", 700),
            "benefit": testo("benefit", 500), "lang": lingua,
            "rubrica": VERSIONE_ANALISI,
            # la prova viaggia GREZZA fino alla verifica (e' una lista di
            # indici, non testo), poi si stacca: non e' contenuto per il
            # lettore, e' il sostegno della frase.
            "_prova": p.get("prova")}


def motiva_e_analizza(modello: ChatModel, profilo_testo: str, offerta: dict,
                      desiderio: str | None = None,
                      lingua: str = "English",
                      evita: list[str] | None = None,
                      nota: str | None = None,
                      on_usage=None) -> tuple[str, dict, dict]:
    """Seconda passata di consegna: motivazione E analisi in UNA chiamata."""
    coda = _coda_analisi(offerta, desiderio, lingua)
    if evita:
        coda += ("\n\nHAI GIA' SCRITTO per altre offerte di questo stesso "
                 "digest:\n" + "\n".join(f"- «{e}»" for e in evita[-8:])
                 + "\nIl lettore le vedra' una dopo l'altra: non riusare "
                   "quelle frasi ne' quegli attacchi.")
    if nota:
        coda += f"\n\nAL PRIMO GIRO HAI SBAGLIATO: {nota}"
    corpo = _testa(profilo_testo, RUBRICA_CONSEGNA.format(lingua=lingua)) + [{
        "role": "user", "content": coda}]
    risposta = modello.chat(corpo, max_tokens=900, temperature=0.5)
    uso = _uso_modello(modello)
    if on_usage:
        on_usage(uso)
    try:
        p = _estrai_json(risposta)
    except (TypeError, ValueError) as exc:
        raise RispostaModelloNonValida(
            f"{modello.model}: JSON di motivazione non valido") from exc
    reason = str(p.get("reason") or "")[:400].strip() or "—"
    return reason, _campi_analisi(p, lingua), uso


def analizza_allineamento(modello: ChatModel, profilo_testo: str,
                          offerta: dict, desiderio: str | None = None,
                          lingua: str = "English", on_usage=None) -> dict:
    """Analisi del dettaglio, sottoposta allo stesso controllo del digest."""
    for tentativo in range(2):
        coda = _coda_analisi(offerta, desiderio, lingua)
        if tentativo:
            coda += ("\n\nAL PRIMO GIRO HAI INVENTATO O FRAINSESO I DATI. "
                     "Riscrivi ogni campo usando SOLO il profilo e l'offerta "
                     "forniti. Se un fatto non è presente, non attribuirlo.")
        corpo = _testa(profilo_testo, RUBRICA_ANALISI.format(lingua=lingua)) + [{
            "role": "user", "content": coda}]
        risposta = modello.chat(corpo, max_tokens=900)
        uso = _uso_modello(modello)
        if on_usage:
            on_usage(uso)
        try:
            p = _estrai_json(risposta)
        except (TypeError, ValueError) as exc:
            raise RispostaModelloNonValida(
                f"{modello.model}: JSON di analisi non valido") from exc
        analisi = _campi_analisi(p, lingua)
        analisi.pop("_prova", None)
        if not _analisi_inventata(modello, "", analisi, profilo_testo, offerta,
                                  desiderio, on_usage=on_usage):
            return analisi
    raise RispostaModelloNonValida(
        f"{modello.model}: analisi rifiutata dal controllo anti-invenzione")


def motiva_offerta(modello: ChatModel, profilo_testo: str, offerta: dict,
                   desiderio: str | None = None, lingua: str = "English",
                   on_usage=None) -> tuple[str, dict]:
    """Seconda passata: la motivazione che il destinatario del digest legge."""
    corpo = _testa(profilo_testo, RUBRICA_MOTIVAZIONE.format(lingua=lingua)) + [{
        "role": "user", "content": offerta_per_prompt(offerta, desiderio)}]
    risposta = modello.chat(corpo, max_tokens=120)
    uso = _uso_modello(modello)
    if on_usage:
        on_usage(uso)
    p = _estrai_json(risposta)
    reason = str(p.get("reason") or "")[:400].strip() or "—"
    return reason, uso


# ── La verifica anti-invenzione (13/10/2026) ─────────────────────────────
#
# Misurato sul digest vero: il maestro locale aveva attribuito al candidato
# la patente A e il cinese perche' l'OFFERTA li nominava. La frase nel
# digest e' il prodotto: una sola invenzione brucia la fiducia in tutte le
# altre. Difesa a due cerchi, nessuna delle due e' un modello che si
# autocorregge a temperatura 0 (riproverebbe lo stesso testo):
#
#  1. PROVA MECCANICA: la rubrica di consegna esige il campo `prova` — i
#     NUMERI dei fatti del profilo che reggono la frase (il profilo e' un
#     elenco numerato). Gli indici si controllano con un confronto fra
#     interi: niente modello, niente sottostringhe. (Prima la prova era una
#     copia letterale: il 4B parafrasava, e tutto cadeva al controllo.)
#  2. VERIFICATORE: una classificazione, non una generazione — compito
#     molto piu' facile per un modello piccolo. «La frase gli attribuisce
#     qualcosa che il profilo non ha?» si/no.
#
# Chi fallisce riprova UNA volta col rimprovero in coda (l'input cambia,
# quindi l'output cambia anche a temperatura 0), poi il ripiego
# deterministico, che puo' dire solo il vero.

VERIFICA = """Controlla questi testi scritti per il candidato:

«{frase}»

PROFILO DEL CANDIDATO (tutto ciò che sai di lui):
{profilo}

{offerta}
I testi gli attribuiscono una competenza, una lingua, un'esperienza, una
patente o una certificazione che NON compare nel profilo? Oppure descrivono
requisiti, responsabilità o benefit che NON compaiono nell'offerta?
Rispondi SOLO con JSON:
{{"inventato": true}} oppure {{"inventato": false}}
"""


def _inventata(modello: ChatModel, reason: str, profilo_testo: str,
               offerta_testo: str = "", on_usage=None) -> bool:
    """Classificazione si/no; un guasto del verificatore non ferma la consegna."""
    try:
        r = modello.chat(
            [{"role": "user", "content": VERIFICA.format(
                frase=reason, profilo=profilo_testo,
                offerta=("OFFERTA COMPLETA\n" + offerta_testo
                         if offerta_testo else ""))}], max_tokens=60)
        uso = _uso_modello(modello)
        if on_usage:
            on_usage(uso)
        return _estrai_json(r).get("inventato") is True
    except Exception:
        return False


def _analisi_inventata(modello: ChatModel, reason: str, analisi: dict,
                       profilo_testo: str, offerta: dict,
                       desiderio: str | None = None, on_usage=None) -> bool:
    campi = [("motivazione", reason)]
    for chiave in ("perche", "responsabilita", "requisiti", "benefit"):
        if analisi.get(chiave):
            campi.append((chiave, analisi[chiave]))
    for chiave in ("pros", "cons"):
        campi.extend((chiave, testo) for testo in analisi.get(chiave) or [])
    testo = "\n".join(f"{nome}: {valore}" for nome, valore in campi if valore)
    if not testo:
        return False
    return _inventata(modello, testo, profilo_testo,
                      _coda_analisi(offerta, desiderio), on_usage=on_usage)


def _indici_prova(prova, n_fatti: int) -> list[int] | None:
    """La prova come lista di indici validi, o None se illeggibile/fuori
    scala. Il modello la scrive come [3, 7], ma accettiamo anche «3» e
    «[3]» scritti come stringa: conta solo che i numeri esistano."""
    if prova in (None, "", []):
        return []
    if isinstance(prova, int):
        prova = [prova]
    if isinstance(prova, str):
        prova = [int(x) for x in re.findall(r"\d+", prova)]
    if not isinstance(prova, list):
        return None
    try:
        idx = [int(x) for x in prova]
    except (TypeError, ValueError):
        return None
    if not all(1 <= i <= n_fatti for i in idx):
        return None
    return idx


# 14/10/2026: i superlativi la rubrica li vieta, il modello li scrive
# comunque («perfetta», cinque volte in un digest). Il divieto sta nel
# prompt da mesi; ora e' un guard meccanico, come la prova.
_SUPERLATIVI = re.compile(
    r"\b(perfett\w*|perfect\w*|perfekt\w*|parfait\w*|ideal\w*|eccellent\w*|"
    r"excellent\w*|exzellent\w*|perfect[oa]s?\b)", re.IGNORECASE)


def _vietata(reason: str) -> str | None:
    """La parola vietata trovata nella frase, o None."""
    m = _SUPERLATIVI.search(reason)
    return m.group(0) if m else None


def motiva_verificata(modello: ChatModel, profilo_testo: str, offerta: dict,
                      desiderio: str | None, lingua: str,
                      ripiego: str, evita: list[str] | None = None,
                      on_usage=None) -> tuple[str, dict, dict]:
    """Motivazione e intera analisi verificate; il ripiego non conserva il rifiuto."""
    nota = None
    uso_totale: dict = {"complete": True}

    def registra(uso: dict) -> None:
        _accumula_uso(uso_totale, uso)
        if on_usage:
            on_usage(uso)

    for _ in range(2):
        reason, analisi, _ = motiva_e_analizza(
            modello, profilo_testo, offerta, desiderio, lingua,
            evita=evita, nota=nota, on_usage=registra)
        prova = analisi.pop("_prova", None)
        n_fatti = len(re.findall(r"^\[\d+\]", profilo_testo, re.M))
        indici = _indici_prova(prova, n_fatti)
        if indici is None:
            log.info("prova fuori scala (%s): seconda possibilita'",
                     offerta.get("id"))
            nota = ("La PROVA che hai scritto non sono numeri dei fatti del "
                    "profilo, o puntano fuori scala. Riscrivi: ogni "
                    "affermazione sul candidato cita i suoi fatti [n].")
            continue
        vietata = _vietata(reason)
        if vietata:
            log.info("superlativo vietato «%s» (%s): seconda possibilita'",
                     vietata, offerta.get("id"))
            nota = (f"Hai usato «{vietata}», un superlativo vietato dalla "
                    "rubrica: riscrivi senza superlativi, sobrio.")
            continue
        if _analisi_inventata(modello, reason, analisi, profilo_testo,
                              offerta, desiderio, on_usage=registra):
            log.info("analisi bocciata dal verificatore (%s): seconda possibilita'",
                     offerta.get("id"))
            nota = ("Un campo attribuiva al candidato qualcosa che il profilo "
                    "non ha, oppure all'offerta contenuti assenti. Riscrivi "
                    "tutti i campi usando SOLO profilo e offerta forniti.")
            continue
        uso_totale.update({"model": modello.model, "provider": modello.source})
        return reason, analisi, uso_totale
    log.info("motivazione al ripiego dopo il retry (%s)", offerta.get("id"))
    uso_totale.update({"model": modello.model, "provider": modello.source})
    return ripiego, {}, uso_totale


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
