"""Il CV: dal file caricato al profilo strutturato.

Due stadi, entrambi qui:

  estrai_testo    il file (PDF o testo) diventa testo. È l'unica parte che
                  guarda il contenuto oltre a GLM: il testo non si logga MAI,
                  è un dato personale come tutto il resto del CV
  estrai_profilo  GLM legge il testo e propone famiglie, seniority, competenze,
                  lingue, anni. È una PROPOSTA: la conferma è dell'utente, in
                  onboarding — e i valori vengono validati contro i vocabolari
                  prima di essere accettati, perché un valore fuori vocabolario
                  filtrerebbe via tutto in silenzio
"""

from __future__ import annotations

import json
import re

from nivult.matching.llm import ChatModel

MAX_BYTES = 5 * 1024 * 1024
MAX_CARATTERI = 25_000   # il CV intero di rado li supera; tagliare protegge
                         # il contesto e il conto, non la qualità del giudizio


def estrai_testo(nome: str, mime: str, dati: bytes) -> str:
    if len(dati) > MAX_BYTES:
        raise ValueError("il file supera 5 MB")
    if (mime or "").lower() == "application/pdf" or nome.lower().endswith(".pdf"):
        from pypdf import PdfReader
        import io
        lettore = PdfReader(io.BytesIO(dati))
        testo = "\n".join((p.extract_text() or "") for p in lettore.pages)
    else:
        testo = dati.decode("utf-8", errors="replace")
    testo = testo.strip()
    if len(testo) < 30:
        raise ValueError("dal file non si riesce a leggere testo sufficiente")
    return testo[:MAX_CARATTERI]


RUBRICA_ESTRAZIONE = """Sei un consulente di carriera. Leggi il CV e proponi il
profilo del candidato. Sarà l'utente a confermarlo: proponi, non decidere.

Rispondi SOLO con questo JSON, niente altro:
{{"families": ["..."], "seniority": "...", "skills": ["..."],
 "languages": ["..."], "years_experience": N,
 "headline": "...", "roles": [{{"title": "...", "employer": "...", "period": "..."}}],
 "certifications": ["..."], "education": [{{"title": "...", "school": "..."}}]}}

- families: da 1 a 3 voci, SOLO fra queste (sono le famiglie professionali
  del sistema): {famiglie}
- seniority: UNO di questi codici: {seniority}
- skills: al massimo 15 competenze distintive del candidato, nella lingua del CV
- languages: le lingue che il CV dimostra, SOLO fra: {lingue}
- years_experience: anni totali di esperienza lavorativa (numero intero)

I quattro campi seguenti servono a MOSTRARE al candidato che l'abbiamo
letto davvero. Copiali dal CV, non inventarli, e usa le sue stesse parole:

- headline: UNA frase, massimo 20 parole, che dice chi è professionalmente.
  Nella lingua del CV. Niente aggettivi da brochure: il mestiere, l'ambito,
  il livello.
- roles: TUTTI gli incarichi, dal più recente al più vecchio. `title` come
  scritto nel CV, `employer` il datore, `period` come compare (es.
  "2023 — oggi"). Lista vuota se il CV non li espone in modo leggibile.
- certifications: TUTTE le certificazioni vere, il nome esatto (es. "Lean
  Six Sigma Black Belt"). Mai corsi generici o competenze.
- education: tutti i titoli di studio, dal più alto.
- suggested_searches: 2-3 titoli di ruolo che il candidato dovrebbe
  cercare ADESSO — il passo naturale successivo della carriera, non i
  titoli già avuti. Brevi (2-4 parole), senza nome del datore, senza
  sigle di area geografica. Nella lingua del CV. Il primo è il più
  ovvio, gli altri allargano. `title` il titolo di studio,
  `school` l'istituto.

Se un campo non è deducibile, usa la lista vuota (o null per i numeri):
meglio un campo vuoto che un valore inventato su cui poi si filtra."""


def estrai_profilo(modello: ChatModel, testo_cv: str, *, famiglie: list[str],
                   seniority: list[str], lingue: list[str]) -> dict:
    """-> il profilo per le colonne di user_cvs, già validato.

    Tutto ciò che GLM risponde fuori vocabolario viene scartato, non corretto:
    un valore inventato su cui poi si filtra è peggio di un campo vuoto.
    """
    rubrica = RUBRICA_ESTRAZIONE.format(
        famiglie=", ".join(famiglie), seniority=", ".join(seniority),
        lingue=", ".join(lingue))
    risposta = modello.chat([
        {"role": "system", "content": rubrica},
        {"role": "user", "content": "CV\n" + testo_cv}], max_tokens=2500)
    grezzo = _json(risposta)

    def puliti(chiave: str, ammessi: list[str], limite: int) -> list[str]:
        valori = grezzo.get(chiave) or []
        if not isinstance(valori, list):
            return []
        esito = []
        for v in valori:
            if isinstance(v, str) and v in ammessi and v not in esito:
                esito.append(v)
            if len(esito) >= limite:
                break
        return esito

    anni = grezzo.get("years_experience")
    try:
        anni = min(max(int(anni), 0), 70) if anni is not None else None
    except (TypeError, ValueError):
        anni = None

    def testo(v, massimo: int) -> str | None:
        return v.strip()[:massimo] if isinstance(v, str) and v.strip() else None

    def elenco(chiave: str, limite: int, massimo: int) -> list[str]:
        v = grezzo.get(chiave) or []
        if not isinstance(v, list):
            return []
        return [x.strip()[:massimo] for x in v
                if isinstance(x, str) and x.strip()][:limite]

    def oggetti(chiave: str, campi: tuple[str, ...], limite: int) -> list[dict]:
        """Le liste di oggetti (ruoli, formazione) tenute alla LORO forma.

        Il modello a volte restituisce stringhe al posto di oggetti, o
        campi in piu'. Si prende quello che serve e si scarta il resto:
        la scheda del pannello legge chiavi fisse, e una forma diversa
        laggiu' e' una riga vuota che nessuno sa spiegare.
        """
        v = grezzo.get(chiave) or []
        if not isinstance(v, list):
            return []
        esito = []
        for x in v[: limite * 2]:
            if not isinstance(x, dict):
                continue
            riga = {c: testo(x.get(c), 120) for c in campi}
            if riga.get(campi[0]):
                esito.append(riga)
            if len(esito) >= limite:
                break
        return esito

    return {
        "families": puliti("families", famiglie, 3),
        "seniority": (grezzo.get("seniority")
                      if grezzo.get("seniority") in seniority else None),
        "skills": [s for s in (grezzo.get("skills") or [])
                   if isinstance(s, str) and s.strip()][:15],
        "languages": puliti("languages", lingue, 10),
        "years_experience": anni,
        # I quattro campi «di racconto»: non si filtra su nessuno, servono
        # a far vedere al candidato che l'abbiamo letto. Vivono dentro
        # raw_extraction, che e' gia' jsonb — una colonna per ciascuno
        # sarebbe una migrazione per dati che nessuna query interroga.
        "headline": testo(grezzo.get("headline"), 200),
        "roles": oggetti("roles", ("title", "employer", "period"), 15),
        "certifications": elenco("certifications", 12, 100),
        "education": oggetti("education", ("title", "school"), 5),
        "suggested_searches": elenco("suggested_searches", 3, 60),
        # In archivio va la versione RIPULITA dei quattro campi, non la
        # risposta grezza: e' quella che il pannello legge, e validare in
        # lettura vorrebbe dire rifare lo stesso lavoro a ogni apertura.
        "raw_extraction": {
            **grezzo,
            "headline": testo(grezzo.get("headline"), 200),
            "roles": oggetti("roles", ("title", "employer", "period"), 15),
            "certifications": elenco("certifications", 12, 100),
            "education": oggetti("education", ("title", "school"), 5),
            "suggested_searches": elenco("suggested_searches", 3, 60),
        },
    }


def _json(testo: str) -> dict:
    testo = testo.strip()
    if testo.startswith("```"):
        testo = re.sub(r"^```[a-z]*\s*|\s*```$", "", testo)
    try:
        d = json.loads(testo)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", testo, re.S)
        if not m:
            raise ValueError(f"nessun JSON nella risposta: {testo[:180]}")
        d = json.loads(m.group(0))
    return d if isinstance(d, dict) else {}
