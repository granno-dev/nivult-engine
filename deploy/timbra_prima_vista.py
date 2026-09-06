"""Timbro prima-vista a LOTTI ordinati (regola di casa): posted_at =
created_at col flag, per chi non ha data. Riprendibile, tetto per giro."""
import os, sys, time, psycopg
c = psycopg.connect(os.environ["ATS_DATABASE_URL"], autocommit=True)
tetto = int(sys.argv[1]) if len(sys.argv) > 1 else 50000
fatti = 0
while fatti < tetto:
    ids = [r[0] for r in c.execute("""
        SELECT id FROM ats_jobs
         WHERE posted_at IS NULL AND expired_at IS NULL
           AND created_at > '2026-09-04 20:30+00'
         ORDER BY id LIMIT 300""").fetchall()]
    if not ids:
        break
    for _ in range(4):
        try:
            c.execute("""UPDATE ats_jobs SET posted_at = created_at,
                posted_at_estimated = true WHERE id = ANY(%s)""", (ids,))
            fatti += len(ids)
            break
        except psycopg.errors.DeadlockDetected:
            time.sleep(1)
    else:
        print("lotto bloccato, mi fermo"); break
print("timbrate prima-vista:", fatti)
