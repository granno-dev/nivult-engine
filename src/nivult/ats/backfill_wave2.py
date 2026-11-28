"""Backfill online dell'Ondata 2; dry-run di default, --apply per scrivere."""

from __future__ import annotations

import argparse
import os
import time

import psycopg

INDICI = (
    "ats_jobs_assenze_idx",
    "ats_jobs_zombie_idx",
    "ats_jobs_content_updated_idx",
)
CONSTRAINTS = ("ats_jobs_posted_at_minima_check", "ats_jobs_absence_count_check")


def _count(conn, sql: str) -> int:
    return conn.execute(sql).fetchone()[0]


def _residui(conn) -> tuple[int, int]:
    active = _count(conn, "SELECT count(*) FROM ats_jobs WHERE active_since IS NULL")
    date = _count(
        conn,
        "SELECT count(*) FROM ats_jobs WHERE posted_at IS NOT NULL "
        "AND (posted_at < TIMESTAMPTZ '2020-01-01 00:00:00+00' "
        "OR posted_at > now() + interval '2 days')",
    )
    return active, date


def _indici_pronti(conn) -> list[str]:
    applied = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version = 102"
    ).fetchone()
    if not applied:
        raise RuntimeError("0102 non risulta applicata")
    problemi = []
    for name in INDICI:
        row = conn.execute(
            "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(%s)",
            (f"public.{name}",),
        ).fetchone()
        if not row or row[0] is not True:
            problemi.append(name)
    return problemi


def _batch(conn, step: str, batch_size: int, after: str | None) -> tuple[int, str | None]:
    if step == "A":
        condition = (
            "active_since IS NULL AND (posted_at IS NULL OR "
            "(posted_at >= TIMESTAMPTZ '2020-01-01 00:00:00+00' "
            "AND posted_at <= now() + interval '2 days'))"
        )
        update = "active_since = COALESCE(created_at, fetched_at, now())"
    else:
        condition = (
            "posted_at IS NOT NULL AND "
            "(posted_at < TIMESTAMPTZ '2020-01-01 00:00:00+00' "
            "OR posted_at > now() + interval '2 days')"
        )
        update = (
            "posted_at = greatest(TIMESTAMPTZ '2020-01-01 00:00:00+00', "
            "least(now(), COALESCE(created_at, fetched_at, now()))), "
            "posted_at_estimated = true"
        )
    keyset = "AND id > %s" if after else ""
    params = ([after] if after else []) + [batch_size]
    rows = conn.execute(
        f"SELECT id FROM ats_jobs WHERE {condition} {keyset} ORDER BY id LIMIT %s",
        params,
    ).fetchall()
    if not rows:
        return 0, None
    ids = [row[0] for row in rows]
    with conn.cursor() as cur:
        if step == "B":
            cur.execute("SELECT set_config('nivult.skip_content_updated', 'on', true)")
        result = cur.execute(
            f"UPDATE ats_jobs SET {update} WHERE id = ANY(%s::uuid[]) "
            f"AND {condition} RETURNING id",
            (ids,),
        )
        updated = result.rowcount
    return updated, str(rows[-1][0])


def esegui(
    dsn: str,
    *,
    batch_size: int = 5000,
    sleep_ms: int = 50,
    apply: bool = False,
    steps: str = "ABC",
) -> dict:
    if not 1 <= batch_size <= 10_000:
        raise ValueError("batch_size deve essere fra 1 e 10000")
    stats = {"dry_run": not apply, "updated": {"A": 0, "B": 0}, "validated": []}
    with psycopg.connect(dsn) as conn:
        if not apply:
            stats["remaining_a"], stats["remaining_b"] = _residui(conn)
            if "C" in steps:
                try:
                    stats["indexes_invalid_or_missing"] = _indici_pronti(conn)
                except RuntimeError as exc:
                    stats["indexes_invalid_or_missing"] = [str(exc)]
            print(stats)
            return stats

        for step in "BA":
            if step not in steps:
                continue
            after = None
            while True:
                updated, last_id = _batch(conn, step, batch_size, after)
                conn.commit()
                if last_id is None:
                    break
                after = last_id
                stats["updated"][step] += updated
                if sleep_ms:
                    time.sleep(sleep_ms / 1000)

        stats["remaining_a"], stats["remaining_b"] = _residui(conn)
        if "C" in steps:
            if stats["remaining_a"] or stats["remaining_b"]:
                raise RuntimeError(f"backfill incompleto: {stats}")
            problemi = _indici_pronti(conn)
            if problemi:
                raise RuntimeError(f"indici mancanti o invalidi: {problemi}")
            for constraint in CONSTRAINTS:
                with conn.transaction():
                    conn.execute(f"ALTER TABLE ats_jobs VALIDATE CONSTRAINT {constraint}")
                stats["validated"].append(constraint)
        conn.commit()
    print(stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nivult.ats.backfill_wave2")
    parser.add_argument("--dsn", default=os.environ.get("ATS_DATABASE_URL"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="esegui i backfill; default dry-run")
    mode.add_argument("--dry-run", action="store_true", help="solo conteggi, nessuna scrittura")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--sleep-ms", type=int, default=50)
    parser.add_argument("--steps", choices=("A", "B", "C", "AB", "ABC"), default="ABC")
    args = parser.parse_args(argv)
    if not args.dsn:
        parser.error("serve --dsn oppure ATS_DATABASE_URL")
    esegui(
        args.dsn,
        batch_size=args.batch_size,
        sleep_ms=args.sleep_ms,
        apply=args.apply,
        steps=args.steps,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
