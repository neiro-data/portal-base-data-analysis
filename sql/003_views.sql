-- Idempotent: safe to re-run against a warm volume.

CREATE OR REPLACE VIEW procurement.entities_all AS
SELECT entity_id, name, 'adjudicante' AS role FROM procurement.entities_adjudicantes
UNION ALL
SELECT entity_id, name, 'adjudicatario' AS role FROM procurement.entities_adjudicatarios;

CREATE OR REPLACE VIEW procurement.contracts_enriched AS
SELECT
    c.*,
    e.name AS adjudicante_display_name
FROM procurement.contracts c
LEFT JOIN procurement.entities_adjudicantes e
    ON e.entity_id = c.adjudicante_nipc;
