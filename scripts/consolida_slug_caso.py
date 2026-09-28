#!/usr/bin/env python3
"""Consolida i tenant duplicati solo per MAIUSCOLE («Dominos» vs «dominos»).

Scoperto il 28/09/2026: le fonti consegnano lo slug con le maiuscole a
caso e la chiave UNIQUE (platform_id, slug) non le vede uguali. Risultato:
migliaia di bacheche lette due volte, righe doppie, e — nel caso di
dominos su SmartRecruiters — 60.121 righe dove ne bastavano ~30.000.
Le API degli ATS sono case-insensitive (verificato dal vivo su
smartrecruiters e ashby: stesso totale, stesso primo annuncio).

Per ogni gruppo (platform_id, lower(slug)) con piu' di un tenant:

  1. CANONICO: il tenant VIVO (attivo, con offerte) — la lezione di Lever,
     che e' case-sensitive («academy» torna Document not found, «Academy»
     torna le offerte, misurato il 28/09): la forma minuscola puo' essere
     il guscio morto di una scoperta sbagliata. A parita' vince il
     minuscolo, poi il piu' ricco, il piu' vecchio;
  2. PROVA CHE E' LA STESSA BACHECA: almeno il 40% degli external_id del
     duplicato deve esistere gia' sotto il canonico (misurato sul caso
     dominos: 95% — i gemelli leggono la stessa finestra ogni giro, le
     storie combaciano quasi del tutto). Sotto soglia si SALTA e si
     segnala: due aziende diverse non si fondono mai per un nome simile;
  3. FUSIONE: le righe del duplicato che mancano al canonico si SPOSTANO
     (niente si butta), quelle gia' presenti si cancellano (sono copie
     identiche della stessa offerta), poi sparisce la riga del duplicato
     in ats_companies. Gli id delle offerte non cambiano: classificazioni,
     dettagli e sintesi restano attaccati;
  4. alla fine un ultimo spazzata sugli orfani: righe riapparse sotto lo
     slug del duplicato mentre lo scraper lo aveva gia' in carico.

Idempotente e riprendibile: rilanciarlo non fa danni, e la prova del 40%
viene rifatta ogni volta sui dati del momento.

    python scripts/consolida_slug_caso.py                 # piano, niente scritture
    python scripts/consolida_slug_caso.py --applica       # fonde davvero
    python scripts/consolida_slug_caso.py --applica --solo dominos   # una coppia sola
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import psycopg

SOGLIA_IDENTITA = 0.40     # quota di external_id condivisi per dire «stessa bacheca»
LOTTO = 5000               # righe per transazione: lock brevi, gli scraper non si fermano
PAUSA = 0.05               # fra un lotto e l'altro, per cortesia verso gli altri writer


def gruppi(conn, solo: str | None):
    """I gruppi (platform_id, lower(slug)) con piu' di un tenant."""
    righe = conn.execute("""
        SELECT platform_id, lower(slug) AS k, array_agg(slug ORDER BY slug)
        FROM ats_companies GROUP BY 1, 2 HAVING count(*) > 1 ORDER BY 1, 2""").fetchall()
    if solo:
        righe = [r for r in righe if solo.lower() in r[1]]
    return righe


def canonico_del(conn, pid: str, slugs: list[str]) -> str:
    """Chi si tiene il nome? Prima il tenant VIVO (attivo e con offerte):
    la lezione di Lever (28/09, case-sensitive: «academy» 404, «Academy»
    vive) — la forma minuscola puo' essere il guscio morto e quella
    maiuscola la bacheca vera. A parita' di vita vince il minuscolo, che
    e' la forma normalizzata dal trigger; poi il piu' ricco, il piu'
    vecchio."""
    r = conn.execute("""
        SELECT slug FROM ats_companies
        WHERE platform_id = %s AND slug = ANY(%s)
        ORDER BY is_active DESC, (job_count > 0) DESC,
                 (slug = lower(slug)) DESC, job_count DESC, created_at ASC, slug ASC
        LIMIT 1""", (pid, slugs)).fetchone()
    return r[0]


def ids_di(conn, pid: str, slug: str):
    """(id -> external_id) del tenant. Per i tenant grandi sono decine di
    migliaia di righe: qualche MB, accettabile una tantum."""
    return dict(conn.execute(
        "SELECT id, external_id FROM ats_jobs WHERE platform_id = %s AND slug = %s",
        (pid, slug)).fetchall())


def fondi(conn, pid: str, canonico: str, dupe: str, applica: bool) -> dict:
    """Sposta le uniche, cancella le copie, toglie il tenant duplicato."""
    st = {"mosse": 0, "cancellate": 0, "saltato": None}
    righe_dupe = ids_di(conn, pid, dupe)
    ids_can = set(ids_di(conn, pid, canonico).values())
    if righe_dupe:
        comuni = sum(1 for e in righe_dupe.values() if e in ids_can)
        quota = comuni / len(righe_dupe)
        if quota < SOGLIA_IDENTITA:
            st["saltato"] = (f"external_id condivisi solo al {quota:.0%}: "
                             f"NON e' provato che sia la stessa bacheca")
            return st
    muovere = [i for i, e in righe_dupe.items() if e not in ids_can]
    togliere = [i for i, e in righe_dupe.items() if e in ids_can]
    st["mosse"], st["cancellate"] = len(muovere), len(togliere)
    if not applica:
        return st
    # il tenant sparisce SUBITO dalle prossime selezioni dello scraper;
    # chi lo aveva gia' in carico finisce il giro e la spazzata finale
    # raccoglie cio' che scrive nel frattempo
    conn.execute("DELETE FROM ats_companies WHERE platform_id = %s AND slug = %s", (pid, dupe))
    conn.commit()
    for i in range(0, len(muovere), LOTTO):
        conn.execute("UPDATE ats_jobs SET slug = %s WHERE id = ANY(%s::uuid[])",
                     (canonico, muovere[i:i + LOTTO]))
        conn.commit()
        time.sleep(PAUSA)
    for i in range(0, len(togliere), LOTTO):
        conn.execute("DELETE FROM ats_jobs WHERE id = ANY(%s::uuid[])", (togliere[i:i + LOTTO],))
        conn.commit()
        time.sleep(PAUSA)
    return st


def orfani_globali(conn, solo: str | None):
    """(platform_id, slug) presenti in ats_jobs ma SENZA piu' una riga in
    ats_companies, con un gemello minuscolo esistente: i resti di una
    fusione interrotta a meta' (o di uno scraper in volo). E' cio' che
    rende lo script riprendibile: qualunque cosa si sia fermata, il giro
    dopo la raccoglie."""
    righe = conn.execute("""
        SELECT DISTINCT j.platform_id, j.slug FROM ats_jobs j
        WHERE NOT EXISTS (SELECT 1 FROM ats_companies c
                          WHERE c.platform_id = j.platform_id AND c.slug = j.slug)
          AND EXISTS (SELECT 1 FROM ats_companies t
                      WHERE t.platform_id = j.platform_id AND t.slug = lower(j.slug)
                        AND t.slug <> j.slug)""").fetchall()
    if solo:
        righe = [r for r in righe if solo.lower() in r[1].lower()]
    return righe


def spazzata(conn, pid: str, canonico: str, dupe: str, applica: bool) -> dict:
    """Righe orfane riapparse sotto lo slug del duplicato (scraper in
    volo durante la fusione): stesse regole della fusione."""
    righe = conn.execute("""
        SELECT j.id, j.external_id FROM ats_jobs j
        WHERE j.platform_id = %s AND j.slug = %s
          AND NOT EXISTS (SELECT 1 FROM ats_companies c
                          WHERE c.platform_id = j.platform_id AND c.slug = j.slug)""",
        (pid, dupe)).fetchall()
    if not righe:
        return {"orfane": 0}
    ids_can = set(ids_di(conn, pid, canonico).values())
    muovere = [i for i, e in righe if e not in ids_can]
    togliere = [i for i, e in righe if e in ids_can]
    if applica:
        for i in range(0, len(muovere), LOTTO):
            conn.execute("UPDATE ats_jobs SET slug = %s WHERE id = ANY(%s::uuid[])",
                         (canonico, muovere[i:i + LOTTO]))
            conn.commit()
        for i in range(0, len(togliere), LOTTO):
            conn.execute("DELETE FROM ats_jobs WHERE id = ANY(%s::uuid[])", (togliere[i:i + LOTTO],))
            conn.commit()
            time.sleep(PAUSA)
    return {"orfane": len(righe)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--applica", action="store_true", help="scrive davvero (default: solo piano)")
    ap.add_argument("--solo", help="limita ai gruppi che contengono questo testo")
    a = ap.parse_args()
    dsn = os.environ["ATS_DATABASE_URL"]
    t0 = time.time()
    tot = {"gruppi": 0, "fusi": 0, "saltati": 0, "mosse": 0, "cancellate": 0, "orfane": 0}
    with psycopg.connect(dsn) as conn:
        for pid, chiave, slugs in gruppi(conn, a.solo):
            tot["gruppi"] += 1
            can = canonico_del(conn, pid, slugs)
            for dupe in slugs:
                if dupe == can:
                    continue
                st = fondi(conn, pid, can, dupe, a.applica)
                if st["saltato"]:
                    tot["saltati"] += 1
                    print(f"SALTATO {pid}: {dupe!r} -/-> {can!r}: {st['saltato']}")
                    continue
                tot["fusi"] += 1
                tot["mosse"] += st["mosse"]
                tot["cancellate"] += st["cancellate"]
                print(f"{'FUSO' if a.applica else 'piano'} {pid}: {dupe!r} -> {can!r}  "
                      f"spostate {st['mosse']}, copie cancellate {st['cancellate']}")
                orf = spazzata(conn, pid, can, dupe, a.applica)
                if orf["orfane"]:
                    tot["orfane"] += orf["orfane"]
                    print(f"  spazzata: {orf['orfane']} orfane raccolte")
        # i resti di fusioni interrotte (o dello scraper in volo): tenant
        # spariti da ats_companies ma con righe ancora sotto il loro slug
        for pid, dupe in orfani_globali(conn, a.solo):
            orf = spazzata(conn, pid, dupe.lower(), dupe, a.applica)
            if orf["orfane"]:
                tot["orfane"] += orf["orfane"]
                print(f"  orfani globali {pid}/{dupe!r}: {orf['orfane']} righe -> {dupe.lower()!r}")
    modo = "APPLICATO" if a.applica else "SOLO PIANO (niente scritture)"
    print(f"\n{modo} in {time.time() - t0:.0f}s: {tot['gruppi']} gruppi, {tot['fusi']} duplicati fusi, "
          f"{tot['saltati']} saltati, {tot['mosse']} righe spostate, "
          f"{tot['cancellate']} copie cancellate, {tot['orfane']} orfane")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
