-- Column list/casing below is the treated schema, base_gov.transform.TREATED_COLUMNS
-- (data/contracts.parquet), snake_case. pg_parquet's COPY ... WITH (format 'parquet')
-- requires the staging table to carry every parquet column by name and a compatible
-- type; tests/test_sql_migrations.py checks parity with TREATED_COLUMNS.
-- procurement.contracts only models a subset, the rest are staged and dropped.

-- Dropped (not just truncated) first: an UNLOGGED staging table, recreated
-- every ingest run, so a column-shape change takes effect without a manual
-- `DROP TABLE` on a warm volume that ran an older copy.
DROP TABLE IF EXISTS procurement.stg_contracts;

CREATE UNLOGGED TABLE IF NOT EXISTS procurement.stg_contracts (
    "idcontrato"                bigint,
    "ano"                       integer,
    "adjudicante"               text,
    "adjudicante_nipc"          text,
    "adjudicante_name"          text,
    "adjudicante_key"           text,
    "adjudicante_key_source"    text,
    "adjudicatarios"            text,
    "objecto_contrato"          text,
    "desc_contrato"             text,
    "preco_contratual"          double precision,
    "preco_total_efetivo"       double precision,
    "preco_base_procedimento"   double precision,
    "prazo_execucao"            bigint,
    "valor_por_dia"             double precision,
    "data_celebracao"           date,
    "data_publicacao"           date,
    "data_fecho"                date,
    "tipo_contrato"             text,
    "tipo_procedimento"         text,
    "fundament_ajuste_direto"   text,
    "cpv"                       text,
    "nuts"                      text,
    "concorrentes"              text,
    "n_concorrentes"            bigint,
    "estado_contrato"           text,
    "preco_estimado_proc"       double precision,
    "ultima_atualizacao"        date,
    "id_anuncio"                bigint,
    "is_municipio"              boolean,
    "is_below_min_value"        boolean,
    "row_hash"                  bigint,
    "first_ingested_at"         timestamptz,
    "updated_at"                timestamptz
);
REVOKE SELECT ON procurement.stg_contracts FROM grafana_reader;

TRUNCATE procurement.stg_contracts;
COPY procurement.stg_contracts
    FROM '/parquet/contracts.parquet' WITH (format 'parquet');

-- adjudicatario_pmes deliberately not populated: real "adjudicatarioPMEs"
-- values are themselves a newline-packed list of PME awardee NIPCs, not the
-- single boolean/NIPC the column's `integer` type assumes. Left NULL.
--
-- The flag is_below_min_value never filters this load: every treated row is
-- inserted. first_ingested_at is set on insert only, never in the SET list.
-- The update fires only when the content hash or the flag changed.
INSERT INTO procurement.contracts (
    idcontrato, adjudicante, adjudicante_nipc, adjudicante_name, adjudicatarios,
    objecto_contrato, desc_contrato, preco_contratual, preco_total_efetivo,
    preco_base_procedimento, data_celebracao, data_publicacao, data_fecho,
    tipo_contrato, tipo_procedimento, fundament_ajuste_direto, cpv, nuts,
    ano, prazo_execucao, n_concorrentes, valor_por_dia,
    estado_contrato, preco_estimado_proc, ultima_atualizacao, id_anuncio,
    is_municipio, is_below_min_value, row_hash, first_ingested_at, updated_at
)
SELECT
    "idcontrato",
    "adjudicante",
    "adjudicante_nipc",
    "adjudicante_name",
    "adjudicatarios",
    "objecto_contrato",
    "desc_contrato",
    "preco_contratual"::numeric(18, 2),
    "preco_total_efetivo"::numeric(18, 2),
    "preco_base_procedimento"::numeric(18, 2),
    "data_celebracao",
    "data_publicacao",
    "data_fecho",
    "tipo_contrato",
    "tipo_procedimento",
    "fundament_ajuste_direto",
    "cpv",
    "nuts",
    "ano",
    "prazo_execucao"::integer,
    "n_concorrentes"::integer,
    "valor_por_dia"::numeric(18, 2),
    "estado_contrato",
    "preco_estimado_proc"::numeric(18, 2),
    "ultima_atualizacao",
    "id_anuncio",
    coalesce("is_municipio", false),
    coalesce("is_below_min_value", false),
    "row_hash",
    "first_ingested_at",
    "updated_at"
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
        preco_base_procedimento = EXCLUDED.preco_base_procedimento,
        data_celebracao = EXCLUDED.data_celebracao,
        data_publicacao = EXCLUDED.data_publicacao,
        data_fecho = EXCLUDED.data_fecho,
        tipo_contrato = EXCLUDED.tipo_contrato,
        tipo_procedimento = EXCLUDED.tipo_procedimento,
        fundament_ajuste_direto = EXCLUDED.fundament_ajuste_direto,
        cpv = EXCLUDED.cpv,
        nuts = EXCLUDED.nuts,
        ano = EXCLUDED.ano,
        prazo_execucao = EXCLUDED.prazo_execucao,
        n_concorrentes = EXCLUDED.n_concorrentes,
        valor_por_dia = EXCLUDED.valor_por_dia,
        estado_contrato = EXCLUDED.estado_contrato,
        preco_estimado_proc = EXCLUDED.preco_estimado_proc,
        ultima_atualizacao = EXCLUDED.ultima_atualizacao,
        id_anuncio = EXCLUDED.id_anuncio,
        is_municipio = EXCLUDED.is_municipio,
        is_below_min_value = EXCLUDED.is_below_min_value,
        row_hash = EXCLUDED.row_hash,
        updated_at = EXCLUDED.updated_at
    WHERE procurement.contracts.row_hash IS DISTINCT FROM EXCLUDED.row_hash
       OR procurement.contracts.is_below_min_value IS DISTINCT FROM EXCLUDED.is_below_min_value;
