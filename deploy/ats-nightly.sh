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

# le credenziali (POSTGRES_PASSWORD) stanno nell'env del motore
set -a
. /opt/nivult/.env
set +a

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

# ── 5. Wikidata esteso: solo domenica, il bacino cresce ────────────
if [ "$(date +%u)" = "7" ]; then
  echo "── wikidata esteso (domenica)"
  "$PY" -m nivult.ats.detector --wikidata-estesa \
    >> "$LOG_DIR/ats-nightly.log" 2>&1 \
    && echo "   ok" || echo "   FALLITO"
fi

echo "=== ATS nightly completato $(date -Is) ==="
