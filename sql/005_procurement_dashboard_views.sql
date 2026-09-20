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
-- v_contracts_material depends on v_contracts_enriched, so it is dropped first.
DROP VIEW IF EXISTS procurement.v_contracts_material;
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
    c.ano,
    c.is_municipio,
    c.is_below_min_value,
    c.tipo_procedimento,
    c.n_concorrentes,
    c.first_ingested_at,
    c.updated_at,
    c.preco_total_efetivo,
    c.preco_base_procedimento,
    c.data_fecho::timestamp                              AS closed_at,
    c.prazo_execucao,
    c.valor_por_dia,
    c.fundament_ajuste_direto,
    c.adjudicatario_pmes,
    -- Procedure family, the notebooks' primary grouping (procedures_comparison.ipynb).
    -- Accent-folded + lowercased prefix match; `unaccent` is not installed (see 001), so
    -- translate() does the folding. Anything unmatched is 'other' (framework agreements,
    -- excluded contracting, concurso limitado / de conceção, ...).
    CASE
        WHEN translate(lower(c.tipo_procedimento),
                       'áàâãéèêíìóòôõúùç', 'aaaaeeeiioooouuc')
             LIKE 'ajuste direto%'    THEN 'ajuste_direto'
        WHEN translate(lower(c.tipo_procedimento),
                       'áàâãéèêíìóòôõúùç', 'aaaaeeeiioooouuc')
             LIKE 'concurso publico%' THEN 'concurso_publico'
        WHEN translate(lower(c.tipo_procedimento),
                       'áàâãéèêíìóòôõúùç', 'aaaaeeeiioooouuc')
             LIKE 'consulta previa%'  THEN 'consulta_previa'
        ELSE 'other'
    END                                                  AS procedure_family,
    -- Duration buckets used by procedures_comparison.ipynb's EUR/day breakdown.
    -- NULL outside 1..3650 days, exactly like valor_por_dia (see src/base_gov/stats.py).
    CASE
        WHEN c.prazo_execucao BETWEEN    1 AND   30 THEN '1-30'
        WHEN c.prazo_execucao BETWEEN   31 AND   90 THEN '31-90'
        WHEN c.prazo_execucao BETWEEN   91 AND  180 THEN '91-180'
        WHEN c.prazo_execucao BETWEEN  181 AND  365 THEN '181-365'
        WHEN c.prazo_execucao BETWEEN  366 AND 3650 THEN '366-3650'
        ELSE NULL
    END                                                  AS duration_bucket
FROM procurement.contracts c;

COMMENT ON VIEW procurement.v_contracts_enriched IS
    'One row per contract with adjudicante_id/adjudicante_name resolved and '
    'celebrated_at/published_at cast to timestamp (IMMUTABLE, index-usable; '
    'source columns are plain dates) for $__timeFilter(), plus the notebook '
    'columns (prazo_execucao, valor_por_dia, ...) and procedure_family / '
    'duration_bucket; the single surface every dashboard panel reads.';

-- Default-exclusion surface: what the notebooks analyse and every dashboard panel reads.
-- v_contracts_enriched stays unfiltered so "what did we exclude?" is answerable in SQL.
CREATE OR REPLACE VIEW procurement.v_contracts_material AS
SELECT * FROM procurement.v_contracts_enriched WHERE NOT is_below_min_value;

COMMENT ON VIEW procurement.v_contracts_material IS
    'v_contracts_enriched minus contracts flagged is_below_min_value (the EUR 10 rule); '
    'the surface every dashboard panel reads.';

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
