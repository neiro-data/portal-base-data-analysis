"""Command line entry points for the xlsx -> parquet contracts pipeline."""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer

from base_gov import analysis, dados_gov, raw_ingest
from base_gov import bulk_ingest as bulk_ingest_module
from base_gov import entities_db as entities_db_module
from base_gov import report as report_module
from base_gov.stats import MIN_VALUE
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
        "data/source/contratos2025.xlsx"
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
        "data/source/contratos2025.xlsx"
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
    year: Annotated[
        list[int] | None,
        typer.Option("--year", help="Year to (re)convert from its workbook; repeatable."),
    ] = None,
    source_dir: Annotated[
        Path, typer.Option("--source-dir", help="Directory holding contratos<YEAR>.xlsx.")
    ] = Path("data/source"),
    xlsx: Annotated[
        Path | None,
        typer.Option("--xlsx", help="Deprecated: use --source-dir. Its parent is used."),
    ] = None,
    raw_dir: Annotated[
        Path, typer.Option("--raw-dir", help="Directory for the verbatim raw parquet files.")
    ] = Path("data/raw"),
    out: Annotated[Path, typer.Option("--out", help="Destination parquet for contracts.")] = Path(
        "data/contracts.parquet"
    ),
    adjudicantes_out: Annotated[
        Path, typer.Option("--adjudicantes-out", help="Destination parquet for adjudicantes.")
    ] = Path("data/entities_adjudicantes.parquet"),
    adjudicatarios_out: Annotated[
        Path, typer.Option("--adjudicatarios-out", help="Destination parquet for adjudicatarios.")
    ] = Path("data/entities_adjudicatarios.parquet"),
    min_value: Annotated[
        float,
        typer.Option("--min-value", help="Flag |preco_contratual| below this; 0 flags nothing."),
    ] = MIN_VALUE,
    ingested_at: Annotated[
        datetime | None,
        typer.Option("--ingested-at", help="Stamp this UTC time instead of now()."),
    ] = None,
    raw_only: Annotated[
        bool, typer.Option("--raw-only", help="Stop after writing the raw parquet files.")
    ] = False,
    rebuild: Annotated[
        bool,
        typer.Option(
            "--rebuild",
            help="Delete treated + entity parquets and rebuild from raw; no xlsx is read.",
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Convert workbooks to raw parquet, then rebuild the treated contracts and entities."""
    _configure_logging(verbose)
    if xlsx is not None:
        typer.echo("--xlsx is deprecated; use --source-dir", err=True)
        source_dir = xlsx.parent
    if rebuild and raw_only:
        raise typer.BadParameter("--rebuild and --raw-only are mutually exclusive")

    if raw_only:
        if not year:
            raise typer.BadParameter("--raw-only needs at least one --year")
        for y, rows in raw_ingest.ingest_years(source_dir, raw_dir, year).items():
            typer.echo(f"{y}: {rows:_} rows -> {raw_ingest.raw_path(raw_dir, y)}")
        return

    if not rebuild and not year:
        raise typer.BadParameter("pass at least one --year (or --rebuild)")
    summary = bulk_ingest_module.ingest(
        source_dir,
        raw_dir,
        out,
        adjudicantes_out,
        adjudicatarios_out,
        years=year or (),
        min_value=min_value,
        ingested_at=ingested_at,
        rebuild=rebuild,
    )
    typer.echo(summary.render())
    typer.echo(f"adjudicantes: {summary.adjudicantes_total:_}  ({adjudicantes_out})")
    typer.echo(f"adjudicatarios: {summary.adjudicatarios_total:_}  ({adjudicatarios_out})")


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
        int | None,
        typer.Option("--from-year", help="Fetch every year from this one on; overrides --years."),
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
        typer.Option(
            "--verify-only",
            help="Compare local sha1 to the API; exit 1 on mismatch or missing file.",
        ),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Download contratosYYYY.xlsx from dados.gov.pt with sha1 verification."""
    _configure_logging(verbose)
    with dados_gov.make_client() as client:
        try:
            selected = dados_gov.select_years(
                dados_gov.list_resources(client=client),
                years=years,
                only=year,
                from_year=from_year,
            )
        except (dados_gov.DadosGovError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            typer.echo(f"could not list resources: {exc}", err=True)
            raise typer.Exit(1) from exc
        if not selected:
            typer.echo("no matching resources", err=True)
            raise typer.Exit(1)
        missing = sorted(set(year or []) - {r.year for r in selected})
        if missing:
            typer.echo(f"years not published on dados.gov.pt: {missing}", err=True)
            raise typer.Exit(1)

        if dry_run or verify_only:
            problems = 0
            typer.echo(f"{'year':<6} {'size':<9} {'last_modified':<21} action")
            for res in selected:
                state = dados_gov.verify_local(res, dados_gov.target_path(res, dest))
                if state != "match":
                    problems += 1
                action = {
                    "absent": "MISSING locally" if verify_only else "would download",
                    "match": "would re-download (--force)"
                    if force and not verify_only
                    else "unchanged (local sha1 match)",
                    "mismatch": "LOCAL MISMATCH" if verify_only else "would replace",
                }[state]
                stamp = res.last_modified.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                typer.echo(
                    f"{res.year:<6} {res.filesize / 1e6:>6.1f} MB {stamp:<21} {action}"
                    f"  sha1={res.sha1}"
                )
            if verify_only and problems:
                raise typer.Exit(1)
            return

        manifest_path = dest.parent / "manifest.json"
        try:
            manifest = dados_gov.read_manifest(manifest_path)
        except dados_gov.DadosGovError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
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


@app.command("report")
def report(
    dashboard: Annotated[
        str, typer.Argument(help="Dashboard file stem (adjudicante-analysis) or uid.")
    ],
    var: Annotated[
        list[str] | None,
        typer.Option(
            "--var",
            help="name=value (repeatable). Multi-value: repeat the name or use a comma list.",
        ),
    ] = None,
    time_from: Annotated[
        str | None, typer.Option("--from", help="Range start (e.g. now-2y); default: dashboard's.")
    ] = None,
    time_to: Annotated[
        str | None, typer.Option("--to", help="Range end (e.g. now); default: dashboard's.")
    ] = None,
    out: Annotated[
        Path | None, typer.Option("--out", help="Output PDF; default reports/<stem>-<stamp>.pdf.")
    ] = None,
    grafana_url: Annotated[
        str | None,
        typer.Option(
            "--grafana-url",
            help="Grafana base URL (env GRAFANA_URL; else http://127.0.0.1:$GRAFANA_PORT|3001).",
        ),
    ] = None,
    timeout: Annotated[
        int, typer.Option("--timeout", min=1, help="Render timeout per panel, in seconds.")
    ] = 60,
    workers: Annotated[
        int, typer.Option("--workers", min=1, help="Panels rendered concurrently.")
    ] = 3,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Render a Grafana dashboard to an A4 PDF via the image renderer."""
    _configure_logging(verbose)
    url = (
        grafana_url
        or os.environ.get("GRAFANA_URL")
        or f"http://127.0.0.1:{os.environ.get('GRAFANA_PORT', '3001')}"
    )
    stem = dashboard.removesuffix(".json")
    stem = next((s for s, uid in report_module.REPORTABLE.items() if uid == stem), stem)
    target = out or Path("reports") / f"{stem}-{datetime.now():%Y%m%d-%H%M}.pdf"
    try:
        path = report_module.generate_report(
            dashboard,
            target,
            variables=var or [],
            time_from=time_from,
            time_to=time_to,
            grafana_url=url,
            timeout_s=timeout,
            workers=workers,
        )
    except report_module.ReportError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(str(path))


if __name__ == "__main__":  # pragma: no cover
    app()
