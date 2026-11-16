# SCHEMA.md

The data model of this project: the parquet files, the `procurement` schema in PostgreSQL, and
the views that the dashboards read. Referenced from [README.md](README.md).

## Pipeline

```
dados.gov.pt API -> data/source/*.xlsx -> data/raw/*.parquet -> data/contracts.parquet -> procurement.* -> Grafana
```

The PostgreSQL load reads only the treated and entity parquet files. See [scripts-guide.md](scripts-guide.md) for
the commands of each stage.

## Parquet files

| File | Grain | Written by |
| --- | --- | --- |
| `data/raw/contracts_raw_<YEAR>.parquet` | Verbatim workbook rows, one file per year. | `bin/ingest-contracts.sh` |
| `data/contracts.parquet` | Treated: one row per `idcontrato`, with `row_hash`, `first_ingested_at`, `updated_at`, `is_below_min_value`. 2026 is year-to-date. | `bin/ingest-contracts.sh` |
| `data/entities_adjudicantes.parquet` | One row per contracting entity. | `xlsx-entities`, `bulk-ingest` |
| `data/entities_adjudicatarios.parquet` | One row per contracted entity. | `xlsx-entities`, `bulk-ingest` |

### Raw vs treated

- **Raw** (`data/raw/contracts_raw_<YEAR>.parquet`): the workbook columns verbatim, one file per
  year. Nothing is dropped or derived.
- **Treated** (`data/contracts.parquet`): built from all raw years together. It adds derived
  columns (`ano`, `n_concorrentes`, `is_municipio`, `valor_por_dia`), `row_hash`, the two
  timestamps and `is_below_min_value`. It reads parquet, not xlsx.
- 2026 is a partial, year-to-date file. Do not compare its totals with a full year.

### Deduplication rules

Contracts and entities deduplicate in opposite directions, on purpose.

- **Contracts use `keep="last"`.** BASE amends a row after the signature. Fields such as
  `dataFechoContrato` and `PrecoTotalEfetivo` fill in later. The most recent version of an
  `idcontrato` is therefore the correct one. Whether that row counts as changed is decided by
  `row_hash` (see `contracts` below), not by the position of the row.
- **Entities use `keep="first"`.** The name that an entity had when it was first seen is treated
  as stable.

## Tables

The schema is `procurement`. `sql/001_extensions.sql` creates it.

### `contracts`

The fact table. The grain is one row per contract, keyed on `idcontrato`.

| Column | Type | Note |
| --- | --- | --- |
| `idcontrato` | `bigint` | Primary key. |
| `adjudicante` | `text` | Canonical packed value, `"NIPC - Name"`, one line per entry. The raw `key=value` format is normalised to it. |
| `adjudicante_nipc` | `text` | Derived from `adjudicante`. Indexed. |
| `adjudicante_name` | `text` | Derived from `adjudicante`. |
| `adjudicatarios` | `text` | Canonical packed value, `"NIPC - Name"`, one entry per line. |
| `objecto_contrato` | `text` | Source column `objectoContrato`. |
| `desc_contrato` | `text` | Source column `descContrato`. |
| `preco_contratual` | `numeric(18,2)` | Source column `precoContratual`. |
| `preco_total_efetivo` | `numeric(18,2)` | Source column `PrecoTotalEfetivo`. Nullable. |
| `data_celebracao` | `date` | Source column `dataCelebracaoContrato`. Indexed. |
| `data_publicacao` | `date` | Source column `dataPublicacao`. |
| `data_fecho` | `date` | Source column `dataFechoContrato`. Nullable. |
| `tipo_contrato`, `cpv`, `nuts`, `ano` | `text` / `integer` | `cpv` and `ano` are indexed. |
| `prazo_execucao`, `adjudicatario_pmes` | `integer` | |
| `preco_base_procedimento`, `tipo_procedimento`, `fundament_ajuste_direto` | `numeric(18,2)` / `text` | Added by 007. `tipo_procedimento` is indexed. |
| `n_concorrentes`, `valor_por_dia` | `integer` / `numeric(18,2)` | Added by 007. Derived in the treated parquet. |
| `is_municipio` | `boolean` | Added by 007. Partial index where true. |
| `is_below_min_value` | `boolean` | Added by 007. The EUR 10 flag, see below. |
| `row_hash` | `bigint` | Added by 007. Hash of the treated content columns, from the parquet. |
| `first_ingested_at`, `updated_at` | `timestamptz` | No default. Both come from the parquet. |

**Timestamp semantics.** `first_ingested_at` is the renamed `ingested_at`. It is set once and
never changes in `ON CONFLICT`. `updated_at` and the content columns change only when `row_hash`
or `is_below_min_value` differs from the loaded row, so a reload of unchanged data leaves
`updated_at` alone. The database no longer stamps `now()`.

**The EUR 10 rule is a flag, not a filter.** Rows with `|preco_contratual|` under 10 (or a null
price) load with `is_below_min_value = true`. Nothing is dropped from the table.
`v_contracts_material` leaves them out; `v_contracts_enriched` keeps them.

### `entities_adjudicantes`, `entities_adjudicatarios`

Both hold `entity_id` (the NIPC, primary key), `name`, and `updated_at`.

### `contract_adjudicatarios`

The bridge table between a contract and its awardees. The grain is one row per contract and
entity. The primary key is `(idcontrato, entity_id)`. `position` holds the rank of the awardee
inside the packed source string. The foreign key to `contracts` cascades on delete.

## Views

| View | File | Purpose |
| --- | --- | --- |
| `entities_all` | `sql/003_views.sql` | Both entity tables in one relation, with a `role` column. |
| `contracts_enriched` | `sql/003_views.sql` | `contracts` plus `adjudicante_display_name` from the adjudicante table. |
| `v_contracts_enriched` | `sql/005_procurement_dashboard_views.sql` | The unfiltered base view. `adjudicante_name` is the name in `entities_adjudicantes`, else the raw contract name. It also exposes `is_municipio`, `is_below_min_value`, `tipo_procedimento`, `n_concorrentes`, `first_ingested_at`, `updated_at`, plus `prazo_execucao`, `valor_por_dia`, `preco_total_efetivo`, `preco_base_procedimento`, `closed_at`, `fundament_ajuste_direto`, `adjudicatario_pmes`. |
| `procedure_family` (column) | `sql/005_procurement_dashboard_views.sql` | `ajuste_direto`, `concurso_publico`, `consulta_previa` or `other`, from `tipo_procedimento`. |
| `duration_bucket` (column) | `sql/005_procurement_dashboard_views.sql` | `1-30`, `31-90`, `91-180`, `181-365`, `366-3650` days of `prazo_execucao`; NULL outside 1..3650. |
| `v_contracts_material` | `sql/005_procurement_dashboard_views.sql` | `v_contracts_enriched` where `NOT is_below_min_value`. Every dashboard panel reads it. |
| `v_adjudicatario_name` | `sql/005_procurement_dashboard_views.sql` | One canonical name per awardee NIPC: `entities_adjudicatarios`, else `min()` of the bridge names. |
| `v_contract_adjudicatario` | `sql/005_procurement_dashboard_views.sql` | The awardee selector for the dashboards. The name resolves inline (entity name, else `min()` bridge name); it skips `v_adjudicatario_name` because the UNION fallback forced a bridge seq scan per output row (panel 2: >171 s vs 147 ms). |
| `v_contracts_below_min` | `sql/009_dashboard_filter_support.sql` | `v_contracts_enriched` where `is_below_min_value`: the complement of `v_contracts_material`, with `procedure_family`. Feeds overview panel 10. |
| `v_adjudicatario_contracts` | `sql/008_entity_dashboard_views.sql` | One row per (contract, awardee) over `v_contracts_material`. The name comes from `v_adjudicatario_name`. Multi-awardee contracts repeat with full `preco_contratual`; use `count(DISTINCT idcontrato)`. |

### Why two enriched views

`contracts_enriched` and `v_contracts_enriched` are separate on purpose. The two names never
collide, so the dashboard views can change without an effect on the analysis view.

### The timestamp cast

`v_contracts_enriched` casts `data_celebracao` and `data_publicacao` to `timestamp`, not to
`timestamptz`. It exposes them as `celebrated_at` and `published_at`. The reason is
`$__timeFilter()`: no panel needs its own cast, and `date::timestamp` is IMMUTABLE. The
expression indexes in `sql/006_timestamp_cast_indexes.sql` are therefore usable by the planner.
`date::timestamptz` is only STABLE, because it depends on the session timezone. The source
columns are plain calendar dates, so no timezone meaning is lost.

## Migrations

`sql/0*.sql` runs in order and is idempotent. It is safe against a warm volume.

| File | Content |
| --- | --- |
| `001_extensions.sql` | The `pg_parquet` extension and the `procurement` schema. |
| `002_schema.sql` | The four tables and five indexes, plus the warm-volume upgrade of `contracts`. |
| `003_views.sql` | `entities_all`, `contracts_enriched`. |
| `004_grafana_role.sql` | The read-only `grafana_reader` role and its default privileges. |
| `005_procurement_dashboard_views.sql` | The three dashboard views. |
| `006_timestamp_cast_indexes.sql` | The expression indexes for the timestamp casts. |
| `007_ingestion_metadata.sql` | Guarded rename `ingested_at` -> `first_ingested_at`, drops both timestamp defaults, adds the treated columns and three indexes. `002` applies the same statements so `005` resolves on a warm volume. |
| `008_entity_dashboard_views.sql` | `v_adjudicatario_contracts`. |
| `009_dashboard_filter_support.sql` | `jit = off` for `grafana_reader` (guarded) and `v_contracts_below_min`. |
| `010_query_stats.sql` | The `pg_stat_statements` extension. Compose preloads the library. Used by `base-gov query-stats`. |

`sql/ingest/` holds the load steps that `bin/ingest-postgres.sh` runs: `10_stage_entities.sql`,
`20_stage_contracts.sql`, `30_rebuild_bridge.sql`.

The numbers have no gaps and no duplicates: 001 to 010, and 10/20/30.

`004_grafana_role.sql` grants `SELECT` on every table and view that the owner role creates in
the schema afterwards. The views of 005 need no separate grant.

## Verification status

Not yet done. The migrations and the load are covered by database-free tests only. These have
**not** been executed:

- The one-off wipe-and-reprocess runbook (delete the old data, re-ingest 2024-2026, reload).
- `make init-db` twice on a warm volume, and the `bin/ingest-postgres.sh` load against it.
- Opening the four dashboards, and `EXPLAIN` on the new material index.
- The pg_parquet check that the treated `UInt32` columns COPY into `bigint`.

Do not read the acceptance figures for this change as measured.

## Data notes

The bulk export is the portal's own row-per-contract workbook, so it carries the fields the
portal chose to include rather than a full detail-page record. `xlsx_ingest.py` documents the
observed header and the parsing decisions for prices and dates.
