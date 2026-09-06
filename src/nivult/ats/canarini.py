"""I canarini: tre tenant di riferimento per piattaforma, riletti ogni ora.

Un adapter che smette di leggere una bacheca non fa rumore: torna una
lista vuota e il runner scrive «zero offerte». Il canarino e' la pagina
di cui sappiamo gia' cosa deve uscire: se tutti e tre i tenant piu'
grossi di una piattaforma danno zero nella stessa ora, non sono le
bacheche a essersi svuotate — e' l'adapter che non legge piu'. Si
scopre entro l'ora, non tre giorni dopo dalle scadenze.

    python -m nivult.ats.canarini --scegli      # (ri)sceglie i canarini, per piattaforma
    python -m nivult.ats.canarini --controlla   # li rilegge: scrive canarini_esiti e /opt/nivult/canarini.json
    python -m nivult.ats.canarini --stato       # l'ultimo esito per piattaforma

Sceglie i 3 tenant con piu' offerte attive (>= 5) fra quelli letti con
successo negli ultimi 7 giorni; li rinnova ogni settimana. Le
piattaforme che non si leggono con un adapter semplice (Workday,
In-recruiting, iCIMS via browser) restano fuori: li' il canarino sarebbe
un test di un'altra cosa.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re

import time

import psycopg

from .adapters import ADAPTERS, LetturaFallita

log = logging.getLogger("nivult.ats.canarini")
ESITO_FILE = "/opt/nivult/canarini.json"
CAMPIONI_DIR = "/opt/nivult/campioni"
FUORI = {"workday", "inrecruiting", "icims"}
PER_PIATTAFORMA = 3
MINIMO_ATTESE = 5


def _dsn() -> str:
    return os.environ.get("ATS_DATABASE_URL") or _dsn_da_env()


def _dsn_da_env() -> str:
    for f in ("/opt/nivult/.env", "/opt/nivult/engine/.env"):
        try:
            m = re.search(r"^POSTGRES_PASSWORD=(.*)$", open(f).read(), re.M)
            if m:
                return "postgresql://nivult:" + m.group(1).strip() + "@127.0.0.1:5432/nivult_ats"
        except OSError:
            pass
    raise SystemExit("ATS_DATABASE_URL assente")


def scegli(dsn: str) -> int:
    """Per ogni piattaforma con adapter, i 3 tenant piu' grossi letti con
    successo di recente. Sostituisce i canarini piu' vecchi di 7 giorni o
    che non hanno piu' offerte."""
    n = 0
    with psycopg.connect(dsn, autocommit=True) as db:
        for pid in sorted(ADAPTERS):
            if pid in FUORI:
                continue
            righe = db.execute("""
                SELECT c.slug, count(j.id) AS attive
                  FROM ats_companies c
                  JOIN ats_jobs j ON j.platform_id = c.platform_id AND j.slug = c.slug AND j.expired_at IS NULL
                 WHERE c.platform_id = %s AND c.is_active
                   AND c.last_ok_at > now() - interval '7 days'
                 GROUP BY c.slug HAVING count(j.id) >= %s
                 ORDER BY count(j.id) DESC LIMIT %s""", (pid, MINIMO_ATTESE, PER_PIATTAFORMA)).fetchall()
            if not righe:
                continue
            db.execute("DELETE FROM canarini WHERE platform_id = %s AND (scelto_at < now() - interval '7 days' "
                       "OR slug <> ALL(%s))", (pid, [s for s, _ in righe]))
            for slug, attese in righe:
                db.execute("INSERT INTO canarini (platform_id, slug, attese) VALUES (%s, %s, %s) "
                           "ON CONFLICT (platform_id, slug) DO UPDATE SET attese = EXCLUDED.attese",
                           (pid, slug, attese))
                n += 1
    return n


def _leggi(pid: str, slug: str) -> tuple[int | None, str | None]:
    """(trovate, pagina). trovate = None se la lettura e' fallita.

    Gira in un thread: il limite di tempo e' quello del client HTTP
    dell'adapter (30 s a richiesta), non un alarm — che funziona solo nel
    thread principale. Il primo giro sequenziale e' durato piu' di 15
    minuti su 130 canarini; in parallelo sta nei limiti di un cron orario."""
    try:
        with ADAPTERS[pid]() as a:
            jobs = a.jobs(slug)
            return len(jobs), (a.ultima_pagina if not jobs else None)
    except LetturaFallita as exc:
        log.warning("canarino %s/%s: lettura fallita (%s)", pid, slug, exc)
        return None, None
    except Exception as exc:  # noqa: BLE001
        log.warning("canarino %s/%s: errore %s", pid, slug, type(exc).__name__)
        return None, None


def controlla(dsn: str, solo: str | None = None) -> dict:
    """Rilegge tutti i canarini (o quelli di UNA piattaforma). Una
    piattaforma e' ROTTA se tutti i suoi canarini danno zero con lettura
    riuscita; e' BLOCCATA se tutte le letture falliscono (429/403: rate
    limit o ban, non un template). Con `solo` non tocca il file degli
    esiti globale: e' la verifica dopo un deploy dell'officina."""
    rotte, bloccate = [], []
    vivi = letti = 0
    with psycopg.connect(dsn, autocommit=True) as db:
        can = db.execute("SELECT platform_id, slug, attese FROM canarini WHERE (%s::text IS NULL OR platform_id = %s) "
                         "ORDER BY 1, 2", (solo, solo)).fetchall()
        per_pid: dict[str, list] = {}
        for pid, slug, attese in can:
            per_pid.setdefault(pid, []).append((slug, attese))
        # le letture in parallelo (8 alla volta, una piattaforma per
        # thread cosi' non si martella lo stesso dominio), le scritture
        # qui nel thread principale
        from concurrent.futures import ThreadPoolExecutor
        def _leggi_piattaforma(pid):
            return pid, [(slug, attese, *_leggi(pid, slug)) for slug, attese in per_pid[pid]]
        with ThreadPoolExecutor(max_workers=8) as pool:
            letture = dict(pool.map(_leggi_piattaforma, per_pid))
        for pid, lista in per_pid.items():
            esiti = []
            for slug, attese, trovate, pagina in letture[pid]:
                campione = None
                if trovate == 0 and pagina:
                    try:
                        d = os.path.join(CAMPIONI_DIR, pid)
                        os.makedirs(d, exist_ok=True)
                        campione = os.path.join(d, f"canarino-{slug[:60]}-{time.strftime('%Y%m%d-%H%M')}.html")
                        with open(campione, "w") as f:
                            f.write(pagina[:2_000_000])
                    except OSError:
                        campione = None
                db.execute("INSERT INTO canarini_esiti (platform_id, slug, trovate, attese, campione) VALUES (%s,%s,%s,%s,%s)",
                           (pid, slug, trovate, attese, campione))
                esiti.append((slug, attese, trovate, campione))
            riuscite = [e for e in esiti if e[2] is not None]
            letti += len(riuscite)
            vivi += sum(1 for e in riuscite if e[2] > 0)
            if riuscite and all(e[2] == 0 for e in riuscite) and len(riuscite) >= min(2, len(esiti)):
                rotte.append({"piattaforma": pid,
                              "canarini": [{"slug": s, "attese": a, "campione": c} for s, a, t, c in esiti]})
            elif esiti and not riuscite:
                bloccate.append({"piattaforma": pid, "canarini": len(esiti)})
    out = {"at": time.time(), "rotte": rotte, "bloccate": bloccate,
           "piattaforme": len(per_pid), "canarini": len(can), "vivi": vivi, "letti": letti}
    if solo is None:
        try:
            with open(ESITO_FILE, "w") as f:
                json.dump(out, f)
        except OSError:
            pass
    return out


def stato(dsn: str) -> list[tuple]:
    with psycopg.connect(dsn) as db:
        return db.execute("""
            SELECT DISTINCT ON (platform_id, slug) platform_id, slug, at, trovate, attese
              FROM canarini_esiti ORDER BY platform_id, slug, at DESC""").fetchall()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.ats.canarini", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scegli", action="store_true")
    ap.add_argument("--controlla", action="store_true")
    ap.add_argument("--stato", action="store_true")
    ap.add_argument("--piattaforma", help="solo questa (dopo un deploy dell'officina)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    dsn = _dsn()
    if a.scegli:
        print(f"canarini scelti: {scegli(dsn)}")
    if a.controlla:
        r = controlla(dsn, a.piattaforma)
        print(f"canarini: {r['canarini']} su {r['piattaforme']} piattaforme — VIVI {r['vivi']}/{r['letti']} — "
              f"ROTTE: {', '.join(x['piattaforma'] for x in r['rotte']) or 'nessuna'} — "
              f"bloccate: {', '.join(x['piattaforma'] for x in r['bloccate']) or 'nessuna'}")
    if a.stato:
        for pid, slug, at, trovate, attese in stato(dsn):
            print(f"{pid:18s} {slug[:30]:30s} {at:%d/%m %H:%M}  {('FALLITA' if trovate is None else trovate):>7}  attese {attese}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
