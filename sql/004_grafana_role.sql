-- Idempotent: safe to re-run against a warm volume.
--
-- Placeholder password below — initdb .sql files cannot read environment
-- variables, so this is a literal, documented placeholder.
-- ROTATE ME before any non-local deployment.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_reader') THEN
        CREATE ROLE grafana_reader
            WITH LOGIN NOSUPERUSER
            PASSWORD 'changeme_grafana'; -- ROTATE ME before any non-local deployment
    END IF;
END
$$;

GRANT USAGE ON SCHEMA procurement TO grafana_reader;

-- ALL TABLES currently in the schema, which at initdb time is only the
-- public procurement.* tables/views (sql/ingest/*.sql creates the stg_*
-- staging tables later, outside docker-entrypoint-initdb.d, and each of
-- those scripts explicitly REVOKEs grafana_reader's default-privilege grant
-- on its own staging table right after creating it — see sql/ingest/10_* and
-- sql/ingest/20_* — so reporting never sees a table while ingest is clearing
-- and reloading it).
GRANT SELECT ON ALL TABLES IN SCHEMA procurement TO grafana_reader;

ALTER DEFAULT PRIVILEGES IN SCHEMA procurement
    GRANT SELECT ON TABLES TO grafana_reader;
