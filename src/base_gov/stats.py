"""Shared numeric helpers for the analysis notebooks."""

import polars as pl

MIN_VALUE = 10.0  # contracts priced below this (€) are disregarded in the analyses
MAX_DAYS = 3650  # prazoExecucao above ~10 years is treated as a data-entry error


def filter_min_value(
    df: pl.DataFrame, price: str = "preco_contratual", min_value: float = MIN_VALUE
) -> pl.DataFrame:
    """Drop contracts with |value| below ``min_value`` (negative prices count by magnitude)."""
    return df.filter(pl.col(price).abs() >= min_value)


def value_per_day(price: str = "preco_contratual", days: str = "prazo_execucao") -> pl.Expr:
    """Contract value per execution day; null unless 1 <= days <= MAX_DAYS."""
    return (
        pl.when(pl.col(days).is_between(1, MAX_DAYS))
        .then(pl.col(price) / pl.col(days))
        .otherwise(None)
        .alias("valor_por_dia")
    )


def value_impact(
    before: pl.DataFrame, after: pl.DataFrame, col: str = "preco_contratual"
) -> pl.DataFrame:
    """Contract count, mean, median and total of ``col`` before/after a filter, with the change."""

    def stats(df: pl.DataFrame) -> list[float]:
        s = df[col]
        return [float(df.height), s.mean() or 0.0, s.median() or 0.0, s.sum()]  # type: ignore[list-item]

    b, a = stats(before), stats(after)
    return pl.DataFrame(
        {
            "metric": ["n_contracts", "mean_value", "median_value", "total_value"],
            "before": b,
            "after": a,
            "change": [x - y for x, y in zip(a, b, strict=True)],
            "change_pct": [
                round((x - y) / y * 100, 2) if y else 0.0 for x, y in zip(a, b, strict=True)
            ],
        }
    )
