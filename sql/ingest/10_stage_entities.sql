-- Stage + upsert the two entities parquet files produced by
-- `uv run base-gov xlsx-entities` (data/entities_adjudicantes.parquet,
-- data/entities_adjudicatarios.parquet), bind-mounted read-only at /parquet.
-- Re-runnable: staging tables are truncated first, upserts use ON CONFLICT.

CREATE UNLOGGED TABLE IF NOT EXISTS procurement.stg_entities_adjudicantes (
    entity_id text,
    name      text
);
REVOKE SELECT ON procurement.stg_entities_adjudicantes FROM grafana_reader;

CREATE UNLOGGED TABLE IF NOT EXISTS procurement.stg_entities_adjudicatarios (
    entity_id text,
    name      text
);
REVOKE SELECT ON procurement.stg_entities_adjudicatarios FROM grafana_reader;

TRUNCATE procurement.stg_entities_adjudicantes;
COPY procurement.stg_entities_adjudicantes
    FROM '/parquet/entities_adjudicantes.parquet' WITH (format 'parquet');

TRUNCATE procurement.stg_entities_adjudicatarios;
COPY procurement.stg_entities_adjudicatarios
    FROM '/parquet/entities_adjudicatarios.parquet' WITH (format 'parquet');

-- entity_id is nullable in the staging tables: base_gov.entities_db
-- deliberately keeps entities the portal has no NIPC for (individuals whose
-- ID is masked, or a packed "- - Name" placeholder) rather than dropping
-- them, so they still show up when read directly from parquet. But
-- entities_adjudicantes/entities_adjudicatarios use entity_id as a NOT NULL
-- primary key, so those rows can't become dimension rows here — same
-- discard-with-a-warning treatment as 30_rebuild_bridge.sql's no-NIPC
-- segments. Not blocking: contracts.adjudicante_nipc/adjudicante_name and
-- the contract_adjudicatarios bridge don't depend on this table having every
-- name.
INSERT INTO procurement.entities_adjudicantes (entity_id, name)
SELECT entity_id, name FROM procurement.stg_entities_adjudicantes
WHERE entity_id IS NOT NULL
ON CONFLICT (entity_id) DO UPDATE
    SET name = EXCLUDED.name,
        updated_at = now();

INSERT INTO procurement.entities_adjudicatarios (entity_id, name)
SELECT entity_id, name FROM procurement.stg_entities_adjudicatarios
WHERE entity_id IS NOT NULL
ON CONFLICT (entity_id) DO UPDATE
    SET name = EXCLUDED.name,
        updated_at = now();

DO $$
DECLARE
    discarded_adjudicantes   bigint;
    discarded_adjudicatarios bigint;
BEGIN
    SELECT count(*) INTO discarded_adjudicantes
    FROM procurement.stg_entities_adjudicantes WHERE entity_id IS NULL;
    SELECT count(*) INTO discarded_adjudicatarios
    FROM procurement.stg_entities_adjudicatarios WHERE entity_id IS NULL;

    RAISE NOTICE 'entities_adjudicantes: % rows discarded (null entity_id)', discarded_adjudicantes;
    RAISE NOTICE 'entities_adjudicatarios: % rows discarded (null entity_id)', discarded_adjudicatarios;
END
$$;
