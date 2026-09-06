"""nivult-v0 in linea: il primo classificatore, gratis e in millisecondi.

Legge le offerte senza famiglia (prima le davvero recenti, con data di
fonte), le classifica col modello locale e SCRIVE solo dove e' sicuro
(confidenza >= SOGLIA): famiglia in job_classifications (model =
'nivult-v0', confidence salvata) e seniority se manca. Sotto soglia non
scrive: quelle restano all'LLM. Marcatore locale_at: ogni offerta
passa dal modello una volta sola.

Lotti da 8 e 2 thread: la RAM del server e' stretta (7,6GB), un lotto
da 32 accanto allo sprint e' andato in OOM. Scritture a lotti ordinati
con retry, regola di casa."""
import os, sys, time, psycopg
from nivult.ats.modello_locale import ModelloLocale

SOGLIA = float(os.environ.get("SOGLIA_LOCALE", "0.9"))
TETTO = int(sys.argv[1]) if len(sys.argv) > 1 else 50000
LOTTO = 8

def _colonna_manca(c, t, col):
    return c.execute("SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
                     (t, col)).fetchone() is None

def main():
    m = ModelloLocale()
    st = {"viste": 0, "fam_scritte": 0, "sen_scritte": 0, "incerte": 0}
    t0 = time.time()
    with psycopg.connect(os.environ["ATS_DATABASE_URL"], autocommit=True) as c:
        if _colonna_manca(c, "ats_jobs", "locale_at"):
            c.execute("ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS locale_at timestamptz")
        while st["viste"] < TETTO:
            righe = c.execute("""
                SELECT j.id, j.title, coalesce(j.location, j.city, ''),
                       left(coalesce(j.raw->>'description', j.raw->>'descriptionPlain', ''), 600),
                       j.seniority
                  FROM ats_jobs j
                 WHERE j.expired_at IS NULL AND j.locale_at IS NULL
                   AND NOT EXISTS (SELECT 1 FROM job_classifications x WHERE x.job_id = j.id)
                 ORDER BY (NOT coalesce(j.posted_at_estimated, false)) DESC,
                          j.posted_at DESC NULLS LAST
                 LIMIT 400""").fetchall()
            if not righe:
                break
            fam_rows, sen_rows, marcati = [], [], []
            for i in range(0, len(righe), LOTTO):
                b = righe[i:i+LOTTO]
                pred = m.predici([f"{t} | {l} | {d}" for _, t, l, d, _ in b])
                for (jid, _, _, _, sen_att), (fam, cf, sen, cs) in zip(b, pred):
                    st["viste"] += 1
                    marcati.append(jid)
                    if cf >= SOGLIA:
                        fam_rows.append((jid, fam, round(cf, 3)))
                    else:
                        st["incerte"] += 1
                    if sen_att is None and cs >= SOGLIA:
                        sen_rows.append((sen, jid))
            # scritture a lotti ordinati, retry sul deadlock
            def scrivi(sql, rows, key):
                rows = sorted(rows, key=key)
                for k in range(0, len(rows), 300):
                    for _ in range(3):
                        try:
                            with c.cursor() as cc:
                                cc.executemany(sql, rows[k:k+300])
                            break
                        except psycopg.errors.DeadlockDetected:
                            time.sleep(1)
            scrivi("INSERT INTO job_classifications (job_id, family, confidence, model, classified_at) "
                   "VALUES (%s, %s, %s, 'nivult-v0', now()) ON CONFLICT (job_id) DO NOTHING",
                   fam_rows, lambda r: r[0])
            scrivi("UPDATE ats_jobs SET seniority = coalesce(seniority, %s) WHERE id = %s",
                   sen_rows, lambda r: r[1])
            scrivi("UPDATE ats_jobs SET locale_at = now() WHERE id = %s",
                   [(j,) for j in marcati], lambda r: r[0])
            st["fam_scritte"] += len(fam_rows); st["sen_scritte"] += len(sen_rows)
            dt = time.time() - t0
            print(f"{st['viste']} viste | famiglie {st['fam_scritte']} | seniority {st['sen_scritte']} "
                  f"| incerte {st['incerte']} | {st['viste']/max(dt,1):.1f}/s", flush=True)
    print(f"FINE {st} in {time.time()-t0:.0f}s")

if __name__ == "__main__":
    main()
