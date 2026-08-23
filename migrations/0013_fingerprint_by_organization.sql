-- 0013 — Fingerprint per azienda invece che per dominio.
--
-- Il fingerprint della deduplica morbida usava domain_derived. Su un ATS
-- CONDIVISO — aplitrak.com (Bullhorn), varbi.com, greenhouse.io, lever.co,
-- teamtailor.com — quel dominio non identifica l'azienda ma il fornitore del
-- software. Due aziende diverse che pubblicano lo stesso titolo nella stessa
-- settimana tramite lo stesso ATS producevano lo stesso fingerprint, e una
-- sarebbe stata marcata come duplicato dell'altra: un'offerta vera scartata,
-- senza lasciare traccia.
-- Scoperto interrogando davvero Arbetsförmedlingen: il secondo risultato della
-- prima query stava su aplitrak.com.
--
-- Si passa quindi al nome dell'azienda, che è anche più stabile fra fonti
-- diverse: la stessa offerta sul sito aziendale e sulla pagina dell'agenzia ha
-- domini diversi ma la stessa organization.
--
-- Si aggiunge anche la città. Il fingerprint governa una deduplica MORBIDA, e
-- un falso positivo costa molto più di un falso negativo: marcare duplicate due
-- offerte diverse le fa sparire, mentre non accorgersi di un duplicato lascia
-- solo una riga in più. Meglio precisione che copertura.

CREATE OR REPLACE FUNCTION jobs_derive_fields() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_skills text;
BEGIN
  IF NEW.purged_at IS NOT NULL THEN
    NEW.tsv := NULL;
    RETURN NEW;
  END IF;

  v_skills := COALESCE(array_to_string(NEW.ai_key_skills, ' '), '');

  NEW.tsv :=
      setweight(to_tsvector('simple', COALESCE(NEW.title, '')), 'A')
   || setweight(to_tsvector('simple', COALESCE(NEW.organization, '')), 'B')
   || setweight(to_tsvector('simple', v_skills), 'B')
   || setweight(to_tsvector('simple',
        COALESCE(NEW.ai_requirements_summary, '') || ' ' ||
        COALESCE(NEW.ai_core_responsibilities, '')), 'C');

  NEW.fingerprint := encode(sha256(convert_to(
      lower(btrim(regexp_replace(COALESCE(NEW.organization, ''), '\s+', ' ', 'g')))
                                      || '|' ||
      NEW.title_normalized            || '|' ||
      COALESCE(NEW.countries[1], '')  || '|' ||
      lower(COALESCE(NEW.cities[1], '')) || '|' ||
      to_char(NEW.date_posted AT TIME ZONE 'UTC', 'IYYY-IW'),
    'UTF8')), 'hex');

  RETURN NEW;
END $$;
