ALTER TABLE users
  ADD COLUMN creem_customer_id text,
  ADD COLUMN creem_subscription_id text,
  ADD COLUMN subscription_plan text,
  ADD COLUMN subscription_interval text,
  ADD COLUMN creem_period_start timestamptz,
  ADD COLUMN creem_last_transaction_id text;

ALTER TABLE users DROP CONSTRAINT IF EXISTS users_subscription_status_check;
ALTER TABLE users ADD CONSTRAINT users_subscription_status_check
  CHECK (subscription_status IN (
    'trialing', 'active', 'scheduled_cancel', 'past_due', 'canceled',
    'expired', 'paused', 'unpaid'
  ));

ALTER TABLE users ADD CONSTRAINT users_subscription_plan_check
  CHECK (subscription_plan IS NULL OR subscription_plan IN
         ('starter', 'growth', 'scale', 'business'));
ALTER TABLE users ADD CONSTRAINT users_subscription_interval_check
  CHECK (subscription_interval IS NULL OR subscription_interval IN ('month', 'year'));

CREATE UNIQUE INDEX users_creem_customer_idx
  ON users (creem_customer_id) WHERE creem_customer_id IS NOT NULL;
CREATE UNIQUE INDEX users_creem_subscription_idx
  ON users (creem_subscription_id) WHERE creem_subscription_id IS NOT NULL;

CREATE TABLE creem_subscription_eventi (
  subscription_id text NOT NULL,
  state_key text NOT NULL,
  event_id text NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (subscription_id, state_key)
);

GRANT SELECT, INSERT ON creem_subscription_eventi TO nivult_app;
