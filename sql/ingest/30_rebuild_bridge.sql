-- Rebuild procurement.contract_adjudicatarios from the packed "adjudicatarios"
-- column staged by 20_stage_contracts.sql. Must run after 20_stage_contracts.sql
-- in the same session, while procurement.stg_contracts still holds this batch.

DELETE FROM procurement.contract_adjudicatarios
WHERE idcontrato IN (SELECT idcontrato FROM procurement.stg_contracts);

WITH segments AS (
    SELECT
        s."idcontrato",
        trim(seg.value) AS raw_segment,
        seg.ordinality AS position
    FROM procurement.stg_contracts s,
         LATERAL regexp_split_to_table(s."adjudicatarios", '\s*;\s*') WITH ORDINALITY AS seg(value, ordinality)
    WHERE s."adjudicatarios" IS NOT NULL AND s."adjudicatarios" <> ''
),
parsed AS (
    SELECT
        idcontrato,
        raw_segment,
        position,
        (regexp_match(raw_segment, '^(\d{9})\s*(.*)$')) AS m
    FROM segments
),
valid AS (
    SELECT
        idcontrato,
        (m)[1] AS entity_id,
        nullif(trim((m)[2]), '') AS name,
        position::smallint AS position
    FROM parsed
    WHERE m IS NOT NULL
)
INSERT INTO procurement.contract_adjudicatarios (idcontrato, entity_id, name, position)
SELECT idcontrato, entity_id, name, position FROM valid
ON CONFLICT (idcontrato, entity_id) DO UPDATE
    SET name = EXCLUDED.name,
        position = EXCLUDED.position;

-- Summary: how many segments were discarded for not matching the ^\d{9}
-- leading-NIPC pattern. Recomputed standalone since the CTEs above don't
-- outlive their INSERT statement. Non-blocking (never fails the ingest) but
-- RAISE WARNING makes a parsing regression visible in psql/docker logs
-- rather than only in a SELECT result a human has to think to check.
DO $$
DECLARE
    total_segments     bigint;
    discarded_segments bigint;
BEGIN
    SELECT count(*), count(*) FILTER (WHERE raw_segment !~ '^\d{9}')
    INTO total_segments, discarded_segments
    FROM (
        SELECT trim(seg.value) AS raw_segment
        FROM procurement.stg_contracts s,
             LATERAL regexp_split_to_table(s."adjudicatarios", '\s*;\s*')
                 WITH ORDINALITY AS seg(value, ordinality)
        WHERE s."adjudicatarios" IS NOT NULL AND s."adjudicatarios" <> ''
    ) segments;

    RAISE NOTICE 'contract_adjudicatarios: % of % adjudicatarios segments discarded (no leading 9-digit NIPC)',
        discarded_segments, total_segments;

    IF discarded_segments > 0 THEN
        RAISE WARNING 'contract_adjudicatarios: % segments discarded this run — check for an NIPC format change upstream',
            discarded_segments;
    END IF;
END
$$;
