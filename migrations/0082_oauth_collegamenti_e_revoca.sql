-- 0082 — Provenienza e revoca delle identità OAuth, collegamenti autenticati.
--
-- Le righe preesistenti restano 'legacy': non possiamo ricostruire come sono
-- state collegate. In particolare, un'identità Microsoft enterprise legacy non
-- può più aprire una sessione finché non viene confermata da un flusso
-- autenticato. Le chiavi interne api_chiavi con user_id NULL non cambiano.

ALTER TABLE oauth_identities
  ADD COLUMN link_method text NOT NULL DEFAULT 'legacy',
  ADD COLUMN tenant_id text,
  ADD COLUMN revoked_at timestamptz;

ALTER TABLE oauth_identities
  ADD CONSTRAINT oauth_identities_link_method_check
    CHECK (link_method IN ('legacy', 'email', 'authenticated')),
  ADD CONSTRAINT oauth_identities_tenant_check
    CHECK (tenant_id IS NULL OR provider = 'microsoft'),
  ADD CONSTRAINT oauth_identities_revoked_check
    CHECK (revoked_at IS NULL OR revoked_at >= created_at),
  ADD CONSTRAINT oauth_identities_enterprise_authenticated_check
    CHECK (link_method <> 'authenticated'
           OR provider <> 'microsoft' OR tenant_id IS NOT NULL);

COMMENT ON COLUMN oauth_identities.link_method IS
  'legacy = provenienza precedente alla 0082; email = collegamento automatico '
  'da email fidata; authenticated = provider confermato da sessione esistente.';
COMMENT ON COLUMN oauth_identities.tenant_id IS
  'Tenant Microsoft (tid) osservato al collegamento; NULL per gli altri provider '
  'e per le identità legacy che non lo hanno ancora dichiarato.';
COMMENT ON COLUMN oauth_identities.revoked_at IS
  'Identità non più utilizzabile per accedere; riattivabile solo con un '
  'collegamento autenticato dello stesso utente.';

ALTER TABLE oauth_flows
  ADD COLUMN link_user_id uuid REFERENCES users(id) ON DELETE CASCADE;

CREATE INDEX oauth_flows_link_user_idx ON oauth_flows (link_user_id)
  WHERE link_user_id IS NOT NULL;

COMMENT ON TABLE oauth_flows IS
  'Stato effimero OAuth. link_user_id è presente soltanto quando il flusso è '
  'nato da una sessione autenticata per collegare un provider, non per accedere.';
COMMENT ON COLUMN oauth_flows.link_user_id IS
  'Utente autenticato che ha avviato il collegamento; il callback non accetta '
  'questo valore dal client e non emette token quando è presente.';

ALTER TABLE api_chiavi DROP CONSTRAINT IF EXISTS api_chiavi_user_id_fkey;
ALTER TABLE api_chiavi
  ADD CONSTRAINT api_chiavi_user_id_fkey
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
