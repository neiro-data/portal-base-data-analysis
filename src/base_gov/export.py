"""CSV output for scraped records."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import polars as pl

from base_gov.models import Contract, Entity

ENTITY_COLUMNS = ["id", "nif", "description", "country"]
CONTRACT_COLUMNS = [
    "id",
    "publication_date",
    "contracting",
    "contracted",
    "object_description",
    "procedure_type",
    "initial_price",
]


def entities_frame(entities: Iterable[Entity]) -> pl.DataFrame:
    return _frame([asdict(e) for e in entities], ENTITY_COLUMNS)


def contracts_frame(contracts: Iterable[Contract]) -> pl.DataFrame:
    return _frame([asdict(c) for c in contracts], CONTRACT_COLUMNS)


def _frame(rows: list[dict[str, Any]], columns: list[str]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema={name: pl.String for name in columns})
    return pl.DataFrame(rows).select(columns)


def write_csv(frame: pl.DataFrame, destination: Path) -> Path:
    """Write `frame` to `destination`, creating parent directories as needed."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.write_csv(destination)
    return destination
