-- La conservazione dei dati personali smette di essere una promessa scritta
-- solo nella privacy policy.
--
-- La pagina dice due cose che nessun codice faceva rispettare: il CV e
-- l'account spariscono entro 30 giorni dalla fine dell'abbonamento, e i dati
-- non si tengono oltre lo scopo. Serve un posto dove segnare che l'avviso di
-- inattivita' e' partito, altrimenti il lavoro periodico lo rimanda ogni
-- notte e non cancella mai.
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS inactivity_warned_at timestamptz;

COMMENT ON COLUMN users.inactivity_warned_at IS
  'Quando e'' partito l''avviso di inattivita''. NULL = mai avvisato. '
  'Si azzera da sola appena l''utente torna: e'' il lavoro periodico a '
  'rimetterla a NULL, cosi'' un rientro annulla il conto alla rovescia.';

-- L'ultimo segno di vita, in una vista sola: e' la definizione di
-- «inattivo» del prodotto, e va scritta UNA volta. Tre sorgenti, perche'
-- un utente puo' essere vivo senza aprire il pannello (riceve i digest) e
-- puo' essere appena iscritto senza aver fatto nulla.
CREATE OR REPLACE VIEW user_activity_v AS
SELECT u.id AS user_id,
       u.email,
       u.status,
       u.subscription_status,
       u.current_period_end,
       u.inactivity_warned_at,
       GREATEST(
         u.created_at,
         COALESCE(u.last_digest_at, u.created_at),
         COALESCE((SELECT max(s.last_seen_at) FROM sessions s
                    WHERE s.user_id = u.id), u.created_at)
       ) AS last_activity_at
  FROM users u;
