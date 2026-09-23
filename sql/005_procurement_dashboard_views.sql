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
-- 008's v_adjudicatario_contracts selects from v_contracts_material; drop it first
-- (008 recreates it later in the same init-db run).
DROP VIEW IF EXISTS procurement.v_adjudicatario_contracts;
-- 009's v_contracts_below_min selects from v_contracts_enriched; 009 recreates it.
DROP VIEW IF EXISTS procurement.v_contracts_below_min;
DROP VIEW IF EXISTS procurement.v_contracts_material;
DROP VIEW IF EXISTS procurement.v_contracts_enriched;

CREATE VIEW procurement.v_contracts_enriched AS
SELECT
    c.idcontrato,
    c.adjudicante_nipc                                   AS adjudicante_id,
    -- Canonical name per NIPC from the entity table: the per-contract raw name varies in
    -- spelling/case across contracts (e.g. 'MUNICÍPIO DO X' vs 'Município do X'), which
    -- split GROUP BY name panels. Raw name only when the NIPC has no entity row.
    coalesce(e.name, c.adjudicante_name, c.adjudicante)  AS adjudicante_name,
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
FROM procurement.contracts c
LEFT JOIN procurement.entities_adjudicantes e ON e.entity_id = c.adjudicante_nipc;

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

-- One canonical name per awardee NIPC. The bridge row's denormalized name varies in
-- spelling/case across contracts for the same NIPC (e.g. 'ACCENTURE, ...' vs
-- 'Accenture ...'), which split GROUP BY name panels. entities_adjudicatarios wins;
-- NIPCs missing from it fall back to min(bridge name). Anti-join + hash join keeps
-- full-scan panels at the old cost (a correlated subquery tripled it).
CREATE OR REPLACE VIEW procurement.v_adjudicatario_name AS
SELECT entity_id, name
FROM procurement.entities_adjudicatarios
UNION ALL
SELECT b.entity_id, min(b.name)
FROM procurement.contract_adjudicatarios b
WHERE NOT EXISTS (
    SELECT 1 FROM procurement.entities_adjudicatarios e WHERE e.entity_id = b.entity_id
)
GROUP BY b.entity_id;

COMMENT ON VIEW procurement.v_adjudicatario_name IS
    'One canonical name per adjudicatario NIPC: entities_adjudicatarios, else '
    'min(contract_adjudicatarios.name). Join target for every adjudicatario_name.';

-- Bridge surface, one row per (contract, awardee), with the canonical name.
-- The name is resolved inline (entity name, else min bridge name per NIPC, else the row
-- name) and does NOT join v_adjudicatario_name: its UNION ALL ... GROUP BY fallback branch
-- cannot take a pushed-down parameter, so a LATERAL/EXISTS use re-ran a full seq scan of the
-- bridge per output row (panel 2, 90 d, LIMIT 5000: >171 s, about 355 s extrapolated).
-- The correlated subquery below runs only for rows without an entity row (~306) and uses
-- idx_contract_adjudicatarios_entity_id (same query: 147 ms).
CREATE OR REPLACE VIEW procurement.v_contract_adjudicatario AS
SELECT
    b.idcontrato,
    b.entity_id AS adjudicatario_id,
    coalesce(
        e.name,
        (SELECT min(b2.name) FROM procurement.contract_adjudicatarios b2
          WHERE b2.entity_id = b.entity_id),
        b.name
    ) AS adjudicatario_name
FROM procurement.contract_adjudicatarios b
LEFT JOIN procurement.entities_adjudicatarios e ON e.entity_id = b.entity_id;

COMMENT ON VIEW procurement.v_contract_adjudicatario IS
    'One row per (contract, awardee), for the adjudicatario selector and the '
    'contracts table''s awardee-list column.';
