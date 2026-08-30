"""I servizi pubblici per l'impiego europei — API dirette, gratis.

    python -m nivult.ats.servizi_pubblici --arbetsformedlingen --limite 1000
    python -m nivult.ats.servizi_pubblici --francetravail --limite 1000
    python -m nivult.ats.servizi_pubblici --stats

Queste sono le fonti che Fantastic Jobs rivende: i portali nazionali
per l'impiego. Le chiamiamo direttamente, senza intermediario.

Compatibilità coi filtri utente (user_clusters):
- lingua: il campo ai_job_language si mappa dalla lingua dell'annuncio
- tipo contratto: employment_type → FULL_TIME / PART_TIME / CONTRACT
- seniority: data dall'API quando c'è (occupation field)

Ogni offerta ha:
- url: link diretto al portale nazionale (NON attraverso aggregatori)
- paese: ISO certo (il servizio nazionale LO SA)
- data: publication_date o equivalente
- città: workplace_address o equivalente
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime

import httpx
import psycopg

log = logging.getLogger("nivult.ats.servizi_pubblici")

ATS_DSN = os.environ.get(
    "ATS_DATABASE_URL",
    "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")


# ── ARBETSÖRMEDLINGEN (Svezia) ────────────────────────────────────

def _af_iso_tipo(tipo: dict | None) -> str | None:
    """L'etichetta svedese del tipo di impiego → il nostro vocabolario."""
    if not isinstance(tipo, dict):
        return None
    label = (tipo.get("label") or "").lower()
    if "tidsbegränsad" in label or "visstid" in label:
        return "CONTRACT"
    if "deltid" in label:
        return "PART_TIME"
    if "tillsvidare" in label or "fast" in label:
        return "FULL_TIME"
    return None


def scarica_arbetsformedlingen(dsn: str, limite: int = 1000) -> dict:
    """Arbetsförmedlingen JobTech API — gratis, senza auth, documentata.

    Base: https://jobsearch.api.jobtechdev.se/search
    Paginazione: offset/limit (max 100 per pagina).
    """
    stats = {"offerte": 0, "nuove": 0, "aggiornate": 0, "errori": 0}
    base = "https://jobsearch.api.jobtechdev.se/search"
    PER_PAGINA = 100

    with httpx.Client(timeout=30, headers={"User-Agent": "nivult-ats/0.1"}) as c:
        for offset in range(0, limite, PER_PAGINA):
            try:
                r = c.get(base, params={
                    "limit": PER_PAGINA,
                    "offset": offset,
                    "sort": "pubdate-desc",
                })
                if r.status_code != 200:
                    log.warning("AF %d a offset %d", r.status_code, offset)
                    break
                hits = r.json().get("hits", [])
                if not hits:
                    break
            except httpx.HTTPError as exc:
                log.warning("AF errore: %s", exc)
                stats["errori"] += 1
                break

            for hit in hits:
                jid = str(hit.get("id") or "")
                if not jid:
                    continue
                stats["offerte"] += 1

                # i campi che servono ai filtri utente
                loc = hit.get("workplace_address") or {}
                citta = loc.get("municipality") if isinstance(loc, dict) else None
                pubbl = hit.get("publication_date")
                try:
                    dt = datetime.fromisoformat(pubbl) if pubbl else None
                except ValueError:
                    dt = None
                tipo = _af_iso_tipo(hit.get("employment_type"))
                occupazione = hit.get("occupation") or {}
                occ_label = occupazione.get("label") if isinstance(occupazione, dict) else None

                with psycopg.connect(dsn) as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO ats_jobs
                              (platform_id, slug, external_id, title, url,
                               location, country, city, posted_at, raw)
                            VALUES ('arbetsformedlingen', 'platsbanken', %s, %s, %s,
                                    %s, 'SE', %s, %s, %s)
                            ON CONFLICT (platform_id, external_id) DO UPDATE SET
                              title = EXCLUDED.title, url = EXCLUDED.url,
                              location = EXCLUDED.location, city = EXCLUDED.city,
                              posted_at = EXCLUDED.posted_at, raw = EXCLUDED.raw,
                              fetched_at = now()
                            RETURNING (xmax = 0) AS is_new
                        """, (jid,
                              hit.get("headline") or "",
                              hit.get("webpage_url") or "",
                              citta, citta, dt,
                              psycopg.types.json.Json(hit)))
                        r2 = cur.fetchone()
                        if r2 and r2[0]:
                            stats["nuove"] += 1
                        else:
                            stats["aggiornate"] += 1
                    conn.commit()

    log.info("Arbetsförmedlingen: %s", stats)
    return stats


# ── FRANCE TRAVAIL ────────────────────────────────────────────────

def _ft_token(client_id: str, client_secret: str) -> str | None:
    """Il token OAuth2 per France Travail (flusso client_credentials)."""
    r = httpx.post(
        "https://entreprise.francetravail.fr/connexion/oauth2/access-token"
        "?realm=%2Fpartenaire",
        data={"grant_type": "client_credentials",
              "client_id": client_id,
              "client_secret": client_secret,
              "scope": "api_offresdemploiv2 o2dsoffre"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30)
    if r.status_code == 200:
        return r.json().get("access_token")
    log.warning("France Travail token %d: %s", r.status_code, r.text[:200])
    return None


def _ft_iso_tipo(contract: str | None) -> str | None:
    if not contract:
        return None
    c = contract.upper()
    if "CDD" in c or "INTERIM" in c:
        return "CONTRACT"
    if "CDI" in c:
        return "FULL_TIME"
    if "TEMPS_PARTIEL" in c or "PARTIEL" in c:
        return "PART_TIME"
    return None


def scarica_francetravail(dsn: str, limite: int = 1000) -> dict:
    """France Travail API Offres d'Emploi v2 — OAuth2, gratuita.

    Richiede FRANCE_TRAVAIL_CLIENT_ID e FRANCE_TRAVAIL_CLIENT_SECRET
    nell'ambiente (registrazione su https://francetravail.io).
    """
    client_id = os.environ.get("FRANCE_TRAVAIL_CLIENT_ID", "")
    client_secret = os.environ.get("FRANCE_TRAVAIL_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        log.warning("France Travail: servono le credenziali OAuth2 "
                    "(registrazione su https://francetravail.io)")
        return {"offerte": 0, "errori": 1}

    token = _ft_token(client_id, client_secret)
    if not token:
        return {"offerte": 0, "errori": 1}

    stats = {"offerte": 0, "nuove": 0, "aggiornate": 0, "errori": 0}
    base = "https://api.francetravail.io"
    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(timeout=30, headers=headers) as c:
        offset = 0
        while offset < limite:
            try:
                r = c.get(f"{base}/partenaire/offresdemploi/v2/offres/search",
                          params={"range": f"{offset}-{offset + 149}",
                                  "codePays": "FR"})
                if r.status_code != 206 and r.status_code != 200:
                    log.warning("FT %d a offset %d", r.status_code, offset)
                    break
                offres = r.json().get("resultats", [])
                if not offres:
                    break
            except httpx.HTTPError as exc:
                log.warning("FT errore: %s", exc)
                stats["errori"] += 1
                break

            for off in offres:
                oid = str(off.get("id") or "")
                if not oid:
                    continue
                stats["offerte"] += 1

                loc = (off.get("lieuTravail") or {})
                citta = loc.get("libelle") if isinstance(loc, dict) else None
                pubbl = off.get("dateCreation")
                try:
                    dt = datetime.fromisoformat(f"{pubbl[:19]}+00:00") if pubbl else None
                except ValueError:
                    dt = None
                tipo = _ft_iso_tipo(off.get("typeContrat"))

                with psycopg.connect(dsn) as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO ats_jobs
                              (platform_id, slug, external_id, title, url,
                               location, country, city, posted_at, raw)
                            VALUES ('francetravail', 'pole-emploi', %s, %s, %s,
                                    %s, 'FR', %s, %s, %s)
                            ON CONFLICT (platform_id, external_id) DO UPDATE SET
                              title = EXCLUDED.title, url = EXCLUDED.url,
                              location = EXCLUDED.location, city = EXCLUDED.city,
                              posted_at = EXCLUDED.posted_at, raw = EXCLUDED.raw,
                              fetched_at = now()
                            RETURNING (xmax = 0) AS is_new
                        """, (oid,
                              off.get("intitule") or "",
                              off.get("origineOffre", {}).get("urlOrigine")
                              or f"https://candidat.francetravail.fr/offres/recherche/detail/{oid}",
                              citta, citta, dt,
                              psycopg.types.json.Json(off)))
                        r2 = cur.fetchone()
                        if r2 and r2[0]:
                            stats["nuove"] += 1
                        else:
                            stats["aggiornate"] += 1
                    conn.commit()

            offset += 150

    log.info("France Travail: %s", stats)
    return stats




# ── BUNDESAGENTUR FÜR ARBEIT (Germania) ──────────────────────────

def scarica_bundesagentur(dsn: str, limite: int = 2000) -> dict:
    """Bundesagentur Jobsuche API v6 — 127.732 offerte, gratis.

    La chiave 'jobboerse-jobsuche' è pubblica e documentata nella
    specifica OpenAPI. Il v6 funziona (il v4 dà 403 a volte).
    Le offerte hanno URL diretto all'annuncio originale.
    """
    stats = {"offerte": 0, "nuove": 0, "aggiornate": 0, "errori": 0}
    base = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"
    headers = {"X-API-Key": "jobboerse-jobsuche",
               "User-Agent": "nivult-ats/0.1"}
    PER_PAGINA = 100

    with httpx.Client(timeout=30, headers=headers) as c:
        for offset in range(0, limite, PER_PAGINA):
            try:
                # l'API tedesca usa page a partire da 1 (0 dà 400)
                r = c.get(base, params={
                    "size": PER_PAGINA,
                    "page": offset // PER_PAGINA + 1,
                })
                if r.status_code != 200:
                    log.warning("BA %d a offset %d", r.status_code, offset)
                    break
                d = r.json()
                offerte = d.get("ergebnisliste", [])
                if not offerte:
                    break
            except httpx.HTTPError as exc:
                log.warning("BA errore: %s", exc)
                stats["errori"] += 1
                break

            for off in offerte:
                refnr = off.get("referenznummer") or ""
                if not refnr:
                    continue
                stats["offerte"] += 1

                titolo = off.get("stellenangebotsTitel") or ""
                firma = off.get("firma") or ""
                url = off.get("externeURL") or ""

                # luogo
                locs = off.get("stellenlokationen") or []
                citta = None
                if locs and isinstance(locs[0], dict):
                    addr = locs[0].get("adresse") or {}
                    citta = addr.get("ort")

                # data di pubblicazione
                pubbl = off.get("datumErsteVeroeffentlichung")
                try:
                    dt = datetime.fromisoformat(f"{pubbl}T00:00:00+00:00") if pubbl else None
                except ValueError:
                    dt = None

                # tipo contratto
                tipo = None
                if off.get("arbeitszeitVollzeit"):
                    tipo = "FULL_TIME"
                elif off.get("arbeitszeitTeilzeitFlexibel"):
                    tipo = "PART_TIME"

                with psycopg.connect(dsn) as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            INSERT INTO ats_jobs
                              (platform_id, slug, external_id, title, url,
                               location, country, city, posted_at, raw)
                            VALUES ('bundesanstellung', 'jobboerse', %s, %s, %s,
                                    %s, 'DE', %s, %s, %s)
                            ON CONFLICT (platform_id, external_id) DO UPDATE SET
                              title = EXCLUDED.title, url = EXCLUDED.url,
                              location = EXCLUDED.location, city = EXCLUDED.city,
                              posted_at = EXCLUDED.posted_at, raw = EXCLUDED.raw,
                              fetched_at = now()
                            RETURNING (xmax = 0) AS is_new
                        """, (refnr, titolo, url, citta, citta, dt,
                              psycopg.types.json.Json(off)))
                        r2 = cur.fetchone()
                        if r2 and r2[0]:
                            stats["nuove"] += 1
                        else:
                            stats["aggiornate"] += 1
                    conn.commit()

    log.info("Bundesagentur: %s", stats)
    return stats

# ── STATISTICHE ────────────────────────────────────────────────────

def stats(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT platform_id, count(*), count(*) FILTER (
                    WHERE posted_at > now() - interval '7 days')
                FROM ats_jobs
                WHERE platform_id IN ('arbetsformedlingen', 'francetravail',
                                      'bundesanstellung', 'bundesanstellung')
                GROUP BY 1
            """)
            print("\nServizi pubblici:")
            for pid, tot, recenti in cur.fetchall():
                print(f"  {pid:22s} {tot:>7d} totali, {recenti:>5d} ultime 7gg")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-8s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.servizi_pubblici",
                                 description=__doc__)
    ap.add_argument("--arbetsformedlingen", action="store_true",
                    help="scarica da Arbetsförmedlingen (Svezia, gratis)")
    ap.add_argument("--francetravail", action="store_true",
                    help="scarica da France Travail (serve OAuth2)")
    ap.add_argument("--bundesanstellung", action="store_true",
                    help="scarica da Bundesagentur (Germania, gratis)")
    ap.add_argument("--limite", type=int, default=1000)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)

    if args.arbetsformedlingen:
        s = scarica_arbetsformedlingen(ATS_DSN, args.limite)
        print(f"\nArbetsförmedlingen: {s}")
    if args.francetravail:
        s = scarica_francetravail(ATS_DSN, args.limite)
        print(f"\nFrance Travail: {s}")
    if args.bundesanstellung:
        s = scarica_bundesagentur(ATS_DSN, args.limite)
        print(f"\nBundesagentur: {s}")
    if args.stats:
        stats(ATS_DSN)
    if not (args.arbetsformedlingen or args.francetravail
            or args.bundesanstellung or args.stats):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
