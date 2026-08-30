"""Builders for the portal's `query` parameter.

The BASE search form is a plain GET form whose fields the server folds into a
single opaque `query` string, then echoes back into the page's JavaScript. That
string is what the AJAX endpoint actually consumes, e.g.::

    adjudicante=500051070&tipo=0&tipocontrato=0&desdedatacontrato=2025-01-01&...

It looks like a URL query string but it is *one form field value*, so the pairs
inside it are joined raw and percent-encoding happens once, at the outer layer.
Empty fields are dropped by the server; explicit zeros ("all") are kept.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

# Selectors the form always submits, meaning "no restriction".
_CONTRACT_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("tipo", "0"),
    ("tipocontrato", "0"),
    ("pais", "0"),
    ("distrito", "0"),
    ("concelho", "0"),
)


def _join(pairs: list[tuple[str, str]]) -> str:
    """Join key/value pairs the way the portal does: raw `k=v` separated by `&`."""
    return "&".join(f"{key}={value}" for key, value in pairs if value != "")


def entities_query(text: str) -> str:
    """Build the query for an `Entidades` search.

    `text` matches against NIPC, description or country, exactly as the
    "NIPC, descrição ou país da entidade" field does.
    """
    return _join([("texto", text.strip())])


def contracts_query(
    *,
    contracting: str = "",
    contracted: str = "",
    text: str = "",
    contract_date_from: dt.date | None = None,
    contract_date_to: dt.date | None = None,
    publication_date_from: dt.date | None = None,
    publication_date_to: dt.date | None = None,
) -> str:
    """Build the query for a `Contratos` search.

    `contracting` / `contracted` accept either a NIPC or a description
    fragment; the NIPC is the stable identifier and should be preferred.
    """
    pairs: list[tuple[str, str]] = [
        ("texto", text.strip()),
        ("adjudicante", contracting.strip()),
        ("adjudicataria", contracted.strip()),
    ]
    pairs.extend(_CONTRACT_DEFAULTS)
    pairs.extend(
        [
            ("desdedatacontrato", _iso(contract_date_from)),
            ("atedatacontrato", _iso(contract_date_to)),
            ("desdedatapublicacao", _iso(publication_date_from)),
            ("atedatapublicacao", _iso(publication_date_to)),
        ]
    )
    # Re-order so the selector defaults sit where the form puts them: after the
    # free-text fields but before the date range.
    return _join(pairs)


def _iso(value: dt.date | None) -> str:
    """Render a date the way the portal's datepicker does (`yy-mm-dd` = ISO)."""
    return value.isoformat() if value is not None else ""


def month_windows(start: dt.date, end: dt.date) -> Iterator[tuple[dt.date, dt.date]]:
    """Split `[start, end]` into calendar-month windows, inclusive on both ends.

    The portal degrades badly on broad queries, so callers should page a month
    at a time rather than asking for a multi-year range in one request.
    """
    if start > end:
        msg = f"start {start} is after end {end}"
        raise ValueError(msg)

    cursor = start
    while cursor <= end:
        last_of_month = _end_of_month(cursor)
        yield cursor, min(last_of_month, end)
        cursor = last_of_month + dt.timedelta(days=1)


def _end_of_month(value: dt.date) -> dt.date:
    if value.month == 12:
        return value.replace(day=31)
    first_of_next = value.replace(month=value.month + 1, day=1)
    return first_of_next - dt.timedelta(days=1)
