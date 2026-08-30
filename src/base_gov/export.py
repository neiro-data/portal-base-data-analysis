"""CSV output for scraped records."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import polars as pl

from base_gov.models import Contract, Entity

ENTITY_COLUMNS = ["id", "nif", "description", "country"]
CONTRACT_COLUMNS = [
    "id",
    "publication_date",
    "contracting",
    "contracted",
    "object_description",
    "procedure_type",
    "initial_price",
]


def entities_frame(entities: Iterable[Entity]) -> pl.DataFrame:
    return _frame([asdict(e) for e in entities], ENTITY_COLUMNS)


def contracts_frame(contracts: Iterable[Contract]) -> pl.DataFrame:
    return _frame([asdict(c) for c in contracts], CONTRACT_COLUMNS)


def _frame(rows: list[dict[str, Any]], columns: list[str]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema={name: pl.String for name in columns})
    return pl.DataFrame(rows).select(columns)


def write_csv(frame: pl.DataFrame, destination: Path) -> Path:
    """Write `frame` to `destination`, creating parent directories as needed."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.write_csv(destination)
    return destination


# --------------------------------------------------------------- batch parts
#
# Long scrapes land one CSV per batch beside the destination, then get joined
# into the final file. Each batch is durable the moment it is written, so an
# interrupted run keeps everything it fetched and can resume from the next
# batch rather than starting over against a server that takes minutes a page.


def parts_dir(destination: Path) -> Path:
    """Directory holding the per-batch CSVs for `destination`."""
    return destination.parent / f"{destination.stem}.parts"


def part_path(destination: Path, index: int) -> Path:
    """Path of batch `index`. Zero-padded so lexical order is numeric order."""
    return parts_dir(destination) / f"part-{index:05d}.csv"


def existing_parts(destination: Path) -> list[Path]:
    """Every batch already on disk for `destination`, in order."""
    directory = parts_dir(destination)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("part-*.csv"))


def rows_in_parts(destination: Path) -> int:
    """How many rows the batches on disk already hold.

    Resuming needs this exactly: the paginator stops on the server-reported
    total, and estimating from the page stride over-requests when the resumed
    page is a short final one.
    """
    return sum(pl.read_csv(part).height for part in existing_parts(destination))


def write_part(frame: pl.DataFrame, destination: Path, index: int) -> Path:
    """Write one batch. Written to a temporary name first so a crash mid-write
    cannot leave a truncated part that a later resume would trust."""
    target = part_path(destination, index)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(".csv.tmp")
    frame.write_csv(staging)
    staging.replace(target)
    return target


def join_parts(destination: Path, *, columns: list[str]) -> tuple[Path, int]:
    """Concatenate every batch into `destination`; returns the path and row count.

    Batches are read back rather than reused from memory, so the joined file
    reflects exactly what survived on disk.
    """
    parts = existing_parts(destination)
    if not parts:
        frame = _frame([], columns)
    else:
        frame = pl.concat(
            [pl.read_csv(part, schema_overrides={c: pl.String for c in columns}) for part in parts]
        )

    write_csv(frame, destination)
    return destination, frame.height


def discard_parts(destination: Path) -> None:
    """Remove the batch directory once its contents are safely joined."""
    directory = parts_dir(destination)
    if not directory.is_dir():
        return
    for part in directory.glob("part-*.csv*"):
        part.unlink()
    directory.rmdir()
