#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

POSTGRES_USER="${POSTGRES_USER:-base_gov}"
POSTGRES_DB="${POSTGRES_DB:-base_gov}"

docker compose up -d postgres

required_files=(
    "data/contracts.parquet"
    "data/entities_adjudicantes.parquet"
    "data/entities_adjudicatarios.parquet"
)
missing=()
for f in "${required_files[@]}"; do
    [[ -f "$f" ]] || missing+=("$f")
done
if ((${#missing[@]} > 0)); then
    echo "Missing required parquet input(s):" >&2
    for f in "${missing[@]}"; do
        echo "  - $f" >&2
    done
    echo "Produce them first, e.g. 'uv run base-gov xlsx-entities' and the" >&2
    echo "(future) 'uv run base-gov bulk-ingest ...' contracts export." >&2
    exit 1
fi

psql_exec() {
    docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -f -
}

for f in sql/ingest/*.sql; do
    echo "==> running $f"
    psql_exec < "$f"
done

echo "==> row counts"
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT 'entities_adjudicantes' AS table_name, count(*) FROM procurement.entities_adjudicantes
UNION ALL
SELECT 'entities_adjudicatarios', count(*) FROM procurement.entities_adjudicatarios
UNION ALL
SELECT 'contracts', count(*) FROM procurement.contracts
UNION ALL
SELECT 'contract_adjudicatarios', count(*) FROM procurement.contract_adjudicatarios;
"
