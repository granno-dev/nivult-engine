"""Connessione al database."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg

from nivult.config import database_url


@contextmanager
def connect(autocommit: bool = False) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(database_url(), autocommit=autocommit)
    try:
        yield conn
    finally:
        conn.close()
