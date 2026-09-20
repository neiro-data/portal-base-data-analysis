-- Idempotent: safe to re-run against a warm volume (guarded rename, ADD COLUMN
-- IF NOT EXISTS, CREATE INDEX IF NOT EXISTS).
--
-- Ingestion metadata and treated-frame columns on procurement.contracts. Values come
-- from the treated parquet, so the database no longer invents timestamps.
-- 002_schema.sql applies the same statements so 005's views resolve on a warm volume.
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

CREATE INDEX IF NOT EXISTS idx_contracts_is_municipio
    ON procurement.contracts (is_municipio) WHERE is_municipio;

CREATE INDEX IF NOT EXISTS idx_contracts_tipo_procedimento
    ON procurement.contracts (tipo_procedimento);

CREATE INDEX IF NOT EXISTS idx_contracts_data_celebracao_material
    ON procurement.contracts ((data_celebracao::timestamp)) WHERE NOT is_below_min_value;
