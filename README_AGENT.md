# README_AGENT.md

Background that an agent needs to change this repository, and that a person who only wants to
run the tool does not need. Referenced from [README.md](README.md).

## Where the data comes from

BASE has no API of its own. dados.gov.pt republishes the portal's yearly bulk export as
`contratosYYYY.xlsx`. `uv run base-gov download-contracts` lists the resources through the
dados.gov.pt API and reads each one's year, size, `last_modified` and `sha1`. It checks every
download against that sha1 and records the result in `data/manifest.json`.

Caveats:

- The API republishes all years together, so `last_modified` is the same for every year and does
  not show that one year changed. The sha1 is the only change signal.
- A local workbook is never overwritten by an unverified download. A file with a different sha1 is
  replaced only after the new download passes the sha1 check.
- The default scope is the three most recent years. `bin/backfill-contracts.sh` (all years from
  2012) is guarded and stops without `BACKFILL_CONFIRM=yes-download-all-years`.

From there the pipeline is local:

1. `bin/ingest-contracts.sh` converts each workbook to `data/raw/contracts_raw_<YEAR>.parquet`,
   then builds `data/contracts.parquet` and the two entity parquet files from all raw years. The
   treated stage reads parquet, not xlsx. `--rebuild` needs no workbook at all.
2. `bin/ingest-postgres.sh` loads the treated and entity parquet files into the `procurement`
   schema in PostgreSQL, which Grafana renders.

The workbook read peaks near 2-4 GB RSS, because `fastexcel`/calamine materializes the whole
sheet. The raw stage reads one workbook at a time. There is no way to stream a sheet today.

*Future direction: query the source data from PostgreSQL instead of reading the parquet files
directly in Python — not built, see [README.md — Resume](README.md#resume).*

## Security notes

The stack is local only. It is not internet-facing. PostgreSQL binds to
`${POSTGRES_PORT:-55433}` on localhost. Grafana binds to `127.0.0.1` in `docker-compose.yml`.

Grafana runs with anonymous Editor access and no login form. Any client that reaches the port can
read and change the dashboards. Do not deploy this setup to a shared or internet-facing host.

`sql/004_grafana_role.sql` creates the read-only `grafana_reader` role with a literal
placeholder password. Rotate it before any non-local deployment. Change it in two places
together: that SQL file, and `GRAFANA_DB_PASSWORD` in `.env`. The Grafana datasource reads the
`.env` value to authenticate.

## Licence and provenance

Instituto dos Mercados Públicos, do Imobiliário e da Construção (IMPIC) publishes the data on
base.gov.pt. Check the terms of IMPIC before you redistribute a derived dataset.

## Docs map

| File | Content |
| --- | --- |
| [README.md](README.md) | The entry point: what the project is, and the first successful run. |
| [scripts-guide.md](scripts-guide.md) | Every command, grouped by target. |
| [SCHEMA.md](SCHEMA.md) | The parquet files, the tables, the views, the migrations. |
| README_AGENT.md | This file: portal behaviour, load limits, security notes. |
