-- Idempotent: safe to re-run against a warm volume.

CREATE OR REPLACE VIEW procurement.entities_all AS
SELECT entity_id, name, 'adjudicante' AS role FROM procurement.entities_adjudicantes
UNION ALL
SELECT entity_id, name, 'adjudicatario' AS role FROM procurement.entities_adjudicatarios;

-- DROP + CREATE (not OR REPLACE): `c.*` re-expands after 002's rename/ADD COLUMNs,
-- which OR REPLACE rejects on a warm volume. No view depends on this one; default
-- privileges from 004 re-grant SELECT to grafana_reader on the recreated view.
DROP VIEW IF EXISTS procurement.contracts_enriched;

CREATE VIEW procurement.contracts_enriched AS
SELECT
    c.*,
    e.name AS adjudicante_display_name
FROM procurement.contracts c
LEFT JOIN procurement.entities_adjudicantes e
    ON e.entity_id = c.adjudicante_nipc;
