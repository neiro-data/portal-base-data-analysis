"""Offline tests for `base-gov query-stats` (no live stack)."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import polars as pl
import pytest
from typer.testing import CliRunner

from base_gov import query_stats as qs
from base_gov.cli import app
from base_gov.report import TZ, ReportError

NOW = datetime(2026, 10, 4, 15, 30, tzinfo=TZ)


def _dash(*variables: dict[str, Any]) -> dict[str, Any]:
    return {"templating": {"list": list(variables)}}


MIN = {"name": "min_contract_value", "type": "custom", "current": {"value": "10"}}
CT = {
    "name": "contract_type",
    "type": "query",
    "multi": True,
    "includeAll": True,
    "allValue": "'__ALL__'",
    "current": {"value": ["$__all"]},
}
ADJ = {
    "name": "adjudicante",
    "type": "query",
    "multi": True,
    "includeAll": True,
    "allValue": "__ALL__",
    "current": {"value": "$__all"},
}
CID = {"name": "contract_id", "type": "textbox", "current": {"value": ""}}
DASH = _dash(MIN, CT, ADJ, CID)


def test_expand_plain() -> None:
    assert qs.expand_vars("x >= ${min_contract_value}", DASH, {}) == "x >= 10"


def test_expand_plain_chosen_overrides_default() -> None:
    assert qs.expand_vars("${min_contract_value}", DASH, {"min_contract_value": ["500"]}) == "500"


def test_expand_all_uses_all_value_verbatim() -> None:
    assert qs.expand_vars("${contract_type:sqlstring}", DASH, {}) == "'__ALL__'"
    assert qs.expand_vars("'${adjudicante:csv}'", DASH, {}) == "'__ALL__'"


def test_expand_all_text() -> None:
    assert qs.expand_vars("${adjudicante:text}", DASH, {}) == "All"


def test_expand_sqlstring_escapes_quotes_and_joins() -> None:
    out = qs.expand_vars("ARRAY[${contract_type:sqlstring}]", DASH, {"contract_type": ["a'b", "c"]})
    assert out == "ARRAY['a''b','c']"


def test_expand_multi_csv_and_text() -> None:
    chosen = {"adjudicante": ["1", "2"]}
    assert qs.expand_vars("${adjudicante:csv}", DASH, chosen) == "1,2"
    assert qs.expand_vars("${adjudicante:text}", DASH, chosen) == "1 + 2"


def test_expand_multi_plain_errors_but_single_is_raw() -> None:
    with pytest.raises(qs.QueryStatsError, match="plain"):
        qs.expand_vars("${adjudicante}", DASH, {"adjudicante": ["1", "2"]})
    assert qs.expand_vars("${adjudicante}", DASH, {"adjudicante": ["1"]}) == "1"


def test_expand_textbox_empty_sqlstring() -> None:
    assert qs.expand_vars("${contract_id:sqlstring} = ''", DASH, {}) == "'' = ''"


def test_expand_unknown_variable_and_format_raise() -> None:
    with pytest.raises(qs.QueryStatsError, match="unknown variable"):
        qs.expand_vars("${nope}", DASH, {})
    with pytest.raises(qs.QueryStatsError, match="unsupported format"):
        qs.expand_vars("${adjudicante:json}", DASH, {})
    with pytest.raises(qs.QueryStatsError):
        qs.expand_vars("${__from:date}", DASH, {})


def test_expand_does_not_reexpand_values_and_keeps_macros() -> None:
    out = qs.expand_vars("$__timeFrom() ${contract_id:sqlstring}", DASH, {"contract_id": ["${x}"]})
    assert out == "$__timeFrom() '${x}'"


def test_expand_empty_selection_without_default_errors() -> None:
    dash = _dash({"name": "adjudicante", "type": "query", "multi": True, "current": {"value": []}})
    with pytest.raises(qs.QueryStatsError, match="no default"):
        qs.expand_vars("${adjudicante:csv}", dash, {})


def test_all_requires_all_value() -> None:
    dash = _dash({"name": "v", "type": "query", "current": {"value": "$__all"}})
    with pytest.raises(qs.QueryStatsError, match="allValue"):
        qs.expand_vars("${v}", dash, {})


# --------------------------------------------------------------------- real dashboards


def _walk_rawsql(o: Any) -> int:
    if isinstance(o, dict):
        return int("rawSql" in o) + sum(_walk_rawsql(v) for v in o.values())
    if isinstance(o, list):
        return sum(_walk_rawsql(v) for v in o)
    return 0


@pytest.mark.parametrize("name", qs.DASHBOARDS)
def test_panel_walk_matches_independent_rawsql_count(name: str) -> None:
    dash = qs.load_any_dashboard(name)
    # same predicate as jq `[.. | objects | select(has("rawSql"))]`, over the panels subtree
    assert len(qs.query_panels(dash, name)) == _walk_rawsql(dash["panels"]) > 0


@pytest.mark.parametrize("name", qs.DASHBOARDS)
def test_every_real_panel_expands_with_default_variables(name: str) -> None:
    dash = qs.load_any_dashboard(name)
    chosen: dict[str, list[str]] = {}
    if name == "adjudicante-analysis":
        chosen = {"adjudicante": ["1"]}
    if name == "adjudicatario-analysis":
        chosen = {"adjudicatario": ["2"]}
    for p in qs.query_panels(dash, name):
        sql = qs.expand_vars(p.raw_sql, dash, chosen)
        assert "${" not in sql and "$__all" not in sql
        assert p.ds_uid


def test_collapsed_row_panels_are_included() -> None:
    dash = {
        "panels": [
            {
                "type": "row",
                "id": 1,
                "panels": [
                    {
                        "id": 2,
                        "type": "table",
                        "title": "in row",
                        "targets": [{"refId": "A", "rawSql": "select 1"}],
                    }
                ],
            },
            {"type": "text", "id": 3, "targets": []},
        ]
    }
    assert [p.panel_id for p in qs.query_panels(dash)] == [2]


def test_contracts_table_dashboards_are_loadable() -> None:
    assert qs.load_any_dashboard("contracts-table")["uid"]
    with pytest.raises(ReportError):
        qs.load_any_dashboard("nope")


# --------------------------------------------------------------------- scenarios


def test_default_scenarios_cover_the_plan() -> None:
    sc = qs.build_scenarios(
        list(qs.DASHBOARDS), heavy_supplier="999", small_buyer="111", contract_family="x"
    )
    names = {(s.dashboard, s.name) for s in sc}
    assert ("overview", "now-10y/default") in names
    assert ("contracts-table", "now-2y/default") in names
    assert ("contracts-table", "now-10y/default") not in names
    assert ("adjudicante-analysis", f"now-2y/adjudicante={qs.HEAVY_ADJUDICANTE}") in names
    assert ("adjudicante-analysis", "now-10y/adjudicante=111") in names
    assert ("adjudicatario-analysis", "now-2y/adjudicatario=999") in names
    assert ("overview", "now-2y/contract_type=x") in names
    assert ("overview", "now-2y/show_all_contract_types=on") in names


def test_user_ranges_and_vars_replace_builtins() -> None:
    sc = qs.build_scenarios(
        ["adjudicante-analysis"], ranges=["now-1y"], var_items=["adjudicante=5"]
    )
    assert [(s.time_from, dict(s.variables)) for s in sc] == [("now-1y", {"adjudicante": ["5"]})]
    with pytest.raises(ReportError):
        qs.build_scenarios(["overview"], var_items=["bogus=1"])


# --------------------------------------------------------------------- metric deltas


def test_cgroup_delta() -> None:
    cpu, peak = qs.cgroup_delta(qs.CgroupSnap(1_000_000, 0), qs.CgroupSnap(3_500_000, 5 * 1024**2))
    assert (cpu, peak) == (2500.0, 5.0)


def _row(calls: int, exec_ms: float, **kw: Any) -> dict[str, Any]:
    base = {
        "calls": calls, "total_exec_time": exec_ms, "total_plan_time": 1.0,
        "shared_blks_hit": 10, "shared_blks_read": 2, "temp_blks_written": 0,
        "temp_blks_read": 0, "shared_blk_read_time": 0.5, "rows": 3,
    }  # fmt: skip
    return base | kw


def test_pgss_delta_picks_the_row_whose_calls_went_up() -> None:
    before = {"1": _row(4, 100.0), "2": _row(1, 5.0)}
    after = {"1": _row(5, 112.5, shared_blks_hit=30, rows=5), "2": _row(1, 5.0), "3": _row(0, 0.0)}
    d = qs.pgss_delta(before, after)
    assert d is not None
    assert (d.calls, d.matched, d.exec_ms, d.shared_blks_hit, d.rows) == (1, 1, 12.5, 20, 2)
    assert qs.pgss_delta(after, after) is None


def test_pgss_delta_new_row_and_ambiguity() -> None:
    d = qs.pgss_delta({}, {"1": _row(1, 4.0), "2": _row(1, 9.0, temp_blks_written=7)})
    assert d is not None and d.matched == 2 and d.exec_ms == 9.0 and d.temp_blks_written == 7


def test_plan_metrics_detects_spill_and_memory() -> None:
    plan = [
        {
            "Plan": {
                "Node Type": "Gather",
                "Workers Launched": 2,
                "Temp Written Blocks": 12,
                "Plans": [
                    {"Node Type": "Sort", "Sort Method": "external merge", "Sort Space Used": 4096},
                    {"Node Type": "Hash", "Peak Memory Usage": 900, "Hash Batches": 1},
                ],
            },
            "Planning": {"Memory Used": 77},
        }
    ]
    m = qs.plan_metrics(plan)
    assert m == {
        "sort_space_kb": 4096, "hash_peak_kb": 900, "spilled": True,
        "workers_launched": 2, "plan_mem_kb": 77, "plan_temp_written": 12,
    }  # fmt: skip


# --------------------------------------------------------------------- summary


def _frame() -> pl.DataFrame:
    def r(panel: int, rep: int, wall: float, **kw: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "dashboard": "overview", "panel_id": panel, "panel_title": f"P{panel}",
            "ref_id": "A", "scenario": "now-2y/default", "rep": rep, "warmup": rep == 1,
            "cold": False, "wall_ms": wall, "pg_exec_ms": wall / 2, "pg_cpu_ms": wall,
            "pg_peak_mb": 10.0, "temp_blks_written": 0, "spilled": False, "error": None,
        }  # fmt: skip
        return base | kw

    rows = [
        r(1, 1, 9999.0), r(1, 2, 10.0), r(1, 3, 20.0),
        r(2, 1, 1.0), r(2, 2, 100.0), r(2, 3, 300.0, temp_blks_written=5),
    ]  # fmt: skip
    return qs.runs_frame(rows)


def test_summarize_excludes_warmup_and_sorts_by_p95() -> None:
    s = qs.summarize(_frame())
    assert s["panel_id"].to_list() == [2, 1]
    p2 = s.filter(pl.col("panel_id") == 2).row(0, named=True)
    assert p2["n"] == 2 and p2["p50_wall_ms"] == 200.0 and p2["p95_wall_ms"] == pytest.approx(290.0)
    p1 = s.filter(pl.col("panel_id") == 1).row(0, named=True)
    assert p1["p95_wall_ms"] < 100  # the 9999 ms warmup row is excluded


def test_build_summary_markdown() -> None:
    md = qs.build_summary(_frame())
    assert md.index("Top 10 overall") < md.index("## overview")
    top = md.split("## overview")[0]
    assert top.index("P2") < top.index("P1")
    assert "| yes |" in md  # panel 2 spilled


# --------------------------------------------------------------------- collector


class FakeBackend:
    """Deterministic stand-in: every ds_query bumps one pg_stat_statements row."""

    def __init__(self) -> None:
        self.calls = 0
        self.cold = 0
        self.explained: list[str] = []

    def preflight(self) -> None: ...
    def reset_peak(self, container: str) -> None: ...
    def close(self) -> None: ...
    def cold_reset(self) -> None:
        self.cold += 1

    def pgss_snapshot(self) -> dict[str, dict[str, Any]]:
        return {"q": _row(self.calls, 10.0 * self.calls)}

    def cgroup(self, container: str) -> qs.CgroupSnap:
        return qs.CgroupSnap(self.calls * 2000, 8 * 1024**2)

    def scalar(self, sql: str) -> str | None:
        return "42"

    def explain(self, sql: str) -> list[dict[str, Any]]:
        self.explained.append(sql)
        return [{"Plan": {"Node Type": "Seq Scan"}}]


def _grafana(backend: FakeBackend) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        q = body["queries"][0]
        assert "${" not in q["rawSql"] and q["datasource"]["uid"]
        backend.calls += 1
        frame = {
            "schema": {"meta": {"executedQueryString": "SELECT 1 /* " + q["rawSql"][:5] + " */"}},
            "data": {"values": [[1, 2, 3]]},
        }
        return httpx.Response(200, json={"results": {q["refId"]: {"frames": [frame]}}})

    return httpx.Client(base_url="http://g", transport=httpx.MockTransport(handler))


def test_collect_row_count_is_panels_x_scenarios_x_repeat() -> None:
    be = FakeBackend()
    scenarios = qs.build_scenarios(["overview", "contracts-table"], ranges=["now-90d", "now-2y"])
    rows = qs.collect(be, _grafana(be), scenarios, repeat=3, cold=False, now=NOW)
    n_panels = sum(len(qs.query_panels(qs.load_any_dashboard(d), d)) for d in
                   ("overview", "contracts-table"))  # fmt: skip
    assert len(rows) == n_panels * 2 * 3
    assert all(
        r["rows_returned"] == 3 and r["pg_calls"] == 1 and r["pg_exec_ms"] == 10.0 for r in rows
    )
    assert all(r["pg_cpu_ms"] == 2.0 for r in rows)
    assert [r["warmup"] for r in rows[:3]] == [True, False, False]
    assert be.cold == 0


class NoCgroupBackend(FakeBackend):
    def reset_peak(self, container: str) -> None:
        raise AssertionError("reset_peak called in fast mode")

    def cgroup(self, container: str) -> qs.CgroupSnap:
        raise AssertionError("cgroup called in fast mode")


def test_collect_fast_skips_cgroup_and_leaves_nulls(caplog: pytest.LogCaptureFixture) -> None:
    be = NoCgroupBackend()
    sc = qs.build_scenarios(["overview"], ranges=["now-90d"])
    with caplog.at_level("INFO", logger=qs.logger.name):
        rows = qs.collect(be, _grafana(be), sc, repeat=3, cold=True, now=NOW, fast=True)
    assert be.cold > 0 and rows
    for k in ("pg_cpu_ms", "pg_peak_mb", "grafana_cpu_ms", "grafana_peak_mb"):
        assert all(r.get(k) is None for r in rows)
    assert all(r["pg_exec_ms"] == 10.0 and r["wall_ms"] >= 0 for r in rows)
    assert ("progress" in caplog.text) == (len(rows) >= qs.PROGRESS_EVERY)


def test_fast_summary_renders_dashes(tmp_path: Path) -> None:
    be = NoCgroupBackend()
    out = qs.run_query_stats(
        dashboards=["overview"], ranges=["now-90d"], repeat=2, out_dir=tmp_path / "run",
        backend=be, client=_grafana(be), now=NOW, explain_top=1, fast=True,
    )  # fmt: skip
    df = pl.read_parquet(out / "runs.parquet")
    assert df["pg_cpu_ms"].null_count() == df.height and df["pg_peak_mb"].null_count() == df.height
    md = (out / "summary.md").read_text()
    assert "| - | - |" in md and len(be.explained) == 1


def test_collect_progress_every_50(caplog: pytest.LogCaptureFixture) -> None:
    be = FakeBackend()
    sc = qs.build_scenarios(["overview"], ranges=["now-90d"])
    n = len(qs.query_panels(qs.load_any_dashboard("overview"), "overview"))
    with caplog.at_level("INFO", logger=qs.logger.name):
        rows = qs.collect(be, _grafana(be), sc, repeat=60, cold=False, now=NOW)
    assert n * 60 >= 50 and len(rows) == n * 60
    assert f"progress 50/{n * 60}" in caplog.text


def test_cli_fast_wiring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake(**kw: Any) -> Path:
        captured.update(kw)
        return tmp_path

    monkeypatch.setattr(qs, "run_query_stats", fake)
    res = CliRunner().invoke(app, ["query-stats", "--fast", "--cold", "--yes"])
    assert res.exit_code == 0, res.output
    assert captured["fast"] is True and captured["cold"] is True
    captured.clear()
    assert CliRunner().invoke(app, ["query-stats"]).exit_code == 0
    assert captured["fast"] is False


def test_collect_cold_resets_before_each_first_rep() -> None:
    be = FakeBackend()
    sc = qs.build_scenarios(["contracts-table"], ranges=["now-2y"])
    rows = qs.collect(be, _grafana(be), sc, repeat=2, cold=True, now=NOW)
    assert be.cold == len(rows) // 2
    assert [r["cold"] for r in rows[:2]] == [True, False]


def test_run_query_stats_writes_outputs(tmp_path: Path) -> None:
    be = FakeBackend()
    out = qs.run_query_stats(
        dashboards=["overview"], ranges=["now-90d"], repeat=2, out_dir=tmp_path / "run",
        backend=be, client=_grafana(be), now=NOW, explain_top=2,
    )  # fmt: skip
    df = pl.read_parquet(out / "runs.parquet")
    assert df.columns == list(qs.RUN_SCHEMA) and df.height > 0
    assert (out / "summary.md").read_text().startswith("# Query statistics")
    assert len(be.explained) == 2 and len(list((out / "plans").glob("*.json"))) == 2


def test_ds_query_reports_grafana_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"results": {"A": {"error": "pq: boom"}}})

    client = httpx.Client(base_url="http://g", transport=httpx.MockTransport(handler))
    panel = qs.QueryPanel("d", 1, "t", "A", "uid", "table", "select 1")
    assert qs.ds_query(client, panel, "select 1", NOW, NOW) == ("", 0, "pq: boom")


def test_docker_backend_explain_refuses_non_select() -> None:
    with pytest.raises(qs.QueryStatsError):
        qs.DockerBackend().explain("DELETE FROM procurement.contracts")
    with pytest.raises(qs.QueryStatsError):
        qs.DockerBackend().explain("SELECT 1; DROP TABLE x")


# --------------------------------------------------------------------- CLI


def test_cli_wiring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake(**kw: Any) -> Path:
        captured.update(kw)
        return tmp_path

    monkeypatch.setattr(qs, "run_query_stats", fake)
    monkeypatch.delenv("GRAFANA_URL", raising=False)
    res = CliRunner().invoke(
        app,
        ["query-stats", "--dashboard", "overview", "--dashboard", "contracts-table",
         "--range", "now-2y", "--var", "min_contract_value=10", "--repeat", "2",
         "--out", str(tmp_path)],
    )  # fmt: skip
    assert res.exit_code == 0, res.output
    assert captured["dashboards"] == ["overview", "contracts-table"]
    assert captured["ranges"] == ["now-2y"] and captured["repeat"] == 2
    assert captured["var_items"] == ["min_contract_value=10"] and captured["cold"] is False


def test_cli_cold_needs_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[bool] = []

    def fake(**kw: Any) -> Path:
        called.append(True)
        return Path(".")

    monkeypatch.setattr(qs, "run_query_stats", fake)
    res = CliRunner().invoke(app, ["query-stats", "--cold"], input="n\n")
    assert res.exit_code != 0 and not called
    res = CliRunner().invoke(app, ["query-stats", "--cold", "--yes"])
    assert res.exit_code == 0 and called


def test_cli_error_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kw: Any) -> Path:
        raise qs.QueryStatsError("nope")

    monkeypatch.setattr(qs, "run_query_stats", boom)
    assert CliRunner().invoke(app, ["query-stats"]).exit_code == 1


# --------------------------------------------------------------------- extra coverage


def test_expand_all_chosen_as_literal_all_and_plain_format() -> None:
    assert qs.expand_vars("${adjudicante}", DASH, {"adjudicante": ["All"]}) == "__ALL__"
    # All wins even when mixed with explicit values
    assert qs.expand_vars("${adjudicante:csv}", DASH, {"adjudicante": ["1", "$__all"]}) == "__ALL__"


def test_expand_several_variables_and_malformed_body() -> None:
    sql = "a=${min_contract_value} b=${adjudicante:csv} c=${contract_type:sqlstring}"
    assert qs.expand_vars(sql, DASH, {"adjudicante": ["7"]}) == "a=10 b=7 c='__ALL__'"
    with pytest.raises(qs.QueryStatsError, match="cannot expand"):
        qs.expand_vars("${bad name}", DASH, {})


def test_expand_quote_only_escaped_in_sqlstring() -> None:
    assert qs.expand_vars("${adjudicante:csv}", DASH, {"adjudicante": ["a'b"]}) == "a'b"
    assert qs.expand_vars("${adjudicante:sqlstring}", DASH, {"adjudicante": ["'"]}) == "''''"


def test_pgss_delta_tolerates_null_columns() -> None:
    d = qs.pgss_delta({}, {"1": _row(1, 2.0, total_plan_time=None, shared_blk_read_time=None)})
    assert d is not None and d.plan_ms == 0.0 and d.blk_read_ms == 0.0 and d.matched == 1


def test_plan_metrics_minimal_plan_has_no_spill() -> None:
    m = qs.plan_metrics([{"Plan": {"Node Type": "Result"}}])
    assert m["spilled"] is False and m["sort_space_kb"] == 0 and m["workers_launched"] == 0


def test_build_summary_lists_errors_and_cold_section() -> None:
    df = _frame().with_columns(
        pl.when(pl.col("panel_id") == 1)
        .then(pl.lit("boom"))
        .otherwise(pl.lit(None, dtype=pl.String))
        .alias("error"),
        (pl.col("rep") == 1).alias("cold"),
    )
    md = qs.build_summary(df)
    assert "## Errors (3 rows)" in md
    assert md.count("- overview #1 (now-2y/default): boom") == 1  # de-duplicated
    assert "## Cold-cache first run" in md


def test_build_summary_without_cold_or_errors_omits_sections() -> None:
    md = qs.build_summary(_frame())
    assert "Cold-cache" not in md and "## Errors" not in md


def test_summarize_counts_errors_and_spill_flag() -> None:
    df = _frame().with_columns(
        pl.when(pl.col("rep") == 3)
        .then(pl.lit("x"))
        .otherwise(pl.lit(None, dtype=pl.String))
        .alias("error"),
        (pl.col("panel_id") == 2).alias("spilled"),
    )
    s = qs.summarize(df)
    assert s["errors"].to_list() == [1, 1]
    assert dict(zip(s["panel_id"], s["spilled"], strict=True)) == {1: False, 2: True}


# --------------------------------------------------------------------------- DockerBackend

MIB = 1 << 20


class FakeRun:
    """Scripted `DockerBackend._run`: `handler(argv, stdin) -> (rc, stdout, stderr)`."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv: list[str], stdin: str | None = None) -> Any:
        self.calls.append((argv, stdin))
        rc, out, err = self.handler(argv, stdin)
        return subprocess.CompletedProcess(argv, rc, out, err)


def _backend(handler: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[qs.DockerBackend, FakeRun]:
    be = qs.DockerBackend(user="u", db="d")
    fake = FakeRun(handler)
    monkeypatch.setattr(be, "_run", fake)
    return be, fake


class StubSampler:
    def __init__(self, container: str) -> None:
        self.container = container
        self.closed = False

    def reset(self) -> None: ...

    def peak(self) -> int:
        return 7

    def close(self) -> None:
        self.closed = True


def _cg(peak: int, current: int) -> str:
    return f"usage_usec 100\n{peak}\n{current}\n"


def test_parse_cgroup() -> None:
    assert qs.parse_cgroup("usage_usec 10\n5\n4\n") == (10, 5, 4)
    assert qs.parse_cgroup("usage_usec 10\n-1\n4\n") == (10, -1, 4)
    for bad in ("", "usage_usec 1\n2\n", "usage_usec x\n1\n2\n"):
        with pytest.raises(qs.QueryStatsError):
            qs.parse_cgroup(bad)


def test_preflight_warns_when_busy(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    be, _ = _backend(lambda a, s: (0, "3\n", ""), monkeypatch)
    with caplog.at_level("WARNING"):
        be.preflight()
    assert "polluted" in caplog.text


def test_preflight_pgss_unusable(monkeypatch: pytest.MonkeyPatch) -> None:
    be, _ = _backend(lambda a, s: (1, "", "relation does not exist"), monkeypatch)
    with pytest.raises(qs.QueryStatsError, match="pg_stat_statements is not usable"):
        be.preflight()


@pytest.mark.parametrize(
    "sql", ["DELETE FROM t", "SELECT 1; SELECT 2", "INSERT INTO t VALUES (1)", "  "]
)
def test_explain_guard_refuses(sql: str, monkeypatch: pytest.MonkeyPatch) -> None:
    be, fake = _backend(lambda a, s: (0, "[]", ""), monkeypatch)
    with pytest.raises(qs.QueryStatsError, match="refusing"):
        be.explain(sql)
    assert fake.calls == []


def test_explain_wraps_in_read_only_tx(monkeypatch: pytest.MonkeyPatch) -> None:
    be, fake = _backend(lambda a, s: (0, 'NOTICE\n[{"Plan": {}}]\n', ""), monkeypatch)
    assert be.explain("WITH a AS (SELECT 1) SELECT * FROM a;") == [{"Plan": {}}]
    argv, stdin = fake.calls[0]
    assert argv[0] == "docker"
    assert stdin is not None
    lines = stdin.splitlines()
    assert lines[0] == "BEGIN READ ONLY;"
    assert lines[1] == f"SET LOCAL ROLE {qs.DATASOURCE_USER};"
    assert lines[-1] == "ROLLBACK;"
    assert "EXPLAIN (ANALYZE" in lines[3]


def test_reset_peak_trusted_when_readback_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
        if "echo 0 >" in argv[-1]:
            return 0, "", ""
        return 0, _cg(100 * MIB, 100 * MIB), ""

    be, _ = _backend(handler, monkeypatch)
    monkeypatch.setattr(qs, "_MemorySampler", StubSampler)
    be.reset_peak("c")
    assert be._samplers == {}


def test_reset_peak_rc0_but_lifetime_peak_engages_sampler(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
        if "echo 0 >" in argv[-1]:
            return 0, "", ""
        return 0, _cg(900 * MIB, 100 * MIB), ""

    be, _ = _backend(handler, monkeypatch)
    monkeypatch.setattr(qs, "_MemorySampler", StubSampler)
    be.reset_peak("c")
    assert isinstance(be._samplers["c"], StubSampler)
    assert be.cgroup("c").peak_bytes == 100 * MIB  # max(sampler 7, current)


def test_reset_peak_readonly_cgroup_engages_sampler(monkeypatch: pytest.MonkeyPatch) -> None:
    be, _ = _backend(lambda a, s: (1, "", "Read-only file system"), monkeypatch)
    monkeypatch.setattr(qs, "_MemorySampler", StubSampler)
    be.reset_peak("c")
    assert "c" in be._samplers


def test_cgroup_without_memory_peak_uses_sampler(monkeypatch: pytest.MonkeyPatch) -> None:
    be, _ = _backend(lambda a, s: (0, _cg(-1, 5 * MIB), ""), monkeypatch)
    monkeypatch.setattr(qs, "_MemorySampler", StubSampler)
    assert be.cgroup("c").peak_bytes == 5 * MIB
    assert "c" in be._samplers


def test_cold_reset_drops_pg_sampler(monkeypatch: pytest.MonkeyPatch) -> None:
    be, _ = _backend(lambda a, s: (0, "", ""), monkeypatch)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    stub = StubSampler(qs.PG_CONTAINER)
    be._samplers[qs.PG_CONTAINER] = stub  # type: ignore[assignment]
    be.cold_reset()
    assert stub.closed
    assert qs.PG_CONTAINER not in be._samplers


def test_sampler_close_touches_stop_file(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    class P:
        stdout = iter(())

        def wait(self, timeout: float) -> int:
            return 0

        def terminate(self) -> None: ...

    def popen(argv: list[str], **kw: Any) -> P:
        seen.append(argv)
        return P()

    def run(argv: list[str], **kw: Any) -> None:
        seen.append(argv)

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess, "run", run)
    qs._MemorySampler("c").close()
    loop, touch = seen[0][-1], seen[1]
    assert "-lt" in loop
    assert "-e" in loop  # bounded and stop-file aware
    assert touch[:4] == ["docker", "exec", "c", "touch"]
    assert touch[4] in loop


# --------------------------------------------------------------------------- error handling


def _panel() -> qs.QueryPanel:
    return qs.QueryPanel(
        dashboard="d",
        panel_id=1,
        title="t",
        ref_id="A",
        ds_uid="u",
        fmt="table",
        raw_sql="select 1",
    )


def _raise(exc: Exception) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


@pytest.mark.parametrize(
    ("handler", "needle"),
    [
        (_raise(httpx.ReadTimeout("slow")), "ReadTimeout"),
        (_raise(httpx.ConnectError("down")), "ConnectError"),
        (lambda r: httpx.Response(200, content=b"<html>"), "invalid response"),
    ],
)
def test_ds_query_records_errors(handler: Any, needle: str) -> None:
    client = httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler))
    executed, rows, error = qs.ds_query(client, _panel(), "select 1", NOW, NOW)
    assert (executed, rows) == ("", 0)
    assert error is not None
    assert needle in error


def test_run_query_stats_writes_partial_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Boom(Exception): ...

    def fake_collect(*args: Any, sink: list[dict[str, Any]], **kw: Any) -> list[dict[str, Any]]:
        sink.append(
            {k: None for k in qs.RUN_SCHEMA}
            | {
                "dashboard": "d",
                "scenario": "s",
                "ref_id": "A",
                "panel_id": 1,
                "panel_title": "t",
                "warmup": False,
                "error": "x",
            }
        )
        raise Boom

    class B:
        def preflight(self) -> None: ...

        def scalar(self, sql: str) -> None:
            return None

        def close(self) -> None: ...

    monkeypatch.setattr(qs, "collect", fake_collect)
    monkeypatch.setattr(qs, "build_scenarios", lambda *a, **k: [])
    client = httpx.Client(
        base_url="http://x", transport=httpx.MockTransport(lambda r: httpx.Response(200))
    )
    with pytest.raises(Boom):
        qs.run_query_stats(
            dashboards=["overview"],
            out_dir=tmp_path,
            backend=B(),  # type: ignore[arg-type]
            client=client,
            now=NOW,
        )
    assert pl.read_parquet(tmp_path / "runs.parquet").height == 1
