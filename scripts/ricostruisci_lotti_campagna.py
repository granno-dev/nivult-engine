"""Ricostruisce i lotti mancanti della campagna v4 dal corpus vivo.

I sorgenti originali vivevano in /tmp sul Mac e la pulizia di macOS li
ha mangiati (01/10/2026). La verita' e' Postgres: si campionano offerte
attive MAI etichettate da kimi-agenti, con descrizione. PRIMA VERSIONE:
ORDER BY random() su tutto il corpus — la sort e' uscita su disco e ha
riempito il root da 75 GB (DiskFull, Postgres per un soffio). SECONDA:
la lettura di raw->>'description' in scansione piena era il collo vero
(26 GB di jsonb). Ora: colonne leggere in scansione, i testi solo per
gli id scelti, a lookup per chiave primaria. I lotti nuovi partono dal
701. Forma invariata: n, id, famiglia, titolo, testo (max 4000).
"""
import json
import os

import psycopg

OUT = "/opt/nivult/campagna_lotti"
PER_LOTTO = 50
OBIETTIVO_LOTTI = 700  # la campagna era nata da 700 lotti da 50

dsn = os.environ["ATS_DATABASE_URL"]
os.makedirs(OUT, exist_ok=True)
with psycopg.connect(dsn) as conn, conn.cursor() as cur:
    cur.execute("SELECT count(*) FROM etichette_tec WHERE modello = 'kimi-agenti'")
    fatte = cur.fetchone()[0]
    da_fare = OBIETTIVO_LOTTI * PER_LOTTO - fatte
    print(f"etichettate: {fatte} · da campionare: {da_fare}", flush=True)
    if da_fare <= 0:
        raise SystemExit("campagna gia' coperta")

    # 1) colonne leggere soltanto: niente jsonb in scansione
    soglia = (da_fare * 3) / 1_500_000
    cur.execute(
        """SELECT j.id::text, x.v1_family, j.title
             FROM ats_jobs j
             LEFT JOIN job_classifications x ON x.job_id = j.id
            WHERE j.expired_at IS NULL AND random() < %s""", (soglia,))
    pool = cur.fetchall()
    print(f"pool: {len(pool)}", flush=True)

    # 2) via i gia' etichettati: temp table + anti-join hash
    cur.execute("CREATE TEMP TABLE _pool_ids (id uuid PRIMARY KEY)")
    cur.executemany("INSERT INTO _pool_ids VALUES (%s::uuid)",
                    [(r[0],) for r in pool])
    cur.execute(
        """DELETE FROM _pool_ids p
            USING etichette_tec e
            WHERE e.job_id = p.id AND e.modello = 'kimi-agenti'""")
    cur.execute("SELECT id::text FROM _pool_ids")
    liberi = {r[0] for r in cur.fetchall()}
    cur.execute("DROP TABLE _pool_ids")
    scelte = [r for r in pool if r[0] in liberi][:da_fare]
    print(f"scelte: {len(scelte)}", flush=True)

    # 3) i testi solo degli scelti, a lookup per PK, a pezzi da 500
    testi = {}
    for i in range(0, len(scelte), 500):
        pezzo = scelte[i:i + 500]
        cur.execute(
            """SELECT id::text, left(raw->>'description', 4000)
                 FROM ats_jobs WHERE id = ANY(%s::uuid[])""",
            ([r[0] for r in pezzo],))
        for rid, t in cur.fetchall():
            testi[rid] = t or ""

# il filtro di qualita' PRIMA di tagliare i lotti: dentro il loop ogni
# pezzo scendeva sotto i 50 e veniva saltato intero (0 lotti scritti)
buone = [r for r in scelte if len(testi.get(r[0], "")) > 200]
scritti = 0
for i in range(0, len(buone) - PER_LOTTO + 1, PER_LOTTO):
    pezzo = buone[i:i + PER_LOTTO]
    n_lotto = 701 + scritti
    lotto = {"offerte": [
        {"n": k + 1, "id": r[0], "famiglia": r[1] or "",
         "titolo": r[2] or "", "testo": testi.get(r[0], "")}
        for k, r in enumerate(pezzo)]}
    with open(f"{OUT}/lotto-{n_lotto}.json", "w", encoding="utf-8") as f:
        json.dump(lotto, f, ensure_ascii=False)
    scritti += 1
print(f"lotti scritti: {scritti} (701..{700 + scritti}) in {OUT}", flush=True)
