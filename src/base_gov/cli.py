"""Command line entry points for scraping base.gov.pt."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Annotated

import typer

from base_gov.client import BaseGovClient, ClientConfig
from base_gov.export import contracts_frame, entities_frame, write_csv
from base_gov.models import Contract, Entity
from base_gov.queries import month_windows
from base_gov.scrape import entity_contracts, search_entities

app = typer.Typer(
    add_completion=False,
    help="Retrieve public procurement data from base.gov.pt (BASE / Base4).",
)

logger = logging.getLogger("base_gov")

#: Rows between CSV checkpoints; one page of results.
CHECKPOINT_EVERY = 25


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
    limit: Annotated[int | None, typer.Option("--limit", help="Stop after this many rows.")] = None,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Per-request timeout in seconds.")
    ] = 300.0,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Search `Entidades` and save the full result set as CSV.

    Roughly two minutes per 25-row page on a cold query, so a few hundred
    entities is a long-running job. The CSV is rewritten every
    `CHECKPOINT_EVERY` rows, so an interrupted run still leaves on disk
    whatever it had already fetched.
    """
    _configure_logging(verbose)
    collected: list[Entity] = []
    with BaseGovClient(ClientConfig(timeout=timeout)) as client:
        for entity in search_entities(client, text, max_items=limit):
            collected.append(entity)
            if len(collected) % CHECKPOINT_EVERY == 0:
                write_csv(entities_frame(collected), out)
                logger.info("%s rows fetched (checkpointed)", len(collected))

    path = write_csv(entities_frame(collected), out)
    typer.echo(f"wrote {len(collected)} entities to {path}")


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
    timeout: Annotated[
        float, typer.Option("--timeout", help="Per-request timeout in seconds.")
    ] = 300.0,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Collect one entity's contracts over a date range, one month at a time.

    The CSV is rewritten after every month window, so an interrupted run still
    leaves the months already fetched on disk.
    """
    _configure_logging(verbose)
    start_date = _parse_date(start)
    end_date = _parse_date(end)
    windows = list(month_windows(start_date, end_date))
    logger.info("%s month window(s) to fetch", len(windows))

    collected: list[Contract] = []
    with BaseGovClient(ClientConfig(timeout=timeout)) as client:
        for window_start, window_end in windows:
            before = len(collected)
            collected.extend(
                entity_contracts(
                    client,
                    contracting=contracting,
                    start=window_start,
                    end=window_end,
                )
            )
            write_csv(contracts_frame(collected), out)
            logger.info(
                "%s: +%s rows (%s total, checkpointed)",
                window_start.strftime("%Y-%m"),
                len(collected) - before,
                len(collected),
            )

    typer.echo(f"wrote {len(collected)} contracts to {out}")


if __name__ == "__main__":  # pragma: no cover
    app()
