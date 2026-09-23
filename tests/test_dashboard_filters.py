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
    v = _var(_load(name), "contract_type")
    assert v["type"] == "custom"
    assert v["includeAll"] is True
    assert v["allValue"] == "__ALL__"
    assert v["current"]["value"] == ["$__all"]
    values = {part.split(":")[1].strip() for part in v["query"].split(",")}
    assert values == {"ajuste_direto", "concurso_publico", "consulta_previa", "other"}


@pytest.mark.parametrize("name", NAMES)
def test_contract_queries_filter_on_contract_type(name: str) -> None:
    for o in _walk(_load(name)):
        sql = o.get("rawSql")
        if sql and VIEW_RE.search(sql):
            assert "${contract_type:csv}" in sql, sql[:120]


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
