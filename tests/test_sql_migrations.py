"""Cheap regression guards over sql/*.sql — regex checks, not a real parser."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from base_gov.xlsx_ingest import REQUIRED_COLUMNS

REPO_ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = REPO_ROOT / "sql"

ALL_SQL_FILES = sorted(SQL_DIR.rglob("*.sql"))
MIGRATION_FILES = sorted(SQL_DIR.glob("00*.sql"))


@pytest.mark.parametrize("path", ALL_SQL_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_sql_file_non_empty(path: Path) -> None:
    assert path.read_text().strip()


@pytest.mark.parametrize("path", MIGRATION_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_create_table_is_idempotent(path: Path) -> None:
    text = path.read_text()
    for match in re.finditer(r"CREATE\s+(?:UNLOGGED\s+)?TABLE\s+(.*)", text, re.IGNORECASE):
        assert "IF NOT EXISTS" in match.group(0).upper(), (
            f"{path.name}: CREATE TABLE statement missing IF NOT EXISTS: {match.group(0)[:80]!r}"
        )


@pytest.mark.parametrize("path", MIGRATION_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_create_index_is_idempotent(path: Path) -> None:
    text = path.read_text()
    for match in re.finditer(r"CREATE\s+INDEX\s+.*?\bON\b", text, re.IGNORECASE):
        assert "IF NOT EXISTS" in match.group(0).upper(), (
            f"{path.name}: CREATE INDEX statement missing IF NOT EXISTS: {match.group(0)[:80]!r}"
        )


@pytest.mark.parametrize("path", MIGRATION_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_create_view_uses_or_replace(path: Path) -> None:
    text = path.read_text()
    for match in re.finditer(r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW", text, re.IGNORECASE):
        assert "OR REPLACE" in match.group(0).upper(), (
            f"{path.name}: CREATE VIEW statement missing OR REPLACE: {match.group(0)!r}"
        )


def test_grafana_role_grant_is_guarded() -> None:
    path = SQL_DIR / "004_grafana_role.sql"
    text = path.read_text()
    assert "DO $$" in text or "DO$$" in text.replace(" ", "")
    assert "IF NOT EXISTS" in text.upper()
    assert re.search(r"CREATE\s+ROLE", text, re.IGNORECASE)


def _staging_columns() -> list[str]:
    path = SQL_DIR / "ingest" / "20_stage_contracts.sql"
    text = path.read_text()
    body_match = re.search(
        r"CREATE\s+UNLOGGED\s+TABLE\s+IF\s+NOT\s+EXISTS\s+procurement\.stg_contracts\s*\((.*?)\)\s*;",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    assert body_match is not None
    columns = []
    for line in body_match.group(1).splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        col_match = re.match(r'"([^"]+)"', line)
        if col_match:
            columns.append(col_match.group(1))
    return columns


def test_staging_table_has_declared_columns() -> None:
    columns = _staging_columns()
    assert columns


def test_ingest_sql_lives_outside_migration_glob() -> None:
    """sql/ingest/*.sql must never auto-run via docker-entrypoint-initdb.d.

    docker-compose.yml bind-mounts the whole ./sql tree there; the official
    postgres entrypoint's init loop globs that directory non-recursively, so
    a nested sql/ingest/ directory is skipped today. That's an entrypoint
    implementation detail, not a structural guarantee — this test at least
    catches the most likely way to break it: someone renaming/flattening the
    ingest scripts to sql/1x_*.sql, which WOULD auto-run TRUNCATE + COPY FROM
    parquet files that may not exist yet on first container boot.
    """
    ingest_files = sorted((SQL_DIR / "ingest").glob("*.sql"))
    assert ingest_files, "expected sql/ingest/*.sql to exist"
    for path in ingest_files:
        assert path.parent.name == "ingest", (
            f"{path.name} must stay under sql/ingest/, not directly in sql/ — "
            "files directly in sql/ are auto-executed by docker-entrypoint-initdb.d"
        )
    for path in MIGRATION_FILES:
        assert "TRUNCATE" not in path.read_text().upper(), (
            f"{path.name}: TRUNCATE found in a sql/00*.sql migration file — "
            "these auto-run on container init and must never truncate data"
        )


def test_staging_table_covers_xlsx_required_columns() -> None:
    columns = set(_staging_columns())
    name_map = {
        "idcontrato": "idcontrato",
        "adjudicante": "adjudicante",
        "adjudicatarios": "adjudicatarios",
        "precoContratual": "precoContratual",
        "tipoContrato": "tipoContrato",
    }
    for required in REQUIRED_COLUMNS:
        staged_name = name_map[required]
        assert staged_name in columns, (
            f"xlsx_ingest.REQUIRED_COLUMNS entry {required!r} has no matching "
            f"staging column {staged_name!r} in sql/ingest/20_stage_contracts.sql"
        )
