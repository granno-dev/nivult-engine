"""L'organico DICHIARATO DALL'AZIENDA, letto negli annunci che gia'
abbiamo: «oltre 500 dipendenti», «3.000 Mitarbeiter», «team of 200».

E' la via pulita dove i registri non arrivano (USA privati, Italia e
Germania a pagamento): nessun captcha, nessuna licenza — la
dichiarazione della fonte, letta dove la fonte l'ha scritta, come per
il nome del datore. Sette lingue, guardie sui numeri assurdi, e per
azienda vince la MEDIANA delle dichiarazioni: un annuncio che spara un
numero strano non sposta il valore se gli altri concordano.

Campo suo (employees_self) con il conteggio delle prove
(employees_self_n): mai mescolato con registri o Wikidata — in esporta
la fonte si chiama 'self_declared'.

Pagine a chiave (id > ultimo), regola di casa: nessuna transazione
lunga, riprendibile.
"""
from __future__ import annotations

import logging
import re
import statistics

import psycopg

log = logging.getLogger("nivult.ats.organico")


def _colonna_manca(c, tabella: str, colonna: str) -> bool:
    return c.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = %s AND column_name = %s",
        (tabella, colonna)).fetchone() is None


# «oltre/über/more than/plus de/più di/mais de/meer dan» + numero + parola
_NUM = r"([0-9][0-9.,  ]{0,8}[0-9]|[0-9])"
_RX = re.compile(
    r"(?:\b(?:over|more than|nearly|around|approximately|about|some|"
    r"oltre|piu' di|più di|circa|"
    r"über|ueber|mehr als|rund|knapp|etwa|"
    r"plus de|pres de|près de|environ|"
    r"mas de|más de|cerca de|"
    r"meer dan|ruim|"
    r"over|fler än|mer än|drygt)\s+)?"
    + _NUM +
    r"\s*\+?\s*"
    r"(?:employees|people\s+worldwide|staff\s+members|professionals\s+worldwide|"
    r"dipendenti|collaboratori|"
    r"mitarbeiter(?:innen|nde)?(?:\s*\(m/w/d\))?|besch[aä]ftigte|"
    r"collaborateurs(?:\.trices)?|salari[eé]s|"
    r"empleados|colaboradores|funcion[aá]rios|"
    r"medewerkers|medarbetare|anst[aä]llda|ansatte|ty[oö]ntekij[aä][aä])\b",
    re.I)

# i numeri-slogan che non sono un organico
_MIN, _MAX = 10, 3_000_000


_ANNO = re.compile(r"(?:\bin|\bsince|\bdal|\bnel|\bseit|\bdepuis|"
                   r"\bdesde|\bfounded|\bgegr[u\u00fc]ndet|\bfond[e\u00e9]e?)\s*$",
                   re.I)


def _numeri(testo: str) -> list[int]:
    out = []
    for m in _RX.finditer(testo):
        grezzo = re.sub(r"[.,  ]", "", m.group(1))
        try:
            n = int(grezzo)
        except ValueError:
            continue
        if not (_MIN <= n <= _MAX):
            continue
        # «in 2023 employees joined»: un anno preceduto da in/dal/seit
        # non e' un organico. Un'azienda con davvero 2019 dipendenti
        # perde un voto, la mediana degli altri annunci la protegge.
        if 1990 <= n <= 2035 and _ANNO.search(
                testo[max(0, m.start()-12):m.start()]):
            continue
        out.append(n)
    return out


_CHIAVI = ("description", "descriptionHtml", "descriptionPlain",
           "jobDescription", "job_description", "content",
           "externalDescription")


def _testo(raw: dict) -> str:
    for k in _CHIAVI:
        v = raw.get(k)
        if isinstance(v, str) and len(v) > 100:
            return re.sub(r"<[^>]+>", " ", v)[:20000]
    return ""


def estrai(dsn: str) -> dict:
    """Scorre le offerte attive con testo e accumula le dichiarazioni
    per azienda; alla fine scrive la mediana in ats_companies."""
    stats = {"lette": 0, "con_dichiarazione": 0, "aziende": 0}
    dichiarazioni: dict[tuple, list[int]] = {}
    with psycopg.connect(dsn, autocommit=True) as c:
        if _colonna_manca(c, "ats_companies", "employees_self"):
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT "
                      "EXISTS employees_self int")
            c.execute("ALTER TABLE ats_companies ADD COLUMN IF NOT "
                      "EXISTS employees_self_n int")
        ultimo = None
        while True:
            filtro = "AND id > %s" if ultimo is not None else ""
            parametri = (ultimo, ) if ultimo is not None else ()
            righe = c.execute(f"""
                SELECT id, platform_id, slug, raw FROM ats_jobs
                 WHERE expired_at IS NULL {filtro}
                 ORDER BY id LIMIT 5000""", parametri).fetchall()
            if not righe:
                break
            for jid, pid, slug, raw in righe:
                stats["lette"] += 1
                testo = _testo(raw or {})
                if not testo:
                    continue
                nums = _numeri(testo)
                if nums:
                    stats["con_dichiarazione"] += 1
                    # un annuncio vota UNA volta, col suo numero maggiore
                    # (spesso cita reparto e gruppo: il gruppo e' l'organico)
                    dichiarazioni.setdefault((pid, slug), []).append(
                        max(nums))
            ultimo = righe[-1][0]
            if stats["lette"] % 100000 < 5000:
                log.info("%d lette, %d dichiarazioni, %d aziende",
                         stats["lette"], stats["con_dichiarazione"],
                         len(dichiarazioni))
        for (pid, slug), nums in dichiarazioni.items():
            mediana = int(statistics.median(nums))
            c.execute("""UPDATE ats_companies
                            SET employees_self = %s, employees_self_n = %s
                          WHERE platform_id = %s AND slug = %s""",
                      (mediana, len(nums), pid, slug))
            stats["aziende"] += 1
    log.info("organico dichiarato: %s", stats)
    return stats


def main() -> int:
    from .runner import ATS_DSN
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    import json
    print(json.dumps(estrai(ATS_DSN)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
