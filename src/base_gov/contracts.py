"""Shared loading and filters for the contracts parquet used by the analysis notebooks."""

from pathlib import Path

import polars as pl

from base_gov.entities import entity_key, normalize_name, split_packed
from base_gov.stats import value_per_day


def load_contracts(path: Path) -> pl.DataFrame:
    """Read the contracts parquet, keying ``adjudicante`` by NIPC and adding ``valor_por_dia``.

    Columns are renamed to snake_case; ``concorrentes`` and ``TipoCriterioAdjudicacao`` keep
    their source names. No value filter is applied (see ``stats.filter_min_value``).
    """
    raw = pl.read_parquet(path)
    keyed = entity_key(
        raw.with_columns(split_packed("adjudicante", prefix="adjudicante")), prefix="adjudicante"
    )
    return keyed.select(
        pl.col("idcontrato"),
        pl.col("adjudicante"),
        pl.col("adjudicante_nipc"),
        pl.col("adjudicante_name"),
        pl.col("adjudicante_key"),
        pl.col("adjudicante_key_source"),
        pl.col("adjudicatarios"),
        pl.col("objectoContrato").alias("objecto_contrato"),
        pl.col("precoContratual").alias("preco_contratual"),
        pl.col("prazoExecucao").alias("prazo_execucao"),
        pl.col("dataCelebracaoContrato").alias("data_celebracao"),
        pl.col("CPV").alias("cpv"),
        pl.col("NUTs").alias("nuts"),
        pl.col("Ano").alias("ano"),
        pl.col("tipoContrato").alias("tipo_contrato"),
        pl.col("tipoprocedimento").alias("tipo_procedimento"),
        pl.col("fundamentAjusteDireto").alias("fundament_ajuste_direto"),
        pl.col("concorrentes"),
        pl.col("TipoCriterioAdjudicacao"),
    ).with_columns(value_per_day())


def is_municipio_expr() -> pl.Expr:
    """Accent-folded, lowercased 'municipio' prefix on the adjudicante name."""
    return (
        pl.col("adjudicante_name")
        .map_elements(normalize_name, return_dtype=pl.String)
        .str.starts_with("municipio")
    )
