"""Configurazione da variabili d'ambiente.

Nessun segreto nel codice. Il file .env viene letto solo se presente e non
sovrascrive mai una variabile già esportata nell'ambiente: sul server comanda
l'ambiente, il .env è una comodità locale.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"

ENV_VAR = "DATABASE_URL"
MIGRATOR_ENV_VAR = "MIGRATOR_DATABASE_URL"


def load_dotenv(path: Path | None = None) -> None:
    """Carica KEY=VALUE da un file .env, senza sovrascrivere l'ambiente."""
    path = path or REPO_ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def database_url() -> str:
    """Connessione dell'applicazione: ruolo nivult_app, solo DML."""
    load_dotenv()
    url = os.environ.get(ENV_VAR)
    if not url:
        raise SystemExit(
            f"{ENV_VAR} non impostata.\n"
            f"  Locale : copia .env.example in .env e compilala\n"
            f"  Server : la stringa di connessione sta in /opt/nivult/.env"
        )
    return url


def migrator_database_url() -> str:
    """Connessione del runner di migrazioni: ruolo nivult_migrator, con DDL.

    Ricade su DATABASE_URL quando non è impostata, perché in sviluppo c'è un
    ruolo solo. In produzione le due sono distinte, ed è il punto: il ruolo che
    l'applicazione usa tutti i giorni non può alterare lo schema.
    """
    load_dotenv()
    return os.environ.get(MIGRATOR_ENV_VAR) or database_url()


def database_name(url: str | None = None) -> str:
    """Nome del database dalla stringa di connessione.

    Passa da psycopg invece di spezzare l'URL a mano: con le URL a socket
    (postgresql:///nome?host=/tmp) l'ultimo segmento dopo '/' è la directory
    del socket, non il database.
    """
    from psycopg.conninfo import conninfo_to_dict

    return str(conninfo_to_dict(url or database_url()).get("dbname", ""))


def safe_dsn(url: str) -> str:
    """DSN con la password oscurata, per i log."""
    if "@" not in url:
        return url
    head, _, tail = url.rpartition("@")
    scheme, sep, creds = head.partition("://")
    if ":" in creds:
        user, _, _ = creds.partition(":")
        creds = f"{user}:***"
    return f"{scheme}{sep}{creds}@{tail}"
