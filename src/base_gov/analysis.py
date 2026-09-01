"""Pure `pl.DataFrame -> DataFrame/scalar` summaries over loaded contracts.

No I/O here; callers pass in a frame already produced by
`xlsx_ingest.load_contracts`.
"""

from __future__ import annotations

import polars as pl

from base_gov.xlsx_ingest import ADJUDICANTE, ADJUDICATARIOS, CONTRACT_ID, CONTRACT_TYPE, VALUE_EUR

_ENTITY_RE = r"^\s*(\d+)\s*-\s*(.*)$"

# Accented -> plain-ASCII map for the letters that appear in "município" /
# "municipio" variants, used since `str.normalize` NFKD decomposition support
# varies by polars/arrow build.
_ACCENT_MAP = {
    "á": "a",
    "à": "a",
    "ã": "a",
    "â": "a",
    "é": "e",
    "ê": "e",
    "í": "i",
    "ó": "o",
    "õ": "o",
    "ô": "o",
    "ú": "u",
    "ç": "c",
}


def parse_entity_column(df: pl.DataFrame, column: str) -> pl.DataFrame:
    """Split a `"NIPC - Name"` column into `entity_id` / `name`.

    Falls back to the raw string as `name` (with a null `entity_id`) when a
    cell doesn't match the expected `id - name` shape.
    """
    # `str.strip_chars()` first: a trailing "\n"/"\r\n" on the raw cell (some
    # Excel exports embed one) defeats `$` under polars' Rust regex engine,
    # which — unlike Python's `re` — does not treat "$" as matching just
    # before a trailing newline. Without the strip, such a cell silently
    # produces a null entity_id (see tests/test_analysis.py).
    extracted = pl.col(column).str.strip_chars().str.extract_groups(_ENTITY_RE)
    return df.select(
        entity_id=extracted.struct.field("1"),
        name=pl.coalesce([extracted.struct.field("2"), pl.col(column)]),
    )


def distinct_adjudicantes(df: pl.DataFrame) -> int:
    return df[ADJUDICANTE].n_unique()


def distinct_adjudicatarios(df: pl.DataFrame) -> int:
    return df[ADJUDICATARIOS].n_unique()


def contract_id_stats(df: pl.DataFrame) -> dict[str, int | bool]:
    count = df.height
    n_unique = df[CONTRACT_ID].n_unique()
    return {"count": count, "n_unique": n_unique, "is_unique": count == n_unique}


def total_value_eur(df: pl.DataFrame) -> float:
    """Sum of contract values, handling either a numeric column or a
    pt-PT formatted string column (`"1.234,56 €"`)."""
    series = df[VALUE_EUR]
    if series.dtype == pl.Utf8 or series.dtype == pl.String:
        parsed = (
            series.str.replace_all(r"[€\s]", "")
            .str.replace_all(r"\.", "")
            .str.replace_all(",", ".")
            .cast(pl.Float64, strict=False)
        )
        return float(parsed.sum() or 0.0)
    return float(series.cast(pl.Float64).sum() or 0.0)


def by_contract_type(df: pl.DataFrame) -> pl.DataFrame:
    """Counts and summed value per `tipoContrato`, sorted by value desc."""
    value = df[VALUE_EUR]
    if value.dtype == pl.Utf8 or value.dtype == pl.String:
        value_col = (
            pl.col(VALUE_EUR)
            .str.replace_all(r"[€\s]", "")
            .str.replace_all(r"\.", "")
            .str.replace_all(",", ".")
            .cast(pl.Float64, strict=False)
        )
    else:
        value_col = pl.col(VALUE_EUR).cast(pl.Float64)
    return (
        df.group_by(CONTRACT_TYPE)
        .agg(count=pl.len(), total_value_eur=value_col.sum())
        .sort("total_value_eur", descending=True)
    )


def is_municipality(column: str = ADJUDICANTE) -> pl.Expr:
    """Case-insensitive match on `municipio`/`município` after stripping the
    accents in `_ACCENT_MAP`, applied to `column`."""
    expr = pl.col(column).str.to_lowercase()
    for accented, plain in _ACCENT_MAP.items():
        expr = expr.str.replace_all(accented, plain)
    return expr.str.contains(r"municipio")


def municipality_contracts(df: pl.DataFrame) -> pl.DataFrame:
    return df.filter(is_municipality())
