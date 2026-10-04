-- Per-query statistics for `base-gov query-stats`.
-- The library must be preloaded: docker-compose.yml starts postgres with
-- shared_preload_libraries=pg_parquet,pg_stat_statements (plus track_io_timing=on).
-- Without the preload CREATE EXTENSION succeeds but querying the view errors.
-- No GRANT: the tool reads the view as the superuser through `docker exec`.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements SCHEMA public;
