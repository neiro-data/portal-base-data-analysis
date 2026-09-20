"""Verbatim `data/source/contratos<YEAR>.xlsx` -> `data/raw/contracts_raw_<YEAR>.parquet`.

This is the one place in the package sanctioned to read a contracts workbook in full — everywhere
else goes through `xlsx_ingest.load_contracts`, which always caps rows. Columns are written exactly
as `pl.read_excel` returns them, with no additions: provenance lives in `data/manifest.json`
and the filename year.

`pl.read_excel` (calamine) materialises the whole sheet (2-4 GB RSS for a 66 MB workbook), so
`ingest_years` handles one workbook at a time and drops it before the next.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from pathlib import Path

import polars as pl

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
_RAW_RE = re.compile(r"^contracts_raw_(\d{4})\.parquet$")


def source_path(source_dir: Path, year: int) -> Path:
    return source_dir / f"contratos{year}.xlsx"


def raw_path(raw_dir: Path, year: int) -> Path:
    return raw_dir / f"contracts_raw_{year}.parquet"


def raw_years(raw_dir: Path) -> list[int]:
    """Years that have a raw parquet in `raw_dir`, ascending."""
    if not raw_dir.exists():
        return []
    found = (_RAW_RE.match(p.name) for p in raw_dir.iterdir())
    return sorted(int(m.group(1)) for m in found if m)


def load_workbook(path: Path) -> pl.DataFrame:
    """Read every row of a contracts workbook, no cap.

    Raises `ValueError` if a required column is missing, or if any of the typed-core columns
    (`idcontrato`, `precoContratual`, `dataCelebracaoContrato`) arrived as `String` — a silent
    string date or price would poison every later time-series.
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


def ingest_year(source_dir: Path, raw_dir: Path, year: int) -> int:
    """Convert one year's workbook to its raw parquet; returns the row count read."""
    src = source_path(source_dir, year)
    if not src.exists():
        raise FileNotFoundError(f"no workbook for {year}: {src}")
    start = time.monotonic()
    df = load_workbook(src)
    rows = df.height
    raw_dir.mkdir(parents=True, exist_ok=True)
    df.write_parquet(raw_path(raw_dir, year))
    del df
    logger.info("raw %s: %s rows from %s (%.1fs)", year, rows, src, time.monotonic() - start)
    return rows


def ingest_years(source_dir: Path, raw_dir: Path, years: Iterable[int]) -> dict[int, int]:
    """Raw-ingest each year sequentially (one workbook in memory at a time)."""
    return {year: ingest_year(source_dir, raw_dir, year) for year in years}


def scan_raw(raw_dir: Path, years: Iterable[int] | None = None) -> pl.LazyFrame:
    """Lazy concat of the raw parquets; dtypes are relaxed to a common supertype across years."""
    selected = sorted(years) if years is not None else raw_years(raw_dir)
    if not selected:
        raise FileNotFoundError(f"no raw parquet files in {raw_dir}")
    return pl.concat(
        [pl.scan_parquet(raw_path(raw_dir, y)) for y in selected], how="diagonal_relaxed"
    )
