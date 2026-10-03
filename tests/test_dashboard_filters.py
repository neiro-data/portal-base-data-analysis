"""Structural checks for dashboard filters: contract type, entity search, contract ID."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

DASHBOARD_DIR = Path(__file__).resolve().parents[1] / "docker" / "grafana" / "dashboards"
FILES = sorted(DASHBOARD_DIR.glob("*.json"))
NAMES = [f.name for f in FILES]
CONTRACTS_TABLES = ["contracts-table.json", "contracts-table-municipios.json"]
ANALYSIS = ["adjudicante-analysis.json", "adjudicatario-analysis.json"]
VIEW_RE = re.compile(r"v_contracts_material|v_adjudicatario_contracts|v_contracts_below_min")


def _load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((DASHBOARD_DIR / name).read_text())
    return data


def _walk(o: object) -> Iterator[dict[str, Any]]:
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _var(d: dict[str, Any], name: str) -> dict[str, Any]:
    return next(v for v in d["templating"]["list"] if v["name"] == name)


def test_six_dashboards() -> None:
    assert len(FILES) == 6


@pytest.mark.parametrize("name", NAMES)
def test_contract_type_variable(name: str) -> None:
    d = _load(name)
    v = _var(d, "contract_type")
    assert v["type"] == "query"
    assert v["includeAll"] is True
    assert v["multi"] is True
    assert v["allValue"] == "'__ALL__'"
    assert v["refresh"] == 1
    assert v["current"]["value"] == ["$__all"]
    assert "v_contract_type_options" in v["query"]
    assert "${show_all_contract_types}" in v["query"]
    names = [x["name"] for x in d["templating"]["list"]]
    assert names.index("show_all_contract_types") < names.index("contract_type")


@pytest.mark.parametrize("name", NAMES)
def test_show_all_contract_types_toggle(name: str) -> None:
    v = _var(_load(name), "show_all_contract_types")
    assert v["type"] == "custom"
    assert v["label"] == "Show all contract types"
    assert v["query"] == "Off : off,On : on"
    assert v["current"]["value"] == "off"
    assert v["multi"] is False
    assert v["includeAll"] is False


@pytest.mark.parametrize("name", NAMES)
def test_contract_queries_filter_on_contract_type(name: str) -> None:
    for o in _walk(_load(name)):
        sql = o.get("rawSql")
        if sql and VIEW_RE.search(sql):
            assert "${contract_type:sqlstring}" in sql, sql[:120]
            assert "${show_all_contract_types}" in sql, sql[:120]
        if sql:
            assert "${contract_type:csv}" not in sql, sql[:120]


@pytest.mark.parametrize("name", NAMES)
def test_no_caveats(name: str) -> None:
    text = (DASHBOARD_DIR / name).read_text()
    assert "caveat" not in text.lower()
    assert '"title": "Caveats"' not in text


@pytest.mark.parametrize("name", NAMES)
def test_entity_variables_search_by_name_or_nif(name: str) -> None:
    d = _load(name)
    entity_vars = [
        v for v in d["templating"]["list"] if v["name"] in ("adjudicante", "adjudicatario")
    ]
    assert entity_vars
    for v in entity_vars:
        assert "AS __value" in v["query"]
        assert "' — '" in v["query"]
        assert v["refresh"] == 1


@pytest.mark.parametrize("name", CONTRACTS_TABLES)
def test_contracts_table_contract_id(name: str) -> None:
    d = _load(name)
    assert _var(d, "contract_id")["type"] == "textbox"
    panels = {p["id"]: p for p in d["panels"]}
    for pid in (1, 2):
        sql = " ".join(t["rawSql"] for t in panels[pid]["targets"])
        assert "${contract_id:sqlstring}" in sql


@pytest.mark.parametrize("name", ANALYSIS)
def test_analysis_id_links_to_contracts(name: str) -> None:
    panel = next(o for o in _walk(_load(name)) if o.get("id") == 35 and "fieldConfig" in o)
    ov = next(o for o in panel["fieldConfig"]["overrides"] if o["matcher"]["options"] == "ID")
    url = ov["properties"][0]["value"][0]["url"]
    assert "/d/base-contracts-table/" in url
    assert "var-contract_id=${__value.raw}" in url


@pytest.mark.parametrize("name", NAMES)
def test_min_value_links_carry_contract_type(name: str) -> None:
    for o in _walk(_load(name)):
        for v in o.values():
            if isinstance(v, str) and "var-min_contract_value=${" in v:
                assert "${contract_type:queryparam}" in v, v
                assert "${show_all_contract_types:queryparam}" in v, v


def _panels(d: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in _walk(d["panels"]) if "gridPos" in p and "type" in p]


@pytest.mark.parametrize("name", NAMES)
def test_ten_year_quick_range(name: str) -> None:
    ranges = _load(name)["timepicker"]["quick_ranges"]
    assert {"display": "Last 10 years", "from": "now-10y/d", "to": "now/d"} in ranges
    assert any(r["from"] == "now-5y/d" for r in ranges)


@pytest.mark.parametrize("name", NAMES)
def test_horizontal_barcharts_truncate_labels(name: str) -> None:
    for p in _panels(_load(name)):
        if p["type"] == "barchart" and p["options"].get("orientation") == "horizontal":
            assert "xTickLabelMaxLength" in p["options"], p["title"]


@pytest.mark.parametrize(
    ("name", "var"),
    [
        ("adjudicante-analysis.json", "adjudicante"),
        ("adjudicatario-analysis.json", "adjudicatario"),
    ],
)
def test_analysis_entity_vars_multi(name: str, var: str) -> None:
    v = _var(_load(name), var)
    assert v["multi"] is True
    assert v["includeAll"] is False
    assert isinstance(v["current"]["value"], list)
    for o in _walk(_load(name)):
        sql = o.get("rawSql")
        if sql:
            assert f"= '${{{var}}}'" not in sql, sql[:120]


def test_adjudicatario_analysis_dedupes_joint_awards() -> None:
    d = _load("adjudicatario-analysis.json")
    for o in _walk(d["panels"]):
        sql = o.get("rawSql")
        if sql:
            assert "v_adjudicatario_contracts" not in sql, sql[:120]
            assert "contract_adjudicatarios WHERE entity_id = ANY(" in sql, sql[:120]


@pytest.mark.parametrize("name", ANALYSIS)
def test_analysis_nif_text_panel(name: str) -> None:
    top = [p for p in _load(name)["panels"] if p["gridPos"]["y"] == 0]
    assert len(top) == 1
    assert top[0]["type"] == "text"
    assert ":csv}" in top[0]["options"]["content"]


def _overlaps(a: dict[str, int], b: dict[str, int]) -> bool:
    return (
        a["x"] < b["x"] + b["w"]
        and b["x"] < a["x"] + a["w"]
        and a["y"] < b["y"] + b["h"]
        and b["y"] < a["y"] + a["h"]
    )


@pytest.mark.parametrize("name", NAMES)
def test_no_overlapping_panels(name: str) -> None:
    d = _load(name)
    groups = [d["panels"]] + [p["panels"] for p in _walk(d["panels"]) if p.get("panels")]
    for group in groups:
        grids = [(p["title"], p["gridPos"]) for p in group]
        for i, (t1, g1) in enumerate(grids):
            for t2, g2 in grids[i + 1 :]:
                assert not _overlaps(g1, g2), f"{t1} overlaps {t2}"
