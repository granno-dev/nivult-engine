"""Tipo di contratto e contatto del recruiter: due campi che valgono.

- employment_type: il campo che ogni compratore di dati si aspetta
  (full_time, part_time, contract, temporary, internship,
  apprenticeship). Prima si leggono i campi strutturati che le
  piattaforme gia' danno (employmentType del JSON-LD, commitment di
  lever, eccetera), poi le parole nel titolo/testo in sette lingue.
- contact_email: SOLO l'email che il datore ha scritto NELL'ANNUNCIO —
  pubblicata apposta per essere contattati, non pescata altrove. E' la
  via pulita al valore che Mantiks vende: TheirStack dichiara di
  fermarsi alle aziende, noi arriviamo alla porta giusta quando il
  datore stesso l'ha indicata.

Passata bulk con marcatore (extra_checked_at), lotti ordinati per id
(regola di casa anti-deadlock), nel giro continuo dell'arricchimento.
"""
from __future__ import annotations

import logging
import os
import re

import psycopg

log = logging.getLogger("nivult.ats.estrai_extra")

# campi strutturati per piattaforma/standard -> nostro vocabolario
_MAPPA = {
    "full-time": "full_time", "full_time": "full_time",
    "fulltime": "full_time", "permanent": "full_time",
    "part-time": "part_time", "part_time": "part_time",
    "parttime": "part_time",
    "contract": "contract", "contractor": "contract",
    "freelance": "contract",
    "temporary": "temporary", "temp": "temporary", "interim": "temporary",
    "internship": "internship", "intern": "internship",
    "apprenticeship": "apprenticeship", "apprentice": "apprenticeship",
}
_CHIAVI_RAW = ("employmentType", "employment_type", "commitment",
               "workType", "type", "contract_type", "jobType")

# le parole nel testo, per lingua: cercate nel TITOLO prima (piu'
# affidabile), poi nel testo
_PAROLE = [
    ("internship", r"\b(internship|intern|stage|stagista|tirocin\w+|"
                   r"praktikum|praktikant\w*|becario|est[aá]gio)\b"),
    ("apprenticeship", r"\b(apprentice\w*|apprenti\w*|alternance|"
                       r"ausbildung|azubi|apprendist\w+|lehrling)\b"),
    ("part_time", r"\b(part[ -]?time|teilzeit|deeltijd|tempo parziale|"
                  r"temps partiel|media jornada|deltid)\b"),
    ("temporary", r"\b(temporary|cdd|zeitarbeit|interinato|interim|"
                  r"a tempo determinato|tijdelijk|vikariat)\b"),
    ("contract", r"\b(freelance|contractor|b2b contract|partita iva)\b"),
    ("full_time", r"\b(full[ -]?time|vollzeit|voltijd|tempo pieno|"
                  r"temps plein|jornada completa|cdi|heltid|"
                  r"a tempo indeterminato)\b"),
]
_PAROLE_RX = [(v, re.compile(rx, re.I)) for v, rx in _PAROLE]

# email nel testo dell'annuncio: quella del datore, non rumore da
# infrastruttura (noreply, esempio, privacy)
_EMAIL = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,12}")
_EMAIL_NO = re.compile(r"noreply|no-reply|donotreply|example|sentry|"
                       r"@.*\.(png|jpg|gif|css|js)$|privacy@|dpo@|"
                       r"unsubscribe|webmaster@", re.I)

_DESCR = ("COALESCE(raw->>'description', raw->>'externalDescription', "
          "raw->>'descriptionHtml', raw->>'jobDescription', "
          "raw->>'job_description', raw->>'content', '')")


def _tipo_da_raw(raw: dict) -> str | None:
    for k in _CHIAVI_RAW:
        v = raw.get(k)
        if isinstance(v, dict):
            v = v.get("name") or v.get("label") or v.get("value")
        if isinstance(v, list) and v:
            v = v[0]
        if isinstance(v, str):
            t = _MAPPA.get(v.strip().lower().replace(" ", "_"))
            if t:
                return t
    return None


def _tipo_da_testo(titolo: str, testo: str) -> str | None:
    for campo in (titolo or "", (testo or "")[:4000]):
        for valore, rx in _PAROLE_RX:
            if rx.search(campo):
                return valore
    return None


def _email_da_testo(testo: str) -> str | None:
    for m in _EMAIL.finditer(testo or ""):
        e = m.group(0).lower().rstrip(".")
        if not _EMAIL_NO.search(e) and len(e) <= 80:
            return e
    return None


def arricchisci(dsn: str, limite: int = 100000) -> dict:
    stats = {"esaminate": 0, "tipo": 0, "contatto": 0}
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS "
                  "employment_type text")
        c.execute("ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS "
                  "contact_email text")
        c.execute("ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS "
                  "extra_checked_at timestamptz")
        righe = c.execute(f"""
            SELECT id, title, raw, {_DESCR}
              FROM ats_jobs
             WHERE expired_at IS NULL AND extra_checked_at IS NULL
             ORDER BY posted_at DESC NULLS LAST
             LIMIT %s""", (limite,)).fetchall()

        def scrivi(lotto: list) -> None:
            lotto.sort(key=lambda r: r[2])
            for _ in range(2):
                try:
                    with c.cursor() as cur:
                        cur.executemany(
                            "UPDATE ats_jobs SET employment_type=%s, "
                            "contact_email=%s, extra_checked_at=now() "
                            "WHERE id=%s", lotto)
                    return
                except psycopg.errors.DeadlockDetected:
                    pass
            raise RuntimeError("estrai_extra: lotto bloccato due volte")

        lotto: list = []
        for jid, titolo, raw, descr in righe:
            stats["esaminate"] += 1
            tipo = _tipo_da_raw(raw if isinstance(raw, dict) else {}) \
                or _tipo_da_testo(titolo, descr)
            email = _email_da_testo(descr)
            if tipo:
                stats["tipo"] += 1
            if email:
                stats["contatto"] += 1
            lotto.append((tipo, email, jid))
            if len(lotto) >= 300:
                scrivi(lotto)
                lotto = []
        if lotto:
            scrivi(lotto)
    log.info("estrai_extra: %s", stats)
    return stats


def main() -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.estrai_extra")
    ap.add_argument("--limite", type=int, default=100000)
    args = ap.parse_args()
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    print(arricchisci(dsn, args.limite))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
