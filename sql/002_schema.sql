-- Idempotent: safe to re-run against a warm volume.

CREATE TABLE IF NOT EXISTS procurement.entities_adjudicantes (
    entity_id  text PRIMARY KEY,
    name       text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS procurement.entities_adjudicatarios (
    entity_id  text PRIMARY KEY,
    name       text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS procurement.contracts (
    idcontrato           bigint PRIMARY KEY,
    adjudicante          text,            -- raw packed "NIPC name"
    adjudicante_nipc     text,            -- derived, indexed
    adjudicante_name     text,            -- derived
    adjudicatarios       text,            -- raw packed, ';'-separated
    objecto_contrato     text,            -- source col: objectoContrato
    desc_contrato        text,            -- source col: descContrato
    preco_contratual     numeric(18, 2),  -- source col: precoContratual
    preco_total_efetivo  numeric(18, 2),  -- source col: PrecoTotalEfetivo, nullable
    data_celebracao      date,            -- source col: dataCelebracaoContrato
    data_publicacao      date,            -- source col: dataPublicacao
    data_fecho           date,            -- source col: dataFechoContrato, nullable
    tipo_contrato        text,
    cpv                  text,
    nuts                 text,
    ano                  integer,
    prazo_execucao       integer,
    adjudicatario_pmes   integer,
    -- Lifecycle timestamps come from the treated parquet, never from now().
    first_ingested_at    timestamptz NOT NULL,
    updated_at           timestamptz NOT NULL,
    row_hash                 bigint,          -- from parquet, drives the conditional update
    preco_base_procedimento  numeric(18, 2),
    tipo_procedimento        text,
    fundament_ajuste_direto  text,
    n_concorrentes           integer,
    is_municipio             boolean NOT NULL DEFAULT false,
    is_below_min_value       boolean NOT NULL DEFAULT false,  -- the EUR 10 rule as a flag, never a delete
    valor_por_dia            numeric(18, 2)
);

-- Warm-volume upgrade: CREATE TABLE IF NOT EXISTS above is a no-op on an existing
-- table, and 005's views read the new columns, so they are added here as well as in
-- 007_ingestion_metadata.sql. All guarded, safe to re-run.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'procurement' AND table_name = 'contracts'
                 AND column_name = 'ingested_at')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'procurement' AND table_name = 'contracts'
                 AND column_name = 'first_ingested_at') THEN
        ALTER TABLE procurement.contracts RENAME COLUMN ingested_at TO first_ingested_at;
    END IF;
END
$$;

ALTER TABLE procurement.contracts ALTER COLUMN first_ingested_at DROP DEFAULT;
ALTER TABLE procurement.contracts ALTER COLUMN updated_at DROP DEFAULT;
ALTER TABLE procurement.contracts
    ADD COLUMN IF NOT EXISTS row_hash                bigint,
    ADD COLUMN IF NOT EXISTS preco_base_procedimento numeric(18, 2),
    ADD COLUMN IF NOT EXISTS tipo_procedimento       text,
    ADD COLUMN IF NOT EXISTS fundament_ajuste_direto text,
    ADD COLUMN IF NOT EXISTS n_concorrentes          integer,
    ADD COLUMN IF NOT EXISTS is_municipio            boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS is_below_min_value      boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS valor_por_dia           numeric(18, 2);

CREATE TABLE IF NOT EXISTS procurement.contract_adjudicatarios (
    idcontrato bigint NOT NULL REFERENCES procurement.contracts (idcontrato) ON DELETE CASCADE,
    entity_id  text NOT NULL,
    name       text,
    position   smallint NOT NULL,
    PRIMARY KEY (idcontrato, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_contracts_data_celebracao
    ON procurement.contracts (data_celebracao);

CREATE INDEX IF NOT EXISTS idx_contracts_adjudicante_nipc
    ON procurement.contracts (adjudicante_nipc);

CREATE INDEX IF NOT EXISTS idx_contracts_ano
    ON procurement.contracts (ano);

CREATE INDEX IF NOT EXISTS idx_contracts_cpv
    ON procurement.contracts (cpv);

CREATE INDEX IF NOT EXISTS idx_contracts_is_municipio
    ON procurement.contracts (is_municipio) WHERE is_municipio;

CREATE INDEX IF NOT EXISTS idx_contracts_tipo_procedimento
    ON procurement.contracts (tipo_procedimento);

CREATE INDEX IF NOT EXISTS idx_contracts_data_celebracao_material
    ON procurement.contracts ((data_celebracao::timestamp)) WHERE NOT is_below_min_value;

CREATE INDEX IF NOT EXISTS idx_contract_adjudicatarios_entity_id
    ON procurement.contract_adjudicatarios (entity_id);
