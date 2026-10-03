# portal-base-data-analysis

## Resume

This project analyses public procurement data from Portugal's
[BASE portal](https://www.base.gov.pt/base4). The goal is to open-source the collected data.

BASE has no API of its own, but dados.gov.pt republishes its yearly `.xlsx` bulk export. The CLI
downloads the three most recent years (default cap) from that API, converts each to a raw parquet
file, and builds one treated parquet. A local PostgreSQL stack loads the treated parquet into a
`procurement` schema and Grafana renders it. *Future direction: query the source data from
PostgreSQL instead of reading the parquet files directly in Python.*

- **Stack:** Python 3.12, [uv](https://docs.astral.sh/uv/), polars, typer. ruff for lint
  and format, mypy in strict mode for types, pytest for tests.
- **What it does:** it ingests the bulk workbook to parquet (contracts, adjudicantes,
  adjudicatarios). It loads the parquet files into a `procurement` schema in PostgreSQL 17 with
  `pg_parquet`. It serves six provisioned Grafana dashboards.
- **Entry point:** `src/base_gov/cli.py` (the `base-gov` console script).
- **Host requirements:** `uv` for the CLI and the checks. Docker and `make` for the database and
  the dashboards.

## Agent read
If you are an AI agent, read also [README_AGENT.md](README_AGENT.md).

## Quick start

```bash
uv sync                                                            # install
uv run base-gov download-contracts                                 # data/source/contratosYYYY.xlsx
bin/ingest-contracts.sh --year 2024 --year 2025 --year 2026        # raw + treated parquet
```

`download-contracts` reads the dados.gov.pt API and writes `data/source/contratosYYYY.xlsx`. Run
it before `bulk-ingest`. See
[README_AGENT.md — Where the data comes from](README_AGENT.md#where-the-data-comes-from).

For every other command — the bulk ingest, the database, the dashboards, and the development
tasks — see **[scripts-guide.md](scripts-guide.md)**.

## Download contracts

`uv run base-gov download-contracts` gets `contratosYYYY.xlsx` from the dados.gov.pt API into
`data/source/`. Each file is checked against the sha1 that the API publishes. The command writes
the result to `data/manifest.json`, and a run skips a file that did not change.

Flags: `--years N` (default 3; `--years 10` gives the 10-year range for the "Last 10 years" dashboard picker), `--year Y` (repeatable), `--from-year Y`, `--dest PATH`,
`--force`, `--dry-run`, `--verify-only`, `--verbose`.

A local file with a different sha1 is replaced after a verified download.
`bin/backfill-contracts.sh` downloads every year from 2012. It stops unless you set
`BACKFILL_CONFIRM=yes-download-all-years`.

## Bulk ingest

The scope is whatever years you downloaded: the default is the last three, and `download-contracts --years 10` plus a re-ingest gives the 10-year range (the dashboards' "Last 10 years" picker). The currently loaded data starts in 2022; the current year is year-to-date. `bin/ingest-contracts.sh`
works in two layers. **Raw:** each `data/source/contratos<YEAR>.xlsx` becomes
`data/raw/contracts_raw_<YEAR>.parquet`, columns verbatim. **Treated:** `data/contracts.parquet`
is built from all raw years together. Contracts under EUR 10 are flagged
(`is_below_min_value`), not dropped.

`xlsx-analyze` and `xlsx-entities` still read one workbook with a row cap, default 1000. The
uncapped read peaks near 2 to 4 GB RSS per workbook, because calamine materializes the whole sheet.

See [scripts-guide.md — Bulk ingest: raw and treated](scripts-guide.md#bulk-ingest-raw-and-treated).

## Database and dashboards

A Docker stack runs PostgreSQL 17 with
[pg_parquet](https://github.com/CrunchyData/pg_parquet) 0.5.1. It loads the parquet files into
the `procurement` schema. A Compose profile adds Grafana with six provisioned dashboards: an
overview, a contract table, the same two scoped to municípios, and one drill-down each for an
adjudicante and an adjudicatário.

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
bin/                    download-contracts.sh, backfill-contracts.sh, ingest-contracts.sh (xlsx → parquet), ingest-postgres.sh (parquet → PostgreSQL)
docker/                 postgres (pg_parquet build) + grafana provisioning and dashboards
docker-compose.yml      postgres service + grafana (profile: dashboard)
notebooks/              exploratory analysis notebooks, run over the parquet files directly
sql/                    schema, views and indexes (001-007); sql/ingest/ holds the load steps
src/base_gov/           xlsx ingest, bulk ingest, entities/analysis helpers + the `base-gov` CLI
tests/                  pytest suite, no database required
data/                   gitignored: source/ (xlsx), raw/ (raw parquet), contracts.parquet,
                        entity parquets, manifest.json (download checksums)
README.md               this file - what it is and the first run
scripts-guide.md        every command, grouped by target
SCHEMA.md               the data model reference
README_AGENT.md         portal behaviour, load limits, security notes
```

## Analysis notebook

`notebooks/procurement_analysis.ipynb` is a full exploratory analysis over `data/*.parquet`
directly (no database needed): the raw ingested dataframe, the column-selection rationale for the
cleaned frame, an adjudicante fuzzy-name-duplicate pass, contract time-series and a single-contract
lifecycle walkthrough, and a set of database/indexing improvement suggestions grounded in what the
data actually shows. Six focused notebooks sit beside it (ajuste direto, municipio, municipio vs
ajuste direto, concurso publico, consulta previa, procedures comparison). They key entities by
NIPC (`base_gov.entities`), drop contracts under €10 and use `base_gov.stats`. Needs the
`jupyter`/`matplotlib` dev deps (`uv sync`; `rapidfuzz` is a runtime dep), and
`data/contracts.parquet` + the two entity parquet files already ingested (see Bulk ingest above).
