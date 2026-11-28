from __future__ import annotations

from .backfill import attendi
from .riscoperta import _ATS_HOST

PUBLIC_BOARD_PLATFORMS = frozenset({
    "francetravail", "bundesanstellung", "arbetsformedlingen", "eures",
    "nav", "mycareersfuture", "epraca", "nva",
})
STAFFING_PLATFORMS = frozenset({"agenzie"})
STAFFING_JSONLD = frozenset({"randstad.de", "actief.be"})
SOURCE_TYPES = frozenset({"direct", "aggregator", "staffing", "public_board"})

COMPANY_SQL = """CASE WHEN co.source_type = 'aggregator'
    THEN NULLIF(btrim(j.raw->'company'->>'name'), '')
    ELSE COALESCE(NULLIF(btrim(co.company_name), ''),
                  NULLIF(btrim(j.raw->'company'->>'name'), ''),
                  NULLIF(btrim(j.raw->'hiringOrganization'->>'name'), ''),
                  NULLIF(btrim(j.raw->'_jobposting'->'hiringOrganization'->>'name'), ''),
                  NULLIF(btrim(j.raw->'employer'->>'name'), ''),
                  NULLIF(btrim(j.raw->'entreprise'->>'nom'), ''),
                  NULLIF(btrim(j.raw->>'firma'), ''),
                  NULLIF(btrim(j.raw->>'company_name'), ''),
                  NULLIF(btrim(j.raw->>'Company_Name'), '')) END"""
SOURCE_TYPE_SQL = """COALESCE(co.source_type, CASE
    WHEN j.platform_id IN ('francetravail','bundesanstellung','arbetsformedlingen',
                           'eures','nav','mycareersfuture','epraca','nva')
        THEN 'public_board'
    WHEN j.platform_id = 'agenzie' THEN 'staffing'
    ELSE 'direct' END)"""


def da_blocklist(dominio: str) -> bool:
    from nivult.ingestion.urls import JOB_BOARD_DOMAINS

    from .radar_indeed import BACHECHE

    d = dominio.lower().strip().removeprefix("www.")
    if _ATS_HOST.search(d):
        return True
    return any(d == b or d.endswith("." + b) for b in BACHECHE | JOB_BOARD_DOMAINS)


def classifica(platform_id: str, slug: str, *, offerte: int = 0, nomi: int = 0,
               sameas_esterni: int = 0, offerte_sameas_esterno: int = 0) -> str:
    if platform_id in PUBLIC_BOARD_PLATFORMS:
        return "public_board"
    if platform_id in STAFFING_PLATFORMS:
        return "staffing"
    if platform_id != "jsonld":
        return "direct"
    if slug in STAFFING_JSONLD:
        return "staffing"
    if da_blocklist(slug):
        return "aggregator"
    if nomi >= 5 or (offerte >= 500 and nomi == 0):
        return "aggregator"
    if sameas_esterni >= 2 and offerte_sameas_esterno >= 5:
        return "aggregator"
    return "direct"


def _batch(conn, righe: list[tuple[str, str, str]]) -> None:
    if not righe:
        return
    attendi(conn)
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "UPDATE ats_companies SET source_type=%s "
            "WHERE platform_id=%s AND slug=%s", righe)


def classifica_base(conn) -> dict[str, int]:
    stats = {"public_board": 0, "staffing": 0, "aggregator": 0}
    righe = conn.execute(
        "SELECT platform_id,slug FROM ats_companies "
        "ORDER BY platform_id,slug").fetchall()
    correnti: list[tuple[str, str, str]] = []
    for platform_id, slug in righe:
        tipo = classifica(platform_id, slug)
        if tipo == "direct":
            continue
        stats[tipo] += 1
        correnti.append((tipo, platform_id, slug))
        if len(correnti) >= 200:
            _batch(conn, correnti)
            correnti = []
    _batch(conn, correnti)
    return stats


def classifica_pubblici(conn) -> int:
    righe = conn.execute(
        "SELECT j.platform_id,j.slug FROM ats_jobs j "
        "LEFT JOIN ats_companies co ON co.platform_id=j.platform_id AND co.slug=j.slug "
        "WHERE j.platform_id = ANY(%s) AND co.slug IS NULL "
        "GROUP BY j.platform_id,j.slug ORDER BY j.platform_id,j.slug",
        (sorted(PUBLIC_BOARD_PLATFORMS),)).fetchall()
    inseriti = 0
    for inizio in range(0, len(righe), 200):
        batch = righe[inizio:inizio + 200]
        attendi(conn)
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO ats_companies(platform_id,slug,company_name,source_type,"
                "discovered_from) VALUES (%s,%s,NULL,'public_board','source_type') "
                "ON CONFLICT (platform_id,slug) DO NOTHING", batch)
            inseriti += cur.rowcount
    return inseriti


def classifica_staffing(conn) -> int:
    righe = conn.execute(
        "SELECT j.platform_id,j.slug FROM ats_jobs j "
        "LEFT JOIN ats_companies co ON co.platform_id=j.platform_id AND co.slug=j.slug "
        "WHERE j.platform_id = ANY(%s) AND co.slug IS NULL "
        "GROUP BY j.platform_id,j.slug ORDER BY j.platform_id,j.slug",
        (sorted(STAFFING_PLATFORMS),)).fetchall()
    inseriti = 0
    for inizio in range(0, len(righe), 200):
        batch = righe[inizio:inizio + 200]
        attendi(conn)
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO ats_companies(platform_id,slug,company_name,source_type,"
                "discovered_from) VALUES (%s,%s,NULL,'staffing','source_type') "
                "ON CONFLICT (platform_id,slug) DO NOTHING", batch)
            inseriti += cur.rowcount
    return inseriti


_STATS_SQL = r"""
WITH base AS (
    SELECT j.slug,
           NULLIF(btrim(COALESCE(j.raw->'company'->>'name',
                                 j.raw->'hiringOrganization'->>'name')), '') AS nome,
           lower(regexp_replace(COALESCE(
               substring(COALESCE(j.raw->'hiringOrganization'->>'sameAs',
                                  j.raw->'hiringOrganization'->'sameAs'->>0)
                         from 'https?://([^/]+)'), ''), '^www\.', '')) AS host
      FROM ats_jobs j
      JOIN ats_companies co ON co.platform_id=j.platform_id AND co.slug=j.slug
     WHERE j.platform_id='jsonld' AND co.source_type='direct'
       AND (%s::boolean IS FALSE OR co.last_fetch_at >= now() - interval '2 days')
), estesi AS (
    SELECT slug,nome,host,
           CASE WHEN host IS NOT NULL AND host <> '' AND host <> slug
                     AND host NOT LIKE '%%.' || slug THEN host END AS esterno
      FROM base
)
SELECT slug,count(*)::bigint AS offerte,
       count(DISTINCT nome)::bigint AS nomi,
       count(DISTINCT esterno)::bigint AS sameas_esterni,
       count(esterno)::bigint AS offerte_sameas_esterno
  FROM estesi GROUP BY slug ORDER BY slug
"""


def classifica_cardinalita(conn, solo_recenti: bool = False) -> dict[str, int]:
    conn.execute("SET statement_timeout = '30min'")
    righe = conn.execute(_STATS_SQL, (solo_recenti,)).fetchall()
    stats = {"aggregator": 0, "direct": 0}
    correnti: list[tuple[str, str, str]] = []
    for slug, offerte, nomi, esterni, offerte_esterno in righe:
        tipo = classifica("jsonld", slug, offerte=offerte, nomi=nomi,
                          sameas_esterni=esterni,
                          offerte_sameas_esterno=offerte_esterno)
        stats[tipo] += 1
        if tipo == "aggregator":
            correnti.append((tipo, "jsonld", slug))
            if len(correnti) >= 200:
                _batch(conn, correnti)
                correnti = []
    _batch(conn, correnti)
    return stats
