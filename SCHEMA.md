# SCHEMA.md

The data model of this project: the parquet files, the `procurement` schema in PostgreSQL, and
the views that the dashboards read. Referenced from [README.md](README.md).

## Pipeline

```
BASE portal (HTML + AJAX)  --scrape-->  data/*.csv
data/contratos2025.xlsx    --bulk-ingest-->  data/*.parquet
data/*.parquet             --pg_parquet COPY-->  procurement.*  --views-->  Grafana
```

The scrape path and the workbook path are independent. The PostgreSQL load reads only the
parquet files. See [scripts-guide.md](scripts-guide.md) for the commands of each stage.

## Parquet files

| File | Grain | Written by |
| --- | --- | --- |
| `data/contracts.parquet` | One row per `idcontrato`. | `bin/ingest-contracts.sh` |
| `data/entities_adjudicantes.parquet` | One row per contracting entity. | `xlsx-entities`, `bulk-ingest` |
| `data/entities_adjudicatarios.parquet` | One row per contracted entity. | `xlsx-entities`, `bulk-ingest` |

### Deduplication rules

Contracts and entities deduplicate in opposite directions, on purpose.

- **Contracts use `keep="last"`.** BASE amends a row after the signature. Fields such as
  `dataFechoContrato` and `PrecoTotalEfetivo` fill in later. The most recent version of an
  `idcontrato` is therefore the correct one.
- **Entities use `keep="first"`.** The name that an entity had when it was first seen is treated
  as stable.

## Tables

The schema is `procurement`. `sql/001_extensions.sql` creates it.

### `contracts`

The fact table. The grain is one row per contract, keyed on `idcontrato`.

| Column | Type | Note |
| --- | --- | --- |
| `idcontrato` | `bigint` | Primary key. |
| `adjudicante` | `text` | Raw packed value, `"NIPC name"`. |
| `adjudicante_nipc` | `text` | Derived from `adjudicante`. Indexed. |
| `adjudicante_name` | `text` | Derived from `adjudicante`. |
| `adjudicatarios` | `text` | Raw packed value, separated by `;`. |
| `objecto_contrato` | `text` | Source column `objectoContrato`. |
| `desc_contrato` | `text` | Source column `descContrato`. |
| `preco_contratual` | `numeric(18,2)` | Source column `precoContratual`. |
| `preco_total_efetivo` | `numeric(18,2)` | Source column `PrecoTotalEfetivo`. Nullable. |
| `data_celebracao` | `date` | Source column `dataCelebracaoContrato`. Indexed. |
| `data_publicacao` | `date` | Source column `dataPublicacao`. |
| `data_fecho` | `date` | Source column `dataFechoContrato`. Nullable. |
| `tipo_contrato`, `cpv`, `nuts`, `ano` | `text` / `integer` | `cpv` and `ano` are indexed. |
| `prazo_execucao`, `adjudicatario_pmes` | `integer` | |
| `ingested_at`, `updated_at` | `timestamptz` | Default `now()`. |

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
| `v_contracts_enriched` | `sql/005_procurement_dashboard_views.sql` | The single surface that every Grafana panel reads. |
| `v_contract_adjudicatario` | `sql/005_procurement_dashboard_views.sql` | The awardee selector for the dashboards. |

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

`sql/00*.sql` runs in order and is idempotent. It is safe against a warm volume.

| File | Content |
| --- | --- |
| `001_extensions.sql` | The `pg_parquet` extension and the `procurement` schema. |
| `002_schema.sql` | The four tables and five indexes. |
| `003_views.sql` | `entities_all`, `contracts_enriched`. |
| `004_grafana_role.sql` | The read-only `grafana_reader` role and its default privileges. |
| `005_procurement_dashboard_views.sql` | The two dashboard views. |
| `006_timestamp_cast_indexes.sql` | The expression indexes for the timestamp casts. |

`sql/ingest/` holds the load steps that `bin/ingest-postgres.sh` runs: `10_stage_entities.sql`,
`20_stage_contracts.sql`, `30_rebuild_bridge.sql`.

The numbers have no gaps and no duplicates: 001 to 006, and 10/20/30.

`004_grafana_role.sql` grants `SELECT` on every table and view that the owner role creates in
the schema afterwards. The two views of 005 need no separate grant.

## Data notes

A search result from the portal is a narrow projection. Prices arrive as display strings, for
example `"1.055.006,92 €"`. Dates arrive as `DD-MM-YYYY`. The scraper preserves both exactly as
received, so the analysis layer keeps the parsing decisions. The full contract records live on
the detail pages, `/Base4/pt/detalhe/?type=contratos&id=...`. This client does not read them yet.
