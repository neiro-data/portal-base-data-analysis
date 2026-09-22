-- Idempotent: safe to re-run against a warm volume.
-- Depends on 005 (v_contracts_material, v_adjudicatario_name) and 002 (contract_adjudicatarios).
-- No GRANT needed: 004's ALTER DEFAULT PRIVILEGES covers views created after it.

CREATE INDEX IF NOT EXISTS idx_contract_adjudicatarios_entity_id
    ON procurement.contract_adjudicatarios (entity_id);

CREATE OR REPLACE VIEW procurement.v_adjudicatario_contracts AS
SELECT
    b.entity_id  AS adjudicatario_id,
    coalesce(n.name, b.name)  AS adjudicatario_name,  -- canonical per NIPC (005)
    b.position   AS adjudicatario_position,
    v.*
FROM procurement.contract_adjudicatarios b
JOIN procurement.v_contracts_material v ON v.idcontrato = b.idcontrato
LEFT JOIN procurement.v_adjudicatario_name n ON n.entity_id = b.entity_id;

COMMENT ON VIEW procurement.v_adjudicatario_contracts IS
    'One row per (contract, awardee) over the material surface. A multi-awardee '
    'contract appears once per awardee and its full `preco_contratual` is counted '
    'for each — the same convention every notebook uses. Count contracts with '
    'count(DISTINCT idcontrato), never count(*).';
