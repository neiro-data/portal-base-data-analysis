"""Loader honours the row cap and fails loudly on unexpected columns."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from base_gov.xlsx_ingest import DEFAULT_ROWS, load_contracts

FIXTURE_ROWS = [
    {
        "idcontrato": i,
        "adjudicante": "506609553 - Município de Montemor-o-Novo",
        "adjudicatarios": "517304660 - Sítio dos Arados Planeamento e Projetos Lda",
        "precoContratual": 1000.0 + i,
        "tipoContrato": "Aquisição de serviços",
    }
    for i in range(10)
]


def _write_fixture(tmp_path: Path, rows: list[dict[str, object]] | None = None) -> Path:
    df = pl.DataFrame(rows if rows is not None else FIXTURE_ROWS)
    path = tmp_path / "contratos.xlsx"
    df.write_excel(path)
    return path


def test_default_rows_is_1000() -> None:
    assert DEFAULT_ROWS == 1000


def test_load_contracts_honours_row_cap(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    df = load_contracts(path, rows=3)
    assert df.height == 3


def test_load_contracts_reads_expected_columns(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    df = load_contracts(path)
    assert df.height == len(FIXTURE_ROWS)
    for col in ("idcontrato", "adjudicante", "adjudicatarios", "precoContratual", "tipoContrato"):
        assert col in df.columns


def test_load_contracts_raises_on_missing_columns(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path, rows=[{"foo": 1, "bar": 2}])
    with pytest.raises(ValueError, match="missing"):
        load_contracts(path)
