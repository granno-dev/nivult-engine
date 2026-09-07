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
import re
from datetime import datetime, timezone

import httpx
import psycopg

from .adapters import senza_nulli

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
                              posted_at = EXCLUDED.posted_at, raw = CASE WHEN ats_jobs.raw ? 'description' AND NOT (EXCLUDED.raw ? 'description') THEN EXCLUDED.raw || jsonb_build_object('description', ats_jobs.raw->'description') ELSE EXCLUDED.raw END,
                              fetched_at = now()
                            RETURNING (xmax = 0) AS is_new
                        """, (jid,
                              hit.get("headline") or "",
                              hit.get("webpage_url") or "",
                              citta, citta, dt,
                              psycopg.types.json.Json(senza_nulli(hit))))
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
        "https://entreprise.francetravail.fr/connexion/oauth2/access_token",
        params={"realm": "/partenaire"},
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
                              posted_at = EXCLUDED.posted_at, raw = CASE WHEN ats_jobs.raw ? 'description' AND NOT (EXCLUDED.raw ? 'description') THEN EXCLUDED.raw || jsonb_build_object('description', ats_jobs.raw->'description') ELSE EXCLUDED.raw END,
                              fetched_at = now()
                            RETURNING (xmax = 0) AS is_new
                        """, (oid,
                              off.get("intitule") or "",
                              off.get("origineOffre", {}).get("urlOrigine")
                              or f"https://candidat.francetravail.fr/offres/recherche/detail/{oid}",
                              citta, citta, dt,
                              psycopg.types.json.Json(senza_nulli(off))))
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
                # Link diretto: se l'offerta non ha una URL esterna propria
                # (la maggioranza vive solo sul portale), si costruisce la
                # pagina di dettaglio su arbeitsagentur.de dal refnr —
                # altrimenti l'offerta resta senza link e non e' consegnabile.
                from urllib.parse import quote as _quote
                url = off.get("externeURL") or (
                    "https://www.arbeitsagentur.de/jobsuche/jobdetail/"
                    + _quote(refnr, safe=""))

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
                              posted_at = EXCLUDED.posted_at, raw = CASE WHEN ats_jobs.raw ? 'description' AND NOT (EXCLUDED.raw ? 'description') THEN EXCLUDED.raw || jsonb_build_object('description', ats_jobs.raw->'description') ELSE EXCLUDED.raw END,
                              fetched_at = now()
                            RETURNING (xmax = 0) AS is_new
                        """, (refnr, titolo, url, citta, citta, dt,
                              psycopg.types.json.Json(senza_nulli(off))))
                        r2 = cur.fetchone()
                        if r2 and r2[0]:
                            stats["nuove"] += 1
                        else:
                            stats["aggiornate"] += 1
                    conn.commit()

    log.info("Bundesagentur: %s", stats)
    return stats


# Codici ROME per le nostre 33 famiglie (i più rappresentativi)
ROME_FAMIGLIE = {
    "Human Resources": ["M1603", "M1604", "M1605", "M1606"],
    "Software": ["M1805", "M1806"],
    "Healthcare": ["J1301", "J1302", "J1501", "J1502", "K1303"],
    "Social Services": ["K1301", "K1302", "K2105"],
    "Construction": ["F1701", "F1702", "F1102"],
    "Transportation": ["N4102", "N4103", "N4101"],
    "Logistics": ["N1101", "N1102", "N4103"],
    "Retail": ["D1501", "D1401", "M1705"],
    "Sales": ["M1703", "M1704", "M1705", "D1401"],
    "Marketing": ["E1103", "M1705"],
    "Finance & Accounting": ["M1205", "M1206", "D1401"],
    "Administrative": ["M1607", "M1601"],
    "Education": ["K2108", "K2111"],
    "Engineering": ["H1206", "H1102", "H1103"],
    "Manufacturing": ["H1502", "H2102"],
    "Food & Beverage": ["D1101", "D1201", "G1802"],
    "Hospitality": ["G1802", "G1803"],
    "Customer Service & Support": ["M1707", "D1401"],
    "Data & Analytics": ["M1805"],
    "Trades": ["F1703", "F1601", "I1308"],
    "Security & Safety": ["K2602"],
    "Consulting": ["M1801"],
    "Management & Leadership": ["M1801", "M1802"],
    "Technology": ["M1810"],
    "Legal": ["K1904"],
    "Science & Research": ["K2401"],
}

def scarica_francetravail_rome(dsn: str, limite_per_rome: int = 3000) -> dict:
    """Scarica France Travail per codice ROME (famiglia professionale).

    L'API limita a ~3.150 risultati per query senza filtri. Con il
    codeROME ogni famiglia professionale è una query separata: 25+
    famiglie × 3.150 = fino a 75.000 offerte francesi.
    """
    client_id = os.environ.get("FRANCE_TRAVAIL_CLIENT_ID", "")
    client_secret = os.environ.get("FRANCE_TRAVAIL_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        log.warning("France Travail: servono le credenziali OAuth2")
        return {"offerte": 0, "errori": 1}

    token = _ft_token(client_id, client_secret)
    if not token:
        return {"offerte": 0, "errori": 1}

    stats = {"offerte": 0, "nuove": 0, "aggiornate": 0, "errori": 0}
    base = "https://api.francetravail.io"
    headers = {"Authorization": f"Bearer {token}"}

    with httpx.Client(timeout=30, headers=headers) as c:
        for famiglia, codici in ROME_FAMIGLIE.items():
            for rome in codici:
                offset = 0
                while offset < limite_per_rome:
                    try:
                        r = c.get(f"{base}/partenaire/offresdemploi/v2/offres/search",
                                  params={"range": f"{offset}-{offset + 149}",
                                          "codeROME": rome})
                        if r.status_code not in (206, 200):
                            break
                        offres = r.json().get("resultats", [])
                        if not offres:
                            break
                    except httpx.HTTPError:
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
                                      fetched_at = now()
                                    RETURNING (xmax = 0) AS is_new
                                """, (oid,
                                      off.get("intitule") or "",
                                      off.get("origineOffre", {}).get("urlOrigine")
                                      or f"https://candidat.francetravail.fr/offres/recherche/detail/{oid}",
                                      citta, citta, dt,
                                      psycopg.types.json.Json(senza_nulli(off))))
                                r2 = cur.fetchone()
                                if r2 and r2[0]:
                                    stats["nuove"] += 1
                                else:
                                    stats["aggiornate"] += 1
                            conn.commit()

                    offset += 150

                log.info("  %s/%s: %d offerte", famiglia, rome, stats["offerte"])

    log.info("France Travail ROME: %s", stats)
    return stats

# ── STATISTICHE ────────────────────────────────────────────────────

# ── NAV / ARBEIDSPLASSEN (Norvegia) ────────────────────────────────

NAV_FEED = "https://pam-stilling-feed.nav.no/api/v1/feed"
NAV_TOKEN = "https://pam-stilling-feed.nav.no/api/publicToken"
NAV_CURSORE = "/opt/nivult/nav-cursore.txt"
# engagementtype/extent di NAV -> il nostro vocabolario dei contratti
_NAV_TIPO = {"fast": "full_time", "vikariat": "temporary", "engasjement": "contract",
             "prosjekt": "contract", "sesong": "temporary", "lærling": "apprenticeship",
             "trainee": "internship", "selvstendig": "contract", "åremål": "contract",
             "annet": None}


def _nav_token(client: httpx.Client) -> str | None:
    """Il token PUBBLICO di prova (ruota senza preavviso). Quello privato
    si chiede a nav.team.arbeidsplassen@nav.no accettando i termini:
    NAV_TOKEN_PRIVATO nell'ambiente ha la precedenza."""
    priv = os.environ.get("NAV_TOKEN_PRIVATO")
    if priv:
        return priv.strip()
    try:
        r = client.get(NAV_TOKEN)
        m = re.search(r"eyJ[A-Za-z0-9._-]+", r.text)
        return m.group(0) if m else None
    except httpx.HTTPError:
        return None


def scarica_nav(dsn: str, limite: int = 2000) -> dict:
    """NAV Job Vacancy Feed — il pubblico impiego norvegese, gratis.

    Un feed di EVENTI (JSON Feed, 1000 per pagina, `next_url`): ogni voce
    e' «l'annuncio X e' ACTIVE/INACTIVE da quest'ora». Si legge da dove
    si era rimasti (If-Modified-Since dal cursore), per le voci ACTIVE si
    scarica il dettaglio (`/api/v1/feedentry/{uuid}`: titolo, testo,
    datore, sede, date, ESCO, e spesso l'URL di candidatura sull'ATS del
    datore), per le INACTIVE si marca scaduta — i termini d'uso lo
    chiedono («rimuovere subito gli annunci non piu' attivi»), e per noi
    e' una scadenza dichiarata dalla fonte, la piu' onesta che ci sia.
    Termini: repubblicazione consentita con link alla candidatura
    originale (link_kind national_agency); rivendita non contemplata —
    fuori dal dataset in vendita finche' non lo chiediamo.
    """
    stats = {"eventi": 0, "attive": 0, "nuove": 0, "aggiornate": 0, "scadute": 0, "errori": 0}
    try:
        da = open(NAV_CURSORE).read().strip()
    except OSError:
        da = (datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=26)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    with httpx.Client(timeout=30, headers={"User-Agent": "nivult-ats/0.1 (+https://nivult.com)"},
                      follow_redirects=True) as c:
        token = _nav_token(c)
        if not token:
            log.warning("NAV: nessun token"); stats["errori"] += 1; return stats
        c.headers["Authorization"] = f"Bearer {token}"
        url = NAV_FEED
        intest = {"If-Modified-Since": da}
        ultimo_visto = None
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("""INSERT INTO ats_platforms (id, name, is_active, api_type, notes)
                            VALUES ('nav', 'NAV Arbeidsplassen (Norvegia)', true, 'json',
                                    'feed pubblico di eventi + dettaglio; scadenze dichiarate dalla fonte')
                            ON CONFLICT (id) DO NOTHING""")
            while url and stats["eventi"] < limite:
                try:
                    r = c.get(url, headers=intest)
                except httpx.HTTPError as exc:
                    log.warning("NAV feed: %s", exc); stats["errori"] += 1; break
                if r.status_code == 304:
                    break
                if r.status_code != 200:
                    log.warning("NAV feed HTTP %d", r.status_code); stats["errori"] += 1; break
                d = r.json()
                voci = d.get("items") or []
                if not voci:
                    break
                for v in voci:
                    fe = v.get("_feed_entry") or {}
                    uuid, stato = fe.get("uuid"), fe.get("status")
                    if not uuid:
                        continue
                    stats["eventi"] += 1
                    ultimo_visto = v.get("date_modified") or ultimo_visto
                    if stato != "ACTIVE":
                        n = conn.execute("UPDATE ats_jobs SET expired_at = now() WHERE platform_id='nav' AND external_id=%s "
                                         "AND expired_at IS NULL", (uuid,)).rowcount
                        stats["scadute"] += n
                        continue
                    stats["attive"] += 1
                    try:
                        rd = c.get(f"https://pam-stilling-feed.nav.no{v.get('url') or '/api/v1/feedentry/' + uuid}")
                        ad = (rd.json() or {}).get("ad_content") if rd.status_code == 200 else None
                    except (httpx.HTTPError, ValueError):
                        ad = None
                    if not ad or not (ad.get("title") or "").strip():
                        stats["errori"] += 1
                        continue
                    sede = (ad.get("workLocations") or [{}])[0] or {}
                    citta = (sede.get("city") or sede.get("municipal") or "").title() or None
                    paese = "NO" if (sede.get("country") or "NORGE").upper() in ("NORGE", "NORWAY", "NO") else None
                    tipo = _NAV_TIPO.get((ad.get("engagementtype") or "").strip().lower())
                    if tipo == "full_time" and (ad.get("extent") or "").lower().startswith("deltid"):
                        tipo = "part_time"
                    try:
                        pub = datetime.fromisoformat(str(ad.get("published")).replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        pub = None
                    datore = (ad.get("employer") or {}).get("name") if isinstance(ad.get("employer"), dict) else None
                    raw = {k: ad.get(k) for k in ("title", "description", "published", "expires", "updated", "employer",
                                                  "workLocations", "engagementtype", "extent", "positioncount",
                                                  "sector", "categoryList", "applicationUrl", "link", "source",
                                                  "jobtitle", "applicationDue")}
                    if datore:
                        raw["company"] = {"name": datore}
                    r2 = conn.execute("""
                        INSERT INTO ats_jobs (platform_id, slug, external_id, title, url, location, country, city,
                                              posted_at, employment_type, raw)
                        VALUES ('nav', 'arbeidsplassen', %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (platform_id, external_id) DO UPDATE SET
                          title = EXCLUDED.title, url = EXCLUDED.url, location = EXCLUDED.location,
                          city = EXCLUDED.city, posted_at = COALESCE(EXCLUDED.posted_at, ats_jobs.posted_at),
                          employment_type = COALESCE(EXCLUDED.employment_type, ats_jobs.employment_type),
                          raw = EXCLUDED.raw, expired_at = NULL, fetched_at = now()
                        RETURNING (xmax = 0) AS is_new
                    """, (uuid, (ad.get("title") or "")[:300],
                          (ad.get("applicationUrl") or ad.get("link") or f"https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}")[:1000],
                          citta, paese, citta, pub, tipo, psycopg.types.json.Json(senza_nulli(raw)))).fetchone()
                    if r2 and r2[0]:
                        stats["nuove"] += 1
                    else:
                        stats["aggiornate"] += 1
                nxt = d.get("next_url")
                url = ("https://pam-stilling-feed.nav.no" + nxt) if nxt and nxt.startswith("/") else nxt
                intest = {}   # dalla seconda pagina in poi si segue next_url, senza If-Modified-Since
        if ultimo_visto:
            try:
                dt = datetime.fromisoformat(ultimo_visto)
                with open(NAV_CURSORE, "w") as f:
                    f.write(dt.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT"))
            except (ValueError, OSError):
                pass
    log.info("NAV: %s", stats)
    return stats


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


def eures(dsn: str, limite: int = 2000, paesi: str = "IT") -> dict:
    """EURES: il portale UE che aggrega i centri per l'impiego nazionali.

    E' la fonte che copre l'ITALIA, dove non esiste un'API pubblica
    nazionale decente: EURES riceve le offerte dei PES italiani (e di
    tutti gli altri) e le espone da un endpoint JSON pubblico. Non e'
    un'API garantita — niente SLA, niente rate limit dichiarato — quindi
    il codice tratta ogni risposta come sospetta e si ferma al primo
    segnale di forma cambiata, invece di inserire spazzatura.

    L'URL dell'offerta e' la pagina di dettaglio EURES, non il sito del
    datore: il campo apply non viaggia nella ricerca, e una richiesta di
    dettaglio per offerta moltiplicherebbe il costo per un link che la
    pagina EURES comunque contiene. Gli id contengono SPAZI (sono cosi',
    davvero): l'escape non e' opzionale.
    """
    from urllib.parse import quote
    stats = {"offerte": 0, "nuove": 0, "aggiornate": 0, "errori": 0}
    base = "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
    PER_PAGINA = 50

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ats_platforms (id, name, is_active, api_type, notes)
            VALUES ('eures', 'EURES (portale UE)', true, 'json',
                    'endpoint pubblico non garantito: jv-searchengine')
            ON CONFLICT (id) DO NOTHING""")
        conn.commit()

    with httpx.Client(timeout=30, headers={"User-Agent": "nivult-ats/0.1"}) as c:
        for paese in [x.strip().upper() for x in paesi.split(",") if x.strip()]:
            pagine = max(1, min(limite, 5000) // PER_PAGINA)
            for pagina in range(1, pagine + 1):
                try:
                    r = c.post(base, json={
                        "resultsPerPage": PER_PAGINA,
                        "page": pagina,
                        "sortSearch": "MOST_RECENT",
                        "locationCodes": [paese.lower()],
                        "keywords": [],
                        "requestLanguage": "en",
                    })
                    if r.status_code != 200:
                        log.warning("EURES %d a pagina %d (%s)",
                                    r.status_code, pagina, paese)
                        break
                    jvs = r.json().get("jvs") or []
                    if not jvs:
                        break
                except (httpx.HTTPError, ValueError) as exc:
                    log.warning("EURES errore: %s", exc)
                    stats["errori"] += 1
                    break

                for jv in jvs:
                    jid = str(jv.get("id") or "").strip()
                    titolo = (jv.get("title") or "").strip()
                    if not jid or not titolo:
                        continue
                    stats["offerte"] += 1

                    datore = ((jv.get("employer") or {}).get("name") or "").strip()
                    # I PES scrivono «Non renseigné»/«non disponibile» al
                    # posto del vuoto: e' un'assenza travestita, e da noi
                    # le assenze si scrivono NULL.
                    if datore.lower() in ("non renseigné", "non disponibile",
                                          "not available", "n/a", ""):
                        datore = None
                    mappa = jv.get("locationMap") or {}
                    citta = None
                    for valori in mappa.values():
                        for v in (valori or []):
                            if v and str(v).strip():
                                citta = str(v).strip()
                                break
                        if citta:
                            break
                    ms = jv.get("creationDate")
                    dt = (datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
                          if isinstance(ms, (int, float)) else None)
                    url = ("https://europa.eu/eures/portal/jv-se/jv-details/"
                           + quote(jid, safe="") + "?lang=en")
                    if datore:
                        jv = dict(jv)
                        jv["company"] = {"name": datore}

                    with psycopg.connect(dsn) as conn:
                        with conn.cursor() as cur:
                            cur.execute("""
                                INSERT INTO ats_jobs
                                  (platform_id, slug, external_id, title, url,
                                   location, country, city, posted_at, raw)
                                VALUES ('eures', %s, %s, %s, %s,
                                        %s, %s, %s, %s, %s)
                                ON CONFLICT (platform_id, external_id) DO UPDATE SET
                                  title = EXCLUDED.title, url = EXCLUDED.url,
                                  location = EXCLUDED.location, city = EXCLUDED.city,
                                  country = COALESCE(EXCLUDED.country, ats_jobs.country),
                                  posted_at = EXCLUDED.posted_at, raw = CASE WHEN ats_jobs.raw ? 'description' AND NOT (EXCLUDED.raw ? 'description') THEN EXCLUDED.raw || jsonb_build_object('description', ats_jobs.raw->'description') ELSE EXCLUDED.raw END,
                                  fetched_at = now()
                                RETURNING (xmax = 0) AS is_new
                            """, (paese.lower(), jid, titolo[:300], url,
                                  citta, paese, citta, dt,
                                  psycopg.types.json.Json(senza_nulli(jv))))
                            r2 = cur.fetchone()
                            if r2 and r2[0]:
                                stats["nuove"] += 1
                            else:
                                stats["aggiornate"] += 1
                        conn.commit()

    log.info("EURES (%s): %s", paesi, stats)
    return stats


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
    ap.add_argument("--francetravail-rome", action="store_true",
                    help="scarica FT per codeROME (25+ famiglie, fino a 75k)")
    ap.add_argument("--eures", action="store_true",
                    help="EURES, il portale UE: la fonte che copre l'Italia")
    ap.add_argument("--nav", action="store_true",
                    help="NAV Arbeidsplassen (Norvegia): feed di eventi + dettaglio, scadenze dalla fonte")
    ap.add_argument("--paesi", default="IT",
                    help="paesi per --eures, separati da virgola")
    ap.add_argument("--limite", type=int, default=1000)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)

    if args.eures:
        st = eures(ATS_DSN, args.limite, args.paesi)
        print(f"EURES: {st}")

    if args.arbetsformedlingen:
        s = scarica_arbetsformedlingen(ATS_DSN, args.limite)
        print(f"\nArbetsförmedlingen: {s}")
    if args.francetravail:
        s = scarica_francetravail(ATS_DSN, args.limite)
        print(f"\nFrance Travail: {s}")
    if args.francetravail_rome:
        s = scarica_francetravail_rome(ATS_DSN, args.limite)
        print(f"\nFrance Travail ROME: {s}")
    if args.bundesanstellung:
        s = scarica_bundesagentur(ATS_DSN, args.limite)
        print(f"\nBundesagentur: {s}")
    if args.nav:
        s = scarica_nav(ATS_DSN, args.limite)
        print(f"\nNAV: {s}")
    if args.stats:
        stats(ATS_DSN)
    if not (args.arbetsformedlingen or args.francetravail
            or args.bundesanstellung or args.francetravail_rome
            or args.stats or args.eures or args.nav):
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
