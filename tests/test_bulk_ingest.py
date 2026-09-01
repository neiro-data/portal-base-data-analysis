"""Bulk ingest reads the workbook uncapped and upserts contracts + entities."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import pytest

from base_gov import bulk_ingest, entities_db, xlsx_ingest

FIXTURE_ROWS = [
    {
        "idcontrato": i,
        "adjudicante": "506609553 - Município de Montemor-o-Novo",
        "adjudicatarios": "517304660 - Sítio dos Arados Planeamento e Projetos Lda",
        "objectoContrato": "Serviços diversos",
        "descContrato": "Descrição do contrato",
        "precoContratual": 1000.0 + i,
        "dataCelebracaoContrato": dt.date(2025, 1, 1),
        "dataPublicacao": dt.date(2025, 1, 2),
        "tipoContrato": "Aquisição de serviços",
        "CPV": "79000000",
        "NUTs": "PT18",
        "Ano": 2025,
    }
    for i in range(5)
]


def _write_fixture(tmp_path: Path, rows: list[dict[str, object]] | None = None) -> Path:
    df = pl.DataFrame(rows if rows is not None else FIXTURE_ROWS)
    path = tmp_path / "contratos.xlsx"
    df.write_excel(path)
    return path


def test_load_all_contracts_ignores_row_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(xlsx_ingest, "DEFAULT_ROWS", 2)
    path = _write_fixture(tmp_path)
    df = bulk_ingest.load_all_contracts(path)
    assert df.height == len(FIXTURE_ROWS)


def test_load_all_contracts_raises_on_missing_columns(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path, rows=[{"foo": 1, "bar": 2}])
    with pytest.raises(ValueError, match="missing"):
        bulk_ingest.load_all_contracts(path)


def test_write_contracts_upsert_keeps_last(tmp_path: Path) -> None:
    out = tmp_path / "contracts.parquet"
    first = pl.DataFrame(FIXTURE_ROWS[:1])
    bulk_ingest.write_contracts(first, out)

    updated_row = dict(FIXTURE_ROWS[0])
    updated_row["precoContratual"] = 9999.0
    second = pl.DataFrame([updated_row])
    height = bulk_ingest.write_contracts(second, out)

    assert height == 1
    result = pl.read_parquet(out)
    assert result.height == 1
    assert result["precoContratual"][0] == 9999.0


def test_write_contracts_force_replaces(tmp_path: Path) -> None:
    out = tmp_path / "contracts.parquet"
    bulk_ingest.write_contracts(pl.DataFrame(FIXTURE_ROWS), out)
    height = bulk_ingest.write_contracts(pl.DataFrame(FIXTURE_ROWS[:1]), out, force=True)
    assert height == 1
    assert pl.read_parquet(out).height == 1


def test_write_contracts_dedups_duplicate_ids_on_fresh_and_forced_writes(
    tmp_path: Path,
) -> None:
    out = tmp_path / "contracts.parquet"
    dupes = pl.DataFrame([FIXTURE_ROWS[0], FIXTURE_ROWS[0]])

    height = bulk_ingest.write_contracts(dupes, out)
    assert height == 1
    assert pl.read_parquet(out).height == 1

    height = bulk_ingest.write_contracts(dupes, out, force=True)
    assert height == 1
    assert pl.read_parquet(out).height == 1


def test_write_contracts_null_idcontrato_rows_do_not_accumulate_on_rerun(
    tmp_path: Path,
) -> None:
    out = tmp_path / "contracts.parquet"
    row = dict(FIXTURE_ROWS[0])
    row["idcontrato"] = None
    null_id_frame = pl.DataFrame([row])

    bulk_ingest.write_contracts(null_id_frame, out)
    height = bulk_ingest.write_contracts(null_id_frame, out)
    height = bulk_ingest.write_contracts(null_id_frame, out)

    assert height == 1
    assert pl.read_parquet(out).height == 1


def test_write_contracts_schema_mismatch_raises(tmp_path: Path) -> None:
    out = tmp_path / "contracts.parquet"
    bulk_ingest.write_contracts(pl.DataFrame(FIXTURE_ROWS), out)
    mismatched = pl.DataFrame([{"idcontrato": 999, "other_col": "x"}])
    with pytest.raises(ValueError, match="schema"):
        bulk_ingest.write_contracts(mismatched, out)


def test_ingest_extends_preseeded_entity_parquets(tmp_path: Path) -> None:
    xlsx = _write_fixture(tmp_path)
    out = tmp_path / "contracts.parquet"
    adjudicantes_out = tmp_path / "entities_adjudicantes.parquet"
    adjudicatarios_out = tmp_path / "entities_adjudicatarios.parquet"

    seed = pl.DataFrame(
        {"entity_id": ["999999999"], "name": ["Pre-existing Entity"]},
        schema=entities_db.ENTITY_SCHEMA,
    )
    entities_db.write_entities(seed, adjudicantes_out)
    entities_db.write_entities(seed, adjudicatarios_out)

    result = bulk_ingest.ingest(xlsx, out, adjudicantes_out, adjudicatarios_out)

    assert result.rows_read == len(FIXTURE_ROWS)
    assert pl.read_parquet(out).height == len(FIXTURE_ROWS)

    adjudicantes_ids = pl.read_parquet(adjudicantes_out)["entity_id"].to_list()
    adjudicatarios_ids = pl.read_parquet(adjudicatarios_out)["entity_id"].to_list()
    assert "999999999" in adjudicantes_ids
    assert "999999999" in adjudicatarios_ids
    assert result.adjudicantes_total >= 2
    assert result.adjudicatarios_total >= 2
