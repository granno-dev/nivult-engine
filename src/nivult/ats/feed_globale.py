"""Puntare dritto alle offerte attive, non alle aziende a caso.

La scoperta trova aziende che USANO un ATS; poi lo scrape le visita una
a una, e su molte non trova nulla — hanno il tenant ma zero posizioni
aperte oggi. È lavoro a vuoto.

Alcune piattaforme però pubblicano un FEED GLOBALE: un solo elenco con
le offerte attive di TUTTI i loro tenant. Lì si ragiona al contrario —
si leggono le offerte e si ricavano le aziende che ne hanno davvero,
senza una singola visita a vuoto. E si scoprono tenant nuovi già col
lavoro in mano.

SmartRecruiters è il caso d'oro: jobs.smartrecruiters.com/sr-jobs/search
elenca ~350.000 offerte, ognuna col nome dell'azienda e la località,
ordinate per data di pubblicazione (le più recenti prime). L'ordine è
ciò che rende la raccolta incrementale: ogni notte si scorre dall'alto
e ci si ferma quando si incontra una pagina di sole offerte già in
banca. Il filtro per paese lo fa il client — il server non ce l'ha —
tenendo solo i paesi che ci servono.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

import httpx
import psycopg

from .runner import ATS_DSN
from .adapters import senza_nulli

log = logging.getLogger("nivult.ats.feed_globale")

# I paesi che vale la pena tenere: quelli dei cluster e i grandi mercati
# europei vicini, per non buttare offerte utili a un iscritto futuro.
PAESI_UTILI = {"it", "fr", "de", "es", "gb", "nl", "be", "se", "ch",
               "at", "pt", "ie", "pl", "dk", "fi", "no", "lu"}

SR_SEARCH = "https://jobs.smartrecruiters.com/sr-jobs/search"


def smartrecruiters(dsn: str, paesi: set[str] | None = None,
                    pagine_max: int = 600, per_pagina: int = 100,
                    stop_dopo_vuote: int = 10) -> dict:
    """Le offerte attive di tutti i tenant SmartRecruiters, dal feed.

    Si ferma quando `stop_dopo_vuote` pagine di fila non portano più
    nessuna offerta nuova nei paesi utili: da lì in giù è tutto già in
    banca o fuori area, e insistere è sprecare richieste.
    """
    paesi = paesi or PAESI_UTILI
    stats = {"lette": 0, "nuove": 0, "aggiornate": 0, "tenant_nuovi": 0,
             "fuori_area": 0, "pagine": 0}
    tenant_visti: set[str] = set()

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ats_platforms (id, name, is_active, api_type, notes)
            VALUES ('smartrecruiters', 'SmartRecruiters', true, 'json_get',
                    'feed globale: sr-jobs/search, tutti i tenant')
            ON CONFLICT (id) DO NOTHING""")
        conn.commit()

    vuote_di_fila = 0
    with httpx.Client(timeout=30,
                      headers={"User-Agent": "nivult-ats/1.0"}) as c:
        for pagina in range(pagine_max):
            try:
                r = c.get(SR_SEARCH, params={"limit": per_pagina,
                                             "offset": pagina * per_pagina})
                if r.status_code != 200:
                    log.warning("SmartRecruiters %d a pagina %d",
                                r.status_code, pagina)
                    break
                contenuto = r.json().get("content") or []
            except (httpx.HTTPError, ValueError) as e:
                log.warning("SmartRecruiters errore pagina %d: %s", pagina, e)
                break
            if not contenuto:
                break
            stats["pagine"] += 1

            nuove_in_pagina = 0
            with psycopg.connect(dsn) as conn:
                for j in contenuto:
                    stats["lette"] += 1
                    loc = j.get("location") or {}
                    paese = (loc.get("country") or "").lower()
                    if paese not in paesi:
                        stats["fuori_area"] += 1
                        continue
                    tenant = (j.get("company") or {}).get("identifier")
                    jid = str(j.get("id") or "").strip()
                    titolo = (j.get("name") or "").strip()
                    if not tenant or not jid or not titolo:
                        continue

                    # il tenant entra nel censimento: lo scrape poi lo
                    # mantiene, ma intanto l'offerta è gia' nostra
                    if tenant not in tenant_visti:
                        tenant_visti.add(tenant)
                        r2 = conn.execute("""
                            INSERT INTO ats_companies
                              (platform_id, slug, company_name, country,
                               discovered_from)
                            VALUES ('smartrecruiters', %s, %s, %s, 'feed')
                            ON CONFLICT (platform_id, slug) DO NOTHING""",
                            (tenant, (j.get("company") or {}).get("name")
                             or tenant, paese.upper()))
                        stats["tenant_nuovi"] += r2.rowcount

                    dt = None
                    rd = j.get("releasedDate")
                    if rd:
                        try:
                            dt = datetime.fromisoformat(
                                rd.replace("Z", "+00:00"))
                        except ValueError:
                            dt = None
                    url = (f"https://jobs.smartrecruiters.com/"
                           f"{tenant}/{jid}")
                    citta = loc.get("city") or None
                    res = conn.execute("""
                        INSERT INTO ats_jobs
                          (platform_id, slug, external_id, title, url,
                           location, country, city, posted_at, raw)
                        VALUES ('smartrecruiters', %s, %s, %s, %s,
                                %s, %s, %s, %s, %s)
                        ON CONFLICT (platform_id, external_id) DO UPDATE SET
                          title = EXCLUDED.title, url = EXCLUDED.url,
                          city = EXCLUDED.city,
                          country = COALESCE(EXCLUDED.country,
                                             ats_jobs.country),
                          posted_at = EXCLUDED.posted_at,
                          -- la descrizione la recupera un passo a valle
                          -- (dettaglio /postings): non c'e' nel payload
                          -- della lista, e un raw=EXCLUDED.raw nudo la
                          -- cancellava a ogni ri-ingestione (misurato:
                          -- SR fermo al 4%). La si ri-attacca.
                          raw = CASE
                            WHEN ats_jobs.raw ? 'description'
                                 AND NOT (EXCLUDED.raw ? 'description')
                            THEN EXCLUDED.raw || jsonb_build_object(
                                   'description', ats_jobs.raw->'description')
                            ELSE EXCLUDED.raw END,
                          fetched_at = now()
                        RETURNING (xmax = 0) AS is_new
                    """, (tenant, jid, titolo[:300], url, citta,
                          paese.upper(), citta, dt,
                          psycopg.types.json.Json(senza_nulli(j))))
                    riga = res.fetchone()
                    if riga and riga[0]:
                        stats["nuove"] += 1
                        nuove_in_pagina += 1
                    else:
                        stats["aggiornate"] += 1
                conn.commit()

            # raccolta incrementale: se il feed (ordinato per data) non
            # porta piu' niente di nuovo per qualche pagina, il resto e'
            # roba gia' vista — ci si ferma.
            if nuove_in_pagina == 0:
                vuote_di_fila += 1
                if vuote_di_fila >= stop_dopo_vuote:
                    log.info("SmartRecruiters: %d pagine senza novita', "
                             "mi fermo a pagina %d", vuote_di_fila, pagina)
                    break
            else:
                vuote_di_fila = 0

    log.info("SmartRecruiters feed: %s", stats)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--smartrecruiters", action="store_true")
    ap.add_argument("--paesi", default="",
                    help="ISO2 separati da virgola; vuoto = i paesi utili")
    ap.add_argument("--pagine-max", type=int, default=600)
    ap.add_argument("--completo", action="store_true",
                    help="niente stop incrementale: scorre tutto il feed "
                         "(per il backfill iniziale, non per il notturno)")
    args = ap.parse_args()
    paesi = ({p.strip().lower() for p in args.paesi.split(",") if p.strip()}
             or None)
    if args.smartrecruiters:
        stop = args.pagine_max if args.completo else 10
        print(smartrecruiters(ATS_DSN, paesi, args.pagine_max,
                              stop_dopo_vuote=stop))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
