"""Query-string construction must match what the portal's own form produces."""

from __future__ import annotations

import datetime as dt

import pytest

from base_gov.queries import contracts_query, entities_query, month_windows


def test_entities_query_matches_portal_format() -> None:
    assert entities_query("Município") == "texto=Município"


def test_entities_query_strips_surrounding_whitespace() -> None:
    assert entities_query("  Município  ") == "texto=Município"


def test_contracts_query_reproduces_observed_string() -> None:
    # Captured from the live page for
    # ?type=contratos&adjudicante=500051070&desdedatacontrato=2025-01-01&...
    query = contracts_query(
        contracting="500051070",
        contract_date_from=dt.date(2025, 1, 1),
        contract_date_to=dt.date(2025, 1, 31),
    )

    assert query == (
        "adjudicante=500051070&tipo=0&tipocontrato=0&pais=0&distrito=0&concelho=0"
        "&desdedatacontrato=2025-01-01&atedatacontrato=2025-01-31"
    )


def test_contracts_query_drops_empty_fields_but_keeps_zero_selectors() -> None:
    query = contracts_query()

    assert query == "tipo=0&tipocontrato=0&pais=0&distrito=0&concelho=0"
    assert "adjudicante" not in query
    assert "desdedatacontrato" not in query


def test_month_windows_splits_on_calendar_months() -> None:
    windows = list(month_windows(dt.date(2025, 1, 15), dt.date(2025, 3, 10)))

    assert windows == [
        (dt.date(2025, 1, 15), dt.date(2025, 1, 31)),
        (dt.date(2025, 2, 1), dt.date(2025, 2, 28)),
        (dt.date(2025, 3, 1), dt.date(2025, 3, 10)),
    ]


def test_month_windows_handles_single_day_and_year_end() -> None:
    assert list(month_windows(dt.date(2025, 12, 5), dt.date(2025, 12, 5))) == [
        (dt.date(2025, 12, 5), dt.date(2025, 12, 5))
    ]
    assert list(month_windows(dt.date(2025, 12, 1), dt.date(2026, 1, 2))) == [
        (dt.date(2025, 12, 1), dt.date(2025, 12, 31)),
        (dt.date(2026, 1, 1), dt.date(2026, 1, 2)),
    ]


def test_month_windows_rejects_reversed_range() -> None:
    with pytest.raises(ValueError, match="after end"):
        list(month_windows(dt.date(2025, 3, 1), dt.date(2025, 1, 1)))
