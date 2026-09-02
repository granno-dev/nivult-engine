#!/usr/bin/env bash
#
# Giro notturno del sistema ATS autonomo: cresce da solo ogni notte.
#
# 02:30, dopo il giro del motore (01:00) e prima del backup (03:00).
# Lavora su nivult_ats, database SEPARATO dal motore: nessuna
# interferenza con ingestione e digest.
#
# Cosa fa, in ordine:
#   1. SCRAPE — aggiorna le offerte di tutte le aziende registrate
#   2. DETECTOR — analizza i domini nuovi in attesa (batch 3000)
#   3. RENDER DETECTOR — riprova con Playwright le grandi aziende
#      rimaste senza ATS (batch 60: è lento, un browser per volta)
#   4. COMMON CRAWL — 3 piattaforme a rotazione per notte sulle 37:
#      si ripuliscono tutte in ~12 notti senza maltrattare l'indice
#   5. WIKIDATA (solo domenica) — ricarica il censimento con le
#      classi estese: il bacino delle aziende cresce da solo
#
# flock: se il giro precedente dura più di 24 ore, questo non parte.

set -uo pipefail

BASE=/opt/nivult/engine
PY="$BASE/.venv/bin/python"
LOCK=/var/run/nivult-ats-nightly.lock
STATE=/opt/nivult/ats-nightly-state
LOG_DIR=/opt/nivult/engine/logs
mkdir -p "$STATE" "$LOG_DIR"

# le credenziali: solo POSTGRES_PASSWORD, il resto dell'env del motore
# ha righe con spazi (SMTP_FROM='Nivult <digest@…>') che rompono source
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export POSTGRES_PASSWORD
# France Travail: le chiavi ERANO nel .env ma nessuno le leggeva, e con
# `set -u` il solo nominare la variabile piu' sotto uccideva il giro a
# meta' — il 2026-08-31 classificatore e wikidata non sono mai partiti
# per questo. Il :- e' la cintura oltre alle bretelle.
FRANCE_TRAVAIL_CLIENT_ID=$(grep -E '^FRANCE_TRAVAIL_CLIENT_ID=' /opt/nivult/.env | head -1 | cut -d= -f2-)
FRANCE_TRAVAIL_CLIENT_SECRET=$(grep -E '^FRANCE_TRAVAIL_CLIENT_SECRET=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export FRANCE_TRAVAIL_CLIENT_ID FRANCE_TRAVAIL_CLIENT_SECRET

# Il DSN del database ATS: dentro Docker, utente del motore
export ATS_DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult_ats"
export DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult"

exec 9>"$LOCK"
flock -n 9 || { echo "ATS nightly: giro precedente ancora in corso, esco"; exit 0; }

cd "$BASE"
echo "=== ATS nightly $(date -Is) ==="

# ── 1. Scrape: aggiorna tutte le aziende registrate ─────────────────
echo "── scrape"
"$PY" -m nivult.ats.runner >> "$LOG_DIR/ats-nightly.log" 2>&1 \
  && echo "   ok" || echo "   FALLITO (vedi log)"

# ── 2. Detector statico sui domini in attesa ────────────────────────
echo "── detector (batch 3000)"
"$PY" -m nivult.ats.detector --rileva --limite 3000 --thread 20 \
  >> "$LOG_DIR/ats-nightly.log" 2>&1 \
  && echo "   ok" || echo "   FALLITO"

# ── 3. Render detector sulle grandi senza ATS ──────────────────────
echo "── render detector (60 grandi)"
"$PY" -m nivult.ats.detector --render --limite 60 --dip-minimi 3000 \
  --thread 2 >> "$LOG_DIR/ats-nightly.log" 2>&1 \
  && echo "   ok" || echo "   FALLITO"

# ── 4. Common Crawl: 3 piattaforme a rotazione ─────────────────────
echo "── common crawl (rotazione)"
"$PY" - "$STATE" <<'PYEOF' >> "$LOG_DIR/ats-nightly.log" 2>&1
import os, sys
sys.path.insert(0, "/opt/nivult/engine")
import psycopg
from nivult.ats.registry import REGISTRY
from nivult.ats.discovery import cc_scopri_piattaforma, ATS_DSN

# la rotazione sta in un file di stato: 3 piattaforme per notte
state_file = os.path.join(sys.argv[1], "cc-rotazione.txt")
tutte = sorted(p["id"] for p in REGISTRY if p.get("cc_search"))
try:
    pos = int(open(state_file).read().strip())
except (OSError, ValueError):
    pos = 0
selezioni = [tutte[(pos + i) % len(tutte)] for i in range(3)]
open(state_file, "w").write(str((pos + 3) % len(tutte)))
print(f"CC rotazione: {selezioni} (posizione {pos}/{len(tutte)})")
for pid in selezioni:
    piattaforma = next(p for p in REGISTRY if p["id"] == pid)
    slugs = cc_scopri_piattaforma(piattaforma, limite=800)
    if slugs:
        with psycopg.connect(ATS_DSN) as conn:
            with conn.cursor() as cur:
                for s in slugs:
                    cur.execute(
                        "INSERT INTO ats_companies (platform_id, slug, "
                        "discovered_from) VALUES (%s, %s, 'common_crawl') "
                        "ON CONFLICT (platform_id, slug) DO NOTHING",
                        (pid, s))
            conn.commit()
    print(f"  {pid}: {len(slugs)} nuove aziende")
PYEOF
echo "   ok"

# ── 5. Mantenimento: expira, normalizza i nuovi, dedup ────────────
echo "── mantenimento (expira/normalizza/dedup)"
"$PY" -m nivult.ats.mantenimento --expira >> "$LOG_DIR/ats-nightly.log" 2>&1 \
  && echo "   ok" || echo "   FALLITO"
"$PY" -m nivult.ats.mantenimento --normalizza --limite 20000 \
  >> "$LOG_DIR/ats-nightly.log" 2>&1 \
  && echo "   ok" || echo "   FALLITO"
"$PY" -m nivult.ats.mantenimento --dedup >> "$LOG_DIR/ats-nightly.log" 2>&1 \
  && echo "   ok" || echo "   FALLITO"

# ── 5b. Arricchimento: paese/data dalle pagine di dettaglio ──────
echo "── arricchisci (phenom 1000)"
"$PY" -m nivult.ats.arricchisci --phenom --limite 1000 --thread 8 \
  >> "$LOG_DIR/ats-nightly.log" 2>&1 \
  && echo "   ok" || echo "   FALLITO"
"$PY" -m nivult.ats.arricchisci --da-localita \
  >> "$LOG_DIR/ats-nightly.log" 2>&1 \
  && echo "   ok" || echo "   FALLITO"
"$PY" -m nivult.ats.arricchisci --da-azienda \
  >> "$LOG_DIR/ats-nightly.log" 2>&1 \
  && echo "   ok" || echo "   FALLITO"

# ── 5c. Servizi pubblici europei: API dirette ─────────────────────
echo "── arbetsformedlingen (Svezia)"
"$PY" -m nivult.ats.servizi_pubblici --arbetsformedlingen --limite 2000 \
  >> "$LOG_DIR/ats-nightly.log" 2>&1 \
  && echo "   ok" || echo "   FALLITO"
if [ -n "${FRANCE_TRAVAIL_CLIENT_ID:-}" ]; then
  echo "── francetravail (Francia)"
  "$PY" -m nivult.ats.servizi_pubblici --francetravail --limite 2000 \
    >> "$LOG_DIR/ats-nightly.log" 2>&1 \
    && echo "   ok" || echo "   FALLITO"
fi

# ── Bundesagentur (Germania) — API pubblica, 100k+ offerte ──────
echo "── eures (portale UE — copre l'Italia)"
"$PY" -m nivult.ats.servizi_pubblici --eures --paesi IT --limite 4000 \
  >> "$LOG_DIR/ats-nightly.log" 2>&1 \
  && echo "   ok" || echo "   FALLITO"

echo "── bundesanstellung (Germania)"
"$PY" -m nivult.ats.servizi_pubblici --bundesanstellung --limite 2000 \
  >> "$LOG_DIR/ats-nightly.log" 2>&1 \
  && echo "   ok" || echo "   FALLITO"

# ── 6. Classificazione a 3 livelli: dizionario + fuzzy + GLM ────
echo "── classificatore a livelli (5000)"
"$PY" -m nivult.ats.classificatore_livelli --limite 5000 \
  >> "$LOG_DIR/ats-nightly.log" 2>&1 \
  && echo "   ok" || echo "   FALLITO"

# ── 7. Wikidata esteso: ogni notte, il bacino cresce ──────────────
# La query riscarica tutto (non incrementale) e le novità reali sono
# decine a settimana, ma costa 15 minuti e tiene il censimento sempre
# fresco: preferiamo la ridondanza all'attesa.
echo "── wikidata esteso"
"$PY" -m nivult.ats.detector --wikidata-estesa \
  >> "$LOG_DIR/ats-nightly.log" 2>&1 \
  && echo "   ok" || echo "   FALLITO"

# ── 7. Il ponte, di nuovo: le offerte scrappate e classificate STANOTTE
# entrano nel motore adesso, non domattina alle 05:00. Prima di questa
# corsa ogni novita' interna arrivava al digest con un giorno di ritardo
# fisso: il giro notturno finisce dopo le 05:00 del ponte a cron, e
# l'offerta fresca restava a guardare. Il flock dentro ponte-ats.sh
# protegge dalla sovrapposizione con la corsa a cron.
echo "── ponte verso il motore"
/opt/nivult/engine/deploy/ponte-ats.sh >> "$LOG_DIR/ats-nightly.log" 2>&1 \
  && echo "   ok" || echo "   FALLITO"

echo "=== ATS nightly completato $(date -Is) ==="
