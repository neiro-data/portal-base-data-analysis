# README_AGENT.md

Background that an agent needs to change this repository, and that a person who only wants to
run the tool does not need. Referenced from [README.md](README.md).

## How the BASE portal works

BASE publishes no API. Each search page is a server-rendered shell. The server folds the GET
form under `/Base4/pt/pesquisa/` into a single opaque `query` string. The page then posts that
string to one AJAX endpoint.

```
POST /Base4/pt/resultados/
  type=search_entidades | search_contratos
  version=109.0
  query=adjudicante=500051070&tipo=0&tipocontrato=0&desdedatacontrato=2025-01-01&...
  sort=+description | -publicationDate
  page=0
  size=25
```

The response is a JavaScript object literal. The page evaluates it. Its shape is
`{ "total": N, "items": [...] }`. In practice it parses as JSON.

### Form fields

| Field | Meaning |
| --- | --- |
| `texto4` | The entities search box. It accepts a NIPC, a description or a country. |
| `adjudicante` / `adjudicataria` | The contracting or contracted entity. It accepts a **NIPC or a description**. |
| `desdedatacontrato` / `atedatacontrato` | The contract date range. |
| `desdedatapublicacao` / `atedatapublicacao` | The publication date range. |
| `tipo`, `tipocontrato`, `pais`, `distrito`, `concelho` | Selectors. `0` means "all". |

Dates use the format of the datepicker, `yy-mm-dd`. This is ISO `YYYY-MM-DD`.

There is no separate entity-id parameter. `adjudicante` is one field. The portal documents it as
*"Pode pesquisar pelo NIPC ou descrição da entidade"*. Use the NIPC as the stable identifier: a
name fragment matches many entities. A search result does carry an internal `id`, but that `id`
addresses the detail page and not the contracts filter.

## Measured behaviour of the server

These measurements set the defaults of the client. They are the reason the client is patient and
not fast.

- **A cold query takes about 125 s.** The same query again costs about 30 s, so the backend
  caches per query. The default timeout is 300 s, with retries and backoff.
- **The server honours `size` only up to 25.** A larger value does not fail. It returns 20 rows
  without a message: 25 gives 25, 100 gives 20, 400 gives 20. The paginator reads the stride
  back from each response. It does not trust the value that it sent.
- **Session cookies are mandatory.** A POST to `/Base4/pt/resultados/` without a previous GET of
  a `/Base4/pt/pesquisa/` page hangs. It does not return an error. The client therefore primes a
  session once per search form.
- **The "Exportar resultados" button of the portal is unreliable.** Its `type=csv_entidades`
  link carries only `sort`. In a test it ignored the active search: it exported the first 250
  entities overall, not the 360 entities that matched "Município". This project builds its CSV
  files from the paged JSON instead. That path has no 250-row cap and it honours the query.

## Load limits

The server is old and it fails under load. The client sends one request at a time. It waits
`min_interval`, 2 s, between requests. It retries with backoff. Keep this behaviour. To reduce
the size of a job, make the date window smaller. Do not raise the concurrency.

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
