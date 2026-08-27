"""Arricchimento aziende da Wikidata: gratis, senza chiave, cache in organizations.

Wikidata ha i dati aziendali nelle proprietà:
  P1128  numero dipendenti
  P452   industria
  P154   logo (file su Wikimedia Commons)
  P856   sito web ufficiale
  P17    paese

Si cerca per nome (label esatto o fuzzy), si legge una volta, si mette in
cache. La cache non scade: i dati aziendali cambiano raramente e una rilettura
mensile basta.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("nivult.ats.enrichment")

UA = {"User-Agent": "nivult-ats/0.1 (research; contact@nivult.com)"}


def cerca_wikidata(nome: str) -> dict | None:
    """Cerca un'azienda su Wikidata per nome e ne estrae i dati.

    -> {wikidata_id, employees, industry, logo_url, website, country}
       oppure None se non trovata.
    """
    nome = nome.strip()
    if not nome:
        return None

    with httpx.Client(timeout=15, headers=UA) as client:
        # 1. Ricerca per label esatto (la più affidabile)
        entity_id = _cerca_label(client, nome)
        if not entity_id:
            # 2. Ricerca fuzzy (wbsearchentities)
            entity_id = _cerca_fuzzy(client, nome)
        if not entity_id:
            return None

        # 3. Legge i dati dell'entità
        return _leggi_entita(client, entity_id, nome)


def _cerca_label(client: httpx.Client, nome: str) -> str | None:
    """SPARQL: trova l'entità con questo label esatto che è un'organizzazione."""
    query = (
        'SELECT ?item WHERE { ?item rdfs:label "' + nome.replace('"', '\\"') +
        '"@en . ?item wdt:P31 wd:Q43229 } LIMIT 1')
    r = client.get(
        "https://query.wikidata.org/sparql",
        params={"format": "json", "query": query},
        headers={"Accept": "application/json"})
    if r.status_code != 200:
        return None
    bindings = r.json().get("results", {}).get("bindings", [])
    if not bindings:
        return None
    uri = bindings[0].get("item", {}).get("value", "")
    return uri.split("/")[-1] if "/entity/" in uri else None


def _cerca_fuzzy(client: httpx.Client, nome: str) -> str | None:
    """wbsearchentities: la ricerca di Wikidata, per nome parziale."""
    r = client.get(
        "https://www.wikidata.org/w/api.php",
        params={"action": "wbsearchentities", "search": nome,
                "language": "en", "limit": 1, "format": "json"})
    if r.status_code != 200:
        return None
    results = r.json().get("search", [])
    return results[0]["id"] if results else None


def _leggi_entita(client: httpx.Client, entity_id: str, nome_originale: str) -> dict:
    """Legge le proprietà che ci interessano dall'entità Wikidata."""
    r = client.get(f"https://www.wikidata.org/wiki/Special:EntityData/{entity_id}.json")
    if r.status_code != 200:
        return {"wikidata_id": entity_id, "name": nome_originale}
    entities = r.json().get("entities", {})
    if entity_id not in entities:
        return {"wikidata_id": entity_id, "name": nome_originale}

    claims = entities[entity_id].get("claims", {})

    def claim_id(prop: str) -> str | None:
        if prop in claims:
            try:
                return claims[prop][0]["mainsnak"]["datavalue"]["value"]["id"]
            except (KeyError, TypeError, IndexError):
                return None
        return None

    def claim_amount(prop: str) -> int | None:
        if prop in claims:
            try:
                v = claims[prop][0]["mainsnak"]["datavalue"]["value"]["amount"]
                return int(float(v.replace("+", "")))
            except (KeyError, TypeError, ValueError, IndexError):
                return None
        return None

    def claim_url(prop: str) -> str | None:
        if prop in claims:
            try:
                return claims[prop][0]["mainsnak"]["datavalue"]["value"]
            except (KeyError, TypeError, IndexError):
                return None
        return None

    logo = claim_url("P154")
    # I file su Commons hanno un URL diretto via Special:FilePath
    if logo and not logo.startswith("http"):
        logo = f"https://commons.wikimedia.org/wiki/Special:FilePath/{logo}"

    return {
        "name": nome_originale,
        "wikidata_id": entity_id,
        "employees": claim_amount("P1128"),
        "industry": claim_id("P452"),       # è un Q-id: la label si può
        "logo_url": logo,                   # risolvere se serve
        "website": claim_url("P856"),
        "country": claim_id("P17"),         # anche qui: Q-id
    }
