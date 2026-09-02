-- Idempotent: safe to re-run against a warm volume (DROP VIEW IF EXISTS +
-- CREATE VIEW / CREATE OR REPLACE VIEW).
--
-- Dashboard-support views for the `dashboards` worktree's Grafana panels.
-- Depends on 002_schema.sql (procurement.contracts, contract_adjudicatarios)
-- and 004_grafana_role.sql (grafana_reader). Deliberately named
-- v_contracts_enriched / v_contract_adjudicatario, distinct from 003_views.sql's
-- own procurement.contracts_enriched, so the two worktrees never collide.
--
-- No explicit GRANT here: 004_grafana_role.sql's
-- `ALTER DEFAULT PRIVILEGES IN SCHEMA procurement GRANT SELECT ON TABLES TO
-- grafana_reader` already covers every view/table the owner role creates in
-- this schema afterwards, including these two.

-- One row per contract, the single surface every dashboard panel reads.
-- Casts data_celebracao/data_publicacao to timestamp (not timestamptz) once
-- here so no panel SQL needs its own cast for $__timeFilter(). `date::timestamp`
-- is IMMUTABLE (unlike `date::timestamptz`, which is only STABLE since it
-- depends on the session timezone) so 006's matching expression indexes are
-- usable by the planner; the source columns are plain calendar dates anyway,
-- so no timezone semantics are lost.
--
-- CREATE OR REPLACE VIEW cannot change an existing column's data type, so the
-- DROP below is required before recreating with the new (timestamp) type.
DROP VIEW IF EXISTS procurement.v_contracts_enriched;

CREATE VIEW procurement.v_contracts_enriched AS
SELECT
    c.idcontrato,
    c.adjudicante_nipc                                   AS adjudicante_id,
    coalesce(c.adjudicante_name, c.adjudicante)          AS adjudicante_name,
    c.objecto_contrato,
    c.desc_contrato,
    c.preco_contratual,
    c.data_celebracao::timestamp                         AS celebrated_at,
    c.data_publicacao::timestamp                         AS published_at,
    c.tipo_contrato,
    c.cpv,
    c.nuts,
    c.ano
FROM procurement.contracts c;

COMMENT ON VIEW procurement.v_contracts_enriched IS
    'One row per contract with adjudicante_id/adjudicante_name resolved and '
    'celebrated_at/published_at cast to timestamp (IMMUTABLE, index-usable; '
    'source columns are plain dates) for $__timeFilter(); the single surface '
    'every dashboard panel reads.';

-- Bridge surface, one row per (contract, awardee). Thin passthrough over
-- procurement.contract_adjudicatarios, which already carries the awardee's
-- name denormalized onto the bridge row — no join needed.
CREATE OR REPLACE VIEW procurement.v_contract_adjudicatario AS
SELECT
    idcontrato,
    entity_id AS adjudicatario_id,
    name      AS adjudicatario_name
FROM procurement.contract_adjudicatarios;

COMMENT ON VIEW procurement.v_contract_adjudicatario IS
    'One row per (contract, awardee), for the adjudicatario selector and the '
    'contracts table''s awardee-list column.';
