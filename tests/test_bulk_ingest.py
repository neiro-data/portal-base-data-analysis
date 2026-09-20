"""Orchestration: raw -> treated, change tracking, summary arithmetic, idempotence, rebuild."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import polars as pl
import pytest

from base_gov import bulk_ingest, raw_ingest
from base_gov.transform import TREATED_COLUMNS
from tests.fixtures import make_rows, write_workbook

T0 = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
T1 = dt.datetime(2026, 9, 20, tzinfo=dt.UTC)


class Env:
    def __init__(self, root: Path) -> None:
        self.source, self.raw = root / "source", root / "raw"
        self.out = root / "contracts.parquet"
        self.adjudicantes = root / "adjudicantes.parquet"
        self.adjudicatarios = root / "adjudicatarios.parquet"

    def run(
        self,
        years: tuple[int, ...] = (),
        *,
        at: dt.datetime | None = None,
        min_value: float = 10.0,
        rebuild: bool = False,
    ) -> bulk_ingest.IngestSummary:
        return bulk_ingest.ingest(
            self.source,
            self.raw,
            self.out,
            self.adjudicantes,
            self.adjudicatarios,
            years=years,
            ingested_at=at,
            min_value=min_value,
            rebuild=rebuild,
        )

    def treated(self) -> pl.DataFrame:
        return pl.read_parquet(self.out)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    e = Env(tmp_path)
    write_workbook(e.source, 2024, make_rows(2024, 4, start_id=100, price=5.0))  # 5,6,7,8 < €10
    write_workbook(e.source, 2025, make_rows(2025, 6, start_id=200))
    return e


def test_treated_schema_and_row_count_equal_raw(env: Env) -> None:
    s = env.run((2024, 2025), at=T0)
    df = env.treated()
    assert df.columns == TREATED_COLUMNS
    assert df.height == s.read == s.ingested == 10
    assert (df["first_ingested_at"] == T0).all() and (df["updated_at"] == T0).all()
    assert df["is_below_min_value"].sum() == 4  # flagged, never dropped


def test_summary_arithmetic(env: Env) -> None:
    s = env.run((2024, 2025), at=T0)
    assert [y.year for y in s.per_year] == [2024, 2025]
    for y in (*s.per_year,):
        assert y.new + y.updated + y.unchanged == y.ingested == y.read
        assert y.below_min <= y.ingested
    assert s.new + s.updated + s.unchanged == s.ingested == s.read
    assert (s.per_year[0].below_min, s.per_year[1].below_min) == (4, 0)
    assert "total" in s.render() and "below_min(<€10)" in s.render()


def test_ingested_at_stamps_given_value_not_now(env: Env) -> None:
    env.run((2025,), at=T0)
    assert env.treated()["first_ingested_at"].unique().to_list() == [T0]


def test_rerun_is_idempotent(env: Env) -> None:
    env.run((2024, 2025), at=T0)
    before = env.treated()
    s = env.run((2024, 2025), at=T1)
    after = env.treated()
    assert (s.new, s.updated, s.unchanged) == (0, 0, 10)
    assert after.height == before.height
    assert after.sort("idcontrato").equals(before.sort("idcontrato"))


def test_merge_new_updated_unchanged(env: Env) -> None:
    env.run((2025,), at=T0)
    changed = make_rows(
        2025, 6, start_id=200, objectoContrato=["Serviços diversos"] * 5 + ["Alterado"]
    )
    extra = make_rows(2025, 1, start_id=300)
    write_workbook(env.source, 2025, pl.concat([changed, extra]))
    s = env.run((2025,), at=T1)
    assert (s.new, s.updated, s.unchanged) == (1, 1, 5)

    df = env.treated().sort("idcontrato").with_row_index()
    by_id = {r["idcontrato"]: r for r in df.iter_rows(named=True)}
    assert by_id[200]["updated_at"] == T0  # unchanged: both carried over
    assert (by_id[205]["first_ingested_at"], by_id[205]["updated_at"]) == (T0, T1)  # updated
    assert (by_id[300]["first_ingested_at"], by_id[300]["updated_at"]) == (T1, T1)  # new


def test_min_value_change_does_not_touch_hash_or_updated_at(env: Env) -> None:
    env.run((2024, 2025), at=T0)
    before = env.treated().sort("idcontrato")
    s = env.run((2024, 2025), at=T1, min_value=0.0)
    after = env.treated().sort("idcontrato")
    assert (s.new, s.updated) == (0, 0) and s.below_min == 0
    assert after["is_below_min_value"].sum() == 0
    assert after["row_hash"].equals(before["row_hash"])
    assert after["updated_at"].equals(before["updated_at"])


def test_rebuild_from_raw_without_xlsx_stamps_ingested_at(env: Env) -> None:
    env.run((2024, 2025), at=T1)
    for xlsx in env.source.glob("*.xlsx"):
        xlsx.unlink()
    s = env.run(at=T0, rebuild=True)
    assert s.new == s.ingested == 10
    assert (env.treated()["first_ingested_at"] == T0).all()


def test_treated_reprocesses_all_raw_years_even_if_one_is_requested(env: Env) -> None:
    env.run((2024, 2025), at=T0)
    s = env.run((2025,), at=T1)
    assert [y.year for y in s.per_year] == [2024, 2025]
    assert env.treated().height == 10


def test_previous_file_without_tracking_columns_is_treated_as_new(env: Env) -> None:
    pl.DataFrame({"idcontrato": [200], "x": [1]}).write_parquet(env.out)
    s = env.run((2025,), at=T0)
    assert s.new == 6


def test_entities_written_and_manifest_records_min_value(env: Env) -> None:
    s = env.run((2025,), at=T0, min_value=25.0)
    assert s.adjudicantes_total >= 1 and s.adjudicatarios_total >= 1
    assert raw_ingest.raw_years(env.raw) == [2025]
    manifest = (env.out.parent / "manifest.json").read_text()
    assert '"min_value": 25.0' in manifest
