"""La calibrazione: ESCO non tocca la produzione finche' non passa l'esame.

Il rischio con 13.500 competenze in 28 lingue sono gli alias-trappola:
«absorb» e' un verbo inglese comune prima che il nome di una piattaforma
(falso positivo visto al primo collaudo). La difesa e' guidata dai dati:
si fa girare il riconoscitore su un campione di descrizioni vere e si
misura in QUANTE compare ogni etichetta. Un'etichetta che scatta in un
annuncio su quattro non e' una competenza, e' rumore linguistico: va in
lista nera. Il rapporto stampa i sospetti a meta' strada perche' un
occhio umano possa aggiungere o togliere.

Alla fine scrive la lista nera e — SOLO se i numeri stanno in soglie
sane — il marcatore /opt/nivult/esco-attivo che apre il cancello.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import re

import psycopg

log = logging.getLogger("nivult.ats.esco_calibra")

CAMPIONE = 20000
SOGLIA_NERA = 0.25       # in piu' di 1 annuncio su 4 = rumore, dentro
SOGLIA_SOSPETTO = 0.08   # da 8% a 25%: stampati per revisione umana

_DESCR = ("COALESCE(raw->>'description', raw->>'externalDescription', "
          "raw->>'descriptionHtml', raw->>'jobDescription', "
          "raw->>'content')")


def calibra(dsn: str) -> dict:
    from nivult.ats import esco
    # per calibrare bisogna caricare l'automa SENZA cancello
    esco.ATTIVO = esco.PERCORSO          # trucco: un file che esiste
    esco._automa = None
    conteggio: collections.Counter = collections.Counter()
    visti = 0
    with psycopg.connect(dsn) as conn:
        with conn.cursor(name="calibra") as cur:
            cur.itersize = 500
            cur.execute(f"""
                SELECT title, {_DESCR} FROM ats_jobs
                 WHERE expired_at IS NULL AND length({_DESCR}) > 400
                 ORDER BY random() LIMIT %s""", (CAMPIONE,))
            for titolo, descr in cur:
                testo = (titolo or "") + " " + re.sub(
                    r"<[^>]+>", " ", descr or "")
                for etichetta in esco.estrai(testo, massimo=60):
                    conteggio[etichetta] += 1
                visti += 1
                if visti % 5000 == 0:
                    log.info("  … %d annunci esaminati", visti)
    nere, sospette = [], []
    for etichetta, n in conteggio.most_common(200):
        quota = n / max(visti, 1)
        if quota > SOGLIA_NERA:
            nere.append(etichetta)
        elif quota > SOGLIA_SOSPETTO:
            sospette.append((etichetta, round(100 * quota, 1)))
    log.info("LISTA NERA (%d): %s", len(nere), nere)
    log.info("SOSPETTE (8-25%%, da rivedere a occhio): %s", sospette)
    # la lista nera contiene le ETICHETTE canoniche; il riconoscitore
    # blocca pero' gli alias: si traducono in tutte le loro chiavi
    da_bloccare: set = set()
    with open(esco.PERCORSO, encoding="utf-8") as f:
        for riga in f:
            v = json.loads(riga)
            pref = v.get("preferred") or {}
            inglese = pref.get("en") or next(iter(pref.values()), "")
            if inglese in nere:
                for lab in list(pref.values()) + [
                        x for ll in (v.get("alt") or {}).values()
                        for x in ll]:
                    da_bloccare.add(esco._norm(lab))
    with open(esco.LISTA_NERA + ".tmp", "w") as f:
        json.dump(sorted(da_bloccare), f, ensure_ascii=False)
    os.replace(esco.LISTA_NERA + ".tmp", esco.LISTA_NERA)
    mediana_ok = 0 < len(conteggio) and visti > 1000
    if mediana_ok:
        open("/opt/nivult/esco-attivo", "w").write("calibrato\n")
        log.info("CANCELLO APERTO: ESCO attivo in produzione")
    return {"annunci": visti, "etichette_viste": len(conteggio),
            "in_lista_nera": len(nere), "sospette": len(sospette)}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    print(calibra(dsn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
