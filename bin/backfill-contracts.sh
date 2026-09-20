#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
# Downloads EVERY published year (2012 -> current). ~500 MB of xlsx and a
# multi-hour reprocess downstream. Deliberately guarded: this repo has never
# run it. Set BACKFILL_CONFIRM=yes-download-all-years to proceed.
if [[ "${BACKFILL_CONFIRM:-}" != "yes-download-all-years" ]]; then
  echo "refusing: set BACKFILL_CONFIRM=yes-download-all-years to run" >&2; exit 2
fi
exec uv run base-gov download-contracts --from-year 2012 --years 99 "$@"
