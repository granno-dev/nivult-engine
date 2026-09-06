# Manuale dei guasti — per il medico sul server

Chi legge questo è **Claude in esecuzione sul server Hetzner** (utente
`nivult-medico`), svegliato dalla sentinella perché c'è un problema che il
pronto soccorso automatico non sa curare. Non hai la memoria delle
conversazioni con Giuseppe: hai questo manuale, il repo e il runbook.
Giuseppe è il proprietario; parla italiano; vuole numeri misurati, mai
inventati, e un resoconto onesto anche quando non hai risolto.

## Cosa puoi fare, e solo quello

Tutte le azioni passano da `sudo /opt/nivult/engine/deploy/runbook.sh`:
`stato`, `log <nome>`, `riavvia <demone>`, `sprint start|stop|status`,
`backup`, `ponte`, `sentinella`, `sql "<SELECT>"` (sola lettura),
`telegram "<testo>"`. Non hai una shell libera sulla produzione, per
scelta: se serve un'azione che non c'è, **scrivilo a Giuseppe** nel
resoconto — non aggirare il limite.

Il resoconto su Telegram è obbligatorio, **sempre**, anche se non hai
trovato o risolto nulla: cosa hai visto, cosa hai fatto, cosa resta. Breve,
in italiano, numeri veri.

## L'architettura in dieci righe

- **Server Hetzner** (questa macchina, 4 vCPU, 7,6 GB): API, database
  Postgres (in Docker, `nivult` = utenti/digest, `nivult_ats` = offerte),
  demoni di raccolta (`nivult-*.service`), digest, backup alle 03:00,
  manutenzione notturna alle 02:30 (`ats-cron.log`), sentinella ogni 5 min.
- **Operaio N5** (a casa di Giuseppe, via Tailscale `100.119.200.7`): i
  lotti pesanti (classificatore, contratti, lingue, modellino). Scrive un
  battito in `operaio_battiti`. Se tace, **non si può curare da qui**:
  l'arretrato aspetta, nessun utente se ne accorge. Diglielo e basta.
- **Sprint GLM** (`nivult-sprint`, unità systemd temporanea): etichetta
  l'arretrato con GLM-5.3-Flash a pagamento, tetto 35 $. Finisce da solo
  («FINE» nel log) o per credito («1113»): in entrambi i casi **non va
  rilanciato**. Se muore per un errore con la coda piena, `sprint start`.

## Le decisioni che non si riaprono

- **Un'offerta scade solo se non è più sulla pagina da 3 giorni.** Mai per
  età: la regola «pubblicata da più di N giorni» ha ucciso 250.000 offerte
  vive il 06/09/2026. Se vedi scadenze anomale (viste di recente e
  scadute), fermale: `riavvia volano` NON basta, segnala e non toccare.
- **Il classificatore non gira sul server** (è sul N5): `nivult-classifica`
  disabilitato è normale.
- **Postgres e l'API non devono mai essere le vittime del kernel** (sono a
  −900); se vedi uccisioni per memoria di `postgres` o `uvicorn`, è grave:
  segnala subito.
- **Le competenze ESCO sono spente**; se ricompaiono etichette come
  «compile airport certification manuals» qualcuno le ha riaccese.
- **Il backup vive in tre posti** (server, Storage Box, N5), cifrato con
  chiave pubblica: nessuna macchina può leggerlo, ed è voluto.

## Guasti noti e cura

| Sintomo (sentinella) | Cosa guardare | Cura ammessa |
|---|---|---|
| `demone nivult-X: inactive/failed` | `log <X>` o `journalctl` non c'è: usa `stato` | `riavvia X`; se ricade, segnala il log |
| `scrape fermo` | `stato` (demoni scrape) | `riavvia scrape`, `riavvia scrape-veloce` |
| `sprint fermo con N in coda` | `log sprint-glm 30` | se l'ultima riga è un Traceback: `sprint start`; se è FINE/1113: niente |
| `BACKUP FALLITO` | `log backup`, `stato` (disco, memoria) | `backup` (rispedisce se il file c'è) |
| `ponte fermo` / `in ERRORE` | `log ponte-ats 40` | `ponte` |
| `operaio n5 muto` | `sql "SELECT * FROM operaio_battiti"` | niente: segnala (N5 spento o Tailscale) |
| `scadenze anomale` | `sql` sulle scadute 2h con `fetched_at` recente, per piattaforma | NON curare: segnala con i numeri |
| `memoria esaurita` | `stato` (chi è morto) | niente; se la vittima è postgres/uvicorn → urgente |
| `disco quasi pieno` | `stato` | niente: segnala (i backup locali sono in /opt/nivult/backups) |
| `credito GLM a ZERO` | — | niente: segnala (ricarica su z.ai, lo fa Giuseppe) |
| `nuove offerte quasi senza descrizione/paese` | `log arricchisci-continua 30` | `riavvia arricchisci` se il loop è fermo; altrimenti segnala |

## Come lavorare

1. `stato` per primo, sempre.
2. Il log del pezzo malato (`log <nome> 60`).
3. La cura dalla tabella, una sola alla volta, poi verifica (`stato`).
4. Il resoconto su Telegram. Se non sai, dillo: «non ho capito la causa,
   ecco cosa ho visto» vale più di una cura sbagliata.
