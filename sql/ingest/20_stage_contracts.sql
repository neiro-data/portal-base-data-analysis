-- Column list/casing below matches the real data/contracts.parquet produced
-- by base_gov.bulk_ingest (verified 2026-09-01 against an actual file — this
-- comment previously flagged the set as unverified before one existed).
-- pg_parquet's COPY ... WITH (format 'parquet') requires the staging table to
-- carry every column the parquet file has, by name and a compatible type —
-- procurement.contracts only takes the subset it actually models; the rest
-- (nAnuncio, TipoAnuncio, idINCM, ... below) are staged and then dropped.

-- Dropped (not just truncated) first: an UNLOGGED staging table, recreated
-- every ingest run, so a column-shape change here (like this one) takes
-- effect without a manual `DROP TABLE` on whatever volume ran an older copy.
DROP TABLE IF EXISTS procurement.stg_contracts;

CREATE UNLOGGED TABLE IF NOT EXISTS procurement.stg_contracts (
    "idcontrato"                bigint,
    "nAnuncio"                  text,
    "TipoAnuncio"               text,
    "idINCM"                    bigint,
    "tipoContrato"              text,
    "idprocedimento"            bigint,
    "tipoprocedimento"          text,
    "objectoContrato"           text,
    "descContrato"              text,
    "adjudicante"               text,
    "adjudicatarios"            text,
    "dataPublicacao"            date,
    "dataCelebracaoContrato"    date,
    "precoContratual"           double precision,
    "CPV"                       text,
    "prazoExecucao"             bigint,
    "LocalExecucao"             text,
    "fundamentacao"             text,
    "ProcedimentoCentralizado"  text,
    "numAcordoQuadro"           bigint,
    "DescrAcordoQuadro"         text,
    "precoBaseProcedimento"     double precision,
    "dataDecisaoAdjudicacao"    date,
    "dataFechoContrato"         date,
    "PrecoTotalEfetivo"         double precision,
    "regime"                    text,
    "justifNReducEscrContrato"  text,
    "tipoFimContrato"           text,
    "CritMateriais"             text,
    "concorrentes"              text,
    "linkPecasProc"             text,
    "Observacoes"               text,
    "ContratEcologico"          text,
    "fundamentAjusteDireto"     text,
    "Ano"                       bigint,
    -- Text despite the name: values seen are 9-digit NIPCs or "-", not a
    -- Sim/Não flag — see the NULLIF(...)::integer cast below.
    "adjudicatarioPMEs"         text,
    "NUTs"                      text,
    "Lotes"                     text,
    "TipoCriterioAdjudicacao"   text
);
REVOKE SELECT ON procurement.stg_contracts FROM grafana_reader;

TRUNCATE procurement.stg_contracts;
COPY procurement.stg_contracts
    FROM '/parquet/contracts.parquet' WITH (format 'parquet');

-- adjudicatario_pmes deliberately not populated: real "adjudicatarioPMEs"
-- values are themselves a newline-packed list of PME awardee NIPCs (e.g.
-- "504092502\n501683631"), not the single boolean/NIPC the column's
-- `integer` type assumes. Left NULL rather than forced through a lossy or
-- error-prone cast; not read by any dashboard panel. Revisit as a proper
-- text/bridge column if this data is needed later.
INSERT INTO procurement.contracts (
    idcontrato, adjudicante, adjudicante_nipc, adjudicante_name, adjudicatarios,
    objecto_contrato, desc_contrato, preco_contratual, preco_total_efetivo,
    data_celebracao, data_publicacao, data_fecho, tipo_contrato, cpv, nuts,
    ano, prazo_execucao
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
    "CPV",
    "NUTs",
    "Ano"::integer,
    "prazoExecucao"::integer
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
        updated_at = now();
