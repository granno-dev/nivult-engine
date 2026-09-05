import os, psycopg
c = psycopg.connect(os.environ["ATS_DATABASE_URL"], autocommit=True)
n = c.execute("""UPDATE ats_jobs SET posted_at = created_at,
    posted_at_estimated = true
  WHERE posted_at IS NULL AND expired_at IS NULL
    AND created_at > (now() - make_interval(days => 2))""").rowcount
print("timbrate prima-vista:", n)
