"""Raw ingest is verbatim: the parquet columns are exactly the workbook's."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from base_gov import raw_ingest
from tests.fixtures import HEADER, make_rows, write_workbook


def test_raw_columns_equal_workbook_columns(tmp_path: Path) -> None:
    write_workbook(tmp_path / "source", 2025, make_rows(2025, 3))
    rows = raw_ingest.ingest_year(tmp_path / "source", tmp_path / "raw", 2025)
    raw = pl.read_parquet(raw_ingest.raw_path(tmp_path / "raw", 2025))
    assert rows == raw.height == 3
    assert raw.columns == list(HEADER)


def test_ingest_years_writes_one_parquet_per_year(tmp_path: Path) -> None:
    for year in (2024, 2025):
        write_workbook(tmp_path / "source", year, make_rows(year, 2, start_id=year * 10))
    counts = raw_ingest.ingest_years(tmp_path / "source", tmp_path / "raw", [2024, 2025])
    assert counts == {2024: 2, 2025: 2}
    assert raw_ingest.raw_years(tmp_path / "raw") == [2024, 2025]


def test_missing_workbook_and_missing_columns_fail_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        raw_ingest.ingest_year(tmp_path, tmp_path / "raw", 2030)
    write_workbook(tmp_path, 2031, pl.DataFrame({"foo": [1], "bar": [2]}))
    with pytest.raises(ValueError, match="missing"):
        raw_ingest.ingest_year(tmp_path, tmp_path / "raw", 2031)


def test_scan_raw_without_files_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        raw_ingest.scan_raw(tmp_path / "nope")
