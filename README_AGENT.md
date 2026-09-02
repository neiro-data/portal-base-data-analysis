# README_AGENT.md

Background that an agent needs to change this repository, and that a person who only wants to
run the tool does not need. Referenced from [README.md](README.md).

## Where the data comes from

BASE publishes no API. The source is the portal's own bulk export: a person downloads
`data/contratos2025.xlsx` by hand from base.gov.pt (there is no scripted path to it).

From there the pipeline is entirely local:

1. `bin/ingest-contracts.sh` reads the workbook uncapped and writes `data/contracts.parquet`
   plus the two entity parquet files (adjudicantes, adjudicatarios). The `xlsx-analyze` /
   `xlsx-entities` / `bulk-ingest` CLI commands wrap this same read, capped or uncapped.
2. `bin/ingest-postgres.sh` loads those parquet files into the `procurement` schema in
   PostgreSQL, which Grafana renders.

The uncapped read peaks near 2-4 GB RSS, because `fastexcel`/calamine materializes the whole
sheet. There is no way to stream it in smaller pieces today.

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
