"""nivult-v1 in linea, sul N5: famiglia, seniority, contratto, remoto, lingue.

Legge le offerte non ancora passate dal modello (marcatore `locale_v1_at`:
`locale_at` e' di nivult-v0, e le offerte gia' viste da v0 vanno riviste da v1 —
prima le piu' recenti con data di fonte) e SCRIVE solo dove e' sicuro:

  - famiglia in job_classifications (model='nivult-v1', confidence) se
    la confidenza supera la soglia (dal 21/09/2026: al massimo 0,5 su ogni
    testa, RIEMPI_V1 — un campo vuoto non si vende, un errore piccolo si
    corregge; prima era la soglia del 95% misurata all'esame);
  - seniority / employment_type / remote SOLO dove la colonna e' NULL:
    riempiono il vuoto, non sovrascrivono mai un valore letto dalla fonte
    o da GLM;
  - languages_required dove e' NULL, con le lingue a probabilita' >= 0.5
    (precisione misurata 0.875, copertura 0.808).

    ATS_DATABASE_URL=... python -m nivult.ats.classifica_v1 [tetto] [--dry-run]
"""
from __future__ import annotations

import os
import sys
import time

import psycopg

from nivult.ats.dichiarati import PIATTAFORME
from nivult.ats.modello_v1 import ModelloV1, testo, pulito
from nivult.ats.testo import evidenza_stage

SOGLIA_RIPIEGO = float(os.environ.get("SOGLIA_RIPIEGO_V1", "0.85"))
# Il contratto e' la testa piu' debole (83,9% all'esame) e sbaglia in modo
# vistoso: «Lead Product Engineer» → internship con confidenza sopra 0,85
# (campione del 07/09/2026 sera). Per scriverlo si pretende molto di piu'.
SOGLIA_CONTRATTO = float(os.environ.get("SOGLIA_CONTRATTO_V1", "0.97"))
LOTTO = int(os.environ.get("LOTTO_V1", "32"))
# Quando c'e' poco da fare, v1 dorme invece di contendere la scheda al 2B:
# il lavoro si accumula e si fa in blocchi lunghi, che rendono di piu' (16/09/2026).
SOGLIA_CEDI = int(os.environ.get("SOGLIA_CEDI_V1", "600"))
RIPOSO_V1 = float(os.environ.get("RIPOSO_V1", "90"))

# ── La fascia silenziosa ─────────────────────────────────────────────
# Il N5 sta in casa, non in sala macchine: a piena velocita' la GPU lo
# tiene a 76 °C e la ventola si sente di notte. Misurato l'08/09/2026:
# v1 fa 92.000 offerte l'ora e ne entrano 9.700 — nove volte il
# fabbisogno. Fra NOTTE_DA e NOTTE_A (ora di Roma) il demone lavora
# quindi a scatti: un lotto, poi riposa NOTTE_RIPOSO secondi. Restano
# ~22.000 l'ora, il doppio delle nuove, e l'arretrato cala lo stesso.
# NOTTE_DA uguale a NOTTE_A spegne la fascia.
NOTTE_DA = os.environ.get("NOTTE_DA", "23:30")
NOTTE_A = os.environ.get("NOTTE_A", "07:00")
NOTTE_RIPOSO = float(os.environ.get("NOTTE_RIPOSO", "8"))


def e_notte() -> bool:
    if NOTTE_DA == NOTTE_A:
        return False
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ora = datetime.now(ZoneInfo("Europe/Rome")).strftime("%H:%M")
    if NOTTE_DA <= NOTTE_A:
        return NOTTE_DA <= ora < NOTTE_A
    return ora >= NOTTE_DA or ora < NOTTE_A      # a cavallo di mezzanotte


def main() -> int:
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in sys.argv
    continuo = "--continuo" in sys.argv      # un demone: quando non c'e' niente, dorme un minuto
    # --pareri: il RIPASSO delle offerte gia' viste, per raccogliere il parere
    # di v1 dove la famiglia c'era gia'. Serve una volta sola sull'arretrato:
    # da li' in poi il giro normale lo salva da se'.
    pareri = "--pareri" in sys.argv
    tetto = int(argv[0]) if argv else (10**12 if continuo else 20000)
    m = ModelloV1()
    soglia_fam = float(m.soglie_95.get("family") or 0.9)
    # Ogni testa usa la SUA soglia misurata al 95% di precisione; le costanti
    # restano come rete per una testa senza soglia, e le variabili d'ambiente
    # continuano a vincere su tutto (16/09/2026).
    def _soglia(testa: str, riserva: float) -> float:
        amb = os.environ.get("SOGLIA_" + testa.upper() + "_V1")
        if amb: return float(amb)
        misurata = m.soglie_95.get(testa)
        return float(misurata) if misurata is not None else riserva
    soglia_sen = _soglia("seniority", SOGLIA_RIPIEGO)
    soglia_con = _soglia("employment_type", SOGLIA_CONTRATTO)
    soglia_rem = _soglia("remote", SOGLIA_RIPIEGO)
    # RIEMPIRE, NON TACERE (decisione di Giuseppe, 21/09/2026). Sulle 40 righe
    # del golden v2 lette col testo intero v1 aveva ragione sulla seniority 28
    # volte su 35, ma la soglia 0,85 gliene lasciava scrivere DUE: su sei classi
    # contigue la confidenza sta fra 0,5 e 0,7 anche quando ha ragione, e gli
    # errori sono fra livelli vicini (senior/lead, junior/mid). Un campo vuoto
    # non si vende; un errore piccolo si corregge. Tetto 0,5 su tutte e quattro
    # le teste: sopra 0,5 la classe scelta e' comunque la piu' probabile con un
    # margine. RIEMPI_V1=0 rimette le soglie al 95% di precisione.
    if os.environ.get("RIEMPI_V1", "1") == "1":
        tetto_soglia = float(os.environ.get("TETTO_SOGLIA_V1", "0.5"))
        soglia_fam, soglia_sen = min(soglia_fam, tetto_soglia), min(soglia_sen, tetto_soglia)
        soglia_con, soglia_rem = min(soglia_con, tetto_soglia), min(soglia_rem, tetto_soglia)
    print(f"soglie in uso: famiglia {soglia_fam} seniority {soglia_sen} "
          f"contratto {soglia_con} remoto {soglia_rem}", flush=True)
    st = {"viste": 0, "famiglie": 0, "seniority": 0, "contratto": 0, "remoto": 0,
          "lingue": 0, "incerte": 0, "device": m.device}
    notte_detta = False
    t0 = time.time()
    with psycopg.connect(os.environ["ATS_DATABASE_URL"], autocommit=not dry) as c:
        # Il RIPASSO ha una query sua. Con quella del giro normale faceva 9
        # offerte al secondo invece di 27 (misurato il 10/09/2026), e la GPU
        # non c'entrava: l'ORDER BY riordinava 1,65 milioni di righe a ogni
        # lotto per mettere davanti quelle senza famiglia, che in questa
        # modalita' sono zero per definizione — si ripassano proprio quelle
        # che la famiglia ce l'hanno. Qui si parte da job_classifications e
        # non si ordina niente, perche' l'ordine non conta: vanno viste tutte.
        SQL_PARERI = f"""
                SELECT j.id, j.title, coalesce(j.location, j.city, ''),
                       left(coalesce((SELECT v FROM unnest(ARRAY[j.raw->>'description', j.raw->>'content', j.raw->>'descriptionHtml', j.raw->>'descriptionPlain', j.raw->>'externalDescription', j.raw->>'jobDescription', j.raw->>'job_description', j.raw->>'Job_Description', j.raw->>'body', j.raw->>'content_html', j.raw->>'description_html', j.raw->>'descriptionBody', j.raw->>'text', j.raw->'_jobposting'->>'description', j.raw->>'ShortDescriptionStr']) v WHERE length(v) >= 80 LIMIT 1), ''), 12000),
                       j.seniority, j.employment_type, j.remote, j.languages_required,
                       true AS ha_famiglia, j.created_at, j.lang
                  FROM job_classifications x
                  JOIN ats_jobs j ON j.id = x.job_id
                 WHERE x.v1_family IS NULL AND j.expired_at IS NULL
                 LIMIT 2048"""
        SQL_NORMALE = f"""
                SELECT j.id, j.title, coalesce(j.location, j.city, ''),
                       left(coalesce((SELECT v FROM unnest(ARRAY[j.raw->>'description', j.raw->>'content', j.raw->>'descriptionHtml', j.raw->>'descriptionPlain', j.raw->>'externalDescription', j.raw->>'jobDescription', j.raw->>'job_description', j.raw->>'Job_Description', j.raw->>'body', j.raw->>'content_html', j.raw->>'description_html', j.raw->>'descriptionBody', j.raw->>'text', j.raw->'_jobposting'->>'description', j.raw->>'ShortDescriptionStr']) v WHERE length(v) >= 80 LIMIT 1), ''), 12000),
                       j.seniority, j.employment_type, j.remote, j.languages_required,
                       EXISTS (SELECT 1 FROM job_classifications x WHERE x.job_id = j.id) AS ha_famiglia,
                       j.created_at, j.lang
                  FROM ats_jobs j
                 WHERE j.expired_at IS NULL AND j.locale_v1_at IS NULL
                 -- IL DICHIARATO PASSA PRIMA (26/09/2026): sulle piattaforme
                 -- che dichiarano contratto/seniority/remoto nel raw, v1
                 -- aspetta il passo `dichiarati`. Se arrivasse prima lui, la
                 -- sua stima occuperebbe la colonna e il coalesce di
                 -- dichiarati la terrebbe: il valore esatto scritto dal
                 -- recruiter andrebbe perso (stesso motivo del passo 2 del
                 -- ripasso del 21/09).
                 AND (j.dichiarati_at IS NOT NULL OR NOT (j.platform_id = ANY(%s::text[])))
                 -- LA GARA COL DETTAGLIO (21/09/2026): le offerte piu' nuove
                 -- vengono prese per prime, e per meta' delle piattaforme il
                 -- testo arriva DOPO, da un passo di dettaglio a parte. Senza
                 -- questo v1 decideva famiglia, seniority, contratto e remoto
                 -- dal SOLO TITOLO, marcava l'offerta come vista e non ci
                 -- tornava piu'. Il controllo del testo si fa in Python sulle
                 -- 2048 righe prese (un EXISTS su raw qui dentro costava 78 s a
                 -- lotto, misurato: il jsonb va scompattato per ogni candidata);
                 -- chi non ha testo viene DIFFERITO di due ore, fino a 7 giorni.
                 AND (j.differito_v1_at IS NULL OR j.differito_v1_at < now() - interval '2 hours')
                 -- prima chi NON ha famiglia: la notte dell'08/09 il demone ha
                 -- speso 295k letture per scriverne 22k, perche' rileggeva
                 -- offerte gia' classificate mentre l'arretrato senza famiglia
                 -- cresceva (325k -> 339k)
                 ORDER BY EXISTS (SELECT 1 FROM job_classifications x WHERE x.job_id = j.id) ASC,
                          (NOT coalesce(j.posted_at_estimated, false)) DESC,
                          j.posted_at DESC NULLS LAST
                 LIMIT 2048"""
        # IL RIPASSO (21/09/2026). Le ~793.000 offerte classificate dal solo
        # titolo stanno in `ripasso_v1_dal_titolo` con locale_v1_at azzerato. La
        # query normale le prenderebbe, ma ordina TUTTA la coda a ogni lotto (50 s
        # con 170k righe, minuti con 800k): da questa tabella si legge senza
        # ordinare, un lotto si' e uno no, cosi' le offerte nuove non aspettano.
        SQL_RIPASSO = SQL_NORMALE.replace(
            "FROM ats_jobs j\n", "FROM ripasso_v1_dal_titolo r JOIN ats_jobs j ON j.id = r.job_id\n"
        ).split("ORDER BY")[0] + " LIMIT 2048"
        giro = 0
        while st["viste"] < tetto:
            giro += 1
            righe = []
            # tre lotti di ripasso, poi uno dalla coda normale: la query normale
            # ordina tutta la coda (2 minuti con 800k righe) e a giri alterni
            # dimezzava il ritmo (5/s misurati alle 13:40 del 21/09)
            if not pareri and giro % 4:
                try:
                    righe = c.execute(SQL_RIPASSO, (list(PIATTAFORME),)).fetchall()
                except psycopg.errors.UndefinedTable:
                    pass                                     # nessun ripasso in corso
            if len(righe) < 2048:
                visti = {r[0] for r in righe}
                # SQL_PARERI non ha parametri: le piattaforme del dichiarato
                # servono solo alla coda normale (e al ripasso, che la eredita)
                base = c.execute(SQL_PARERI).fetchall() if pareri \
                    else c.execute(SQL_NORMALE, (list(PIATTAFORME),)).fetchall()
                righe += [r for r in base if r[0] not in visti]
            if not righe:
                if continuo:
                    time.sleep(60)
                    continue
                break
            fam_rows, sen_rows, con_rows, rem_rows, lin_rows, marcati = [], [], [], [], [], []
            par_rows = []          # il parere di v1 dove la famiglia c'e' gia'
            # senza testo e giovane: si DIFFERISCE (due ore), non si classifica
            # dal titolo. Dopo 7 giorni si prende atto che il testo non arriva.
            from datetime import datetime, timedelta, timezone
            soglia_eta = datetime.now(timezone.utc) - timedelta(days=7)
            differiti = [r[0] for r in righe
                         if len(r[3] or "") < 80 and r[9] is not None and r[9] > soglia_eta]
            if differiti:
                righe = [r for r in righe if r[0] not in set(differiti)]
                if not dry:
                    c.execute("UPDATE ats_jobs SET differito_v1_at = now() WHERE id = ANY(%s::uuid[])", (differiti,))
                st["differite"] = st.get("differite", 0) + len(differiti)
            # I lotti si formano per LUNGHEZZA simile: il tokenizzatore riempie
            # fino al piu' lungo del lotto, e mescolare un annuncio da 1.600 token
            # con quindici da 200 fa pagare 1.600 token a tutti e sedici.
            # Misurato il 16/09/2026: 310k -> 620k offerte/giorno. L'ordine non
            # cambia nulla nei risultati: ogni riga e' predetta per conto suo.
            righe.sort(key=lambda r: len(r[3] or ""))
            for i in range(0, len(righe), LOTTO):
                b = righe[i:i + LOTTO]
                # anche il ripasso rispetta la fascia silenziosa: dura ore e
                # il N5 sta in camera da letto
                if (continuo or pareri) and e_notte():
                    if not notte_detta:
                        print(f"-- fascia silenziosa {NOTTE_DA}-{NOTTE_A}: riposo "
                              f"{NOTTE_RIPOSO}s per lotto", flush=True)
                        notte_detta = True
                    time.sleep(NOTTE_RIPOSO)
                elif notte_detta:
                    print("-- fascia silenziosa finita: piena velocita'", flush=True)
                    notte_detta = False
                pred = m.predici([testo(t, l, d) for _, t, l, d, *_ in b])
                for (jid, tit, _, des, sen, con, rem, lin, ha_fam, _creato, lang), p in zip(b, pred):
                    st["viste"] += 1
                    marcati.append(jid)
                    fam, cf = p["family"]
                    if not ha_fam:
                        if cf >= soglia_fam:
                            fam_rows.append((jid, fam, round(cf, 3)))
                        else:
                            st["incerte"] += 1
                    else:
                        # la famiglia c'e' gia' (GLM): il parere di v1 si SALVA
                        # lo stesso, ed e' l'accordo che rende affidabile
                        # l'etichetta di GLM nel dataset. Prima si buttava, e
                        # ricostruirlo costava tre ore di GPU (09/09/2026).
                        par_rows.append((jid, fam, round(cf, 3)))
                    # LA PROVA LESSICALE (21/09/2026): «internship»,
                    # «apprenticeship» e seniority «intern» si scrivono solo
                    # se l'annuncio nomina lo stage (titolo, o due volte nel
                    # testo). Misurato: 30k «internship» su 72k senza la
                    # parola nel titolo, fra cui «Head of Global Product
                    # Quality». Altrimenti vale la seconda scelta del modello,
                    # se supera la soglia; se no resta NULL.
                    stage_ok = None      # si calcola una volta sola, solo se serve
                    def _ammessa(testa, etichette_stage, soglia):
                        nonlocal stage_ok
                        et, cf = p[testa]
                        if et in etichette_stage:
                            if stage_ok is None:
                                stage_ok = evidenza_stage(tit, pulito(des), lang)
                            if not stage_ok:
                                st["stage_negati"] = st.get("stage_negati", 0) + 1
                                et2, cf2 = p.get(testa + "_2", (None, 0.0))
                                if et2 is not None and et2 not in etichette_stage and cf2 >= soglia:
                                    return et2
                                return None
                        return et if cf >= soglia else None
                    if sen is None:
                        e = _ammessa("seniority", ("intern",), soglia_sen)
                        if e:
                            sen_rows.append((e, jid))
                    if con is None:
                        e = _ammessa("employment_type", ("internship", "apprenticeship"), soglia_con)
                        if e:
                            con_rows.append((e, jid))
                    if rem is None and p["remote"][1] >= soglia_rem:
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
                for tentativo in range(3):
                    try:
                        c.execute(sql, params)
                        return
                    except psycopg.errors.DeadlockDetected:
                        print(f"deadlock in scrivi, tentativo {tentativo + 1}",
                              flush=True)
                        time.sleep(1)
                # Come dettagli.py: un deadlock persistente fa RUMORE (il
                # supervisore rilancia, il ripasso riprende dai marcatori
                # committati). Ingoiarlo marcava il lotto come visto anche
                # quando la scrittura non era avvenuta: perdita silenziosa.
                raise RuntimeError("classifica_v1: deadlock persistente in scrivi")

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
                if par_rows:
                    scrivi("UPDATE job_classifications c SET v1_family = v.fam, v1_conf = v.cf "
                           "FROM unnest(%s::uuid[], %s::text[], %s::real[]) AS v(id, fam, cf) "
                           "WHERE c.job_id = v.id AND c.v1_family IS DISTINCT FROM v.fam",
                           ([r[0] for r in par_rows], [r[1] for r in par_rows], [r[2] for r in par_rows]))
                scrivi("UPDATE ats_jobs SET locale_v1_at = now() WHERE id = ANY(%s::uuid[])", (marcati,))
                # La coda di ripasso si SVUOTA man mano (26/09/2026): le righe
                # lavorate restavano nella tabella per sempre, e ogni lotto la
                # rileggeva tutta per trovare le 2048 ancora da fare. Non e'
                # storico: e' una coda di lavoro. I differiti (in attesa del
                # testo) NON sono in `marcati` e restano in tabella, com'e'
                # giusto: torneranno.
                try:
                    c.execute("DELETE FROM ripasso_v1_dal_titolo WHERE job_id = ANY(%s::uuid[])", (marcati,))
                except psycopg.errors.UndefinedTable:
                    pass                                 # nessun ripasso in corso
            st["famiglie"] += len(fam_rows); st["pareri"] = st.get("pareri", 0) + len(par_rows)
            st["seniority"] += len(sen_rows)
            st["contratto"] += len(con_rows); st["remoto"] += len(rem_rows); st["lingue"] += len(lin_rows)
            dt = time.time() - t0
            print(f"{st['viste']} viste | famiglie {st['famiglie']} | seniority {st['seniority']} | "
                  f"contratto {st['contratto']} | remoto {st['remoto']} | lingue {st['lingue']} | "
                  f"incerte {st['incerte']} | stage negati {st.get('stage_negati', 0)} | "
                  f"{st['viste'] / max(dt, 1):.1f}/s", flush=True)
            if dry:
                break
            # poco lavoro = si cede la scheda al 2B e si torna fra un po'
            if continuo and len(righe) < SOGLIA_CEDI and RIPOSO_V1 > 0:
                time.sleep(RIPOSO_V1)
            if continuo and st["viste"] % 20480 < 2048:
                st["ore"] = round((time.time() - t0) / 3600, 2)
                print(f"BATTITO {st}", flush=True)
    print(f"FINE {st} in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
