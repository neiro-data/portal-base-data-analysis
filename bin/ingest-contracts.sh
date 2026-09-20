#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

usage() {
  cat <<'EOF'
Usage: bin/ingest-contracts.sh [OPTIONS]

Convert data/source/contratos<YEAR>.xlsx to verbatim data/raw/contracts_raw_<YEAR>.parquet,
then rebuild the treated data/contracts.parquet (row_hash, first_ingested_at, updated_at,
is_below_min_value) and both entity parquet files from ALL raw years, and print a per-year
summary. Wraps `uv run base-gov bulk-ingest`; all options are passed straight through.

Options:
  --year YEAR                Workbook year to convert; repeatable (e.g. --year 2024 --year 2025)
  --source-dir PATH          Directory with the workbooks (default: data/source)
  --raw-dir PATH             Directory for the raw parquet files (default: data/raw)
  --out PATH                 Treated contracts parquet (default: data/contracts.parquet)
  --adjudicantes-out PATH    Destination parquet for adjudicantes
  --adjudicatarios-out PATH  Destination parquet for adjudicatarios
  --min-value EUR            Flag |preco_contratual| below EUR (default: 10; 0 flags nothing)
  --ingested-at TIMESTAMP    Stamp this UTC time instead of now (e.g. 2026-09-01)
  --raw-only                 Stop after writing the raw parquet files
  --rebuild                  Delete treated + entity parquets, rebuild from raw (no xlsx read)
  --xlsx PATH                Deprecated: use --source-dir
  --verbose, -v              Verbose logging
  -h, --help                 Show this help and exit
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      usage
      exit 0
      ;;
  esac
done

exec uv run base-gov bulk-ingest "$@"
