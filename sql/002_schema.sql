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
    ingested_at          timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);

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

CREATE INDEX IF NOT EXISTS idx_contract_adjudicatarios_entity_id
    ON procurement.contract_adjudicatarios (entity_id);
