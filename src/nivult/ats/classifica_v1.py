"""nivult-v1 in linea, sul N5: famiglia, seniority, contratto, remoto, lingue.

Legge le offerte non ancora passate dal modello (marcatore `locale_v1_at`:
`locale_at` e' di nivult-v0, e le offerte gia' viste da v0 vanno riviste da v1 —
prima le piu' recenti con data di fonte) e SCRIVE solo dove e' sicuro:

  - famiglia in job_classifications (model='nivult-v1', confidence) se
    la confidenza supera la soglia del 95% misurata all'esame
    (`soglie_95.family`, 0.75 il 07/09/2026); sotto, resta all'LLM;
  - seniority / employment_type / remote SOLO dove la colonna e' NULL e
    la confidenza supera SOGLIA_RIPIEGO (0.85): per queste teste nessuna
    soglia ha raggiunto il 95% all'esame, quindi riempiono il vuoto, non
    sovrascrivono mai un valore letto dalla fonte o da GLM;
  - languages_required dove e' NULL, con le lingue a probabilita' >= 0.5
    (precisione misurata 0.875, copertura 0.808).

    ATS_DATABASE_URL=... python -m nivult.ats.classifica_v1 [tetto] [--dry-run]
"""
from __future__ import annotations

import os
import sys
import time

import psycopg

from nivult.ats.modello_v1 import ModelloV1, testo

SOGLIA_RIPIEGO = float(os.environ.get("SOGLIA_RIPIEGO_V1", "0.85"))
# Il contratto e' la testa piu' debole (83,9% all'esame) e sbaglia in modo
# vistoso: «Lead Product Engineer» → internship con confidenza sopra 0,85
# (campione del 07/09/2026 sera). Per scriverlo si pretende molto di piu'.
SOGLIA_CONTRATTO = float(os.environ.get("SOGLIA_CONTRATTO_V1", "0.97"))
LOTTO = int(os.environ.get("LOTTO_V1", "64"))


def main() -> int:
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    continuo = "--continuo" in sys.argv      # un demone: quando non c'e' niente, dorme un minuto
    tetto = int(argv[0]) if argv else (10**12 if continuo else 20000)
    m = ModelloV1()
    soglia_fam = float(m.soglie_95.get("family") or 0.9)
    st = {"viste": 0, "famiglie": 0, "seniority": 0, "contratto": 0, "remoto": 0,
          "lingue": 0, "incerte": 0, "device": m.device}
    t0 = time.time()
    with psycopg.connect(os.environ["ATS_DATABASE_URL"], autocommit=not dry) as c:
        while st["viste"] < tetto:
            righe = c.execute("""
                SELECT j.id, j.title, coalesce(j.location, j.city, ''),
                       left(COALESCE(j.raw->>'description', j.raw->>'content', j.raw->>'descriptionHtml', j.raw->>'descriptionPlain', j.raw->>'externalDescription', j.raw->>'jobDescription', j.raw->>'job_description', j.raw->>'Job_Description', j.raw->>'body', j.raw->>'description_html', j.raw->>'descriptionBody', j.raw->>'text', ''), 4000),
                       j.seniority, j.employment_type, j.remote, j.languages_required,
                       EXISTS (SELECT 1 FROM job_classifications x WHERE x.job_id = j.id) AS ha_famiglia
                  FROM ats_jobs j
                 WHERE j.expired_at IS NULL AND j.locale_v1_at IS NULL
                 -- prima chi NON ha famiglia: la notte dell'08/09 il demone ha
                 -- speso 295k letture per scriverne 22k, perche' rileggeva
                 -- offerte gia' classificate mentre l'arretrato senza famiglia
                 -- cresceva (325k -> 339k)
                 ORDER BY EXISTS (SELECT 1 FROM job_classifications x WHERE x.job_id = j.id) ASC,
                          (NOT coalesce(j.posted_at_estimated, false)) DESC,
                          j.posted_at DESC NULLS LAST
                 LIMIT 2048""").fetchall()
            if not righe:
                if continuo:
                    time.sleep(60)
                    continue
                break
            fam_rows, sen_rows, con_rows, rem_rows, lin_rows, marcati = [], [], [], [], [], []
            for i in range(0, len(righe), LOTTO):
                b = righe[i:i + LOTTO]
                pred = m.predici([testo(t, l, d) for _, t, l, d, *_ in b])
                for (jid, _, _, _, sen, con, rem, lin, ha_fam), p in zip(b, pred):
                    st["viste"] += 1
                    marcati.append(jid)
                    fam, cf = p["family"]
                    if not ha_fam:
                        if cf >= soglia_fam:
                            fam_rows.append((jid, fam, round(cf, 3)))
                        else:
                            st["incerte"] += 1
                    if sen is None and p["seniority"][1] >= SOGLIA_RIPIEGO:
                        sen_rows.append((p["seniority"][0], jid))
                    if con is None and p["employment_type"][1] >= SOGLIA_CONTRATTO:
                        con_rows.append((p["employment_type"][0], jid))
                    if rem is None and p["remote"][1] >= SOGLIA_RIPIEGO:
                        rem_rows.append((p["remote"][0], jid))
                    if lin is None:
                        lingue = [cod for cod, pr in p["lingue"] if pr >= 0.5]
                        if lingue:
                            lin_rows.append((lingue, jid))

            def scrivi(sql, params):
                """UNA istruzione per tabella, con array unnest: 6 round trip per
                lotto. Con executemany erano migliaia, e sulla Tailscale N5 →
                Hetzner (30 ms l'uno) il modello aspettava il database: 3
                offerte/s contro le 26 della GPU (misurato il 07/09/2026)."""
                for _ in range(3):
                    try:
                        c.execute(sql, params)
                        break
                    except psycopg.errors.DeadlockDetected:
                        time.sleep(1)

            if not dry:
                if fam_rows:
                    scrivi("INSERT INTO job_classifications (job_id, family, confidence, model, classified_at) "
                           "SELECT * FROM unnest(%s::uuid[], %s::text[], %s::real[]), (SELECT 'nivult-v1', now()) m "
                           "ON CONFLICT (job_id) DO NOTHING",
                           ([r[0] for r in fam_rows], [r[1] for r in fam_rows], [r[2] for r in fam_rows]))
                for col, rows in (("seniority", sen_rows), ("employment_type", con_rows), ("remote", rem_rows)):
                    if rows:
                        scrivi(f"UPDATE ats_jobs j SET {col} = coalesce(j.{col}, v.val) "
                               f"FROM unnest(%s::uuid[], %s::text[]) AS v(id, val) WHERE j.id = v.id",
                               ([r[1] for r in rows], [r[0] for r in rows]))
                if lin_rows:
                    scrivi("UPDATE ats_jobs j SET languages_required = coalesce(j.languages_required, string_to_array(v.val, ',')) "
                           "FROM unnest(%s::uuid[], %s::text[]) AS v(id, val) WHERE j.id = v.id",
                           ([r[1] for r in lin_rows], [",".join(r[0]) for r in lin_rows]))
                scrivi("UPDATE ats_jobs SET locale_v1_at = now() WHERE id = ANY(%s::uuid[])", (marcati,))
            st["famiglie"] += len(fam_rows); st["seniority"] += len(sen_rows)
            st["contratto"] += len(con_rows); st["remoto"] += len(rem_rows); st["lingue"] += len(lin_rows)
            dt = time.time() - t0
            print(f"{st['viste']} viste | famiglie {st['famiglie']} | seniority {st['seniority']} | "
                  f"contratto {st['contratto']} | remoto {st['remoto']} | lingue {st['lingue']} | "
                  f"incerte {st['incerte']} | {st['viste'] / max(dt, 1):.1f}/s", flush=True)
            if dry:
                break
            if continuo and st["viste"] % 20480 < 2048:
                st["ore"] = round((time.time() - t0) / 3600, 2)
                print(f"BATTITO {st}", flush=True)
    print(f"FINE {st} in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
