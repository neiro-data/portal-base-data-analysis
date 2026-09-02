# portal-base-data-analysis

## Resume

This project analyses public procurement data from Portugal's
[BASE portal](https://www.base.gov.pt/base4). The goal is to open-source the collected data.

The portal publishes no API. The source is its own `.xlsx` bulk export, downloaded by hand,
converted once to parquet, and queried from there. A local PostgreSQL stack loads that parquet
into a `procurement` schema and Grafana renders it. *Future direction: query the source data from
PostgreSQL instead of reading the parquet files directly in Python.*

- **Stack:** Python 3.12, [uv](https://docs.astral.sh/uv/), polars, typer. ruff for lint
  and format, mypy in strict mode for types, pytest for tests.
- **What it does:** it ingests the bulk workbook to parquet (contracts, adjudicantes,
  adjudicatarios). It loads the parquet files into a `procurement` schema in PostgreSQL 17 with
  `pg_parquet`. It serves four provisioned Grafana dashboards.
- **Entry point:** `src/base_gov/cli.py` (the `base-gov` console script).
- **Host requirements:** `uv` for the CLI and the checks. Docker and `make` for the database and
  the dashboards.

## Agent read
If you are an AI agent, read also [README_AGENT.md](README_AGENT.md).

## Quick start

```bash
uv sync                                                            # install
uv run base-gov xlsx-analyze                                       # summarise the workbook
```

Download `data/contratos2025.xlsx` from the BASE portal by hand first — see
[README_AGENT.md — Where the data comes from](README_AGENT.md#where-the-data-comes-from).

For every other command — the bulk ingest, the database, the dashboards, and the development
tasks — see **[scripts-guide.md](scripts-guide.md)**.

## Bulk ingest

`data/contratos2025.xlsx` is the portal's own bulk export, about 66 MB. Two commands read it with
a row cap, default 1000. One script, `bin/ingest-contracts.sh`, reads it without the cap and
writes `data/contracts.parquet`.

The uncapped read peaks near 2 to 4 GB RSS, because calamine materializes the whole sheet.

See [scripts-guide.md — Bulk xlsx ingest, uncapped](scripts-guide.md#bulk-xlsx-ingest-uncapped).

## Database and dashboards

A Docker stack runs PostgreSQL 17 with
[pg_parquet](https://github.com/CrunchyData/pg_parquet) 0.5.1. It loads the parquet files into
the `procurement` schema. A Compose profile adds Grafana with four provisioned dashboards: an
overview, a contract table, and the same two scoped to municípios.

Run the ingest before you start Grafana, or the dashboards show no data.

Grafana runs with anonymous Editor access and binds to `127.0.0.1` only. Do not expose it. See
[README_AGENT.md — Security notes](README_AGENT.md#security-notes).

See [scripts-guide.md — PostgreSQL](scripts-guide.md#postgresql) and
[scripts-guide.md — Grafana dashboards](scripts-guide.md#grafana-dashboards).

## Schema

The `procurement` schema holds one fact table, `contracts`, two entity tables, and a bridge table
for the awardees. Four views sit above them. Two of the views serve the Grafana panels.

Contracts deduplicate on the last row seen, entities on the first. The reason is that BASE amends
a contract after the signature.

Full table and view reference, the migration list, and the pipeline are in **[SCHEMA.md](SCHEMA.md)**.

## Layout

```
bin/                    ingest-contracts.sh (xlsx → parquet), ingest-postgres.sh (parquet → PostgreSQL)
docker/                 postgres (pg_parquet build) + grafana provisioning and dashboards
docker-compose.yml      postgres service + grafana (profile: dashboard)
sql/                    schema, views and indexes (001-006); sql/ingest/ holds the load steps
src/base_gov/           xlsx ingest, bulk ingest, entities/analysis helpers + the `base-gov` CLI
tests/                  pytest suite, no database required
data/                   csv/, parquet/, the source workbook - gitignored
README.md               this file - what it is and the first run
scripts-guide.md        every command, grouped by target
SCHEMA.md               the data model reference
README_AGENT.md         portal behaviour, load limits, security notes
```
