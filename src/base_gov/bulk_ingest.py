"""Uncapped bulk ingest of `contratos2025.xlsx` into `data/contracts.parquet`.

This is the one place in the package sanctioned to read the contracts
workbook in full — everywhere else goes through `xlsx_ingest.load_contracts`,
which always caps rows. `pl.read_excel` here has no `n_rows` cap, matching
the file's actual row count.

Contracts are upserted on `idcontrato` with `keep="last"`: unlike entities
(whose identity is stable, so `keep="first"` in `entities_db`), contracts are
amended over time (fields like `dataFechoContrato` fill in after signing), so
the most recently ingested version of a row is the truth.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from base_gov import entities_db
from base_gov.xlsx_ingest import (
    ADJUDICANTE,
    ADJUDICATARIOS,
    CONTRACT_ID,
    CONTRACT_TYPE,
    CPV,
    DESCRICAO,
    NUTS,
    OBJECTO,
    PUBLISHED_DATE,
    SIGNED_DATE,
    VALUE_EUR,
    YEAR,
)

logger = logging.getLogger("base_gov")

BULK_REQUIRED_COLUMNS = (
    CONTRACT_ID,
    ADJUDICANTE,
    ADJUDICATARIOS,
    OBJECTO,
    DESCRICAO,
    VALUE_EUR,
    SIGNED_DATE,
    PUBLISHED_DATE,
    CONTRACT_TYPE,
    CPV,
    NUTS,
    YEAR,
)

_TYPED_CORE = (CONTRACT_ID, VALUE_EUR, SIGNED_DATE)


@dataclass(frozen=True)
class BulkIngestResult:
    rows_read: int
    contracts_total: int
    adjudicantes_total: int
    adjudicatarios_total: int


def load_all_contracts(path: Path) -> pl.DataFrame:
    """Read every row of the contracts workbook, no cap.

    Raises `ValueError` if a required column is missing, or if any of the
    typed-core columns (`idcontrato`, `precoContratual`,
    `dataCelebracaoContrato`) arrived as `String` — a silent string date or
    price would poison every later time-series.
    """
    df = pl.read_excel(path)
    missing = [c for c in BULK_REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"expected columns {list(BULK_REQUIRED_COLUMNS)} not found in {path}; "
            f"missing {missing}; observed columns: {df.columns}"
        )
    stringly = [c for c in _TYPED_CORE if df.schema[c] == pl.String]
    if stringly:
        raise ValueError(
            f"columns {stringly} were inferred as String in {path}; expected numeric/date dtypes"
        )
    return df


def read_contracts(path: Path) -> pl.DataFrame:
    if not path.exists():
        return pl.DataFrame()
    return pl.read_parquet(path)


def write_contracts(df: pl.DataFrame, path: Path, *, force: bool = False) -> int:
    """Upsert `df` into `path` on `idcontrato`, keeping the newest row.

    With `force=True`, `df` replaces whatever is on disk instead of merging
    with it. Otherwise, if an existing parquet's schema differs from `df`'s,
    raises `ValueError` telling the caller to delete `path` and re-run rather
    than silently half-merging.

    Rows with a null `idcontrato` are not deduped against each other by id
    (there's nothing to key on), but exact duplicate rows are still collapsed
    so re-running against an unchanged workbook doesn't grow the file forever.
    """
    if force or not path.exists():
        combined = df
    else:
        existing = read_contracts(path)
        if existing.columns and existing.columns != df.columns:
            raise ValueError(
                f"schema of existing {path} ({existing.columns}) differs from the incoming "
                f"frame ({df.columns}); delete {path} and re-run to rebuild it from scratch"
            )
        combined = pl.concat([existing, df], how="vertical" if existing.columns else "diagonal")

    with_id = combined.filter(pl.col(CONTRACT_ID).is_not_null()).unique(
        subset=CONTRACT_ID, keep="last"
    )
    without_id = combined.filter(pl.col(CONTRACT_ID).is_null()).unique()
    merged = pl.concat([with_id, without_id])

    path.parent.mkdir(parents=True, exist_ok=True)
    merged.write_parquet(path)
    return merged.height


def ingest(
    xlsx: Path,
    out: Path,
    adjudicantes_out: Path,
    adjudicatarios_out: Path,
    *,
    force: bool = False,
) -> BulkIngestResult:
    """Load the full workbook and upsert contracts + both entity roles."""
    start = time.monotonic()
    df = load_all_contracts(xlsx)
    logger.info("read %s rows from %s (%.1fs)", df.height, xlsx, time.monotonic() - start)

    contracts_total = write_contracts(df, out, force=force)
    logger.info("upserted %s contracts into %s", contracts_total, out)

    adjudicantes_total = entities_db.write_entities(entities_db.adjudicantes(df), adjudicantes_out)
    logger.info("upserted %s adjudicantes into %s", adjudicantes_total, adjudicantes_out)

    adjudicatarios_total = entities_db.write_entities(
        entities_db.adjudicatarios(df), adjudicatarios_out
    )
    logger.info("upserted %s adjudicatarios into %s", adjudicatarios_total, adjudicatarios_out)

    logger.info("ingest complete in %.1fs", time.monotonic() - start)
    return BulkIngestResult(
        rows_read=df.height,
        contracts_total=contracts_total,
        adjudicantes_total=adjudicantes_total,
        adjudicatarios_total=adjudicatarios_total,
    )
