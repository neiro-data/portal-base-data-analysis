"""Command line entry points for scraping base.gov.pt."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Annotated

import typer

from base_gov.client import BaseGovClient, ClientConfig
from base_gov.export import (
    CONTRACT_COLUMNS,
    ENTITY_COLUMNS,
    contracts_frame,
    discard_parts,
    entities_frame,
    existing_parts,
    join_parts,
    parts_dir,
    rows_in_parts,
    write_part,
)
from base_gov.models import Contract
from base_gov.queries import month_windows
from base_gov.scrape import entity_contracts, search_entity_batches

app = typer.Typer(
    add_completion=False,
    help="Retrieve public procurement data from base.gov.pt (BASE / Base4).",
)

logger = logging.getLogger("base_gov")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"expected YYYY-MM-DD, got {value!r}") from exc


@app.command()
def entities(
    text: Annotated[
        str, typer.Option("--text", "-t", help="NIPC, description or country to match.")
    ] = "Município",
    out: Annotated[Path, typer.Option("--out", "-o", help="Destination CSV path.")] = Path(
        "data/entities.csv"
    ),
    limit: Annotated[
        int | None, typer.Option("--limit", help="Stop after this many rows this run.")
    ] = None,
    resume: Annotated[
        bool,
        typer.Option(
            "--resume/--restart",
            help="Continue from batches already on disk, or discard them and start over.",
        ),
    ] = True,
    keep_parts: Annotated[
        bool, typer.Option("--keep-parts", help="Keep the per-batch CSVs after joining.")
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Per-request timeout in seconds.")
    ] = 300.0,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Search `Entidades` and save the full result set as CSV.

    Ingested in batches of one result page. Every batch is written to its own
    CSV under `<out>.parts/` as soon as it arrives, and the batches are joined
    into `out` at the end. A cold page costs roughly two minutes, so this keeps
    an interrupted run's work: re-running resumes from the next missing batch.
    """
    _configure_logging(verbose)
    done = existing_parts(out)
    if done and not resume:
        discard_parts(out)
        done = []
    rows_before = rows_in_parts(out)
    if done:
        logger.info("resuming: %s batch(es), %s rows already on disk", len(done), rows_before)

    fetched = 0
    with BaseGovClient(ClientConfig(timeout=timeout)) as client:
        batches = search_entity_batches(client, text, start_page=len(done), rows_before=rows_before)
        for offset, batch in enumerate(batches):
            index = len(done) + offset
            write_part(entities_frame(batch), out, index)
            fetched += len(batch)
            logger.info("batch %s: %s rows saved (%s this run)", index, len(batch), fetched)
            if limit is not None and fetched >= limit:
                break

    path, rows = join_parts(out, columns=ENTITY_COLUMNS)
    if keep_parts:
        typer.echo(f"batches kept in {parts_dir(out)}")
    else:
        discard_parts(out)
    typer.echo(f"joined {rows} entities into {path}")


@app.command()
def contracts(
    contracting: Annotated[
        str,
        typer.Option(
            "--contracting",
            "-c",
            help="Awarding entity: NIPC (preferred) or description fragment.",
        ),
    ],
    start: Annotated[str, typer.Option("--start", "-s", help="Contract date from (YYYY-MM-DD).")],
    end: Annotated[str, typer.Option("--end", "-e", help="Contract date to (YYYY-MM-DD).")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Destination CSV path.")] = Path(
        "data/contracts.csv"
    ),
    resume: Annotated[
        bool,
        typer.Option(
            "--resume/--restart",
            help="Continue from batches already on disk, or discard them and start over.",
        ),
    ] = True,
    keep_parts: Annotated[
        bool, typer.Option("--keep-parts", help="Keep the per-batch CSVs after joining.")
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Per-request timeout in seconds.")
    ] = 300.0,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Collect one entity's contracts over a date range, one month at a time.

    Each month is a batch, written to its own CSV under `<out>.parts/` as soon
    as it completes and joined into `out` at the end. Re-running resumes from
    the first month with no batch on disk.
    """
    _configure_logging(verbose)
    start_date = _parse_date(start)
    end_date = _parse_date(end)
    windows = list(month_windows(start_date, end_date))

    done = existing_parts(out)
    if done and not resume:
        discard_parts(out)
        done = []
    if len(done) > len(windows):
        msg = (
            f"{len(done)} batches on disk exceed the {len(windows)} months requested; "
            f"they are from a different range. Pass --restart, or a different --out."
        )
        raise typer.BadParameter(msg)

    remaining = windows[len(done) :]
    logger.info("%s month window(s) to fetch (%s already on disk)", len(remaining), len(done))

    with BaseGovClient(ClientConfig(timeout=timeout)) as client:
        for offset, (window_start, window_end) in enumerate(remaining):
            batch: list[Contract] = list(
                entity_contracts(
                    client,
                    contracting=contracting,
                    start=window_start,
                    end=window_end,
                )
            )
            index = len(done) + offset
            write_part(contracts_frame(batch), out, index)
            logger.info(
                "batch %s (%s): %s rows saved",
                index,
                window_start.strftime("%Y-%m"),
                len(batch),
            )

    path, rows = join_parts(out, columns=CONTRACT_COLUMNS)
    if keep_parts:
        typer.echo(f"batches kept in {parts_dir(out)}")
    else:
        discard_parts(out)
    typer.echo(f"joined {rows} contracts into {path}")


if __name__ == "__main__":  # pragma: no cover
    app()
