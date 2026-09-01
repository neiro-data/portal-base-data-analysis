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

INSERT INTO procurement.entities_adjudicantes (entity_id, name)
SELECT entity_id, name FROM procurement.stg_entities_adjudicantes
ON CONFLICT (entity_id) DO UPDATE
    SET name = EXCLUDED.name,
        updated_at = now();

INSERT INTO procurement.entities_adjudicatarios (entity_id, name)
SELECT entity_id, name FROM procurement.stg_entities_adjudicatarios
ON CONFLICT (entity_id) DO UPDATE
    SET name = EXCLUDED.name,
        updated_at = now();
