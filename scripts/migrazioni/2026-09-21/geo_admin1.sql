-- 21/09/2026 — i nomi delle regioni di GeoNames (admin1CodesASCII.txt, 3.865 righe),
-- per tradurre il codice che geonamescache da' alla citta' («DE.07») nel nome
-- («North Rhine-Westphalia») nel campo `state` di offerte_dettagli.
-- Prima: cut -f1,2 admin1CodesASCII.txt > /tmp/admin1.tsv (dentro il contenitore del db)
CREATE TABLE IF NOT EXISTS geo_admin1 (codice text PRIMARY KEY, nome text NOT NULL);
TRUNCATE geo_admin1;
COPY geo_admin1 FROM '/tmp/admin1.tsv' WITH (FORMAT text, DELIMITER E'\t');
SELECT count(*) AS regioni FROM geo_admin1;
SELECT * FROM geo_admin1 WHERE codice IN ('DE.06', 'DE.07', 'PT.14', 'US.CA', 'IT.09');
