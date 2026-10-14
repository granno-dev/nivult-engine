#!/usr/bin/env bash
# pubblica-vetrina.sh — committa e pusha le pagine SEO rigenerate, cosi'
# nivult.com (Cloudflare Pages, collegato a origin/main) si aggiorna da solo.
#
# 14/10/2026: il passaggio era MANUALE, e si e' visto cosa vuol dire — le
# pagine rigenerate il 13 sera sono rimaste sul server fino al mattino dopo
# mentre nivult.com mostrava dati vecchi di tre giorni. Il generatore
# scrive in vetrina/, questo script porta il contenuto su GitHub.
#
# Serve la chiave deploy del server registrata sul repo (Settings → Deploy
# keys, write). Senza, lo script fallisce e basta — il cron continua.
set -uo pipefail
BASE=/opt/nivult/engine
LOG=/var/log/nivult-pagine-seo.log
export GIT_SSH_COMMAND="ssh -i /root/.ssh/id_ed25519_github -o BatchMode=yes"
cd "$BASE"

# Niente da pubblicare, niente commit: il giro esce zitto.
if git diff --quiet vetrina/ats vetrina/sitemap-ats.xml vetrina/sitemap-jobboard.xml 2>/dev/null \
   && [ -z "$(git status --porcelain vetrina/ats vetrina/sitemap-ats.xml 2>/dev/null)" ]; then
  exit 0
fi

git add vetrina/ats vetrina/sitemap-ats.xml vetrina/sitemap-jobboard.xml 2>/dev/null || true
data=$(date +%d/%m)
git -c user.name="nivult-bot" -c user.email="bot@nivult.com" commit -q \
    -m "seo: rigenerata automaticamente col dato del $data" || exit 0
if git push -q origin main >> "$LOG" 2>&1; then
  echo "$(date -Is) vetrina pubblicata su origin/main" >> "$LOG"
else
  echo "$(date -Is) push FALLITO (chiave deploy registrata?)" >> "$LOG"
fi
