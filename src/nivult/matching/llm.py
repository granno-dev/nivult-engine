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
import logging
import os
import re
from dataclasses import dataclass

from nivult.ingestion.base import HttpSource

log = logging.getLogger("nivult.matching.llm")

# La versione delle rubriche di punteggio. SI ALZA A MANO, e si alza ogni
# volta che una modifica sposta i punteggi: soglie, regole di valutazione,
# pesi. Non per una correzione di refuso.
#
# Finisce in `matches.rubric_version` e risponde a una domanda che prima
# non aveva risposta: con quale metro e' stato misurato QUESTO candidato
# contro QUESTA offerta. Il 30/08/2026 la rubrica e' cambiata due volte in
# un giorno, e senza questa stringa i punteggi di ieri e quelli di oggi
# sembrano confrontabili mentre non lo sono.
VERSIONE_RUBRICA = "2026-08-30.r3"


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
        msg = d["choices"][0]["message"]
        contenuto = msg.get("content")
        if contenuto is None:
            # i modelli che ragionano a volte rispondono solo in
            # reasoning_content: meglio quello che un crash (10/10/2026)
            contenuto = msg.get("reasoning_content")
        if contenuto is None:
            raise RuntimeError(f"{self.model}: risposta senza content "
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
    piu' vicino. NON e' per il matching del digest (quello resta GLM 5.2,
    scelta di prodotto): e' per l'arricchimento ad alto volume."""

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

IL PAESE E' GIA' SCELTO. Questa offerta viene da una ricerca che il candidato
ha aperto su un paese preciso: se l'annuncio e' in quel paese, la citta' NON
conta e non abbassa il punteggio. Il candidato non ha mai dichiarato una
citta' — dedurla dal CV e penalizzarla significa inventargli un vincolo che
non ha posto. Abbassa per la sede solo se l'annuncio dice che si lavora in un
paese DIVERSO da quello della ricerca.

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
- pros: da 2 a 4 punti in cui il candidato combacia con QUESTA offerta.
  Fatti presi dal profilo e dall'offerta, mai lodi generiche.
- cons: da 1 a 3 punti su cui il colloquio farà domande (requisiti
  scoperti, lingua, sede, livello). Onesti, mai scoraggianti.
- responsabilita, requisiti, benefit: il contenuto dei blocchi omonimi
  dell'offerta, reso fedele e compatto. null se il blocco manca.
- TUTTO in {lingua}, ogni frase breve.
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
    # Solo quando e' true: il false di Fantastic significa «l'annuncio non
    # ne parla», e scriverlo qui insegnerebbe al modello una bugia.
    if job.get("ai_visa_sponsorship"):
        pezzi.append("sponsorizzazione visto: dichiarata dall'annuncio")
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


def _campi_analisi(p: dict, lingua: str) -> dict:
    pros = [str(x)[:160] for x in (p.get("pros") or []) if str(x).strip()][:4]
    cons = [str(x)[:160] for x in (p.get("cons") or []) if str(x).strip()][:3]
    testo = lambda k, tetto: (str(p.get(k) or "").strip()[:tetto] or None)
    return {"perche": testo("perche", 300), "pros": pros, "cons": cons,
            "responsabilita": testo("responsabilita", 700),
            "requisiti": testo("requisiti", 700),
            "benefit": testo("benefit", 500), "lang": lingua,
            # la prova viaggia GREZZA fino alla verifica (e' una lista di
            # indici, non testo), poi si stacca: non e' contenuto per il
            # lettore, e' il sostegno della frase.
            "_prova": p.get("prova")}


def motiva_e_analizza(modello: ChatModel, profilo_testo: str, offerta: dict,
                      desiderio: str | None = None,
                      lingua: str = "English",
                      evita: list[str] | None = None,
                      nota: str | None = None) -> tuple[str, dict, dict]:
    """Seconda passata di consegna: motivazione E analisi in UNA chiamata.

    Nasce dal conto della serva: la finestra di dettaglio del pannello
    chiamava GLM al primo clic, a cache fredda, ripagando tutto il
    prefisso. Qui il prefisso e' gia' caldo (stesso profilo della
    valutazione appena fatta) e la chiamata esisteva comunque per la
    motivazione: l'analisi costa solo l'output in piu'.

    `evita` (13/10/2026): le frasi gia' scritte in QUESTO digest. Ogni
    motivazione nasce da una chiamata separata e il modello non sa cosa ha
    scritto prima — misurato sul digest vero: 18 motivazioni su 30 con lo
    stesso attacco. Le frasi gia' scritte vanno nella CODA (messaggio
    utente, diverso a ogni offerta per definizione): la cache del prefisso
    non si tocca.
    """
    coda = _coda_analisi(offerta, desiderio, lingua)
    if evita:
        coda += ("\n\nHAI GIA' SCRITTO per altre offerte di questo stesso "
                 "digest:\n" + "\n".join(f"- «{e}»" for e in evita[-8:])
                 + "\nIl lettore le vedra' una dopo l'altra: non riusare "
                   "quelle frasi ne' quegli attacchi.")
    if nota:
        # il secondo tentativo porta il perche' del primo: stessa coda,
        # piu' il rimprovero del verificatore in fondo, dove si legge
        coda += f"\n\nAL PRIMO GIRO HAI SBAGLIATO: {nota}"
    corpo = _testa(profilo_testo, RUBRICA_CONSEGNA.format(lingua=lingua)) + [{
        "role": "user", "content": coda}]
    # 14/10/2026: temperatura 0.5, non 0. A zero la scrittura convergeva —
    # dodici chiusure identiche in un digest — e `evita` da solo non
    # bastava: stesso input, stessa frase. Il verificatore resta a zero e
    # controlla la verita'; la varieta' la fa il caso controllato.
    risposta = modello.chat(corpo, max_tokens=900, temperature=0.5)
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
        "role": "user", "content": _coda_analisi(offerta, desiderio, lingua)}]
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

VERIFICA = """Controlla questa frase scritta per il candidato:

«{frase}»

PROFILO DEL CANDIDATO (tutto ciò che sai di lui):
{profilo}

La frase gli attribuisce una competenza, una lingua, un'esperienza, una
patente o una certificazione che NON compare nel profilo? O gli attribuisce
un requisito che appartiene all'offerta? Rispondi SOLO con JSON:
{{"inventato": true}} oppure {{"inventato": false}}
"""


def _inventata(modello: ChatModel, reason: str, profilo_testo: str) -> bool:
    """Il verificatore: classificazione si/no, mai generazione. In panne non
    blocca la consegna — la prova meccanica resta a presidiare."""
    try:
        r = modello.chat(
            [{"role": "user",
              "content": VERIFICA.format(frase=reason, profilo=profilo_testo)}],
            max_tokens=60)
        return bool(_estrai_json(r).get("inventato"))
    except Exception:
        return False


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
                      ripiego: str,
                      evita: list[str] | None = None) -> tuple[str, dict, dict]:
    """motiva_e_analizza + la verifica. La reason che esce da qui o e'
    sostenuta dal profilo o e' il ripiego deterministico: mai un'invenzione.

    Chi fallisce NON va al ripiego subito: il secondo tentativo porta il
    perche' del primo (gli indici erano fuori scala / il verificatore ha
    trovato un'invenzione). La temperatura e' 0 ma l'input cambia, quindi
    l'output cambia: e' la riflessione, non la ripetizione."""
    nota = None
    analisi: dict = {}
    for _ in range(2):
        reason, analisi, uso = motiva_e_analizza(modello, profilo_testo,
                                                 offerta, desiderio, lingua,
                                                 evita=evita, nota=nota)
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
                    "rubrica: la soglia e' 80 su 100, non la perfezione. "
                    "Riscrivi senza superlativi, sobrio.")
            continue
        if _inventata(modello, reason, profilo_testo):
            log.info("bocciata dal verificatore (%s): seconda possibilita'",
                     offerta.get("id"))
            nota = ("La frase attribuiva al candidato qualcosa che il "
                    "profilo non ha. Riscrivi usando SOLO i fatti numerati: "
                    "un requisito dell'offerta non e' mai una sua dote.")
            continue
        return reason, analisi, uso
    log.info("motivazione al ripiego dopo il retry (%s)", offerta.get("id"))
    return ripiego, analisi, uso


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
