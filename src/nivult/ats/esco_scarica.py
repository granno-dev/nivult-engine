"""Scarica la tassonomia ESCO: 13.485 competenze, tutte le lingue.

La via maestra (ricerca paginata) ha un tetto a 200 risultati; i CSV del
portale stanno dietro un modulo. La via che funziona e' l'ALBERO: dalla
radice della tassonomia giu' per i rami `narrower`, nodo per nodo, come
si visita uno SKOS. ~14.000 fermate a passo educato (~0.7s): due ore e
mezza in sfondo, riprendibile — visitati e frontiera vivono su file,
un crash riparte da dove era.
"""
from __future__ import annotations

import json
import logging
import os
import time

import re

import httpx

log = logging.getLogger("nivult.ats.esco_scarica")

API = "https://ec.europa.eu/esco/api"

# due schemi, stessa camminata: le COMPETENZE (per il technographic) e
# le OCCUPAZIONI (~3.000 mestieri: normalizzare i titoli e' cio' che
# stringe le stime salariali — lo scarto -38% su Manufacturing era
# proprio la famiglia troppo larga)
SCHEMI = {
    "competenze": {
        "destinazione": "/opt/nivult/esco-competenze.jsonl",
        "radice": "http://data.europa.eu/esco/concept-scheme/skills",
        "foglia": r"/skill/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
                  r"-[0-9a-f]{4}-[0-9a-f]{12}$"},
    "occupazioni": {
        "destinazione": "/opt/nivult/esco-occupazioni.jsonl",
        "radice": "http://data.europa.eu/esco/concept-scheme/occupations",
        "foglia": r"/occupation/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
                  r"-[0-9a-f]{4}-[0-9a-f]{12}$"},
}
# per l'API SKOS ogni nodo e' className "Concept": le FOGLIE si
# riconoscono dall'URI, che finisce con un UUID; i gruppi hanno codici
# corti (S5.7, T2). Scoperto dopo un'ora a zero raccolte.


def _get(cli: httpx.Client, percorso: str, uri: str) -> dict | None:
    for tentativo in range(4):
        try:
            r = cli.get(f"{API}/{percorso}", params={"uri": uri})
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(4 * (tentativo + 1))
    return None


def scarica(schema: str = "competenze") -> int:
    cfg = SCHEMI[schema]
    destinazione = cfg["destinazione"]
    foglia = re.compile(cfg["foglia"])
    radice = cfg["radice"]
    stato_file = destinazione + ".stato"
    parziale = destinazione + ".parziale"
    try:
        st = json.load(open(stato_file))
        frontiera, visti, n = st["frontiera"], set(st["visti"]), st["n"]
        log.info("riprendo: %d in frontiera, %d visti, %d competenze",
                 len(frontiera), len(visti), n)
    except Exception:                                # noqa: BLE001
        frontiera, visti, n = [radice], set(), 0
        open(parziale, "w").close()
    cli = httpx.Client(timeout=45, headers={
        "User-Agent": "nivult-ats/1.0 (taxonomy sync)"})
    passi = 0
    with open(parziale, "a", encoding="utf-8") as fp:
        while frontiera:
            uri = frontiera.pop()
            if uri in visti:
                continue
            visti.add(uri)
            percorso = "resource/taxonomy" if uri == radice \
                else "resource/concept"
            d = _get(cli, percorso, uri)
            if d is None:
                continue
            if foglia.search(uri):
                pref = d.get("preferredLabel") or {}
                fp.write(json.dumps({
                    "uri": uri, "preferred": pref,
                    "alt": d.get("alternativeLabel") or {},
                }, ensure_ascii=False) + "\n")
                fp.flush()
                n += 1
            links = d.get("_links") or {}
            for ramo in ("hasTopConcept", "narrowerConcept",
                         "narrowerSkill", "narrowerTransversalSkill",
                         "narrowerOccupation"):
                for v in links.get(ramo) or []:
                    if v.get("uri") and v["uri"] not in visti:
                        frontiera.append(v["uri"])
            passi += 1
            if passi % 200 == 0:
                with open(stato_file + ".tmp", "w") as f:
                    json.dump({"frontiera": frontiera,
                               "visti": sorted(visti), "n": n}, f)
                os.replace(stato_file + ".tmp", stato_file)
                log.info("  … %d visitati, %d competenze, %d in coda",
                         len(visti), n, len(frontiera))
            time.sleep(0.7)
    os.replace(parziale, destinazione)
    try:
        os.remove(stato_file)
    except OSError:
        pass
    log.info("ESCO completa: %d competenze in %s", n, destinazione)
    return n


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", choices=list(SCHEMI), default="competenze")
    print(scarica(ap.parse_args().schema))
