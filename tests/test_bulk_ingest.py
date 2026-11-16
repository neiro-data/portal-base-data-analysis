"""Orchestration: raw -> treated, change tracking, summary arithmetic, idempotence, rebuild."""

from __future__ import annotations

import datetime as dt
import json
import logging
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
        assert y.superseded == 0
        assert y.below_min <= y.ingested
    assert s.new + s.updated + s.unchanged == s.ingested == s.read
    assert (s.per_year[0].below_min, s.per_year[1].below_min) == (4, 0)
    assert "total" in s.render() and "below_min(<€10)" in s.render()


def test_latest_year_wins_and_superseded_rows_are_reported(env: Env) -> None:
    # 2026 re-publishes ids 100 and 101 (already in 2024) with a different price.
    dup = make_rows(2026, 2, start_id=100, price=9999.0)
    write_workbook(env.source, 2026, dup)
    s = env.run((2024, 2025, 2026), at=T0)
    by_year = {y.year: y for y in s.per_year}
    assert by_year[2024].superseded == 2 and by_year[2024].ingested == 2
    assert by_year[2026].superseded == 0 and by_year[2026].ingested == 2
    assert s.read == 12 and s.ingested == 10 and s.superseded == 2
    df = env.treated().filter(pl.col("idcontrato").is_in([100, 101])).sort("idcontrato")
    assert df["preco_contratual"].to_list() == [9999.0, 10000.0]  # the 2026 rows won
    assert "superseded" in s.render()


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


def test_kv_format_ingest_parses_adjudicante_and_entities(tmp_path: Path) -> None:
    e = Env(tmp_path)
    write_workbook(e.source, 2025, make_rows(2025, 3, party_format="kv"))
    e.run((2025,), at=T0)
    assert e.treated()["adjudicante_name"].to_list() == ["Município de Montemor-o-Novo"] * 3
    ents = pl.read_parquet(e.adjudicantes)
    assert ents["entity_id"].to_list() == ["506609553"]


def test_drift_guard_raises_and_keeps_previous_output(tmp_path: Path) -> None:
    e = Env(tmp_path)
    write_workbook(e.source, 2025, make_rows(2025, 3))
    e.run((2025,), at=T0)
    before = e.out.read_bytes()
    write_workbook(e.source, 2025, make_rows(2025, 3, adjudicante="foo=bar|x=y"))
    # "foo=bar|x=y" has no " - ", so name is null
    with pytest.raises(ValueError, match="2025"):
        e.run((2025,), at=T1)
    assert e.out.read_bytes() == before


def test_check_adjudicante_parse_threshold() -> None:
    ok = pl.DataFrame({"_src_year": [2024], "ingested": [100], "adj_unparsed": [1]})
    bulk_ingest.check_adjudicante_parse(ok)
    bad = pl.DataFrame({"_src_year": [2024], "ingested": [100], "adj_unparsed": [2]})
    with pytest.raises(ValueError, match="2024"):
        bulk_ingest.check_adjudicante_parse(bad)


def _manifest(env: Env) -> dict:
    return json.loads((env.out.parent / "manifest.json").read_text())


def test_drift_in_summary_manifest_and_log(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    frame = make_rows(2025, 6, start_id=200).drop("concorrentes")
    write_workbook(env.source, 2025, frame.with_columns(novaColunaA=pl.lit("x")))
    with caplog.at_level(logging.WARNING, logger="base_gov"):
        s = env.run((2025,), at=T0)
    assert s.drift[2025].missing_optional == ("concorrentes",)
    assert s.drift[2025].unknown == ("novaColunaA",)
    out = s.render()
    assert "2025  missing optional (null-filled): concorrentes" in out
    assert "2025  unknown upstream columns: novaColunaA" in out
    assert _manifest(env)["treated"]["schema_drift"]["2025"] == {
        "missing_optional": ["concorrentes"],
        "unknown": ["novaColunaA"],
    }
    assert "raw year 2025: optional source columns missing" in caplog.text
    assert "raw year 2025: upstream columns not consumed" in caplog.text


def test_no_drift_renders_no_drift_section(env: Env) -> None:
    s = env.run((2024, 2025), at=T0)
    assert "schema drift" not in s.render()
    assert _manifest(env)["treated"]["schema_drift"] == {
        "2024": {"missing_optional": [], "unknown": []},
        "2025": {"missing_optional": [], "unknown": []},
    }


def test_required_missing_in_one_year_raises(env: Env) -> None:
    env.run((2024, 2025), at=T0)
    pl.read_parquet(raw_ingest.raw_path(env.raw, 2024)).drop("precoContratual").write_parquet(
        raw_ingest.raw_path(env.raw, 2024)
    )
    with pytest.raises(ValueError, match="raw year 2024"):
        env.run(at=T1, rebuild=True)


def test_null_filled_optional_marks_rows_updated_once(env: Env) -> None:
    write_workbook(env.source, 2025, make_rows(2025, 6, start_id=200))
    env.run((2025,), at=T0)
    write_workbook(env.source, 2025, make_rows(2025, 6, start_id=200).drop("concorrentes"))
    assert env.run((2025,), at=T1).updated == 6
    assert env.run((2025,), at=T1).unchanged == 6


def test_upstream_only_touch_marks_rows_updated(env: Env) -> None:
    env.run((2025,), at=T0)
    touched = make_rows(
        2025, 6, start_id=200, ultActuliza=[dt.date(2026, 11, 1)] * 2 + [dt.date(2025, 1, 3)] * 4
    )
    write_workbook(env.source, 2025, touched)
    s = env.run((2025,), at=T1)
    assert (s.new, s.updated, s.unchanged) == (0, 2, 4)
