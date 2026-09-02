-- Idempotent: safe to re-run against a warm volume (CREATE INDEX IF NOT EXISTS).
--
-- Depends on 002_schema.sql (procurement.contracts) and 005's
-- v_contracts_enriched, which now casts data_celebracao/data_publicacao to
-- `timestamp` rather than `timestamptz`. `date::timestamp` is IMMUTABLE
-- (timezone-independent), unlike `date::timestamptz` (STABLE only), so the
-- planner can use a matching expression index for Grafana's
-- $__timeFilter(celebrated_at) predicate instead of a full table scan.
CREATE INDEX IF NOT EXISTS idx_contracts_data_celebracao_ts
    ON procurement.contracts ((data_celebracao::timestamp));

CREATE INDEX IF NOT EXISTS idx_contracts_data_publicacao_ts
    ON procurement.contracts ((data_publicacao::timestamp));

-- Decision — keep 002's plain idx_contracts_data_celebracao, no DROP: it
-- still serves plain `WHERE data_celebracao BETWEEN date AND date` predicates
-- (ingest/ad-hoc queries, 003_views.sql's own surfaces) that the expression
-- index above cannot satisfy. A b-tree on one date column is cheap to keep
-- around, so this redundancy is intentional, not an oversight.
