#!/usr/bin/env bash
# pubblica-vetrina.sh — committa e pusha le pagine SEO rigenerate, cosi'
# nivult.com (Cloudflare Pages, collegato a origin/main) si aggiorna da solo.
#
# Il clone operativo NON e' pulito e NON va rebase-ato: file e directory di
# produzione differiscono deliberatamente dalla storia. La pubblicazione usa
# quindi un worktree temporaneo, staccato da origin/main aggiornato, e ci
# copia solo vetrina/ats e le due sitemap. Niente push forzato, niente
# modifiche al clone che esegue i demoni.
set -uo pipefail
BASE=/opt/nivult/engine
LOG=/var/log/nivult-pagine-seo.log
export GIT_SSH_COMMAND="ssh -i /root/.ssh/id_ed25519_github -o BatchMode=yes"

TMP=$(mktemp -d /tmp/nivult-seo-pubblica.XXXXXX)
cleanup() {
  git -C "$BASE" worktree remove --force "$TMP" >> "$LOG" 2>&1 || true
}
trap cleanup EXIT

cd "$BASE"
if ! git fetch -q origin main >> "$LOG" 2>&1; then
  echo "$(date -Is) fetch FALLITO: pubblicazione rinviata" >> "$LOG"
  exit 1
fi
if ! git worktree add --detach "$TMP" origin/main >> "$LOG" 2>&1; then
  echo "$(date -Is) worktree temporaneo FALLITO" >> "$LOG"
  exit 1
fi

# --delete agisce SOLO nel worktree temporaneo: riproduce anche le pagine
# eliminate dal generatore, senza toccare il clone operativo.
rsync -a --delete "$BASE/vetrina/ats/" "$TMP/vetrina/ats/"
for f in sitemap-ats.xml sitemap-jobboard.xml sitemap-sito.xml sitemap.xml; do
  cp "$BASE/vetrina/$f" "$TMP/vetrina/$f"
done

git -C "$TMP" add vetrina/ats vetrina/sitemap-ats.xml vetrina/sitemap-jobboard.xml \
    vetrina/sitemap-sito.xml vetrina/sitemap.xml
if git -C "$TMP" diff --cached --quiet; then
  exit 0
fi

data=$(date +%d/%m)
git -C "$TMP" -c user.name="nivult-bot" -c user.email="bot@nivult.com" commit -q \
    -m "seo: rigenerata automaticamente col dato del $data" || exit 0
if git -C "$TMP" push -q origin HEAD:main >> "$LOG" 2>&1; then
  echo "$(date -Is) vetrina pubblicata su origin/main" >> "$LOG"
else
  echo "$(date -Is) push FALLITO (chiave deploy registrata?)" >> "$LOG"
  exit 1
fi
