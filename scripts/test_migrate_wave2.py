from pathlib import Path

import pytest

from nivult import migrate


def test_sql_parser_isola_dollar_quote_commenti_e_transazioni():
    sql = """
    -- nivult:require-table public.ats_jobs
    CREATE OR REPLACE FUNCTION f() RETURNS trigger AS $f$
    BEGIN
      RAISE NOTICE 'BEGIN; non e' || ' controllo';
    END;
    $f$ LANGUAGE plpgsql;
    CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_test ON tabella(id);
    """
    statements = migrate._split_sql(sql)
    assert len(statements) == 2
    assert "RAISE NOTICE" in statements[0]
    assert statements[1].startswith("CREATE INDEX CONCURRENTLY")
    assert not migrate.has_transaction_control(sql)
    assert migrate.has_transaction_control(sql + "\nCOMMIT;\n")
    assert migrate.tabella_richiesta(sql) == "public.ats_jobs"


def test_require_table_rifiuta_direttive_multiple():
    with pytest.raises(ValueError, match="piu' direttive"):
        migrate.tabella_richiesta(
            "-- nivult:require-table public.a\n"
            "-- nivult:require-table public.b"
        )


def test_no_transaction_richiede_autocommit():
    class Conn:
        autocommit = False

    migration = migrate.Migration(1, "test", Path("x"), "SELECT 1", "", False, None)
    with pytest.raises(RuntimeError, match="autocommit=True"):
        migrate._execute_no_transaction(Conn(), migration)


def test_indice_invalido_viene_eliminato_prima_della_ricostruzione():
    class Cursor:
        def __init__(self):
            self.valid = False
            self.calls = []

        def execute(self, sql, params=None):
            self.calls.append(sql)
            if sql.startswith("DROP INDEX CONCURRENTLY"):
                self.valid = None
            elif "CREATE INDEX CONCURRENTLY" in sql:
                self.valid = True
            return self

        def fetchone(self):
            return None if self.valid is None else (self.valid,)

    cur = Cursor()
    statement = """
    -- nivult:drop-invalid-index public.idx_test
    CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_test ON tabella(id)
    """
    migrate._gestisci_indice_concorrente(cur, statement, dopo=False)
    assert cur.valid is None
    cur.execute(migrate._split_sql(statement)[0])
    migrate._gestisci_indice_concorrente(cur, statement, dopo=True)
    assert cur.valid is True
    assert any(sql.startswith("DROP INDEX CONCURRENTLY") for sql in cur.calls)
