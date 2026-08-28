"""Il classificatore: assegna ogni offerta ATS a una famiglia professionale.

    python -m nivult.ats.classificatore --limite 500
    python -m nivult.ats.classificatore --stats

GLM valuta il titolo (e la località quando aiuta) e sceglie UNA delle
33 famiglie del vocabolario del motore. Le famiglie sono ESATTAMENTE
quelle di job_families nel motore: quello che esce da qui è già
compatibile con i cluster del funnel.

Una chiamata per offerta (il batch induce punteggi relativi — stessa
lezione del matching). Il costo: ~0,005 ¢ per titolo, poche decine di
euro per tutto il database europeo.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re

import psycopg
from dotenv import load_dotenv

from nivult.ingestion.base import HttpSource

load_dotenv()

log = logging.getLogger("nivult.ats.classificatore")

ATS_DSN = os.environ.get(
    "ATS_DATABASE_URL",
    "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")

# Le famiglie del vocabolario del motore (job_families). In caso di
# divergenza futura: SELECT family FROM job_families ORDER BY sort_order
FAMIGLIE = [
    "Administrative", "Agriculture", "Art & Design", "Construction",
    "Consulting", "Creative & Media", "Customer Service & Support",
    "Data & Analytics", "Education", "Energy", "Engineering",
    "Environmental & Sustainability", "Finance & Accounting",
    "Food & Beverage", "Government & Public Sector", "Healthcare",
    "Hospitality", "Human Resources", "Legal", "Logistics",
    "Management & Leadership", "Manufacturing", "Marketing", "Retail",
    "Sales", "Science & Research", "Security & Safety", "Social Services",
    "Software", "Sports & Recreation", "Technology", "Trades",
    "Transportation",
]

MODELLO = "glm-5.2"

PROMPT = """Assegna UNA famiglia professionale a questo titolo di offerta di lavoro.

Famiglie ammesse (rispondi ESATTAMENTE una di queste, in inglese):
{famiglie}

Titolo: "{titolo}"
Azienda: {azienda}
Località: {luogo}

Rispondi solo con un oggetto JSON: {{"famiglia": "<una delle famiglie>", "sicurezza": <0.0-1.0>}}
Se nessuna famiglia va bene, usa "Retail" con sicurezza 0.2."""


def _estrai_json(testo: str) -> dict:
    try:
        return json.loads(testo)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", testo, re.S)
        if not m:
            raise ValueError(f"nessun JSON: {testo[:120]}")
        return json.loads(m.group(0))


class GLMLight(HttpSource):
    """Il client GLM minimale per la classificazione (thinking off)."""

    def __init__(self):
        super().__init__(rate_per_second=4.0, timeout=60.0)
        self.api_key = os.environ.get("GLM_API_KEY", "")
        if not self.api_key:
            raise SystemExit("Serve GLM_API_KEY.")
        self.base_url = os.environ.get(
            "GLM_BASE_URL", "https://api.z.ai/api/paas/v4")

    def chat(self, messages: list[dict]) -> str:
        payload = {"model": MODELLO, "messages": messages,
                   "temperature": 0.0, "max_tokens": 100,
                   "thinking": {"type": "disabled"}}
        r = self.request("POST", f"{self.base_url}/chat/completions",
                         headers={"Authorization": f"Bearer {self.api_key}",
                                  "Content-Type": "application/json"},
                         json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"GLM ({r.status_code}): {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"]


def classifica(dsn: str, limite: int = 500, solo_paesi: str | None = None) -> dict:
    """Classifica le offerte vive non ancora etichettate."""
    modello = GLMLight()
    stats = {"viste": 0, "classificate": 0, "scartate": 0, "errore": 0}
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            sql = """
                SELECT j.id, j.title, j.slug, j.city, j.country
                  FROM ats_jobs j
             LEFT JOIN job_classifications c ON c.job_id = j.id
                 WHERE c.job_id IS NULL
                   AND j.expired_at IS NULL
            """
            params: list = []
            if solo_paesi:
                sql += " AND j.country = ANY(%s)"
                params.append(solo_paesi.split(","))
            sql += " LIMIT %s"
            params.append(limite)
            cur.execute(sql, params)
            offerte = cur.fetchall()

    log.info("classificatore: %d offerte da etichettare", len(offerte))

    def etichetta(riga):
        jid, titolo, slug, citta, paese = riga
        prompt = PROMPT.format(
            famiglie=", ".join(FAMIGLIE),
            titolo=(titolo or "")[:120],
            azienda=(slug or "")[:40],
            luogo=(citta or paese or "")[:40])
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
                return None
        return (jid, famiglia, sicurezza, MODELLO)

    # le chiamate GLM in parallelo (HttpSource regola già il ritmo
    # complessivo), le scritture DB in batch dal thread principale:
    # una connessione per tutto il giro, non una per offerta
    from concurrent.futures import ThreadPoolExecutor, as_completed
    da_scrivere: list[tuple] = []
    with psycopg.connect(dsn) as conn:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(etichetta, r) for r in offerte]
            for fut in as_completed(futures):
                stats["viste"] += 1
                try:
                    riga = fut.result()
                    if riga:
                        da_scrivere.append(riga)
                        stats["classificate"] += 1
                    else:
                        stats["scartate"] += 1
                except Exception as exc:  # noqa: BLE001
                    stats["errore"] += 1
                    log.warning("  errore: %s", str(exc)[:80])
                if len(da_scrivere) >= 200:
                    with conn.cursor() as cur:
                        cur.executemany("""
                            INSERT INTO job_classifications
                              (job_id, family, confidence, model)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (job_id) DO NOTHING
                        """, da_scrivere)
                    conn.commit()
                    da_scrivere.clear()
                if stats["viste"] % 500 == 0:
                    log.info("  … %d viste: %s", stats["viste"], stats)
        if da_scrivere:
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
            classificate = cur.fetchone()[0]
            cur.execute("""
                SELECT c.family, count(*) FROM job_classifications c
                GROUP BY 1 ORDER BY 2 DESC LIMIT 10
            """)
            top = cur.fetchall()
    print(f"\nofferte vive:      {vive}")
    print(f"classificate:      {classificate} "
          f"({classificate / max(vive, 1) * 100:.0f}%)")
    print("top famiglie:")
    for f, n in top:
        print(f"  {f:35s} {n}")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-8s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.classificatore",
                                 description=__doc__)
    ap.add_argument("--limite", type=int, default=500)
    ap.add_argument("--paesi", default=None,
                    help="solo questi paesi ISO, separati da virgola")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)

    if args.stats:
        stats(ATS_DSN)
    else:
        s = classifica(ATS_DSN, args.limite, args.paesi)
        print(f"\nClassificatore: {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
