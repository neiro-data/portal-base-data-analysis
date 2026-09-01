"""Persist deduplicated adjudicante/adjudicatário entities to parquet.

Two files, one schema each (`entity_id`, `name`) — an entity present under
both roles simply appears in both files, no cross-file dedup. Writing is an
upsert: an existing file is read back, concatenated with the new rows, and
deduplicated again, so re-running stays idempotent.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from base_gov.analysis import parse_entity_column
from base_gov.xlsx_ingest import ADJUDICANTE, ADJUDICATARIOS

ENTITY_SCHEMA = ["entity_id", "name"]


def _dedup(df: pl.DataFrame) -> pl.DataFrame:
    """One row per `entity_id`; rows with a null id fall back to dedup by
    `name` so entities missing a NIPC aren't dropped outright."""
    with_id = df.filter(pl.col("entity_id").is_not_null()).unique(subset="entity_id", keep="first")
    without_id = df.filter(pl.col("entity_id").is_null()).unique(subset="name", keep="first")
    return pl.concat([with_id, without_id])


def adjudicantes(df: pl.DataFrame) -> pl.DataFrame:
    return _dedup(parse_entity_column(df, ADJUDICANTE))


def adjudicatarios(df: pl.DataFrame) -> pl.DataFrame:
    return _dedup(parse_entity_column(df, ADJUDICATARIOS))


def read_entities(path: Path) -> pl.DataFrame:
    if not path.exists():
        return pl.DataFrame(schema={"entity_id": pl.String, "name": pl.String})
    return pl.read_parquet(path)


def write_entities(df: pl.DataFrame, path: Path) -> int:
    """Upsert `df` into `path`: merge with what's already there, dedup, write.

    Returns the resulting row count.
    """
    existing = read_entities(path)
    merged = _dedup(pl.concat([existing, df.select(ENTITY_SCHEMA)]))
    path.parent.mkdir(parents=True, exist_ok=True)
    merged.write_parquet(path)
    return merged.height
