-- Dashboard filter support. Depends on 004 (grafana_reader role) and 005 (v_contracts_enriched).
-- No GRANT needed: 004's default privileges cover the new view.

-- Dashboard queries are short OLTP-style queries; JIT added ~266 ms of compile time to each.
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_reader') THEN
    ALTER ROLE grafana_reader SET jit = off;
  END IF;
END $$;

-- Complement of v_contracts_material: the contracts excluded by the EUR 10 rule, with
-- procedure_family so the contract-type filter works on overview panel 10.
-- 005 drops this view before v_contracts_enriched; it is recreated here on every init-db.
CREATE OR REPLACE VIEW procurement.v_contracts_below_min AS
SELECT * FROM procurement.v_contracts_enriched WHERE is_below_min_value;

COMMENT ON VIEW procurement.v_contracts_below_min IS
    'v_contracts_enriched restricted to contracts flagged is_below_min_value: the '
    'complement of v_contracts_material, for the "Excluded below EUR 10" panel.';

-- Options for the dashboards' "Contract type" filter, driven by the show_all_contract_types
-- toggle: 'off' = the four procedure families, 'on' = every distinct tipo_procedimento.
-- The DISTINCT is an index-only scan on idx_contracts_tipo_procedimento (~55 ms on 1M rows),
-- so a plain view is enough; no materialization needed.
CREATE OR REPLACE VIEW procurement.v_contract_type_options AS
SELECT 'off' AS mode, t.text, t.value, t.sort
FROM (VALUES
    ('Ajuste direto',    'ajuste_direto',    1),
    ('Concurso público', 'concurso_publico', 2),
    ('Consulta prévia',  'consulta_previa',  3),
    ('Other',            'other',            4)
) AS t(text, value, sort)
UNION ALL
SELECT 'on', d.tipo_procedimento, d.tipo_procedimento, 10
FROM (SELECT DISTINCT tipo_procedimento FROM procurement.contracts) d;

COMMENT ON VIEW procurement.v_contract_type_options IS
    'Options for the dashboards'' contract_type variable. mode = off: the four '
    'procedure_family values; mode = on: every distinct contracts.tipo_procedimento.';
