#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

usage() {
  cat <<'EOT'
Usage: bin/download-contracts.sh [OPTIONS]

Download contratosYYYY.xlsx from dados.gov.pt into data/source/ with sha1
verification. Wraps `uv run base-gov download-contracts`; all options are
passed straight through.

Options:
  --years N       Most recent N years, current included (default: 3)
  --year YEAR     Explicit year (repeatable); overrides --years
  --from-year Y   Earliest year to fetch (backfill only)
  --dest PATH     Destination directory (default: data/source)
  --force         Ignore the manifest and overwrite local files
  --dry-run       List resources and planned action; download nothing
  --verify-only   Compare local sha1 to the API; exit 1 on mismatch
  --verbose, -v   Verbose logging
  -h, --help      Show this help and exit
EOT
}

for arg in "$@"; do
  case "$arg" in
    -h|--help)
      usage
      exit 0
      ;;
  esac
done

exec uv run base-gov download-contracts "$@"
