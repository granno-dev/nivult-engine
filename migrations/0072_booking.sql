-- 0072: prenotazioni delle call dalla pagina pubblica nivult.com/booking.
--
-- Nata il 10/10/2026: la vetrina B2B manda traffico verso «Get a trial
-- key», ma chi compra dati vuole parlare con una persona. La pagina
-- booking raccoglie nome/email/(telefono) e uno slot da 30 minuti;
-- l'evento Teams sul calendario di Giuseppe si crea SOLO dopo il click
-- di conferma nella mail — il calendario non si riempie di spam.
--
-- Il token e' il segreto di conferma/cancellazione: chi ha il link agisce,
-- chi non ce l'ha no. Va via email, mai in log o analytics.
--
-- Gli stati: pending (in attesa del click), confirmed (evento Teams
-- creato), cancelled (evento rimosso, slot di nuovo libero). Non c'e'
-- delete: la riga resta come storico, anche per il limite anti-abuso
-- per email/IP.

CREATE TABLE booking_calls (
  id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  token           text        NOT NULL UNIQUE
                              CHECK (token ~ '^[A-Za-z0-9_-]{32,64}$'),
  nome            text        NOT NULL,
  email           text        NOT NULL,
  telefono        text,           -- E.164 senza +, solo se il lead lo lascia
  azienda         text,
  note            text,
  starts_at       timestamptz NOT NULL,   -- inizio call; fine = +30 minuti
  status          text        NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending','confirmed','cancelled')),
  ms_event_id     text,           -- id dell'evento Graph, per cancellarlo
  join_url        text,           -- link Teams, rimandato nei reminder
  reminder_24h_at timestamptz,    -- quando il reminder e' partito, non "se"
  reminder_1h_at  timestamptz,
  ip              inet,
  created_at      timestamptz NOT NULL DEFAULT now()
);

-- Slot occupati: il calcolo delle disponibilita' esclude pending e confirmed.
CREATE INDEX booking_calls_slot_idx ON booking_calls (starts_at)
  WHERE status <> 'cancelled';

-- Anti-abuso: quante pending ha gia' questa email.
CREATE INDEX booking_calls_email_idx ON booking_calls (email);
