"""Raw contracts -> treated contracts. `TREATED_COLUMNS` is the single source of truth for the
treated schema (`data/contracts.parquet`).

`treat` produces everything except the two lifecycle timestamps, which `bulk_ingest` stamps when
it merges against the previous treated file. It never drops a row: the minimum-value rule is the
`is_below_min_value` flag.
"""

from __future__ import annotations

import polars as pl

from base_gov.contracts import is_municipio_expr
from base_gov.entities import entity_key, split_packed
from base_gov.stats import MIN_VALUE, below_min_value_expr, value_per_day

# `.hash()` is stable for a given polars version; an upgrade re-hashes every row once, which the
# ingest summary reports as `updated`.
HASH_EXCLUDE = frozenset({"row_hash", "first_ingested_at", "updated_at", "is_below_min_value"})

TREATED_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "idcontrato": pl.Int64,
    "ano": pl.Int32,
    "adjudicante": pl.String,
    "adjudicante_nipc": pl.String,
    "adjudicante_name": pl.String,
    "adjudicante_key": pl.String,
    "adjudicante_key_source": pl.String,
    "adjudicatarios": pl.String,
    "objecto_contrato": pl.String,
    "desc_contrato": pl.String,
    "preco_contratual": pl.Float64,
    "preco_total_efetivo": pl.Float64,
    "preco_base_procedimento": pl.Float64,
    "prazo_execucao": pl.Int64,
    "valor_por_dia": pl.Float64,
    "data_celebracao": pl.Date,
    "data_publicacao": pl.Date,
    "data_fecho": pl.Date,
    "tipo_contrato": pl.String,
    "tipo_procedimento": pl.String,
    "fundament_ajuste_direto": pl.String,
    "cpv": pl.String,
    "nuts": pl.String,
    "concorrentes": pl.String,
    "TipoCriterioAdjudicacao": pl.String,
    "n_concorrentes": pl.UInt32,
    "is_municipio": pl.Boolean,
    "is_below_min_value": pl.Boolean,
    "row_hash": pl.Int64,
}
_BODY_COLUMNS = [c for c in TREATED_SCHEMA if c != "row_hash"]
TIMESTAMP_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "first_ingested_at": pl.Datetime("us", "UTC"),
    "updated_at": pl.Datetime("us", "UTC"),
}
TREATED_COLUMNS = [*TREATED_SCHEMA, *TIMESTAMP_SCHEMA]

# treated name <- source name, for the columns that are a plain rename + cast.
_RENAMES = {
    "idcontrato": "idcontrato",
    "ano": "Ano",
    "adjudicante": "adjudicante",
    "adjudicatarios": "adjudicatarios",
    "objecto_contrato": "objectoContrato",
    "desc_contrato": "descContrato",
    "preco_contratual": "precoContratual",
    "preco_total_efetivo": "PrecoTotalEfetivo",
    "preco_base_procedimento": "precoBaseProcedimento",
    "prazo_execucao": "prazoExecucao",
    "data_celebracao": "dataCelebracaoContrato",
    "data_publicacao": "dataPublicacao",
    "data_fecho": "dataFechoContrato",
    "tipo_contrato": "tipoContrato",
    "tipo_procedimento": "tipoprocedimento",
    "fundament_ajuste_direto": "fundamentAjusteDireto",
    "cpv": "CPV",
    "nuts": "NUTs",
    "concorrentes": "concorrentes",
    "TipoCriterioAdjudicacao": "TipoCriterioAdjudicacao",
}


def row_hash(columns: list[str]) -> pl.Expr:
    """64-bit hash of the treated content columns (sorted), nulls distinct from empty strings."""
    cols = sorted(c for c in columns if c not in HASH_EXCLUDE)
    return (
        pl.concat_str([pl.col(c).cast(pl.String).fill_null("\x00") for c in cols], separator="\x1f")
        .hash()
        .reinterpret(signed=True)  # u64 -> i64 bit pattern; postgres bigint is signed
        .alias("row_hash")
    )


def treat[F: (pl.DataFrame, pl.LazyFrame)](raw: F, *, min_value: float = MIN_VALUE) -> F:
    """Project raw contracts to the treated schema (minus the lifecycle timestamps).

    Lazy in, lazy out; a `DataFrame` in gives a `DataFrame` out. Only the distinct no-NIF
    adjudicante names are collected (for the fuzzy clustering).
    """
    lf = raw.lazy()
    have = lf.collect_schema().names()
    missing = [src for src in _RENAMES.values() if src not in have]
    if missing:
        raise ValueError(f"raw contracts lack columns {missing}; observed: {have}")

    typed = lf.select(
        *(pl.col(src).cast(TREATED_SCHEMA[dst]).alias(dst) for dst, src in _RENAMES.items())
    )
    keyed = entity_key(
        typed.with_columns(split_packed("adjudicante", prefix="adjudicante")), prefix="adjudicante"
    )
    concorrentes = (
        pl.col("concorrentes")
        .str.split("\n")
        .list.eval(pl.element().str.strip_chars().filter(pl.element() != ""))
        .list.len()
    )
    body = keyed.with_columns(
        value_per_day(),
        concorrentes.alias("n_concorrentes"),
        is_municipio_expr().alias("is_municipio"),
        below_min_value_expr("preco_contratual", min_value).alias("is_below_min_value"),
    ).select(*_BODY_COLUMNS)
    hashed = body.with_columns(row_hash(body.collect_schema().names()))
    out = hashed.select(*TREATED_SCHEMA)
    return out.collect() if isinstance(raw, pl.DataFrame) else out
