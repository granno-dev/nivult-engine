"""Drenaggio backlog opt-in; non modifica i breaker del regime automatico."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter

import psycopg


def _parse_tenant(values: list[str]) -> list[tuple[str, str]]:
    result = []
    for value in values:
        platform, sep, slug = value.partition(":")
        if not sep or not platform.strip() or not slug.strip():
            raise ValueError(f"tenant non valido: {value!r}; atteso PIATTAFORMA:SLUG")
        result.append((platform.strip(), slug.strip()))
    return result


def _candidati(
    conn,
    *,
    kind: str,
    max_per_tenant: int,
    max_total: int,
    platforms: list[str],
    tenants: list[tuple[str, str]],
) -> list[tuple]:
    filters = []
    params: list = []
    if kind == "zombie":
        filters.append("j.active_since < now() - interval '365 days'")
    elif kind == "last_seen":
        filters.append("j.active_since >= now() - interval '365 days'")
    if platforms:
        filters.append("j.platform_id = ANY(%s)")
        params.append(platforms)
    if tenants:
        filters.append(
            "(" + " OR ".join("(j.platform_id = %s AND j.slug = %s)" for _ in tenants) + ")"
        )
        for tenant in tenants:
            params.extend(tenant)
    extra = " AND " + " AND ".join(filters) if filters else ""
    return conn.execute(
        f"""
        WITH candidate AS MATERIALIZED (
            SELECT j.id, j.platform_id, j.slug, j.active_since, j.fetched_at,
                   CASE WHEN j.active_since < now() - interval '365 days'
                        THEN 'zombie_365d_backlog' ELSE 'last_seen_7d_backlog' END AS reason
              FROM ats_jobs j
              JOIN ats_companies c ON c.platform_id = j.platform_id AND c.slug = j.slug
             WHERE j.expired_at IS NULL
               AND j.active_since IS NOT NULL
               AND (j.active_since < now() - interval '365 days'
                    OR (j.fetched_at < now() - interval '7 days'
                        AND j.absent_since < now() - interval '7 days'
                        AND j.absence_count >= 2
                        AND c.last_ok_at > j.fetched_at
                        AND c.lettura_parziale = false
                        AND j.platform_id <> 'jsonld'))
               {extra}),
        limited AS (
            SELECT *, row_number() OVER (
                       PARTITION BY platform_id, slug ORDER BY active_since, id) AS rn
              FROM candidate)
        SELECT id, platform_id, slug, active_since, fetched_at, reason
          FROM limited WHERE rn <= %s
         ORDER BY active_since, id LIMIT %s
        """,
        (*params, max_per_tenant, max_total),
    ).fetchall()


def _report(rows: list[tuple]) -> dict:
    tenant = Counter((r[1], r[2], r[5]) for r in rows)
    platform = Counter((r[1], r[5]) for r in rows)
    return {
        "selected": len(rows),
        "tenant": [
            {"platform": p, "tenant": t, "reason": reason, "count": n}
            for (p, t, reason), n in sorted(tenant.items())
        ],
        "platform": [
            {"platform": p, "reason": reason, "count": n}
            for (p, reason), n in sorted(platform.items())
        ],
    }


def esegui(
    dsn: str,
    *,
    kind: str = "both",
    max_per_tenant: int = 50,
    max_total: int = 400,
    platforms: list[str] | None = None,
    tenants: list[tuple[str, str]] | None = None,
    apply: bool = False,
) -> dict:
    if max_per_tenant < 1 or max_total < 1:
        raise ValueError("i tetti devono essere positivi")
    with psycopg.connect(dsn) as conn:
        rows = _candidati(
            conn,
            kind=kind,
            max_per_tenant=max_per_tenant,
            max_total=max_total,
            platforms=platforms or [],
            tenants=tenants or [],
        )
        report = _report(rows)
        print(json.dumps(report, ensure_ascii=False, default=str))
        stats = {"dry_run": not apply, "closed": 0, "report": report}
        if not apply or not rows:
            return stats
        result = conn.execute(
            """
            WITH requested AS (
                SELECT * FROM unnest(
                    %s::uuid[], %s::text[], %s::timestamptz[], %s::timestamptz[])
                AS s(id, reason, active_since, fetched_at))
            UPDATE ats_jobs j SET expired_at = now(), closure_reason = s.reason
              FROM requested s
             WHERE j.id = s.id AND j.expired_at IS NULL
               AND j.active_since = s.active_since
               AND j.fetched_at = s.fetched_at
            """,
            (
                [r[0] for r in rows],
                [r[5] for r in rows],
                [r[3] for r in rows],
                [r[4] for r in rows],
            ),
        )
        stats["closed"] = result.rowcount
        conn.commit()
        return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nivult.ats.backlog_wave2")
    parser.add_argument("--dsn", default=os.environ.get("ATS_DATABASE_URL"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="scrivi il batch; default dry-run")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--kind", choices=("both", "zombie", "last_seen"), default="both")
    parser.add_argument("--max-per-tenant", type=int, default=50)
    parser.add_argument("--max-total", type=int, default=400)
    parser.add_argument("--platform", action="append", default=[])
    parser.add_argument("--tenant", action="append", default=[], metavar="PIATTAFORMA:SLUG")
    args = parser.parse_args(argv)
    if not args.dsn:
        parser.error("serve --dsn oppure ATS_DATABASE_URL")
    try:
        tenants = _parse_tenant(args.tenant)
    except ValueError as exc:
        parser.error(str(exc))
    esegui(
        args.dsn,
        kind=args.kind,
        max_per_tenant=args.max_per_tenant,
        max_total=args.max_total,
        platforms=args.platform,
        tenants=tenants,
        apply=args.apply,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
