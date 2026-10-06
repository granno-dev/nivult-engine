#!/usr/bin/env bash
# Backup cifrato dei pesi addestrati (06/10/2026).
#
# I dati per riaddestrare stanno nel database, ma riaddestrare costa
# GIORNI di GPU: i pesi sono un asset. Stessa cifratura del backup del
# database (openssl CMS, solo chiave pubblica in casa): chi trova
# l'archivio non lo legge, e il ripristino chiede la chiave privata.
#
# Gira sull'N5 (i modelli vivono li'), scrive sull'array del N5 e basta:
# la copia sul server la porta il relay a mano (il N5 non ha scrittura
# verso il server per costruzione del backup DB).
#
# Si backuppa la produzione CORRENTE, non la storia: le varianti
# vecchie (*.vecchio, *.precedente, *-ck*, v1-anteprima) restano fuori.
set -euo pipefail

BASE=/mnt/cache/appdata/nivult-operaio
MODELLI=(modelli/nivult-v1 modelli/tec-v1 modelli/match-v0 mt5 tecnologie)
DEST=/mnt/user/backup-nivult
CERT="${1:-$BASE/backup-recipient.pem}"
OGGI=$(date +%F)
OUT="$DEST/modelli-nivult-$OGGI.tar.gz.enc"

[ -f "$CERT" ] || { echo "manca il certificato: $CERT" >&2; exit 1; }
mkdir -p "$DEST"

cd "$BASE"
tar czf - "${MODELLI[@]}" | openssl cms -encrypt -binary -stream \
  -aes-256-cbc -outform DER -out "$OUT" "$CERT"
echo "scritto: $OUT ($(du -h "$OUT" | cut -f1))"
# si tengono le ultime due generazioni, non di piu'
ls -t "$DEST"/modelli-nivult-*.enc 2>/dev/null | tail -n +3 | xargs -r rm -f
