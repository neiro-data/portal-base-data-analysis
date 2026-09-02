# portal-base-data-analysis

Retrieve, and eventually open-source, public procurement data from Portugal's
[BASE portal](https://www.base.gov.pt/base4).

## Resume

- **Stack:** Python 3.12, [uv](https://docs.astral.sh/uv/), httpx, polars, typer.
  Lint/format with ruff, types with mypy (strict), tests with pytest.
- **Layout:** `src/base_gov/` (library + CLI), `tests/`. Outputs land in `data/`.
- **Install:** `uv sync`
- **Run:** `uv run base-gov --help`
- **Checks:** `uv run pytest -q && uv run ruff check . && uv run mypy src tests`

## Usage

Export every entity whose NIPC, description or country matches a string — this
is the scripted equivalent of *Entidades → "Município" → Pesquisar*:

```bash
uv run base-gov entities --text "Município" --out data/entidades_municipio.csv
```

Collect one entity's contracts over a date range, one calendar month per
request. Prefer the NIPC over a name:

```bash
uv run base-gov contracts \
  --contracting 500051070 \
  --start 2025-01-01 --end 2025-12-31 \
  --out data/lisboa_2025.csv
```

Both commands ingest in batches — one result page for `entities`, one calendar
month for `contracts`. Each batch is written to its own CSV under `<out>.parts/`
as soon as it arrives and the batches are joined into `--out` at the end, so an
interrupted run keeps everything it fetched. Re-running resumes from the first
missing batch; pass `--restart` to discard them and start over, or `--keep-parts`
to leave the per-batch CSVs in place after the join.

### Bulk xlsx ingest (local, capped)

As a faster alternative to live scraping, `data/contratos2025.xlsx` (the
portal's own bulk export) can be analyzed locally and used to seed a
persistent entities "database" in `data/`. Every read of that workbook is
capped at `--rows` (default **1000**) — nothing in this project reads the
full 66 MB file.

```bash
uv run base-gov xlsx-analyze
uv run base-gov xlsx-analyze --municipalities-only
uv run base-gov xlsx-entities
```

`xlsx-analyze` prints distinct adjudicante/adjudicatário counts, contract-id
uniqueness, total EUR value, a per-`tipoContrato` breakdown, and the
municipality row count, all from the capped read.

`xlsx-entities` extracts deduplicated entities (`entity_id`/NIPC, `name`) into
`data/entities_adjudicantes.parquet` and `data/entities_adjudicatarios.parquet`.
Writes are upserts: re-running merges with what's already on disk and dedups
again, so it never duplicates rows.

### Bulk xlsx ingest (local, uncapped)

For a full, unsampled load of `data/contratos2025.xlsx`, use
`bin/ingest-contracts.sh` (wraps `uv run base-gov bulk-ingest`). Unlike
`xlsx-analyze`/`xlsx-entities` above, this is the **one** path in the project
that reads the workbook without the `--rows` cap — expect the parse to peak
around **2–4 GB RSS** for the ~66 MB export (calamine has no lazy/streaming
read path, so the whole sheet materializes at once).

```bash
bin/ingest-contracts.sh
bin/ingest-contracts.sh --xlsx data/contratos2025.xlsx --out data/contracts.parquet
bin/ingest-contracts.sh --force   # replace data/contracts.parquet instead of upserting
```

Writes `data/contracts.parquet`, upserted on `idcontrato`, and also upserts
both entity parquet files from the full dataset (same targets as
`xlsx-entities`). Contracts use `keep="last"`, the opposite of entities'
`keep="first"`: BASE rows get amended after signing (fields like
`dataFechoContrato`/`PrecoTotalEfetivo` fill in later), so the most recently
ingested version of a given `idcontrato` wins, while an entity's first-seen
name is treated as stable. Re-running is idempotent — height stays unchanged
if the source workbook hasn't changed.

As a library:

```python
import datetime as dt
from base_gov import BaseGovClient, entity_contracts, search_entities

with BaseGovClient() as client:
    municipalities = list(search_entities(client, "Município"))
    contracts = list(
        entity_contracts(
            client,
            contracting="500051070",
            start=dt.date(2025, 1, 1),
            end=dt.date(2025, 3, 31),
        )
    )
```

### Postgres ingestion

A local Postgres 17 + [pg_parquet](https://github.com/CrunchyData/pg_parquet)
0.5.1 Docker stack loads the `data/*.parquet` files produced above into a
`procurement` schema for querying (`entities_adjudicantes`,
`entities_adjudicatarios`, `contracts`, `contract_adjudicatarios`, plus
`entities_all` / `contracts_enriched` views).

```bash
cp .env.example .env
make build-db      # slow, compiles pg_parquet from source — first build only
make up
make init-db        # note: sql/00*.sql already runs automatically the first
                     # time `make up` creates a fresh volume (via postgres's
                     # own docker-entrypoint-initdb.d) — `make init-db` is
                     # what re-applies it later, e.g. after a schema change,
                     # against an already-initialised (warm) volume

# once the sibling bulk-ingest worktree lands and this produces contracts.parquet:
# uv run base-gov bulk-ingest ...

bin/ingest-postgres.sh   # or: make ingest-postgres
make psql                # inspect the loaded tables
```

`data/` is gitignored — parquet inputs live only on disk, never committed.
The stack is local-only and not internet-facing (bound to
`${POSTGRES_PORT:-55433}` on localhost). `sql/004_grafana_role.sql` creates a
read-only `grafana_reader` role with a literal placeholder password; rotate
it before any non-local deployment.

## Dashboards

A Grafana service, opt-in via a Compose profile, renders the loaded
`procurement` schema as two provisioned dashboards. It requires the Postgres
ingest above to have already run (`make up` + `make init-db` +
`bin/ingest-postgres.sh`) so there's data to show:

```bash
docker compose --profile dashboard up -d
```

Open `http://127.0.0.1:3001` (override the port via `GRAFANA_PORT`). Four
dashboards are auto-provisioned, all filterable by `adjudicante` and
`adjudicatário` selectors and by the time-range picker:

- **Procurement Overview** — headline stats (total/average contract value,
  distinct adjudicantes), contracts and contract value per day/month.
- **Contracts** — a row-level table of contracts (objecto, descrição, preço,
  awardees), capped at 5000 rows with a visible row count.
- **Procurement Overview — Municípios** / **Contracts — Municípios** — the
  same two dashboards, unconditionally scoped to adjudicantes whose name
  matches "Município"/"Municipio" (`v.adjudicante_name ILIKE '%munic%pio%'`
  on every panel, plus a filtered `adjudicante` dropdown). `adjudicatário`
  stays unscoped — a município can award to any supplier.

Grafana runs with **anonymous Editor access and no login form** — anyone who
can reach the port can read and edit dashboards. That's why the port is bound
to `127.0.0.1` only (`docker-compose.yml`); do not deploy this setup to a
shared or internet-facing host. `grafana_reader`'s password is the same
literal placeholder as above (`sql/004_grafana_role.sql`); rotate it in both
that file and `.env`'s `GRAFANA_DB_PASSWORD` together, since Grafana's
datasource reads the latter to authenticate.

## How the portal actually works

BASE publishes no API. Each search page is a server-rendered shell: the GET form
under `/Base4/pt/pesquisa/` is folded by the server into a single opaque `query`
string, which the page then posts to one AJAX endpoint.

```
POST /Base4/pt/resultados/
  type=search_entidades | search_contratos
  version=109.0
  query=adjudicante=500051070&tipo=0&tipocontrato=0&desdedatacontrato=2025-01-01&...
  sort=+description | -publicationDate
  page=0
  size=25
```

The response is a JavaScript object literal (the page `eval`s it) of the shape
`{ "total": N, "items": [...] }`; in practice it parses as JSON.

### Form fields worth knowing

| Field | Meaning |
| --- | --- |
| `texto4` | Entities search box — NIPC, description or country |
| `adjudicante` / `adjudicataria` | Contracting / contracted entity — **NIPC or description** |
| `desdedatacontrato` / `atedatacontrato` | Contract date range |
| `desdedatapublicacao` / `atedatapublicacao` | Publication date range |
| `tipo`, `tipocontrato`, `pais`, `distrito`, `concelho` | Selectors; `0` means "all" |

Dates use the datepicker's `yy-mm-dd`, i.e. ISO `YYYY-MM-DD`.

There is no separate "entity id" parameter: `adjudicante` is a single field that
the portal itself documents as *"Pode pesquisar pelo NIPC ou descrição da
entidade"*. The NIPC is therefore the stable identifier to filter on — a name
fragment matches loosely across entities. (Search results do carry an internal
`id`, but it addresses detail pages, not the contracts filter.)

### Measured behaviour

These drove the client's defaults, and are the reason it is built to be patient
rather than fast:

- **A cold query takes ~125s.** Re-running the same query costs ~30s, so the
  backend caches per query. Default timeout is 300s with retries and backoff.
- **`size` is only honoured up to 25.** Larger values do not error; they
  silently return 20 rows (25 → 25, 100 → 20, 400 → 20). The paginator reads the
  stride back from each response instead of trusting what it asked for.
- **Session cookies are mandatory.** Posting to `/Base4/pt/resultados/` without
  first fetching a `/Base4/pt/pesquisa/` page hangs rather than returning an
  error, so the client primes a session once per search form.
- **The portal's own "Exportar resultados" button is unreliable.** Its
  `type=csv_entidades` link carries only `sort`, and in testing it ignored the
  active search entirely — exporting the first 250 entities overall rather than
  the 360 matching "Município". This project builds its CSVs from the paged JSON
  instead, which has no 250-row cap and honours the query.

### Being a good citizen

The server is old and easily overwhelmed. The client serialises requests, waits
`min_interval` (2s) between them, and retries with backoff. Please keep it that
way; narrow the date window rather than raising concurrency.

## Data notes

Search results are a narrow projection. Prices arrive as display strings
(`"1.055.006,92 €"`) and dates as `DD-MM-YYYY`, both preserved verbatim on the
way out so parsing decisions stay with the analysis layer. Full contract records
live on the detail pages (`/Base4/pt/detalhe/?type=contratos&id=...`), which this
client does not fetch yet.

## Licence and provenance

Data is published by Instituto dos Mercados Públicos, do Imobiliário e da
Construção (IMPIC) on base.gov.pt. Check its terms before redistributing any
derived dataset.
