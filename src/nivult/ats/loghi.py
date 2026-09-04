"""Il logo dell'azienda, a livello tenant, per la board delle offerte.

La board (cruscotto e futura landing) mostra il logo dell'azienda accanto
all'offerta: crea fiducia. Alcuni ATS danno il logo per-offerta (breezy,
niceboard, taleez, softgarden...), altri solo sulla pagina board come
`og:image` (ashby, lever, workable). Qui lo consolidiamo in
`ats_companies.logo_url` da due fonti — una volta per azienda, poi in cache —
cosi' la query della board fa un solo join e non ricalcola nulla.
"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg

log = logging.getLogger("nivult.ats.loghi")

_UA = "Mozilla/5.0 (compatible; nivult-ats/1.0)"

# Board dove l'og:image E' il logo dell'azienda (verificato). Fuori da qui
# l'og:image e' spesso uno screenshot (teamtailor) o un logo generico
# dell'ATS (personio): meglio il monogramma che un logo sbagliato.
_BOARD = {
    "ashby":           "https://jobs.ashbyhq.com/{s}",
    "lever":           "https://jobs.lever.co/{s}",
    "workable":        "https://apply.workable.com/{s}/",
    "smartrecruiters": "https://careers.smartrecruiters.com/{s}",
    "greenhouse":      "https://job-boards.greenhouse.io/{s}",
    # og:image col logo verificato a campione (3/3 ciascuna):
    "teamtailor":      "https://{s}.teamtailor.com",
    "breezy":          "https://{s}.breezy.hr",
    "recruitee":       "https://{s}.recruitee.com",
    "pinpoint":        "https://{s}.pinpointhq.com",
}
# per alcuni ATS il logo NON e' nell'og:image ma in un <img> dedicato:
# regex per-piattaforma che pesca l'URL del logo dall'HTML della board.
_LOGO_IMG = {
    "greenhouse": re.compile(
        r"(https?://[a-z0-9-]*recruiting\.cdn\.greenhouse\.io/"
        r"external_greenhouse[^\"'\s]+)", re.I),
}
# host da rifiutare: asset generici dell'ATS, non il logo dell'azienda.
_RIFIUTA = re.compile(
    r"screenshots\.|/cdn_assets/|assets\.cdn\.|/rebrand|placeholder|default"
    r"|sr-careersite", re.I)

_OG = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', re.I)


def da_offerte(dsn: str) -> int:
    """Copia il logo per-offerta al livello azienda, in blocco SQL."""
    with psycopg.connect(dsn, autocommit=True) as c:
        r = c.execute("""
            UPDATE ats_companies ac
               SET logo_url = sub.logo, logo_checked_at = now()
              FROM (SELECT DISTINCT ON (platform_id, slug)
                           platform_id, slug, raw->>'logo' AS logo
                      FROM ats_jobs
                     WHERE raw->>'logo' LIKE 'http%'
                       AND expired_at IS NULL
                     ORDER BY platform_id, slug, fetched_at DESC) sub
             WHERE ac.platform_id = sub.platform_id
               AND ac.slug = sub.slug
               AND ac.logo_url IS NULL""")
        return r.rowcount


def da_board(dsn: str, limite: int = 400) -> dict:
    """Legge l'og:image della board per i tenant senza logo (ashby/lever/
    workable), a lotti. Solo loghi validi entrano."""
    cli = httpx.Client(timeout=12, follow_redirects=True,
                       headers={"User-Agent": _UA})
    stats = {"esaminati": 0, "trovati": 0}
    with psycopg.connect(dsn, autocommit=True) as c:
        righe = c.execute("""
            SELECT ac.platform_id, ac.slug FROM ats_companies ac
             WHERE ac.platform_id = ANY(%s) AND ac.job_count > 0
               AND ac.logo_url IS NULL
               AND (ac.logo_checked_at IS NULL
                    OR ac.logo_checked_at < now() - interval '30 days')
             -- prima chi ha pubblicato di recente: sono le aziende che
             -- compaiono nella board live, il logo serve a loro subito
             ORDER BY (SELECT max(j.posted_at) FROM ats_jobs j
                        WHERE j.platform_id = ac.platform_id
                          AND j.slug = ac.slug
                          AND j.expired_at IS NULL) DESC NULLS LAST
             LIMIT %s""", (list(_BOARD), limite)).fetchall()

        def _uno(row):
            pid, slug = row
            url = _BOARD[pid].format(s=slug)
            try:
                r = cli.get(url)
            except httpx.HTTPError:
                return pid, slug, None
            if r.status_code != 200:
                return pid, slug, None
            rx = _LOGO_IMG.get(pid)
            if rx is not None:
                mm = rx.search(r.text)
                logo = mm.group(1) if mm else None
            else:
                m = _OG.search(r.text)
                logo = (m.group(1) or m.group(2)) if m else None
            if logo and (_RIFIUTA.search(logo) or not logo.startswith("http")):
                logo = None
            return pid, slug, logo

        with ThreadPoolExecutor(max_workers=12) as ex:
            for pid, slug, logo in ex.map(_uno, righe):
                stats["esaminati"] += 1
                c.execute(
                    "UPDATE ats_companies SET logo_url=%s, logo_checked_at=now() "
                    "WHERE platform_id=%s AND slug=%s", (logo, pid, slug))
                if logo:
                    stats["trovati"] += 1
    cli.close()
    return stats


def _norm(s: str) -> str:
    """Riduce un nome al suo nocciolo alfanumerico minuscolo."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _pulisci_nome(nome: str) -> str:
    """Toglie le parole di rumore (careers, gmbh, inc...) dal nome tenant."""
    n = re.sub(r"[-_]+", " ", nome or "")
    n = re.sub(r"\b(careers?|jobs?|hiring|the|gmbh|inc|ltd|llc|group|"
               r"holding|srl|spa|bv|ag|co|corp|company)\b", " ", n, flags=re.I)
    return re.sub(r"\s+", " ", n).strip()


def _affidabile(nome: str, brand: dict) -> bool:
    """Accetta il logo solo se il nome corrisponde DAVVERO al brand trovato:
    il nocciolo del nome deve comparire nel dominio o nel nome del brand.
    Cosi' 'Careers Affordablecare' non prende il logo di 'Bespoke Careers'."""
    core = _norm(_pulisci_nome(nome))
    if len(core) < 4:
        return False
    dom = _norm((brand.get("domain") or "").split(".")[0])
    bn = _norm(brand.get("name") or "")
    return core in dom or dom in core or core in bn or bn in core


def da_brandfetch(dsn: str, limite: int = 200,
                  client_id: str | None = None) -> dict:
    """Logo dal nome azienda via Brandfetch (per gli ATS senza logo proprio).

    La Search API (`api.brandfetch.io/v2/search/{nome}`) ritorna il brand col
    dominio e l'icona-logo. Applichiamo un filtro di affidabilita' per non
    mostrare il logo dell'azienda sbagliata. Con un client_id (env
    BRANDFETCH_CLIENT_ID) si usa il CDN proprio, come vuole la licenza in
    produzione; senza, la Search pubblica (basso volume).
    """
    client_id = client_id or os.environ.get("BRANDFETCH_CLIENT_ID")
    cli = httpx.Client(timeout=12, follow_redirects=True,
                       headers={"User-Agent": _UA})
    stats = {"esaminati": 0, "trovati": 0, "scartati_incerti": 0}
    # gli ATS che NON danno un logo proprio: qui serve Brandfetch.
    # greenhouse in testa: non espone il logo da NESSUNA parte pubblica
    # (og:image vuoto sul nuovo job-boards) — qui Brandfetch e' l'unica via.
    senza = ("greenhouse", "personio", "teamtailor", "bamboohr", "workday",
             "icims", "cornerstone", "oracle", "phenom", "jazzhr",
             "zohorecruit", "recruiterbox", "join", "pinpoint", "catsone",
             "successfactors", "crelate", "jobscore", "werecruit",
             "smartrecruiters", "vincere", "applicantstack", "freshteam")
    with psycopg.connect(dsn, autocommit=True) as c:
        righe = c.execute("""
            SELECT ac.platform_id, ac.slug, coalesce(ac.company_name, ac.slug)
              FROM ats_companies ac
             WHERE ac.platform_id = ANY(%s) AND ac.job_count > 0
               AND ac.logo_url IS NULL
               AND (ac.logo_checked_at IS NULL
                    OR ac.logo_checked_at < now() - interval '30 days')
             -- prima chi pubblica adesso: e' in bacheca, il logo serve subito
             ORDER BY (SELECT max(j.posted_at) FROM ats_jobs j
                        WHERE j.platform_id = ac.platform_id
                          AND j.slug = ac.slug
                          AND j.expired_at IS NULL) DESC NULLS LAST
             LIMIT %s""", (list(senza), limite)).fetchall()
        for pid, slug, nome in righe:
            stats["esaminati"] += 1
            logo = None
            dominio = None
            try:
                q = _pulisci_nome(nome) or nome
                r = cli.get(f"https://api.brandfetch.io/v2/search/{q}",
                            params={"c": client_id} if client_id else None)
                brands = r.json() if r.status_code == 200 else []
            except (httpx.HTTPError, ValueError):
                brands = []
            if brands and isinstance(brands, list):
                b = brands[0]
                if _affidabile(nome, b):
                    dominio = b.get("domain")   # per il ripiego favicon
                    if b.get("icon"):
                        if client_id and dominio:
                            logo = (f"https://cdn.brandfetch.io/{dominio}"
                                    f"/w/128/h/128?c={client_id}")
                        else:
                            logo = b.get("icon")
                else:
                    stats["scartati_incerti"] += 1
            c.execute(
                "UPDATE ats_companies SET logo_url=%s, logo_domain=%s, "
                "logo_checked_at=now() WHERE platform_id=%s AND slug=%s",
                (logo, dominio, pid, slug))
            if logo:
                stats["trovati"] += 1
    cli.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.loghi")
    ap.add_argument("--da-offerte", action="store_true")
    ap.add_argument("--da-board", action="store_true")
    ap.add_argument("--da-brandfetch", action="store_true")
    ap.add_argument("--limite", type=int, default=400)
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    fatto = False
    if args.da_offerte:
        log.info("loghi da offerte: %d aziende", da_offerte(dsn)); fatto = True
    if args.da_board:
        log.info("loghi da board: %s", da_board(dsn, args.limite)); fatto = True
    if args.da_brandfetch:
        log.info("loghi da brandfetch: %s",
                 da_brandfetch(dsn, args.limite)); fatto = True
    if not fatto:
        log.info("loghi da offerte: %d aziende", da_offerte(dsn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
