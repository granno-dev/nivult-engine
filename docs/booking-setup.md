# Booking: registrazione dell'app Entra (15 minuti, una volta sola)

La pagina nivult.com/booking crea eventi Teams sul calendario business di
Giuseppe via Microsoft Graph, in contesto applicazione (nessun login utente).
Serve un'app registrata sul tenant M365. Lo fa Giuseppe dal portale, una
volta sola.

## Passi

1. Apri <https://entra.microsoft.com> con l'account admin del tenant.
2. **Identity → Applications → App registrations → New registration**
   - Name: `Nivult Booking`
   - Supported account types: *Accounts in this organizational directory only*
   - Redirect URI: lascia vuoto (non serve, è un demone)
3. Nella app appena creata, pagina **Overview**: copia
   - **Application (client) ID** → `MS_BOOKING_CLIENT_ID`
   - **Directory (tenant) ID** → `MS_BOOKING_TENANT_ID`
4. **API permissions → Add a permission → Microsoft Graph → Application
   permissions** (NON "Delegated"): cerca e aggiungi
   - `Calendars.ReadWrite`
   Poi **Grant admin consent for <tenant>** e verifica che la colonna
   Status mostri la spunta verde "Granted".
5. **Certificates & secrets → New client secret**
   - Description: `nivult-api`, scadenza 24 mesi
   - Copia il **Value** (non lo "Secret ID"): si vede solo ora
   → `MS_BOOKING_CLIENT_SECRET`
6. `MS_BOOKING_ORGANIZER`: l'email/UPN dell'account business di Giuseppe
   (quello il cui calendario ospita le call). Deve avere la licenza Teams
   attiva (ce l'ha con M365 Business).

## Dove vanno i valori

- `/opt/nivult/engine/.env` sul server (li legge l'API e il cron dei
  reminder via `load_dotenv()`), senza virgolette, un per riga.
- `.env` locale per le prove in sviluppo.

## Verifica rapida (dopo il deploy)

```bash
ssh root@37.27.36.85
cd /opt/nivult/engine && .venv/bin/python - <<'EOF'
from nivult.config import load_dotenv
load_dotenv()
from nivult import msgraph
from datetime import datetime, timedelta, timezone
i = msgraph.impegni(datetime.now(timezone.utc),
                    datetime.now(timezone.utc) + timedelta(days=1))
print("Graph ok, impegni nelle prossime 24h:", len(i))
EOF
```

## Scadenza del secret

Il client secret scade (24 mesi consigliati). Alla scadenza le prenotazioni
si rompono con errore 401 nei log di nivult-api: se ne crea uno nuovo e si
aggiorna l'.env. Segnarlo in calendario a ottobre 2028.

## Nota di sicurezza

Il permesso è application-level: l'app può leggere e scrivere TUTTI i
calendari del tenant, non solo quello di Giuseppe. Il client secret resta
solo sul server, nei .env con permesso 600, mai nel repo. Se si sospetta
una fuga: Entra → Certificates & secrets → revoca del secret.
