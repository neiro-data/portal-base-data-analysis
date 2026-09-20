"""Command line entry points for the xlsx -> parquet contracts pipeline."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Annotated

import httpx
import typer

from base_gov import analysis, dados_gov
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


@app.command("download-contracts")
def download_contracts(
    years: Annotated[
        int, typer.Option("--years", help="Most recent N years to fetch (current year included).")
    ] = dados_gov.DEFAULT_YEARS,
    year: Annotated[
        list[int] | None,
        typer.Option("--year", help="Explicit year (repeatable); overrides --years."),
    ] = None,
    from_year: Annotated[
        int | None, typer.Option("--from-year", help="Earliest year to fetch (backfill only).")
    ] = None,
    dest: Annotated[Path, typer.Option("--dest", help="Destination directory.")] = Path(
        "data/source"
    ),
    force: Annotated[
        bool, typer.Option("--force", help="Ignore the manifest and overwrite local files.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="List resources and planned action; no downloads.")
    ] = False,
    verify_only: Annotated[
        bool,
        typer.Option("--verify-only", help="Compare local sha1 to the API; exit 1 on mismatch."),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Download contratosYYYY.xlsx from dados.gov.pt with sha1 verification."""
    _configure_logging(verbose)
    with dados_gov.make_client() as client:
        selected = dados_gov.select_years(
            dados_gov.list_resources(client=client), years=years, only=year, from_year=from_year
        )
        if not selected:
            typer.echo("no matching resources", err=True)
            raise typer.Exit(1)

        if dry_run or verify_only:
            mismatches = 0
            typer.echo(f"{'year':<6} {'size':<9} {'last_modified':<21} action")
            for res in selected:
                state = dados_gov.verify_local(res, dados_gov.target_path(res, dest))
                if state == "mismatch":
                    mismatches += 1
                action = {
                    "absent": "would download",
                    "match": "unchanged (local sha1 match)",
                    "mismatch": "would replace",
                }[state]
                stamp = res.last_modified.strftime("%Y-%m-%dT%H:%M:%SZ")
                typer.echo(
                    f"{res.year:<6} {res.filesize / 1e6:>6.1f} MB {stamp:<21} {action}"
                    f"  sha1={res.sha1}"
                )
            if verify_only and mismatches:
                raise typer.Exit(1)
            return

        manifest_path = dest.parent / "manifest.json"
        manifest = dados_gov.read_manifest(manifest_path)
        failures = 0
        for index, res in enumerate(selected):
            try:
                result = dados_gov.download(
                    res, dest, manifest=manifest, force=force, client=client
                )
            except (dados_gov.DadosGovError, httpx.HTTPError) as exc:
                failures += 1
                typer.echo(f"{res.year}: failed: {exc}", err=True)
                continue
            dados_gov.write_manifest(manifest_path, manifest)
            typer.echo(
                f"{res.year}: {result.status} ({result.bytes_written} bytes) -> {result.path}"
            )
            if result.status == "downloaded" and index < len(selected) - 1:
                time.sleep(1.0)
        if failures:
            raise typer.Exit(1)


if __name__ == "__main__":  # pragma: no cover
    app()
