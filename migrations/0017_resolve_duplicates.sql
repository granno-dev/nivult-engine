-- 0017 — Risoluzione dei quasi-duplicati.
--
-- La deduplica dura (UNIQUE su canonical_url) prende solo gli URL identici.
-- Quella morbida lavora sul fingerprint: stessa azienda, stesso titolo, stessa
-- città, stessa settimana. Non blocca la scrittura — le righe entrano tutte, e
-- questo passo decide dopo quale sia l'originale.
--
-- Chi vince, in ordine:
--   1. il tipo di link (link_kinds.rank): un career site batte una pagina
--      d'agenzia, perché è il link che vogliamo dare all'utente;
--   2. chi è arrivato prima;
--   3. l'id, come spareggio.
--
-- Il terzo criterio non è un dettaglio: senza uno spareggio deterministico due
-- esecuzioni sugli stessi dati potrebbero scegliere originali diversi, e
-- l'insieme delle offerte visibili cambierebbe senza che nessuno abbia toccato
-- niente.
--
-- LE OFFERTE SENZA DATORE DICHIARATO SONO ESCLUSE. Quando organization è vuoto
-- il fingerprint perde il suo elemento più discriminante e resta titolo + città
-- + settimana: due aziende diverse che entrambe nascondono il nome
-- collasserebbero in una, e ne perderemmo una vera. Su un campione reale di 415
-- offerte francesi il caso si è presentato subito. La deduplica dura su
-- canonical_url continua comunque a prenderle quando l'URL è identico.

CREATE OR REPLACE FUNCTION resolve_duplicates(p_batch_size integer DEFAULT 1000)
RETURNS TABLE (groups_resolved bigint, jobs_marked bigint)
LANGUAGE plpgsql AS $$
BEGIN
  IF p_batch_size <= 0 THEN
    RAISE EXCEPTION 'p_batch_size deve essere positivo, ricevuto %', p_batch_size;
  END IF;

  -- Tutto in una CTE, senza tabella temporanea: una TEMP TABLE ON COMMIT DROP
  -- sopravvive fino al commit, quindi due chiamate nella stessa transazione si
  -- scontrerebbero. Il runner ne fa una per transazione, ma un vincolo che
  -- dipende da come viene chiamata la funzione è una trappola.
  RETURN QUERY
  WITH candidati AS (
    SELECT fingerprint
      FROM jobs
     WHERE duplicate_of_job_id IS NULL
       AND status = 'active'
       AND fingerprint IS NOT NULL
       AND employer_kind <> 'undisclosed'
     GROUP BY fingerprint
    HAVING count(*) > 1
     LIMIT p_batch_size
  ),
  ordinati AS (
    SELECT j.id, j.fingerprint,
           row_number() OVER (
             PARTITION BY j.fingerprint
             ORDER BY lk.rank, j.first_seen_at, j.id
           ) AS rn
      FROM jobs j
      JOIN candidati c  ON c.fingerprint = j.fingerprint
      JOIN link_kinds lk ON lk.kind = j.link_kind
     WHERE j.duplicate_of_job_id IS NULL
       AND j.status = 'active'
       AND j.employer_kind <> 'undisclosed'
  ),
  originali AS (
    SELECT fingerprint, id FROM ordinati WHERE rn = 1
  ),
  marcate AS (
    UPDATE jobs j
       SET duplicate_of_job_id = o.id
      FROM ordinati r
      JOIN originali o ON o.fingerprint = r.fingerprint
     WHERE j.id = r.id AND r.rn > 1
    RETURNING j.fingerprint
  )
  SELECT count(DISTINCT fingerprint)::bigint, count(*)::bigint FROM marcate;
END $$;

COMMENT ON FUNCTION resolve_duplicates(integer) IS
  'Marca duplicate le offerte che condividono un fingerprint. Idempotente: '
  'una riga gia marcata esce dai gruppi candidati. Richiamare finche non '
  'ritorna (0,0).';
