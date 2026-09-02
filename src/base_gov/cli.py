"""Command line entry points for the xlsx -> parquet contracts pipeline."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer

from base_gov import analysis
from base_gov import bulk_ingest as bulk_ingest_module
from base_gov import entities_db as entities_db_module
from base_gov.xlsx_ingest import DEFAULT_ROWS, load_contracts

app = typer.Typer(
    add_completion=False,
    help="Load base.gov.pt's public procurement bulk export (.xlsx) and query it as parquet.",
)

logger = logging.getLogger("base_gov")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


@app.command("xlsx-analyze")
def xlsx_analyze(
    xlsx: Annotated[Path, typer.Option("--xlsx", help="Path to the contracts workbook.")] = Path(
        "data/contratos2025.xlsx"
    ),
    rows: Annotated[
        int, typer.Option("--rows", help="Rows to read from the workbook.")
    ] = DEFAULT_ROWS,
    municipalities_only: Annotated[
        bool, typer.Option("--municipalities-only", help="Only print the municipality count.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Print a first-cut summary of the contracts workbook (capped read)."""
    _configure_logging(verbose)
    df = load_contracts(xlsx, rows=rows)
    muni_count = analysis.municipality_contracts(df).height

    if municipalities_only:
        typer.echo(f"municipality contracts: {muni_count}")
        return

    id_stats = analysis.contract_id_stats(df)
    typer.echo(f"rows read: {df.height}")
    typer.echo(f"distinct adjudicantes: {analysis.distinct_adjudicantes(df)}")
    typer.echo(f"distinct adjudicatarios: {analysis.distinct_adjudicatarios(df)}")
    typer.echo(
        f"contract ids: {id_stats['count']} ({id_stats['n_unique']} unique, "
        f"is_unique={id_stats['is_unique']})"
    )
    typer.echo(f"total value (EUR): {analysis.total_value_eur(df):,.2f}")
    typer.echo("by contract type:")
    for row in analysis.by_contract_type(df).iter_rows(named=True):
        typer.echo(f"  {row['tipoContrato']}: {row['count']} ({row['total_value_eur']:,.2f} EUR)")
    typer.echo(f"municipality contracts: {muni_count}")


@app.command("xlsx-entities")
def xlsx_entities(
    xlsx: Annotated[Path, typer.Option("--xlsx", help="Path to the contracts workbook.")] = Path(
        "data/contratos2025.xlsx"
    ),
    rows: Annotated[
        int, typer.Option("--rows", help="Rows to read from the workbook.")
    ] = DEFAULT_ROWS,
    adjudicantes_out: Annotated[
        Path, typer.Option("--adjudicantes-out", help="Destination parquet for adjudicantes.")
    ] = Path("data/entities_adjudicantes.parquet"),
    adjudicatarios_out: Annotated[
        Path, typer.Option("--adjudicatarios-out", help="Destination parquet for adjudicatarios.")
    ] = Path("data/entities_adjudicatarios.parquet"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Extract deduplicated entities from the workbook and upsert them into
    the two entities parquet files."""
    _configure_logging(verbose)
    df = load_contracts(xlsx, rows=rows)

    adjudicantes_rows = entities_db_module.write_entities(
        entities_db_module.adjudicantes(df), adjudicantes_out
    )
    typer.echo(f"wrote {adjudicantes_rows} adjudicantes into {adjudicantes_out}")

    adjudicatarios_rows = entities_db_module.write_entities(
        entities_db_module.adjudicatarios(df), adjudicatarios_out
    )
    typer.echo(f"wrote {adjudicatarios_rows} adjudicatarios into {adjudicatarios_out}")


@app.command("bulk-ingest")
def bulk_ingest(
    xlsx: Annotated[Path, typer.Option("--xlsx", help="Path to the contracts workbook.")] = Path(
        "data/contratos2025.xlsx"
    ),
    out: Annotated[Path, typer.Option("--out", help="Destination parquet for contracts.")] = Path(
        "data/contracts.parquet"
    ),
    adjudicantes_out: Annotated[
        Path, typer.Option("--adjudicantes-out", help="Destination parquet for adjudicantes.")
    ] = Path("data/entities_adjudicantes.parquet"),
    adjudicatarios_out: Annotated[
        Path, typer.Option("--adjudicatarios-out", help="Destination parquet for adjudicatarios.")
    ] = Path("data/entities_adjudicatarios.parquet"),
    force: Annotated[
        bool,
        typer.Option(
            "--force", help="Replace the existing contracts parquet instead of upserting."
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Read the full contracts workbook (no row cap) and upsert contracts and
    both entity roles into their parquet files."""
    _configure_logging(verbose)
    result = bulk_ingest_module.ingest(xlsx, out, adjudicantes_out, adjudicatarios_out, force=force)
    typer.echo(f"rows read: {result.rows_read}")
    typer.echo(f"wrote {result.contracts_total} contracts into {out}")
    typer.echo(f"wrote {result.adjudicantes_total} adjudicantes into {adjudicantes_out}")
    typer.echo(f"wrote {result.adjudicatarios_total} adjudicatarios into {adjudicatarios_out}")


if __name__ == "__main__":  # pragma: no cover
    app()
