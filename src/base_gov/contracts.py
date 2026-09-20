"""Shared loading and filters for the treated contracts parquet used by the analysis notebooks."""

from pathlib import Path

import polars as pl

from base_gov.entities import normalize_name


def load_contracts(path: Path) -> pl.DataFrame:
    """Read the treated contracts parquet written by ``bulk-ingest`` (see ``transform``).

    No value filter is applied: rows below the minimum value carry ``is_below_min_value``, so a
    notebook filters with ``contracts.filter(~pl.col("is_below_min_value"))``.
    """
    return pl.read_parquet(path)


def is_municipio_expr() -> pl.Expr:
    """Accent-folded, lowercased 'municipio' prefix on the adjudicante name."""
    return (
        pl.col("adjudicante_name")
        .map_elements(normalize_name, return_dtype=pl.String)
        .str.starts_with("municipio")
        .fill_null(False)
    )
