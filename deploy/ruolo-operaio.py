"""Il ruolo Postgres dell'operaio a casa (N5): entra SOLO nel database delle
offerte (nivult_ats), mai in quello degli utenti (nivult). Idempotente.

Gira sul server, con la connessione del superutente:
    /opt/nivult/engine/.venv/bin/python deploy/ruolo-operaio.py
Scrive la password (una volta) in /opt/nivult/operaio-db-password (0600).

CONNECT su un database e' di PUBLIC per default: per chiudere `nivult`
all'operaio si revoca a PUBLIC e si ridà esplicitamente ai tre ruoli di
casa. La prova finale lo verifica davvero, in entrambi i versi.
"""
import os
import re
import secrets

import psycopg

pw = re.search(r"^POSTGRES_PASSWORD=(.*)$", open("/opt/nivult/.env").read(), re.M).group(1).strip()
FILE_PW = "/opt/nivult/operaio-db-password"
c = psycopg.connect(f"postgresql://nivult:{pw}@127.0.0.1:5432/nivult_ats", autocommit=True)

if not c.execute("SELECT 1 FROM pg_roles WHERE rolname='nivult_operaio'").fetchone():
    nuova = secrets.token_urlsafe(24)
    # CREATE ROLE non accetta parametri legati: la password va come letterale
    from psycopg import sql
    c.execute(sql.SQL("CREATE ROLE nivult_operaio LOGIN PASSWORD {}").format(sql.Literal(nuova)))
    with open(FILE_PW, "w") as f:
        f.write(nuova)
    os.chmod(FILE_PW, 0o600)
    print("ruolo nivult_operaio creato; password in", FILE_PW)
else:
    print("ruolo nivult_operaio esistente")

for s in [
    "GRANT CONNECT ON DATABASE nivult_ats TO nivult_operaio",
    "GRANT USAGE ON SCHEMA public TO nivult_operaio",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO nivult_operaio",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO nivult_operaio",
    "ALTER DEFAULT PRIVILEGES FOR ROLE nivult IN SCHEMA public "
    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO nivult_operaio",
    "ALTER DEFAULT PRIVILEGES FOR ROLE nivult IN SCHEMA public "
    "GRANT USAGE, SELECT ON SEQUENCES TO nivult_operaio",
    "REVOKE CONNECT ON DATABASE nivult FROM PUBLIC",
    "GRANT CONNECT ON DATABASE nivult TO nivult, nivult_app, nivult_migrator",
    "CREATE TABLE IF NOT EXISTS operaio_battiti "
    "(nome text PRIMARY KEY, battito timestamptz NOT NULL, note text)",
    "GRANT SELECT, INSERT, UPDATE ON operaio_battiti TO nivult_operaio",
]:
    c.execute(s)
print("grant fatti")

pw_op = open(FILE_PW).read().strip()
try:
    psycopg.connect(f"postgresql://nivult_operaio:{pw_op}@127.0.0.1:5432/nivult", connect_timeout=5).close()
    raise SystemExit("ERRORE: l'operaio entra nel database degli utenti")
except psycopg.OperationalError as e:
    print("db utenti chiuso all'operaio:", str(e).strip().splitlines()[0][:80])
n = psycopg.connect(f"postgresql://nivult_operaio:{pw_op}@127.0.0.1:5432/nivult_ats").execute(
    "SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL").fetchone()[0]
print("db offerte aperto all'operaio:", n)
url_app = re.search(r"^DATABASE_URL=(.*)$", open("/opt/nivult/engine/.env").read(), re.M)
if url_app:
    psycopg.connect(url_app.group(1).strip(), connect_timeout=5).close()
    print("nivult_app entra ancora nel db utenti: ok")
