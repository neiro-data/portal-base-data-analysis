#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

usage() {
  cat <<'EOF'
Usage: bin/ingest-contracts.sh [OPTIONS]

Bulk-ingest data/contratos2025.xlsx (uncapped read) into data/contracts.parquet
and upsert both entity parquet files. Wraps `uv run base-gov bulk-ingest`; all
options are passed straight through.

Options:
  --xlsx PATH                Path to the contracts workbook (default: data/contratos2025.xlsx)
  --out PATH                 Destination parquet for contracts (default: data/contracts.parquet)
  --adjudicantes-out PATH    Destination parquet for adjudicantes
  --adjudicatarios-out PATH  Destination parquet for adjudicatarios
  --force                    Replace the existing contracts parquet instead of upserting
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
