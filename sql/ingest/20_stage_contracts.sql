-- NOTE: the exact parquet field names/casing below are UNVERIFIED against a
-- real file yet. The sibling `bulk-ingest` worktree has not produced
-- data/contracts.parquet at the time this file was written; the column list
-- and casing here follow the naming used by base_gov.xlsx_ingest's
-- REQUIRED_COLUMNS plus the wider set documented in the pipeline plan.
-- Cross-check (and fix if needed) once a real contracts.parquet exists.

CREATE UNLOGGED TABLE IF NOT EXISTS procurement.stg_contracts (
    "idcontrato"             bigint,
    "adjudicante"             text,
    "adjudicatarios"          text,
    "objectoContrato"         text,
    "descContrato"            text,
    "precoContratual"         double precision,
    "PrecoTotalEfetivo"       double precision,
    "dataCelebracaoContrato"  date,
    "dataPublicacao"          date,
    "dataFechoContrato"       date,
    "tipoContrato"            text,
    "cpv"                     text,
    "nuts"                    text,
    "ano"                     integer,
    "prazoExecucao"           integer,
    "adjudicatarioPmes"       integer
);
REVOKE SELECT ON procurement.stg_contracts FROM grafana_reader;

TRUNCATE procurement.stg_contracts;
COPY procurement.stg_contracts
    FROM '/parquet/contracts.parquet' WITH (format 'parquet');

INSERT INTO procurement.contracts (
    idcontrato, adjudicante, adjudicante_nipc, adjudicante_name, adjudicatarios,
    objecto_contrato, desc_contrato, preco_contratual, preco_total_efetivo,
    data_celebracao, data_publicacao, data_fecho, tipo_contrato, cpv, nuts,
    ano, prazo_execucao, adjudicatario_pmes
)
SELECT
    "idcontrato",
    "adjudicante",
    -- 9-digit NIPC, matching the bridge table's entity_id pattern in
    -- 30_rebuild_bridge.sql — kept consistent so contracts_enriched's join
    -- on entity_id = adjudicante_nipc doesn't silently miss rows.
    (regexp_match("adjudicante", '^(\d{9})'))[1],
    trim(regexp_replace("adjudicante", '^\d+\s*', '')),
    "adjudicatarios",
    "objectoContrato",
    "descContrato",
    "precoContratual"::numeric(18, 2),
    "PrecoTotalEfetivo"::numeric(18, 2),
    "dataCelebracaoContrato",
    "dataPublicacao",
    "dataFechoContrato",
    "tipoContrato",
    "cpv",
    "nuts",
    "ano",
    "prazoExecucao",
    "adjudicatarioPmes"
FROM procurement.stg_contracts
ON CONFLICT (idcontrato) DO UPDATE
    SET adjudicante = EXCLUDED.adjudicante,
        adjudicante_nipc = EXCLUDED.adjudicante_nipc,
        adjudicante_name = EXCLUDED.adjudicante_name,
        adjudicatarios = EXCLUDED.adjudicatarios,
        objecto_contrato = EXCLUDED.objecto_contrato,
        desc_contrato = EXCLUDED.desc_contrato,
        preco_contratual = EXCLUDED.preco_contratual,
        preco_total_efetivo = EXCLUDED.preco_total_efetivo,
        data_celebracao = EXCLUDED.data_celebracao,
        data_publicacao = EXCLUDED.data_publicacao,
        data_fecho = EXCLUDED.data_fecho,
        tipo_contrato = EXCLUDED.tipo_contrato,
        cpv = EXCLUDED.cpv,
        nuts = EXCLUDED.nuts,
        ano = EXCLUDED.ano,
        prazo_execucao = EXCLUDED.prazo_execucao,
        adjudicatario_pmes = EXCLUDED.adjudicatario_pmes,
        updated_at = now();
