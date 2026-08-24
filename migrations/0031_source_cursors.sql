-- 0031 — Il cursore di ingestione sta sulla coppia cluster-fonte, non sul cluster.
--
-- Bug del primo giro reale: clusters.last_seen_posted_at era un cursore solo,
-- condiviso da tutte le fonti di un cluster. Fantastic — che vede le offerte
-- più recenti — lo trascinava avanti, e le fonti nazionali venivano interrogate
-- con published-after a una data che loro non avevano mai raggiunto: le offerte
-- in mezzo non sono mai state chieste, e il run risultava success. In
-- produzione Arbetsförmedlingen era ferma al 10 agosto mentre il cursore del
-- cluster era già al 21: undici giorni di offerte svedesi perse in silenzio.
--
-- Ogni coppia (cluster, fonte) ha il suo high-water mark. La finestra di
-- backfill resta il ripiego per una coppia che cursore non ha ancora.

CREATE TABLE cluster_source_cursors (
  cluster_id          uuid        NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  source              text        NOT NULL CHECK (source IN (
                                  'fantastic','france_travail','bundesagentur',
                                  'arbetsformedlingen','nav','tyomarkkinatori')),
  last_seen_posted_at timestamptz NOT NULL,

  PRIMARY KEY (cluster_id, source)
);

COMMENT ON TABLE cluster_source_cursors IS
  'High-water mark di date_posted per coppia cluster-fonte: la prossima fetch '
  'di quella fonte per quel cluster chiede da qui in avanti. Era un campo solo '
  'su clusters, e la fonte più veloce trascinava avanti anche le altre.';

-- Inizializzazione dal corpus, NON dal vecchio cursore del cluster: il massimo
-- date_posted delle offerte che quella fonte ha davvero consegnato a quel
-- cluster è il punto a cui quella coppia è arrivata. La differenza fra questo
-- valore e il vecchio cursore condiviso è precisamente ciò che il bug ha
-- perso, e la prossima fetch lo recupera.
INSERT INTO cluster_source_cursors (cluster_id, source, last_seen_posted_at)
SELECT jc.cluster_id, j.source, max(j.date_posted)
FROM job_clusters jc
JOIN jobs j ON j.id = jc.job_id
GROUP BY jc.cluster_id, j.source;

ALTER TABLE clusters DROP COLUMN last_seen_posted_at;
