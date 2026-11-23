-- 0085 — Ogni ricerca ha una propria identita', anche dentro lo stesso cluster.
--
-- La famiglia x paese resta la chiave di ingestione. Il ruolo distingue invece
-- le sottoscrizioni: HR Manager e HRBP non si sovrascrivono piu' e l'app non
-- riceve due elementi con la stessa chiave React.
BEGIN;

ALTER TABLE user_clusters ADD COLUMN IF NOT EXISTS id uuid;
UPDATE user_clusters SET id = gen_random_uuid() WHERE id IS NULL;
ALTER TABLE user_clusters ALTER COLUMN id SET NOT NULL;
ALTER TABLE user_clusters ALTER COLUMN id SET DEFAULT gen_random_uuid();

UPDATE user_clusters
SET target_role = NULLIF(btrim(target_role), '')
WHERE target_role IS DISTINCT FROM NULLIF(btrim(target_role), '');

ALTER TABLE user_clusters DROP CONSTRAINT IF EXISTS user_clusters_pkey;
ALTER TABLE user_clusters
  ADD COLUMN IF NOT EXISTS target_role_key text
  GENERATED ALWAYS AS (lower(btrim(coalesce(target_role, '')))) STORED;
ALTER TABLE user_clusters ADD CONSTRAINT user_clusters_pkey PRIMARY KEY (id);
ALTER TABLE user_clusters ADD CONSTRAINT user_clusters_ricerca_unica
  UNIQUE (user_id, cluster_id, target_role_key);

COMMIT;
