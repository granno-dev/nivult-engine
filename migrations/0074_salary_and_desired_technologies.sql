-- 0074: salario confrontabile e tecnologie desiderate (solo peso nello score).
ALTER TABLE jobs ADD COLUMN salary_min numeric;
ALTER TABLE jobs ADD COLUMN salary_max numeric;
ALTER TABLE jobs ADD COLUMN salary_currency text;
ALTER TABLE jobs ADD COLUMN salary_period text;
ALTER TABLE jobs ADD COLUMN salary_da text;

ALTER TABLE user_clusters ADD COLUMN min_salary_annual numeric;
ALTER TABLE user_clusters ADD COLUMN salary_currency text;
ALTER TABLE user_clusters ADD COLUMN desired_technologies text[] NOT NULL DEFAULT '{}';

ALTER TABLE user_clusters ADD CONSTRAINT user_clusters_salary_pair_ck CHECK (
  (min_salary_annual IS NULL AND salary_currency IS NULL)
  OR (min_salary_annual > 0 AND salary_currency ~ '^[A-Z]{3}$')
);

COMMENT ON COLUMN jobs.salary_da IS
  'Provenienza del compenso: dichiarato o testo. I filtri usano solo dati confrontabili.';
COMMENT ON COLUMN user_clusters.min_salary_annual IS
  'Minimo annuo desiderato; vincolante solo per offerte con compenso dichiarato e confrontabile.';
COMMENT ON COLUMN user_clusters.desired_technologies IS
  'Etichette tecnologiche pesate nel punteggio, mai usate come filtro deterministico.';
