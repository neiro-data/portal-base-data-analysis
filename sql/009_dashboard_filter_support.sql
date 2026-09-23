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
