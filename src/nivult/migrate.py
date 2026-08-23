"""Runner di migrazioni versionate.

Niente Alembic: lo schema è pesantemente specifico di Postgres (pgvector,
indici parziali, FK composite, trigger). Con Alembic ogni migrazione sarebbe
comunque un op.execute() di SQL grezzo, quindi si otterrebbe la dipendenza
senza il beneficio. Qui i file .sql sono la fonte di verità.

Uso:
    python -m nivult.migrate status
    python -m nivult.migrate up [--target N] [--dry-run]

Una migrazione può disattivare l'incapsulamento in transazione mettendo
    -- nivult:no-transaction
fra le prime righe (serve ad ALTER DATABASE e a CREATE INDEX CONCURRENTLY).
Una migrazione così deve essere idempotente.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg

from nivult.config import MIGRATIONS_DIR, migrator_database_url, safe_dsn

FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
LOCK_KEY = 8_246_113_907_431_552  # arbitrario, costante: nivult migrations

BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version      integer     PRIMARY KEY,
  name         text        NOT NULL,
  checksum     text        NOT NULL,
  applied_at   timestamptz NOT NULL DEFAULT now(),
  execution_ms integer     NOT NULL
);
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str
    transactional: bool

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    if not directory.is_dir():
        raise SystemExit(f"cartella migrazioni non trovata: {directory}")

    found: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        m = FILENAME_RE.match(path.name)
        if not m:
            raise SystemExit(
                f"nome file non valido: {path.name}\n"
                f"atteso NNNN_nome_in_snake_case.sql"
            )
        version, name = int(m.group(1)), m.group(2)
        if version in found:
            raise SystemExit(
                f"versione {version:04d} duplicata: "
                f"{found[version].path.name} e {path.name}"
            )
        raw = path.read_bytes()
        sql = raw.decode("utf-8")
        head = "\n".join(sql.splitlines()[:5])
        found[version] = Migration(
            version=version,
            name=name,
            path=path,
            sql=sql,
            checksum=hashlib.sha256(raw).hexdigest(),
            transactional="-- nivult:no-transaction" not in head,
        )

    migrations = [found[v] for v in sorted(found)]
    expected = list(range(1, len(migrations) + 1))
    actual = [m.version for m in migrations]
    if actual != expected:
        raise SystemExit(f"versioni non contigue: attese {expected}, trovate {actual}")
    return migrations


def applied_state(conn: psycopg.Connection) -> dict[int, tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version")
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def verify_checksums(migrations: list[Migration], state: dict[int, tuple[str, str]]) -> None:
    """Una migrazione già applicata non si modifica: il database non lo saprebbe."""
    for m in migrations:
        if m.version in state and state[m.version][1] != m.checksum:
            raise SystemExit(
                f"checksum divergente per {m.label}.\n"
                f"È già applicata su questo database ma il file è cambiato.\n"
                f"Scrivi una nuova migrazione invece di modificare questa."
            )


def cmd_status(args: argparse.Namespace) -> int:
    migrations = discover()
    with psycopg.connect(migrator_database_url(), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(BOOTSTRAP)
        state = applied_state(conn)

    print(f"database: {safe_dsn(migrator_database_url())}\n")
    pending = 0
    for m in migrations:
        if m.version in state:
            mark = "applicata" if state[m.version][1] == m.checksum else "MODIFICATA!"
        else:
            mark = "in attesa"
            pending += 1
        flag = "" if m.transactional else "  [no-transaction]"
        print(f"  [{mark:>11}] {m.label}{flag}")

    orphans = sorted(set(state) - {m.version for m in migrations})
    for v in orphans:
        print(f"  [{'ORFANA':>11}] {v:04d}_{state[v][0]} — applicata ma il file non esiste")

    print(f"\n{len(migrations) - pending}/{len(migrations)} applicate, {pending} in attesa")
    return 1 if orphans else 0


def cmd_up(args: argparse.Namespace) -> int:
    migrations = discover()
    dsn = migrator_database_url()
    print(f"database: {safe_dsn(dsn)}")

    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(BOOTSTRAP)
            # Lock di sessione: due deploy in parallelo non applicano la stessa
            # migrazione due volte.
            cur.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))

        try:
            state = applied_state(conn)
            verify_checksums(migrations, state)

            todo = [m for m in migrations if m.version not in state]
            if args.target is not None:
                todo = [m for m in todo if m.version <= args.target]

            if not todo:
                print("niente da applicare.")
                return 0

            for m in todo:
                mode = "transazione" if m.transactional else "no-transaction"
                if args.dry_run:
                    print(f"  [dry-run] {m.label} ({mode}, {len(m.sql)} byte)")
                    continue

                print(f"  applico {m.label} ({mode}) ... ", end="", flush=True)
                started = time.monotonic()
                try:
                    if m.transactional:
                        with conn.transaction():
                            with conn.cursor() as cur:
                                cur.execute(m.sql)
                                _record(cur, m, started)
                    else:
                        with conn.cursor() as cur:
                            cur.execute(m.sql)
                            _record(cur, m, started)
                except psycopg.Error as exc:
                    print("FALLITA")
                    print(f"\n{m.path}:\n  {exc}", file=sys.stderr)
                    if not m.transactional:
                        print(
                            "  ATTENZIONE: migrazione no-transaction, "
                            "potrebbe essere applicata a metà.",
                            file=sys.stderr,
                        )
                    return 1
                print(f"{int((time.monotonic() - started) * 1000)} ms")

            print(f"\n{len(todo)} migrazioni applicate.")
            return 0
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def _record(cur: psycopg.Cursor, m: Migration, started: float) -> None:
    cur.execute(
        "INSERT INTO schema_migrations (version, name, checksum, execution_ms) "
        "VALUES (%s, %s, %s, %s)",
        (m.version, m.name, m.checksum, int((time.monotonic() - started) * 1000)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nivult.migrate", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="mostra cosa è applicato e cosa manca")

    up = sub.add_parser("up", help="applica le migrazioni mancanti")
    up.add_argument("--target", type=int, help="fermati a questa versione inclusa")
    up.add_argument("--dry-run", action="store_true", help="elenca senza applicare")

    args = parser.parse_args(argv)
    return {"status": cmd_status, "up": cmd_up}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
