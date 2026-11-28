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
Anche BEGIN/COMMIT interni vengono riconosciuti. Una migrazione
no-transaction deve essere idempotente, perché dopo un errore viene
ritentata dall'inizio. `-- nivult:require-table public.tabella` fa saltare
il file senza applicarlo né registrarlo se la tabella manca.
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
    required_table: str | None

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


def _sql_code(sql: str) -> str:
    """SQL senza commenti, stringhe o corpi funzione, conservando i separatori."""
    out = list(sql)
    i = 0
    while i < len(sql):
        if sql.startswith("--", i):
            end = sql.find("\n", i + 2)
            end = len(sql) if end < 0 else end
            for j in range(i, end):
                out[j] = " "
            i = end
            continue
        if sql.startswith("/*", i):
            depth, end = 1, i + 2
            while end < len(sql) and depth:
                if sql.startswith("/*", end):
                    depth += 1
                    end += 2
                elif sql.startswith("*/", end):
                    depth -= 1
                    end += 2
                else:
                    end += 1
            for j in range(i, min(end, len(sql))):
                if out[j] != "\n":
                    out[j] = " "
            i = end
            continue
        if sql[i] in ("'", '"'):
            quote = sql[i]
            end = i + 1
            while end < len(sql):
                if sql[end] == quote:
                    if end + 1 < len(sql) and sql[end + 1] == quote:
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            for j in range(i, min(end, len(sql))):
                if out[j] != "\n":
                    out[j] = " "
            i = end
            continue
        if sql[i] == "$":
            marker = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if marker:
                tag = marker.group(0)
                end = sql.find(tag, i + len(tag))
                end = len(sql) if end < 0 else end + len(tag)
                for j in range(i, end):
                    if out[j] != "\n":
                        out[j] = " "
                i = end
                continue
        i += 1
    return "".join(out)


def _split_sql(sql: str) -> list[str]:
    """Statement separati; i ';' dentro funzioni o letterali non contano."""
    code = _sql_code(sql)
    statements: list[str] = []
    start = 0
    for i, char in enumerate(code):
        if char != ";":
            continue
        statement = sql[start:i].strip()
        if _sql_code(statement).strip():
            statements.append(statement)
        start = i + 1
    statement = sql[start:].strip()
    if _sql_code(statement).strip():
        statements.append(statement)
    return statements


_TRANSACTION_CONTROL = re.compile(
    r"(?:^|;)\s*(?:BEGIN(?:\s+(?:TRANSACTION|WORK))?|START\s+TRANSACTION|"
    r"COMMIT(?:\s+(?:TRANSACTION|WORK))?|ROLLBACK(?:\s+(?:TRANSACTION|WORK))?|"
    r"END(?:\s+(?:TRANSACTION|WORK))?|ABORT)(?:\s*;|\s*\Z)",
    re.IGNORECASE | re.MULTILINE,
)
_REQUIRE_TABLE = re.compile(
    r"^\s*--\s*nivult:require-table\s+"
    r"([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def has_transaction_control(sql: str) -> bool:
    return _TRANSACTION_CONTROL.search(_sql_code(sql)) is not None


def tabella_richiesta(sql: str) -> str | None:
    matches = _REQUIRE_TABLE.findall(sql)
    if len(matches) > 1:
        raise ValueError("piu' direttive nivult:require-table nello stesso file")
    if matches:
        return matches[0]
    targets = set(re.findall(
        r"\b(?:ALTER\s+TABLE|ON)\s+((?:public\.)?ats_[A-Za-z0-9_]*)",
        _sql_code(sql),
        re.IGNORECASE,
    ))
    if len(targets) == 1:
        target = targets.pop()
        return target if "." in target else f"public.{target}"
    return None


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
            transactional=("-- nivult:no-transaction" not in head
                           and not has_transaction_control(sql)),
            required_table=tabella_richiesta(sql),
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


def missing_required_table(conn: psycopg.Connection, migration: Migration) -> str | None:
    table = migration.required_table
    if table is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (table,))
        present = cur.fetchone()[0] is not None
    return None if present else table


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

            applicate = saltate = 0
            for m in todo:
                mode = "transazione" if m.transactional else "no-transaction"
                missing = missing_required_table(conn, m)
                if args.dry_run:
                    suffix = f", salto: {missing} assente" if missing else ""
                    print(f"  [dry-run] {m.label} ({mode}, {len(m.sql)} byte{suffix})")
                    continue
                if missing:
                    print(f"  salto {m.label}: tabella richiesta {missing} assente; "
                          "DDL non eseguito e migrazione non registrata")
                    saltate += 1
                    continue

                print(f"  applico {m.label} ({mode}) ... ", end="", flush=True)
                started = time.monotonic()
                try:
                    if m.transactional:
                        with conn.transaction(), conn.cursor() as cur:
                            cur.execute(m.sql)
                            _record(cur, m, started)
                    else:
                        _execute_no_transaction(conn, m)
                        with conn.cursor() as cur:
                            _record(cur, m, started)
                except psycopg.Error as exc:
                    print("FALLITA")
                    print(f"\n{m.path}:\n  {exc}", file=sys.stderr)
                    if not m.transactional:
                        print("  ATTENZIONE: migrazione no-transaction, "
                              "potrebbe essere applicata a metà.", file=sys.stderr)
                    return 1
                applicate += 1
                print(f"{int((time.monotonic() - started) * 1000)} ms")

            print(f"\n{applicate} migrazioni applicate, {saltate} saltate.")
            return 0
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


_DROP_INVALID_INDEX = re.compile(
    r"^\s*--\s*nivult:drop-invalid-index\s+"
    r"([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_CREATE_CONCURRENT_INDEX = re.compile(
    r"CREATE\s+INDEX\s+CONCURRENTLY\s+IF\s+NOT\s+EXISTS\s+"
    r"((?:[A-Za-z_][A-Za-z0-9_]*\.)?[A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _indice_valido(cur, qualified: str) -> bool | None:
    row = cur.execute(
        "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(%s)",
        (qualified,),
    ).fetchone()
    return row[0] if row else None


def _gestisci_indice_concorrente(cur, statement: str, *, dopo: bool) -> None:
    if not dopo:
        drop = _DROP_INVALID_INDEX.search(statement)
        if drop:
            qualified = drop.group(1)
            if _indice_valido(cur, qualified) is False:
                schema, name = qualified.split(".")
                cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {schema}.{name}")
        return
    create = _CREATE_CONCURRENT_INDEX.search(_sql_code(statement))
    if create:
        qualified = create.group(1)
        if "." not in qualified:
            qualified = f"public.{qualified}"
        if _indice_valido(cur, qualified) is not True:
            raise psycopg.Error(f"indice concorrente non valido dopo CREATE: {qualified}")


def _execute_no_transaction(conn: psycopg.Connection, m: Migration) -> None:
    """Esegue un file idempotente in autocommit e lo ritenta una volta."""
    if not conn.autocommit:
        raise RuntimeError("migrazione no-transaction richiede autocommit=True")
    for attempt in (1, 2):
        conn.rollback()
        try:
            with conn.cursor() as cur:
                for statement in _split_sql(m.sql):
                    _gestisci_indice_concorrente(cur, statement, dopo=False)
                    cur.execute(statement)
                    _gestisci_indice_concorrente(cur, statement, dopo=True)
            return
        except psycopg.Error:
            conn.rollback()
            if attempt == 2:
                raise
            print(f"\n  {m.label}: tentativo {attempt} fallito; "
                  "ritento il file no-transaction idempotente ...", file=sys.stderr)


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
