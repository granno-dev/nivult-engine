"""I dataset da vendere: il magazzino si riempie prima di aprire la porta.

Tre confezioni, nello stile dei venditori di dati (TheirStack, Coresignal)
ma coi nostri argomenti — solo fonti dirette del datore, lingua
dell'annuncio, scadenze oneste, e l'ATS usato da ogni azienda (il
"technographic" che loro vendono caro e noi abbiamo per costruzione):

  attive   — le offerte vive, un JSON per riga, con ciclo di vita
             (first_seen/last_seen), lingua, competenze, salario, testo.
  aziende  — una riga per azienda che assume: ATS usato, dominio, logo,
             dipendenti (dove Wikidata li sa), ritmo di assunzione a 30
             giorni e le tecnologie/competenze più chieste nei SUOI
             annunci — l'aggregato che trasforma offerte in segnali.
  scadute  — lo storico delle chiusure: quando è apparsa, quando è
             sparita. Il tempo-di-riempimento è un dato che pochi hanno.

Scrittura in streaming (cursore lato server): 800k righe senza mangiare
la RAM. File .jsonl.gz datati + un link simbolico "-ultimo" stabile.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import os

import psycopg

log = logging.getLogger("nivult.ats.esporta")

CARTELLA = "/opt/nivult/exports"

_DESCR = ("COALESCE(raw->>'description', raw->>'externalDescription', "
          "raw->>'descriptionHtml', raw->>'jobDescription', "
          "raw->>'job_description', raw->>'content', "
          "raw->>'descriptionPlain')")


def _apri(nome: str):
    os.makedirs(CARTELLA, exist_ok=True)
    oggi = dt.date.today().isoformat()
    percorso = f"{CARTELLA}/{nome}-{oggi}.jsonl.gz"
    return percorso, gzip.open(percorso + ".tmp", "wt", encoding="utf-8")


def _chiudi(percorso: str, f, righe: int) -> None:
    f.close()
    os.replace(percorso + ".tmp", percorso)
    stabile = percorso.rsplit("-", 3)[0] + "-ultimo.jsonl.gz"
    tmp = stabile + ".tmp"
    try:
        os.remove(tmp)
    except OSError:
        pass
    os.symlink(os.path.basename(percorso), tmp)
    os.replace(tmp, stabile)
    log.info("%s: %d righe, %.1f MB", percorso, righe,
             os.path.getsize(percorso) / 1e6)


def _riga(**kv) -> str:
    return json.dumps({k: v for k, v in kv.items() if v not in (None, [], "")},
                      ensure_ascii=False, default=str) + "\n"


def attive(dsn: str) -> int:
    percorso, f = _apri("offerte-attive")
    n = 0
    copertura = {k: 0 for k in ("salary_observed", "salary_estimate",
                                "description", "language", "category",
                                "country", "skills", "employment_type",
                                "contact_email")}
    with psycopg.connect(dsn) as conn:
        # il benchmark in memoria: stima SOLO dove l'annuncio tace, in
        # campi SEPARATI e dichiarati — mai mescolata con l'osservato
        bench: dict = {}
        with conn.cursor() as cur:
            cur.execute("""SELECT country, currency, family, seniority,
                                  p25, p50, p75, n
                             FROM stipendi_benchmark""")
            for r in cur:
                bench[(r[0], r[2], r[3])] = (r[1], r[4], r[5], r[6], r[7])
        with conn.cursor(name="esp_attive") as cur:
            cur.itersize = 2000
            cur.execute(f"""
                SELECT j.id, j.title, j.platform_id, j.slug,
                       COALESCE(co.company_name, j.raw->'company'->>'name'),
                       j.url, j.country, j.city, j.location, j.lang,
                       j.seniority, j.remote, j.skills,
                       j.salary_min, j.salary_max, j.salary_currency,
                       j.posted_at, COALESCE(j.created_at, j.posted_at,
                                             j.fetched_at), j.fetched_at,
                       {_DESCR}, x.family,
                       j.employment_type, j.contact_email
                  FROM ats_jobs j
                  LEFT JOIN ats_companies co
                         ON co.platform_id = j.platform_id
                        AND co.slug = j.slug
                  LEFT JOIN job_classifications x ON x.job_id = j.id
                 WHERE j.expired_at IS NULL""")
            for r in cur:
                stima = {}
                if r[13] is None and r[6] and r[20]:
                    cella = bench.get((r[6], r[20], r[10] or ""))                         or bench.get((r[6], r[20], ""))
                    if cella:
                        val, p25, p50, p75, camp = cella
                        stima = {
                            "salary_is_estimate": True,
                            "salary_estimate_min": p25,
                            "salary_estimate_median": p50,
                            "salary_estimate_max": p75,
                            "salary_estimate_currency": val,
                            "salary_estimate_basis":
                                f"Nivult benchmark: {camp} observed "
                                f"postings, {r[6]} x {r[20]}"}
                f.write(_riga(
                    id=str(r[0]), title=r[1], ats=r[2], company_slug=r[3],
                    company=r[4], url=r[5], country=r[6], city=r[7],
                    location=r[8], language=r[9], seniority=r[10],
                    remote=r[11], skills=list(r[12] or []),
                    salary_min=float(r[13]) if r[13] is not None else None,
                    salary_max=float(r[14]) if r[14] is not None else None,
                    salary_currency=r[15], posted_at=r[16],
                    first_seen=r[17], last_seen=r[18], description=r[19],
                    category=r[20], employment_type=r[21],
                    contact_email=r[22], **stima))
                n += 1
                copertura["salary_observed"] += r[13] is not None
                copertura["salary_estimate"] += bool(stima)
                copertura["description"] += bool(r[19])
                copertura["language"] += r[9] is not None
                copertura["category"] += r[20] is not None
                copertura["country"] += r[6] is not None
                copertura["skills"] += bool(r[12])
                copertura["employment_type"] += r[21] is not None
                copertura["contact_email"] += r[22] is not None
    _chiudi(percorso, f, n)
    # il manifest: la copertura di ogni campo, dichiarata. I venditori
    # seri pubblicano i fill-rate; i buchi dichiarati sono un argomento
    # di vendita, quelli scoperti dal cliente sono un rimborso.
    manifest = {"date": dt.date.today().isoformat(), "rows": n,
                "coverage": {k: round(100 * v / max(n, 1), 1)
                             for k, v in copertura.items()}}
    with open(f"{CARTELLA}/manifest-ultimo.json.tmp", "w") as mf:
        json.dump(manifest, mf, indent=1)
    os.replace(f"{CARTELLA}/manifest-ultimo.json.tmp",
               f"{CARTELLA}/manifest-ultimo.json")
    log.info("manifest: %s", manifest["coverage"])
    return n


def aziende(dsn: str) -> int:
    percorso, f = _apri("aziende-segnali")
    n = 0
    with psycopg.connect(dsn) as conn:
        # le competenze più chieste da ciascuna azienda: l'aggregato che
        # trasforma un mucchio di annunci in un segnale su chi assume cosa
        skill_per_azienda: dict = {}
        with conn.cursor(name="esp_skill") as cur:
            cur.itersize = 5000
            cur.execute("""
                SELECT platform_id, slug, s.skill, count(*)
                  FROM ats_jobs, LATERAL unnest(skills) AS s(skill)
                 WHERE expired_at IS NULL
                 GROUP BY 1, 2, 3""")
            for pid, slug, skill, cnt in cur:
                skill_per_azienda.setdefault((pid, slug), []).append(
                    (cnt, skill))
        with conn.cursor(name="esp_az") as cur:
            cur.itersize = 2000
            cur.execute("""
                SELECT ac.platform_id, ac.slug, ac.company_name, ac.country,
                       ac.logo_domain, ac.logo_url, ac.job_count,
                       COALESCE(ac.employees_wd, cd.employees),
                       ac.industry,
                       (SELECT count(*) FROM ats_jobs j
                         WHERE j.platform_id = ac.platform_id
                           AND j.slug = ac.slug AND j.expired_at IS NULL
                           AND COALESCE(j.posted_at, j.created_at)
                               > now() - interval '30 days'),
                       (SELECT array_agg(DISTINCT j.lang) FROM ats_jobs j
                         WHERE j.platform_id = ac.platform_id
                           AND j.slug = ac.slug AND j.expired_at IS NULL
                           AND j.lang IS NOT NULL)
                  FROM ats_companies ac
                  LEFT JOIN company_domains cd ON cd.domain = ac.logo_domain
                 WHERE ac.is_active AND ac.job_count > 0""")
            for r in cur:
                cime = sorted(skill_per_azienda.get((r[0], r[1]), []),
                              reverse=True)[:15]
                f.write(_riga(
                    ats=r[0], company_slug=r[1], company=r[2], country=r[3],
                    domain=r[4], logo=r[5], active_jobs=r[6],
                    employees=r[7], industry=r[8], jobs_posted_30d=r[9],
                    languages=list(r[10] or []),
                    top_skills=[{"skill": s, "jobs": c} for c, s in cime]))
                n += 1
    _chiudi(percorso, f, n)
    return n


def scadute(dsn: str, giorni: int | None = None) -> int:
    percorso, f = _apri("offerte-chiuse")
    filtro = ("AND expired_at > now() - make_interval(days => %s)"
              if giorni else "")
    n = 0
    with psycopg.connect(dsn) as conn:
        with conn.cursor(name="esp_scadute") as cur:
            cur.itersize = 2000
            cur.execute(f"""
                SELECT id, title, platform_id, slug, url, country, city,
                       lang, seniority, posted_at, expired_at,
                       COALESCE(created_at, posted_at, fetched_at)
                  FROM ats_jobs
                 WHERE expired_at IS NOT NULL {filtro}""",
                (giorni,) if giorni else None)
            for r in cur:
                f.write(_riga(
                    id=str(r[0]), title=r[1], ats=r[2], company_slug=r[3],
                    url=r[4], country=r[5], city=r[6], language=r[7],
                    seniority=r[8], posted_at=r[9], closed_at=r[10],
                    first_seen=r[11]))
                n += 1
    _chiudi(percorso, f, n)
    return n


def flusso(dsn: str) -> dict:
    """Il magazzino in tempo reale: le novita' dall'ultimo giro.

    Nessuna richiesta esterna — si legge il NOSTRO database, quindi
    zero rischio ban: il rischio sta nella raccolta, che ha i suoi
    ritmi; qui si confeziona soltanto. Ogni giro produce un file con
    le offerte NUOVE (prima volta viste) e CHIUSE dall'ultimo cursore:
    e' il formato che i clienti dati chiamano delta feed, e che
    trasforma «foto del mattino» in «flusso continuo».
    """
    cartella = f"{CARTELLA}/flusso"
    os.makedirs(cartella, exist_ok=True)
    cursore_file = f"{cartella}/cursore.json"
    try:
        da = json.load(open(cursore_file))["t"]
    except Exception:                                # noqa: BLE001
        da = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(minutes=15)).isoformat()
    adesso = dt.datetime.now(dt.timezone.utc)
    marca = adesso.strftime("%Y%m%d-%H%M%S")
    percorso = f"{cartella}/novita-{marca}.jsonl.gz"
    stats = {"nuove": 0, "chiuse": 0}
    with psycopg.connect(dsn) as conn, \
            gzip.open(percorso + ".tmp", "wt", encoding="utf-8") as f:
        with conn.cursor(name="fl_nuove") as cur:
            cur.itersize = 2000
            cur.execute(f"""
                SELECT j.id, j.title, j.platform_id, j.slug,
                       COALESCE(co.company_name, j.raw->'company'->>'name'),
                       j.url, j.country, j.city, j.lang, j.seniority,
                       j.remote, j.skills, j.salary_min, j.salary_max,
                       j.salary_currency, j.posted_at, j.created_at,
                       {_DESCR}
                  FROM ats_jobs j
                  LEFT JOIN ats_companies co
                         ON co.platform_id = j.platform_id
                        AND co.slug = j.slug
                 WHERE j.created_at > %s::timestamptz
                   AND j.expired_at IS NULL""", (da,))
            for r in cur:
                f.write(_riga(
                    event="new", id=str(r[0]), title=r[1], ats=r[2],
                    company_slug=r[3], company=r[4], url=r[5],
                    country=r[6], city=r[7], language=r[8],
                    seniority=r[9], remote=r[10],
                    skills=list(r[11] or []),
                    salary_min=float(r[12]) if r[12] is not None else None,
                    salary_max=float(r[13]) if r[13] is not None else None,
                    salary_currency=r[14], posted_at=r[15],
                    first_seen=r[16], description=r[17]))
                stats["nuove"] += 1
        with conn.cursor(name="fl_chiuse") as cur:
            cur.itersize = 2000
            cur.execute("""
                SELECT id, title, platform_id, slug, url, country,
                       posted_at, expired_at
                  FROM ats_jobs
                 WHERE expired_at > %s::timestamptz""", (da,))
            for r in cur:
                f.write(_riga(
                    event="closed", id=str(r[0]), title=r[1], ats=r[2],
                    company_slug=r[3], url=r[4], country=r[5],
                    posted_at=r[6], closed_at=r[7]))
                stats["chiuse"] += 1
    os.replace(percorso + ".tmp", percorso)
    with open(cursore_file + ".tmp", "w") as f:
        json.dump({"t": adesso.isoformat()}, f)
    os.replace(cursore_file + ".tmp", cursore_file)
    log.info("flusso %s: %s", marca, stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.esporta")
    ap.add_argument("--attive", action="store_true")
    ap.add_argument("--aziende", action="store_true")
    ap.add_argument("--scadute", action="store_true")
    ap.add_argument("--flusso", action="store_true")
    ap.add_argument("--giorni", type=int, default=None,
                    help="per --scadute: solo le chiuse negli ultimi N giorni")
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    if args.attive:
        print("attive:", attive(dsn))
    if args.aziende:
        print("aziende:", aziende(dsn))
    if args.scadute:
        print("scadute:", scadute(dsn, args.giorni))
    if args.flusso:
        print("flusso:", flusso(dsn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
