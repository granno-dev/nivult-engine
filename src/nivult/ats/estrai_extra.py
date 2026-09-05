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
import time

import psycopg

log = logging.getLogger("nivult.ats.estrai_extra")


def _colonna_manca(c, tabella: str, colonna: str) -> bool:
    """Un ALTER TABLE, anche IF NOT EXISTS, chiede il lock esclusivo: in
    coda dietro una transazione lunga CONGELA tutto cio' che arriva dopo
    (successo: mezz'ora di sistema fermo, dashboard compresa). Il DDL
    nei cicli caldi si esegue SOLO se serve davvero: prima si guarda il
    catalogo, che non chiede lock a nessuno."""
    return c.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (tabella, colonna)).fetchone() is None


def _tabella_manca(c, tabella: str) -> bool:
    return c.execute("SELECT to_regclass(%s)",
                     (tabella,)).fetchone()[0] is None

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
        if _colonna_manca(c, "ats_jobs", "extra_checked_at"):
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


# ── strato GLM (gratuito) sul residuo con descrizione ───────────────
_PROMPT_EXTRA = """Read this job posting. Answer ONLY with JSON:
{{"employment_type":"full_time|part_time|contract|temporary|internship|apprenticeship|unknown","seniority":"intern|junior|mid|senior|lead|head|unknown"}}
Use "unknown" when the posting does not say it. Never guess.
TITLE: {t}
TEXT: {d}"""

_VAL_ET = {"full_time", "part_time", "contract", "temporary",
           "internship", "apprenticeship"}
_VAL_SEN = {"intern", "junior", "mid", "senior", "lead", "head"}


def glm_residuo(dsn: str, limite: int = 2000) -> dict:
    """Contratto e seniority dal TESTO, per le offerte dove le regole
    non li hanno trovati: GLM Flash (gratuito) li legge dove sono
    scritti in modi che nessuna regex enumera. Non e' una stima: se
    l'annuncio non lo dice, il modello risponde unknown e il campo
    resta NULL. Marcatore suo (glm_extra_at); chiamata fallita = riga
    NON marcata (bruciarla la toglierebbe dai ritenti per sempre)."""
    import json as _json
    from .profilo import _glm_flash, _descrizione
    stats = {"esaminate": 0, "contratto": 0, "seniority": 0,
             "unknown": 0, "errori": 0}
    modello = _glm_flash()
    ko_di_fila = 0
    with psycopg.connect(dsn, autocommit=True) as c:
        if _colonna_manca(c, "ats_jobs", "glm_extra_at"):
            c.execute("ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS "
                      "glm_extra_at timestamptz")
        righe = c.execute("""
            SELECT id, title, raw FROM ats_jobs
             WHERE expired_at IS NULL AND glm_extra_at IS NULL
               AND (employment_type IS NULL OR seniority IS NULL)
               AND extra_checked_at IS NOT NULL
             ORDER BY posted_at DESC NULLS LAST
             LIMIT %s""", (limite,)).fetchall()
        for jid, titolo, raw in righe:
            descr = _descrizione(raw)
            if not descr:
                c.execute("UPDATE ats_jobs SET glm_extra_at = now() "
                          "WHERE id = %s", (jid,))
                continue
            stats["esaminate"] += 1
            try:
                r = modello.chat([{"role": "user",
                                   "content": _PROMPT_EXTRA.format(
                                       t=(titolo or "")[:100],
                                       d=descr[:1200])}], max_tokens=60)
            except Exception:                        # noqa: BLE001
                stats["errori"] += 1
                ko_di_fila += 1
                if ko_di_fila >= 3:
                    log.warning("GLM giu' (429/credito?): lotto interrotto")
                    break
                continue
            ko_di_fila = 0
            time.sleep(0.4)      # il piano gratuito ha un rate limit
            et = sen = None
            try:
                g = _json.loads(re.search(r"\{.*\}", r, re.S).group(0))
                if g.get("employment_type") in _VAL_ET:
                    et = g["employment_type"]
                if g.get("seniority") in _VAL_SEN:
                    sen = g["seniority"]
            except Exception:                        # noqa: BLE001
                pass
            if not et and not sen:
                stats["unknown"] += 1
            c.execute("""UPDATE ats_jobs
                            SET employment_type = coalesce(employment_type, %s),
                                seniority = coalesce(seniority, %s),
                                glm_extra_at = now()
                          WHERE id = %s""", (et, sen, jid))
            stats["contratto"] += 1 if et else 0
            stats["seniority"] += 1 if sen else 0
    log.info("glm_residuo: %s", stats)
    return stats


def main() -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.estrai_extra")
    ap.add_argument("--limite", type=int, default=100000)
    ap.add_argument("--glm", type=int, default=0, metavar="N",
                    help="strato GLM sul residuo: al piu' N offerte")
    args = ap.parse_args()
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    if args.glm:
        print(glm_residuo(dsn, args.glm))
        return 0
    print(arricchisci(dsn, args.limite))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
