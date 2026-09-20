"""La calibrazione: ESCO non tocca la produzione finche' non passa l'esame.

Il rischio con 13.466 competenze in 28 lingue sono gli alias-trappola:
«absorb» e' un verbo inglese comune prima che il nome di una piattaforma
(falso positivo visto al primo collaudo). La difesa e' guidata dai dati:
si fa girare il riconoscitore su un campione di descrizioni vere e si
misura in QUANTI annunci scatta ogni ALIAS — non ogni etichetta
canonica: la prima calibrazione (07/09) contava le canoniche, e «avec»
→ «mobile device management» al 22% passava perche' in inglese e
tedesco la stessa competenza non scattava mai. Un alias che compare in
un annuncio su venti, in tutte le famiglie, non e' una competenza: e'
rumore linguistico e va in lista nera. Fanno eccezione le lingue, che
e' normale trovare in un annuncio su dieci.

Il rapporto stampa i sospetti a meta' strada e un campione di annunci
con le competenze estratte, perche' un occhio umano giudichi PRIMA di
aprire. Il cancello (`/opt/nivult/esco-attivo`) lo apre solo `--apri`:
una calibrazione senza quel flag scrive la lista nera e CHIUDE il
cancello se era aperto, perche' i numeri vecchi non valgono piu'.

    python -m nivult.ats.esco_calibra            # misura e scrive la lista nera
    python -m nivult.ats.esco_calibra --apri     # ...e apre il cancello
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import random
import re

import psycopg

from nivult.ats.testo import SQL_TESTO

log = logging.getLogger("nivult.ats.esco_calibra")

CAMPIONE = 20000
SOGLIA_NERA = 0.20       # in piu' di 1 annuncio su 5 = rumore, sempre
SOGLIA_SOSPETTO = 0.015  # sopra l'1,5%: si misura la concentrazione
# Una competenza DISTINGUE un mestiere: «python» sta nell'80% degli
# annunci IT e quasi mai altrove, «source» e «history» stanno ovunque
# nella stessa proporzione del corpus. Il «lift» e' quante volte la
# famiglia piu' rappresentata fra gli annunci con l'alias supera la sua
# quota nel corpus: sotto questa soglia l'alias non distingue niente.
# Misurato l'08/09/2026 su 20k annunci: «pregnancy» 2,05, «history»
# 1,79, «source» 2,66, «less» 2,51, «attention to detail» 2,41 —
# contro «python» 6,6, «accounting» 11,8, «logistics» 6,5, «customer
# service» 4,7, «project management» 4,2. La soglia sta nel vuoto fra
# i due gruppi.
LIFT_MINIMO = 3.0

# Etichette canoniche escluse a mano, con la ragione misurata accanto:
# compaiono nel testo ma non come competenza richiesta.
NERE_A_MANO = {
    "pregnancy": "boilerplate pari opportunita' (EEO)",
    "genetics": "boilerplate pari opportunita' («genetic information»)",
    "history": "«history» nei boilerplate e nelle biografie aziendali",
    "morality": "via «integrity», valore aziendale non competenza",
    "politics": "«politique de confidentialité» in francese",
    "persist": "via «continue»",
    "betting": "«paris» e' la citta'",
    "journalism": "via «reporting»",
    "religion": "boilerplate pari opportunita'",
    "childbirth": "boilerplate pari opportunita'",
    "Latin": "«Latin America» negli annunci inglesi",
    "LESS": "«less» e' una parola prima che un preprocessore CSS",
    "Source (digital game creation systems)": "«source» e' una parola",
    "Scratch (computer programming)": "«from scratch»",
    "compile airport certification manuals": "alias ESCO «and procedures»",
    "philosophy": "«our philosophy», lift 3,4 ma non e' una competenza",
    "logic": "«logic» e' una parola",
    "social justice": "boilerplate diversita' e inclusione",
    "regulatory requirements": "alias ESCO di «good laboratory practice»: sbagliato",
    "a computer": "alias ESCO di «computer equipment»",
}


def calibra(dsn: str, apri: bool = False, campione: int = CAMPIONE) -> dict:
    from nivult.ats import esco, lingua as _lingua
    # per calibrare bisogna caricare l'automa SENZA cancello, e senza la
    # lista nera precedente: si rimisura tutto da capo
    esco._automa = None
    esco._canonico = {}
    vecchia = esco.LISTA_NERA
    esco.LISTA_NERA = "/nonexistent/esco-lista-nera.json"
    try:
        esco._costruisci(forza=True)
    finally:
        esco.LISTA_NERA = vecchia
    per_alias: collections.Counter = collections.Counter()
    alias_fam: dict = collections.defaultdict(collections.Counter)
    fam_corpus: collections.Counter = collections.Counter()
    alias_canon: dict = {}
    per_canon: collections.Counter = collections.Counter()
    esempi: list = []
    visti = 0
    senza_lingua = 0
    with psycopg.connect(dsn) as conn:
        with conn.cursor(name="calibra") as cur:
            cur.itersize = 500
            cur.execute(f"""
                SELECT j.title, {SQL_TESTO.replace("raw", "j.raw")},
                       (SELECT family FROM job_classifications c
                         WHERE c.job_id = j.id)
                  FROM ats_jobs j
                 WHERE j.expired_at IS NULL
                   AND length({SQL_TESTO.replace("raw", "j.raw")}) > 400
                 ORDER BY random() LIMIT %s""", (campione,))
            for titolo, descr, famiglia in cur:
                famiglia = famiglia or "?"
                fam_corpus[famiglia] += 1
                testo = (titolo or "") + " " + re.sub(
                    r"<[^>]+>", " ", descr or "")
                lingua = _lingua.rileva(testo)
                if lingua is None:
                    senza_lingua += 1
                t = esco._norm(testo)[:12000]
                alias_doc: set = set()
                uris_doc: set = set()
                for alias, uris in esco._riscontri(t, lingua):
                    alias_doc.add(alias)
                    alias_canon.setdefault(alias, set()).update(
                        esco._canonico[u] for u in uris if u in esco._canonico)
                    uris_doc.update(uris)
                per_alias.update(alias_doc)
                for alias in alias_doc:
                    alias_fam[alias][famiglia] += 1
                per_canon.update({esco._canonico[u] for u in uris_doc
                                  if u in esco._canonico})
                visti += 1
                if len(esempi) < 400 and random.random() < 0.05:
                    esempi.append((titolo, lingua, sorted(
                        {esco._canonico[u] for u in uris_doc
                         if u in esco._canonico})[:15]))
                if visti % 5000 == 0:
                    log.info("  … %d annunci esaminati", visti)
    def lift(alias: str) -> float:
        fam = alias_fam[alias]
        n = sum(fam.values())
        return max((k / n) / (fam_corpus[f] / visti)
                   for f, k in fam.items() if f != "?") if n else 0.0

    nere_alias, sospette, tenute = [], [], []
    for alias, n in per_alias.most_common(600):
        quota = n / max(visti, 1)
        canon = alias_canon.get(alias, set())
        if canon and canon <= esco.LINGUE:
            continue                       # le lingue non sono rumore
        if quota < SOGLIA_SOSPETTO:
            break
        lf = lift(alias)
        voce = (alias, round(100 * quota, 1), round(lf, 2), sorted(canon)[:2])
        if quota > SOGLIA_NERA or lf < LIFT_MINIMO:
            nere_alias.append(alias)
            sospette.append(voce)
        else:
            tenute.append(voce)
    log.info("annunci %d, senza lingua rilevata %d (%.1f%%)", visti,
             senza_lingua, 100 * senza_lingua / max(visti, 1))
    log.info("LISTA NERA (%d, alias %% lift canoniche): %s", len(nere_alias), sospette)
    log.info("TENUTE sopra l'1,5%% (alias %% lift canoniche): %s", tenute)
    log.info("PRIME 40 COMPETENZE CANONICHE: %s",
             [(c, round(100 * n / visti, 1)) for c, n in per_canon.most_common(40)])
    random.shuffle(esempi)
    for titolo, lingua, skills in esempi[:25]:
        log.info("  · [%s] %s -> %s", lingua, (titolo or "")[:60], skills)
    con_skill = sum(1 for _, _, s in esempi if s)
    log.info("annunci del campione con almeno una competenza: %d/%d",
             con_skill, len(esempi))
    lista = sorted(set(nere_alias) | set(NERE_A_MANO))
    with open(esco.LISTA_NERA + ".tmp", "w") as f:
        json.dump(lista, f, ensure_ascii=False, indent=0)
    os.replace(esco.LISTA_NERA + ".tmp", esco.LISTA_NERA)
    if apri and visti > 1000:
        open(esco.ATTIVO, "w").write("calibrato\n")
        log.info("CANCELLO APERTO: ESCO attivo in produzione")
    elif os.path.exists(esco.ATTIVO):
        os.remove(esco.ATTIVO)
        log.info("cancello CHIUSO: ricalibrato senza --apri")
    return {"annunci": visti, "alias_visti": len(per_alias),
            "canoniche_viste": len(per_canon), "in_lista_nera": len(lista),
            "sospette": len(sospette), "cancello": "aperto" if apri else "chiuso"}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.esco_calibra")
    ap.add_argument("--apri", action="store_true",
                    help="apre il cancello di produzione a fine calibrazione")
    ap.add_argument("--campione", type=int, default=CAMPIONE)
    a = ap.parse_args()
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    print(calibra(dsn, apri=a.apri, campione=a.campione))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
