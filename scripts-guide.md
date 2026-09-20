# scripts-guide.md

Every command in this project, grouped by target. Referenced from [README.md](README.md).

## Download contracts

```bash
uv run base-gov download-contracts                # last 3 years -> data/source/
uv run base-gov download-contracts --dry-run      # list resources and planned action
uv run base-gov download-contracts --verify-only  # compare local sha1 to the API; exit 1 on mismatch or missing file
uv run base-gov download-contracts --year 2025 --force
```

Flags: `--years N` (default 3), `--year Y` (repeatable, overrides `--years`), `--from-year Y` (every year from Y on; overrides `--years`),
`--dest PATH`, `--force`, `--dry-run`, `--verify-only`, `--verbose`. Results go to
`data/manifest.json`. `bin/download-contracts.sh` wraps the command and passes all options through.

`bin/backfill-contracts.sh` downloads every year from 2012. It refuses to run without
`BACKFILL_CONFIRM=yes-download-all-years`. Do not run it: the project scope is three years, and
older workbooks are large. It is a guarded escape hatch only.

## Bulk xlsx ingest, capped

These two commands read a workbook locally. Each read is capped at `--rows`, default **1000**.
They never read a full workbook.

```bash
uv run base-gov xlsx-analyze                        # summary statistics
uv run base-gov xlsx-analyze --municipalities-only  # only "Município" adjudicantes
uv run base-gov xlsx-entities                       # write the entity parquet files
```

`xlsx-analyze` prints the distinct adjudicante and adjudicatário counts, the contract-id
uniqueness, the total value in EUR, a breakdown per `tipoContrato`, and the municipality row
count. All numbers come from the capped read.

`xlsx-entities` extracts deduplicated entities (`entity_id`/NIPC, `name`) into
`data/entities_adjudicantes.parquet` and `data/entities_adjudicatarios.parquet`. The writes are
upserts. A new run merges with the data on disk and deduplicates again. It never duplicates rows.

## Bulk ingest: raw and treated

`bin/ingest-contracts.sh` wraps `uv run base-gov bulk-ingest`. It passes all options through.
This is the only path in the project that reads the workbook without the `--rows` cap.

**Memory warning:** the parse peaks near **2 to 4 GB RSS** for the 66 MB export. calamine has no
streaming read path, so the whole sheet materializes at once.

```bash
bin/ingest-contracts.sh --year 2024 --year 2025 --year 2026   # raw + treated, all years
bin/ingest-contracts.sh --raw-only --year 2024                # stop after data/raw/
bin/ingest-contracts.sh --rebuild --ingested-at 2026-09-01    # wipe treated, rebuild from raw
bin/ingest-contracts.sh --min-value 0                         # flag nothing
```

Two layers. **Raw:** `data/source/contratos<YEAR>.xlsx` becomes `data/raw/contracts_raw_<YEAR>.parquet`,
columns verbatim, one workbook in memory at a time. **Treated:** `data/contracts.parquet`, built
from *all* raw years together (the fuzzy `adjudicante_key` is dataset-wide). It adds `ano`,
`n_concorrentes`, `is_municipio`, `valor_por_dia`, `row_hash`, `first_ingested_at`,
`updated_at` and `is_below_min_value`.

- Rows with `|preco_contratual| < --min-value` (default 10; null prices too) are **flagged, not
  dropped**. Filter with `~pl.col("is_below_min_value")`.
- `row_hash` covers the treated content columns. `updated_at` moves only when it changes;
  `first_ingested_at` is set once. Re-running on unchanged input reports `new = 0, updated = 0`.
- A repeated `idcontrato` keeps its last row (contracts are amended over time), so `ingested`
  can be lower than `read`.
- `--rebuild` deletes the treated and entity parquets and rebuilds from `data/raw/` without
  reading any xlsx. A new treated column re-hashes every row once (a one-time mass `updated`).
- The command prints a per-year summary: read, ingested, new, updated, and `below_min`.
  `below_min` counts flagged rows. It is not a deduction from `ingested`.
- 2026 is year-to-date: do not read its totals as a full year.
- `--xlsx` still works but is deprecated; use `--source-dir`.

See [SCHEMA.md — Deduplication rules](SCHEMA.md#deduplication-rules) for why contracts and
entities deduplicate in opposite directions.

## PostgreSQL

Start the stack and load the parquet files into the `procurement` schema.

```bash
cp .env.example .env
make build-db          # compiles pg_parquet from source; first build only, slow
make up                # start the postgres service
make init-db           # re-apply sql/00*.sql
bin/ingest-postgres.sh # or: make ingest-postgres
make psql              # open a psql shell
```

`make up` applies `sql/00*.sql` automatically the first time it creates a fresh volume. The
PostgreSQL entrypoint does this. Use `make init-db` to apply the files again later, for example
after a schema change, against a warm volume.

| Target | Effect |
| --- | --- |
| `make env` | Create `.env` from `.env.example` if it does not exist. |
| `make down` | Stop the services. |
| `make destroy` | Stop the services and remove the volumes. |
| `make logs` | Tail the postgres logs. |
| `make ps` | Show the running services. |

`data/` is gitignored. The parquet inputs stay on disk. Never commit them.

## Grafana dashboards

The Grafana service is opt-in through a Compose profile. Run the PostgreSQL ingest first, or
the dashboards have no data.

```bash
docker compose --profile dashboard up -d
```

Open `http://127.0.0.1:3001`. Set `GRAFANA_PORT` to change the port. The service provisions six
dashboards from `docker/grafana/`. You do not need to import anything.

| Dashboard | Shows |
| --- | --- |
| **Procurement Overview** | Total, average and distinct-adjudicante statistics. Contracts and contract value per day and per month. Top 10 adjudicantes by value. |
| **Contracts** | A row-level table of contracts: objecto, descrição, preço, awardees. The table is capped at 5000 rows and shows the row count. |
| **Procurement Overview — Municípios** | The overview, scoped to município adjudicantes. |
| **Contracts — Municípios** | The contract table, scoped to município adjudicantes. |
| **Analysis by adjudicatário** | One supplier: contracts and value won, buyers, procedure mix, duration and price, and a contract table. |
| **Analysis by adjudicante** | One buyer: contracts and value, suppliers and concentration, procedure mix, duration and price, and a contract table. |

All six dashboards read views over `procurement.v_contracts_material`, which leaves out contracts
flagged `is_below_min_value`. The two Municípios dashboards apply `v.is_municipio` on every panel
and filter the `adjudicante` dropdown. The `adjudicatário` selector stays unscoped, because a
município can award a contract to any supplier.

Every dashboard has a `min_contract_value` selector. It hides contracts where
`abs(preco_contratual)` is below the chosen value. It uses the absolute value, so large negative
values stay visible. It works on top of the fixed EUR 10 rule that ingest applies. The default is 10.

The two overview dashboards link to the two analysis dashboards. Click an adjudicante or
adjudicatário bar to open its analysis dashboard. The link keeps the entity, the
`min_contract_value` and the time range. The analysis dashboards link to each other in the same way.

## Development

```bash
uv sync                # install the dependencies
uv run base-gov --help # list the CLI commands
make lint              # ruff check
make fmt               # ruff format
make typecheck         # mypy
make test              # pytest
make check             # lint + typecheck + test
```

## Library use

```python
from base_gov import analysis, load_contracts

df = load_contracts("data/source/contratos2025.xlsx", rows=1000)
municipalities = analysis.municipality_contracts(df)
```
