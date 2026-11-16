"""Bulk ingest: workbooks -> raw parquet (verbatim) -> treated `data/contracts.parquet`.

Two layers (see `raw_ingest` and `transform`). This module orchestrates them and owns the change
tracking: every treated row carries `row_hash`, `first_ingested_at` and `updated_at`, merged per
`idcontrato` against the previous treated file.

The treated stage always reprocesses *every* raw year on disk, never a subset: the fuzzy
`adjudicante_key` clustering is dataset-wide, so keys must be computed over all years together.
`years` only selects which workbooks are (re)converted to raw parquet.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from base_gov import entities_db, raw_ingest
from base_gov.stats import MIN_VALUE
from base_gov.transform import (
    TIMESTAMP_SCHEMA,
    TREATED_COLUMNS,
    SchemaDrift,
    check_raw_columns,
    treat,
    unescape_html_entities,
)
from base_gov.xlsx_ingest import ADJUDICANTE, ADJUDICATARIOS, CONTRACT_ID

logger = logging.getLogger("base_gov")

_TS = pl.Datetime("us", "UTC")
_TRACKED = ("idcontrato", "row_hash", *TIMESTAMP_SCHEMA)


@dataclass(frozen=True)
class YearSummary:
    year: int
    read: int
    ingested: int
    new: int
    updated: int
    unchanged: int
    below_min: int  # flagged rows, a subset of `ingested` — not a deduction

    @property
    def superseded(self) -> int:
        """Rows dropped because a later year's file carries the same `idcontrato`."""
        return self.read - self.ingested


@dataclass(frozen=True)
class IngestSummary:
    per_year: list[YearSummary]
    adjudicantes_total: int = 0
    adjudicatarios_total: int = 0
    min_value: float = MIN_VALUE
    ingested_at: datetime | None = field(default=None)
    drift: dict[int, SchemaDrift] = field(default_factory=dict)

    def _total(self, attr: str) -> int:
        return sum(getattr(y, attr) for y in self.per_year)

    @property
    def read(self) -> int:
        return self._total("read")

    @property
    def ingested(self) -> int:
        return self._total("ingested")

    @property
    def new(self) -> int:
        return self._total("new")

    @property
    def updated(self) -> int:
        return self._total("updated")

    @property
    def unchanged(self) -> int:
        return self._total("unchanged")

    @property
    def below_min(self) -> int:
        return self._total("below_min")

    @property
    def superseded(self) -> int:
        return self._total("superseded")

    def render(self) -> str:
        head = ("year", "read", "superseded", "ingested", "new", "updated", "unchanged")
        below = f"below_min(<€{self.min_value:g})"
        rows = [
            (
                str(y.year),
                y.read,
                y.superseded,
                y.ingested,
                y.new,
                y.updated,
                y.unchanged,
                y.below_min,
            )
            for y in self.per_year
        ]
        total = (
            "total",
            self.read,
            self.superseded,
            self.ingested,
            self.new,
            self.updated,
            self.unchanged,
            self.below_min,
        )

        def fmt(v: str | int) -> str:
            return v if isinstance(v, str) else f"{v:_}"

        table = [[*head, below], *[[fmt(c) for c in r] for r in rows], [fmt(c) for c in total]]
        widths = [max(len(r[i]) for r in table) for i in range(len(head) + 1)]

        def line(cells: list[str]) -> str:
            first, rest = (
                cells[0].ljust(widths[0]),
                [c.rjust(w) for c, w in zip(cells[1:], widths[1:], strict=True)],
            )
            return "  ".join([first, *rest])

        rule = "-" * len(line(table[0]))
        out = [line(table[0]), *map(line, table[1:-1]), rule, line(table[-1])]
        if any(self.drift.values()):
            out += ["", "schema drift:"]
            for y in sorted(self.drift):
                d = self.drift[y]
                if d.missing_optional:
                    out.append(
                        f"  {y}  missing optional (null-filled): {', '.join(d.missing_optional)}"
                    )
                if d.unknown:
                    out.append(f"  {y}  unknown upstream columns: {', '.join(d.unknown)}")
        return "\n".join(out)


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def _previous(path: Path) -> pl.LazyFrame | None:
    """The tracked columns of the previous treated file, or None if it is absent/pre-tracking."""
    if not path.exists():
        return None
    prev = pl.scan_parquet(path)
    have = prev.collect_schema().names()
    if any(c not in have for c in _TRACKED):
        logger.warning("%s has no change-tracking columns; treating every row as new", path)
        return None
    return prev.select(_TRACKED).rename(
        {"row_hash": "_prev_hash", "first_ingested_at": "_prev_first", "updated_at": "_prev_upd"}
    )


def _dedup(raw: pl.LazyFrame) -> pl.LazyFrame:
    """One row per `idcontrato`; the latest raw year wins if a contract shows up in two files."""
    return raw.unique(subset=CONTRACT_ID, keep="last", maintain_order=True)


def merge_tracking(
    treated: pl.LazyFrame, previous: pl.LazyFrame | None, run_ts: datetime
) -> pl.LazyFrame:
    """Add `first_ingested_at` / `updated_at` and a `_status` (new | updated | unchanged)."""
    ts = pl.lit(_utc(run_ts), dtype=_TS)
    if previous is None:
        return treated.with_columns(
            ts.alias("first_ingested_at"), ts.alias("updated_at"), pl.lit("new").alias("_status")
        )
    joined = treated.join(previous, on="idcontrato", how="left")
    status = (
        pl.when(pl.col("_prev_first").is_null())
        .then(pl.lit("new"))
        .when(pl.col("row_hash") == pl.col("_prev_hash"))
        .then(pl.lit("unchanged"))
        .otherwise(pl.lit("updated"))
    )
    return joined.with_columns(status.alias("_status")).with_columns(
        pl.coalesce("_prev_first", ts).alias("first_ingested_at"),
        pl.when(pl.col("_status") == "unchanged")
        .then(pl.col("_prev_upd"))
        .otherwise(ts)
        .alias("updated_at"),
    )


def _write_manifest_treated(
    manifest: Path, min_value: float, drift: dict[int, SchemaDrift]
) -> None:
    data = json.loads(manifest.read_text()) if manifest.exists() else {}
    old = data.get("treated", {}).get("min_value")
    if old is not None and old != min_value:
        logger.info(
            "min_value changed %s -> %s: flags refresh, row hashes are unaffected", old, min_value
        )
    data["treated"] = {
        "min_value": min_value,
        "schema_drift": {str(y): drift[y].as_dict() for y in sorted(drift)},
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(data, indent=2) + "\n")


def _raw_counts(raw_dir: Path, years: Sequence[int]) -> dict[int, int]:
    return {
        y: pl.scan_parquet(raw_ingest.raw_path(raw_dir, y)).select(pl.len()).collect().item()
        for y in years
    }


def _raw_drift(raw_dir: Path, years: Sequence[int]) -> dict[int, SchemaDrift]:
    """Per-year schema check of the raw parquet files (schema only, no data read)."""
    drift: dict[int, SchemaDrift] = {}
    for y in years:
        names = pl.scan_parquet(raw_ingest.raw_path(raw_dir, y)).collect_schema().names()
        try:
            drift[y] = check_raw_columns(names)
        except ValueError as exc:
            raise ValueError(f"raw year {y}: {exc}") from exc
    return drift


def ingest(
    source_dir: Path,
    raw_dir: Path,
    out: Path,
    adjudicantes_out: Path,
    adjudicatarios_out: Path,
    *,
    years: Sequence[int] = (),
    min_value: float = MIN_VALUE,
    ingested_at: datetime | None = None,
    rebuild: bool = False,
    manifest: Path | None = None,
) -> IngestSummary:
    """Raw-ingest `years` (unless `rebuild`), then rebuild the treated layer from all raw years.

    With `rebuild`, the treated and entity parquets are deleted first and the xlsx files are not
    read: everything is recomputed from `raw_dir`.
    """
    start = time.monotonic()
    run_ts = _utc(ingested_at or datetime.now(UTC))

    if rebuild:
        for path in (out, adjudicantes_out, adjudicatarios_out):
            path.unlink(missing_ok=True)
    else:
        raw_ingest.ingest_years(source_dir, raw_dir, years)

    all_years = raw_ingest.raw_years(raw_dir)
    if not all_years:
        raise FileNotFoundError(f"no raw parquet files in {raw_dir}; ingest a workbook first")
    read = _raw_counts(raw_dir, all_years)
    drift = _raw_drift(raw_dir, all_years)
    for y, d in drift.items():
        if d.missing_optional:
            logger.warning(
                "raw year %d: optional source columns missing, null-filled: %s",
                y,
                ", ".join(d.missing_optional),
            )
        if d.unknown:
            logger.warning(
                "raw year %d: upstream columns not consumed (unknown): %s", y, ", ".join(d.unknown)
            )

    src = pl.concat(
        [
            pl.scan_parquet(raw_ingest.raw_path(raw_dir, y)).with_columns(
                pl.lit(y, dtype=pl.Int32).alias("_src_year")
            )
            for y in all_years
        ],
        how="diagonal_relaxed",
    )
    if src.select(pl.col(CONTRACT_ID).is_null().any()).collect().item():
        raise ValueError(f"null {CONTRACT_ID} in raw data; change tracking needs a key")
    deduped = _dedup(src)

    treated = treat(deduped.drop("_src_year"), min_value=min_value)
    tracked = merge_tracking(
        treated.join(deduped.select(CONTRACT_ID, "_src_year"), on=CONTRACT_ID, how="left"),
        _previous(out),
        run_ts,
    )

    out.parent.mkdir(parents=True, exist_ok=True)
    staged = out.with_suffix(".staged.parquet")
    tmp = out.with_suffix(".tmp")
    tracked.sink_parquet(staged)
    try:
        counts = (
            pl.scan_parquet(staged)
            .group_by("_src_year")
            .agg(
                pl.len().alias("ingested"),
                (pl.col("_status") == "new").sum().alias("new"),
                (pl.col("_status") == "updated").sum().alias("updated"),
                (pl.col("_status") == "unchanged").sum().alias("unchanged"),
                pl.col("is_below_min_value").sum().alias("below_min"),
            )
            .collect()
        )
        pl.scan_parquet(staged).select(TREATED_COLUMNS).sink_parquet(tmp)
        tmp.replace(out)
    finally:
        staged.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)

    by_year = {r["_src_year"]: r for r in counts.iter_rows(named=True)}
    zero = {"ingested": 0, "new": 0, "updated": 0, "unchanged": 0, "below_min": 0}
    per_year = [
        YearSummary(year=y, read=read[y], **{k: by_year.get(y, zero)[k] for k in zero})
        for y in all_years
    ]

    parties = (
        deduped.select(ADJUDICANTE, ADJUDICATARIOS, CONTRACT_ID)
        .with_columns(
            unescape_html_entities(pl.col(ADJUDICANTE)),
            unescape_html_entities(pl.col(ADJUDICATARIOS)),
        )
        .collect()
    )
    adjudicantes_total = entities_db.write_entities(
        entities_db.adjudicantes(parties), adjudicantes_out
    )
    adjudicatarios_total = entities_db.write_entities(
        entities_db.adjudicatarios(parties), adjudicatarios_out
    )

    _write_manifest_treated(manifest or out.parent / "manifest.json", min_value, drift)
    superseded = sum(y.superseded for y in per_year)
    if superseded:
        logger.info(
            "%s rows superseded by a later year's file (same %s)", f"{superseded:_}", CONTRACT_ID
        )
    logger.info("ingest complete in %.1fs", time.monotonic() - start)
    return IngestSummary(
        per_year=per_year,
        adjudicantes_total=adjudicantes_total,
        adjudicatarios_total=adjudicatarios_total,
        min_value=min_value,
        ingested_at=run_ts,
        drift=drift,
    )
