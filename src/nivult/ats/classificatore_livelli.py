"""Il classificatore a tre livelli — completo, in cascata.

    python -m nivult.ats.classificatore_livelli --limite 50000
    python -m nivult.ats.classificatore_livelli --stats

LIVELLO 1 — DIZIONARIO: le parole chiave nel titolo ("nurse",
"entwickler", "commis de cuisine") matchano direttamente.

LIVELLO 2 — TITOLI NOTI: fuzzy matching contro i titoli già
classificati nel database. Se "Senior ICU Nurse" non matcha il
dizionario ma "ICU Nurse" è già Healthcare nel database, lo è
anche il Senior.

LIVELLO 3 — GLM SOLO PER I RESIDUI: i titoli che nessuno dei due
livelli matcha vanno a GLM (una chiamata per offerta, solo per
il 5-10% del totale).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import unicodedata
from difflib import SequenceMatcher

import psycopg

from nivult.ats.classificatore_veloce import (
    FAMIGLIE, classifica_titolo, classifica_da_raw, _pulisci_titolo)

log = logging.getLogger("nivult.ats.classificatore_livelli")

ATS_DSN = os.environ.get(
    "ATS_DATABASE_URL",
    "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")

MODELLO = "glm-5.2"

PROMPT_GLM = """Assegna UNA famiglia professionale a questo titolo di offerta di lavoro.

Famiglie ammesse (rispondi ESATTAMENTE una di queste, in inglese):
{famiglie}

Titolo: "{titolo}"
Azienda: {azienda}
Località: {luogo}

Rispondi solo con un oggetto JSON: {{"famiglia": "<una delle famiglie>", "sicurezza": <0.0-1.0>}}
Se nessuna famiglia va bene, usa "Retail" con sicurezza 0.2."""


# ── LIVELLO 2: TITOLI NOTI (fuzzy matching) ───────────────────────

def _token(titolo: str) -> set[str]:
    """Il titolo come insieme di parole significative."""
    t = _pulisci_titolo(titolo)
    return {w for w in re.split(r"[\s/,-]+", t) if len(w) > 2}


def _somiglianza(a: set[str], b: set[str]) -> float:
    """Quanto due insiemi di parole si sovrappongono (Jaccard)."""
    if not a or not b:
        return 0.0
    intersezione = a & b
    unione = a | b
    return len(intersezione) / len(unione)


def costruisci_indice_titoli(dsn: str) -> dict[str, str]:
    """L'indice dei titoli già classificati: parole → famiglia.

    Costruito dai titoli nel database che hanno già una
    classificazione (dal dizionario o da GLM): il loro insieme di
    parole diventa la firma di quella famiglia.
    """
    firme: dict[str, str] = {}  # "parola1|parola2|..." → famiglia
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT lower(j.title), c.family
                  FROM ats_jobs j
                  JOIN job_classifications c ON c.job_id = j.id
                 WHERE j.expired_at IS NULL
                 LIMIT 50000
            """)
            for titolo, famiglia in cur.fetchall():
                parole = _token(titolo)
                if parole:
                    chiave = "|".join(sorted(parole))
                    firme[chiave] = famiglia
    return firme


def _match_titoli_noti(titolo: str, firme: dict[str, str]) -> tuple[str | None, float]:
    """(famiglia, confidenza) dal matching contro i titoli noti."""
    token = _token(titolo)
    if not token:
        return None, 0.0

    migliore_famiglia = None
    migliore_score = 0.0

    for firma, famiglia in firme.items():
        parole_firma = set(firma.split("|"))
        score = _somiglianza(token, parole_firma)
        if score > migliore_score:
            migliore_score = score
            migliore_famiglia = famiglia

    # soglia ALTISSIMA: il fuzzy matching a soglia bassa (0.45) classificava
    # 'ordinatore pacchi' come Energy e 'consegnatore giornali' come Food.
    # Un'offerta nella famiglia SBAGLIATA entra nel cluster sbagliato e
    # arriva nel digest di chi non c'entra — peggio di non classificarla.
    # A 0.85 passa solo il quasi-identico: 'Senior ICU Nurse' quando
    # 'ICU Nurse' è già noto. Il resto va a GLM.
    if migliore_score >= 0.85:
        return migliore_famiglia, round(migliore_score, 2)
    return None, 0.0


# ── LIVELLO 3: GLM (solo per i residui) ──────────────────────────

def _estrai_json(testo: str) -> dict:
    try:
        return json.loads(testo)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", testo, re.S)
        if not m:
            raise ValueError(f"nessun JSON: {testo[:120]}")
        return json.loads(m.group(0))


def _classifica_glm(titolo: str, azienda: str, luogo: str,
                    modello) -> tuple[str | None, float]:
    """Una chiamata GLM per il titolo che i livelli 1-2 non matchano."""
    prompt = PROMPT_GLM.format(
        famiglie=", ".join(FAMIGLIE),
        titolo=titolo[:120], azienda=azienda[:40], luogo=luogo[:40])
    try:
        risposta = _estrai_json(modello.chat(
            [{"role": "user", "content": prompt}]))
        famiglia = risposta.get("famiglia", "")
        sicurezza = float(risposta.get("sicurezza", 0.5))
        if famiglia not in FAMIGLIE:
            for f in FAMIGLIE:
                if f.lower() in famiglia.lower():
                    famiglia = f
                    break
            else:
                return None, 0.0
        return famiglia, sicurezza
    except Exception:
        return None, 0.0


# ── IL CLASSIFICATORE COMPLETO A TRE LIVELLI ──────────────────────

def classifica(dsn: str, limite: int = 50000, usa_glm: bool = True) -> dict:
    stats = {"viste": 0, "livello1": 0, "livello2": 0, "livello3": 0,
             "classificate": 0, "non_classificate": 0}

    # costruisci l'indice dei titoli noti (livello 2)
    log.info("costruisco l'indice dei titoli noti...")
    firme = costruisci_indice_titoli(dsn)
    log.info("indice: %d firme", len(firme))

    # prendi le offerte non classificate
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT j.id, j.title, j.platform_id, j.raw, j.slug
                  FROM ats_jobs j
             LEFT JOIN job_classifications c ON c.job_id = j.id
                 WHERE c.job_id IS NULL AND j.expired_at IS NULL
                 LIMIT %s
            """, (limite,))
            offerte = cur.fetchall()

    # livello 3: inizializza GLM solo se serve
    modello = None
    if usa_glm:
        try:
            from nivult.ats.classificatore import GLMLight
            modello = GLMLight()
            log.info("GLM disponibile per il livello 3")
        except SystemExit:
            log.info("GLM non disponibile (manca la chiave) — livello 3 disattivato")

    da_scrivere: list[tuple] = []
    for jid, titolo, pid, raw, slug in offerte:
        stats["viste"] += 1
        famiglia = None
        conf = 0.0
        livello = None

        # LIVELLO 1: dizionario
        famiglia, conf = classifica_da_raw(raw, pid)
        if famiglia:
            livello = 1
        if not famiglia:
            famiglia, conf = classifica_titolo(titolo)
            if famiglia:
                livello = 1

        # LIVELLO 2: titoli noti
        if not famiglia:
            famiglia, conf = _match_titoli_noti(titolo, firme)
            if famiglia:
                livello = 2

        # LIVELLO 3: GLM (solo se i primi due hanno fallito)
        if not famiglia and modello:
            famiglia, conf = _classifica_glm(
                titolo, slug or "", "", modello)
            if famiglia:
                livello = 3

        if famiglia:
            da_scrivere.append((jid, famiglia, conf, f"livello{livello}"))
            stats["classificate"] += 1
            stats[f"livello{livello}"] += 1
        else:
            stats["non_classificate"] += 1

        if stats["viste"] % 5000 == 0:
            log.info("  … %d viste: %s", stats["viste"], stats)

    # scrittura in batch
    if da_scrivere:
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO job_classifications
                      (job_id, family, confidence, model)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (job_id) DO NOTHING
                """, da_scrivere)
            conn.commit()

    return stats


def stats(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL")
            vive = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM job_classifications")
            tot = cur.fetchone()[0]
            cur.execute("""
                SELECT model, count(*) FROM job_classifications
                GROUP BY 1 ORDER BY 2 DESC
            """)
            per_modello = cur.fetchall()
            cur.execute("""
                SELECT family, count(*) FROM job_classifications
                GROUP BY 1 ORDER BY 2 DESC LIMIT 15
            """)
            top = cur.fetchall()
    print(f"\nofferte vive: {vive}")
    print(f"classificate: {tot} ({tot / max(vive, 1) * 100:.0f}%)")
    print("per metodo:")
    for m, n in per_modello:
        print(f"  {m:15s} {n}")
    print("top famiglie:")
    for f, n in top:
        print(f"  {f:35s} {n}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-8s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.classificatore_livelli",
                                 description=__doc__)
    ap.add_argument("--limite", type=int, default=50000)
    ap.add_argument("--no-glm", action="store_true",
                    help="solo livelli 1-2, senza GLM")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)

    if args.stats:
        stats(ATS_DSN)
    else:
        s = classifica(ATS_DSN, args.limite, usa_glm=not args.no_glm)
        print(f"\nClassificatore a livelli: {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
