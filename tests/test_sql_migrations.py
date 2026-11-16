"""Cheap regression guards over sql/*.sql — regex checks, not a real parser."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from base_gov.transform import TREATED_COLUMNS, TREATED_SCHEMA
from base_gov.xlsx_ingest import REQUIRED_COLUMNS

REPO_ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = REPO_ROOT / "sql"
DASHBOARD_DIR = REPO_ROOT / "docker" / "grafana" / "dashboards"
STAGE_SQL = SQL_DIR / "ingest" / "20_stage_contracts.sql"

ALL_SQL_FILES = sorted(SQL_DIR.rglob("*.sql"))
MIGRATION_FILES = sorted(SQL_DIR.glob("0*.sql"))


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
    text = "\n".join(line.split("--", 1)[0] for line in path.read_text().splitlines())
    for match in re.finditer(r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+(\S+)", text, re.IGNORECASE):
        # A plain CREATE VIEW is allowed only right after DROP VIEW IF EXISTS of the same view.
        if re.search(rf"DROP\s+VIEW\s+IF\s+EXISTS\s+{re.escape(match.group(1))}\s*;", text, re.I):
            continue
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
        "precoContratual": "preco_contratual",
        "tipoContrato": "tipo_contrato",
    }
    for required in REQUIRED_COLUMNS:
        staged_name = name_map[required]
        assert staged_name in columns, (
            f"xlsx_ingest.REQUIRED_COLUMNS entry {required!r} has no matching "
            f"staging column {staged_name!r} in sql/ingest/20_stage_contracts.sql"
        )


def _sql_code(path: Path) -> str:
    """File text with `--` comments stripped, so prose does not trip the greps."""
    return "\n".join(line.split("--", 1)[0] for line in path.read_text().splitlines())


def test_migration_sequence_has_no_gap() -> None:
    numbers = [int(p.name[:3]) for p in MIGRATION_FILES]
    assert numbers == list(range(1, len(numbers) + 1))
    assert (SQL_DIR / "007_ingestion_metadata.sql").exists()


def test_ingest_scripts_are_10_20_30() -> None:
    assert [p.name[:2] for p in sorted((SQL_DIR / "ingest").glob("*.sql"))] == ["10", "20", "30"]


def test_007_is_guarded_rename() -> None:
    text = (SQL_DIR / "007_ingestion_metadata.sql").read_text()
    assert re.search(r"IF\s+EXISTS\s*\(\s*SELECT.*?information_schema\.columns", text, re.I | re.S)
    assert "RENAME COLUMN ingested_at TO first_ingested_at" in text
    assert "ADD COLUMN IF NOT EXISTS" in text
    assert "DEFAULT now()" not in text


def test_staging_columns_match_treated_columns() -> None:
    staged = _staging_columns()
    assert len(staged) == len(set(staged))
    assert set(staged) == set(TREATED_COLUMNS)


def test_staging_column_order_matches_treated_columns() -> None:
    assert _staging_columns() == TREATED_COLUMNS


UPSTREAM_TREATED = ("estado_contrato", "preco_estimado_proc", "ultima_atualizacao", "id_anuncio")


def test_002_declares_upstream_columns_in_create_and_alter() -> None:
    text = (SQL_DIR / "002_schema.sql").read_text()
    create = re.search(
        r"CREATE TABLE IF NOT EXISTS procurement\.contracts \((.*?)\n\);", text, re.S
    )
    assert create is not None
    for col in UPSTREAM_TREATED:
        assert re.search(rf"^\s+{col}\s", create.group(1), re.M), col
        assert re.search(rf"ADD COLUMN IF NOT EXISTS\s+{col}\s", text), col


def test_every_inserted_column_is_in_do_update_set() -> None:
    code = _sql_code(STAGE_SQL)
    ins = re.search(
        r"INSERT\s+INTO\s+procurement\.contracts\s*\((.*?)\)\s*SELECT", code, re.S | re.I
    )
    upd = re.search(r"DO\s+UPDATE\s+SET(.*?)\bWHERE\b", code, re.S | re.I)
    assert ins is not None and upd is not None
    inserted = {c.strip() for c in ins.group(1).split(",")} - {"idcontrato", "first_ingested_at"}
    updated = set(re.findall(r"(\w+)\s*=\s*EXCLUDED\.", upd.group(1)))
    assert inserted == updated


def test_row_hash_is_signed_int64() -> None:
    # postgres bigint is signed; UInt64 would overflow COPY from parquet.
    assert TREATED_SCHEMA["row_hash"] == pl.Int64


def test_stage_has_no_sql_side_adjudicante_parsing_or_blind_updated_at() -> None:
    code = _sql_code(STAGE_SQL)
    assert not re.search(r"regexp_match\s*\([^)]*adjudicante", code, re.I)
    assert not re.search(r"updated_at\s*=\s*now\(\)", code, re.I)


def test_stage_flag_never_filters_the_load_and_never_deletes() -> None:
    code = _sql_code(STAGE_SQL)
    assert not re.search(r"\bDELETE\b", code, re.I)
    select_part = re.search(r"\bINSERT\s+INTO\b.*?\bSELECT\b(.*?)ON\s+CONFLICT", code, re.I | re.S)
    assert select_part is not None
    assert not re.search(r"\bWHERE\b", select_part.group(1), re.I)


def test_first_ingested_at_not_in_conflict_set() -> None:
    code = _sql_code(STAGE_SQL)
    match = re.search(r"DO\s+UPDATE\s+SET(.*?)(?:\bWHERE\b|;)", code, re.I | re.S)
    assert match is not None
    assert "first_ingested_at" not in match.group(1)
    assert re.search(r"row_hash\s+IS\s+DISTINCT\s+FROM", code, re.I)


def test_material_view_defined_over_enriched() -> None:
    text = (SQL_DIR / "005_procurement_dashboard_views.sql").read_text()
    assert re.search(
        r"CREATE OR REPLACE VIEW procurement\.v_contracts_material AS\s+"
        r"SELECT \* FROM procurement\.v_contracts_enriched\s+WHERE NOT is_below_min_value",
        text,
    )
    enriched = re.search(r"CREATE VIEW procurement\.v_contracts_enriched AS(.*?);", text, re.S)
    assert enriched is not None
    assert "WHERE" not in enriched.group(1)


def test_enriched_view_exposes_notebook_columns() -> None:
    text = (SQL_DIR / "005_procurement_dashboard_views.sql").read_text()
    for col in (
        "prazo_execucao",
        "valor_por_dia",
        "preco_total_efetivo",
        "procedure_family",
        "duration_bucket",
    ):
        assert col in text


def test_procedure_family_families_are_the_notebook_four() -> None:
    text = (SQL_DIR / "005_procurement_dashboard_views.sql").read_text()
    for token in ("ajuste direto", "concurso publico", "consulta previa", "'other'"):
        assert token in text


def test_valor_por_dia_is_not_recomputed_in_sql() -> None:
    for path in SQL_DIR.rglob("*.sql"):
        assert not re.search(r"preco_contratual\s*/\s*prazo_execucao", path.read_text()), path


def test_adjudicatario_view_is_defined_over_material() -> None:
    code = _sql_code(SQL_DIR / "008_entity_dashboard_views.sql")
    assert "v_contracts_material" in code
    assert "v_contracts_enriched" not in code


def test_contract_adjudicatario_does_not_join_name_union() -> None:
    code = _sql_code(SQL_DIR / "005_procurement_dashboard_views.sql")
    match = re.search(
        r"CREATE OR REPLACE VIEW procurement\.v_contract_adjudicatario\s+AS(.*?);", code, re.S
    )
    assert match
    assert "v_adjudicatario_name" not in match.group(1)


def test_009_jit_off_is_guarded() -> None:
    code = _sql_code(SQL_DIR / "009_dashboard_filter_support.sql")
    assert re.search(
        r"IF EXISTS\s*\(SELECT 1 FROM pg_roles.*?ALTER ROLE grafana_reader SET jit = off",
        code,
        re.S | re.I,
    )


def test_below_min_view_over_enriched() -> None:
    code_009 = _sql_code(SQL_DIR / "009_dashboard_filter_support.sql")
    assert re.search(
        r"v_contracts_below_min\s+AS\s+SELECT \* FROM procurement\.v_contracts_enriched"
        r"\s+WHERE is_below_min_value",
        code_009,
    )
    code_005 = _sql_code(SQL_DIR / "005_procurement_dashboard_views.sql")
    assert code_005.index("DROP VIEW IF EXISTS procurement.v_contracts_below_min") < code_005.index(
        "DROP VIEW IF EXISTS procurement.v_contracts_enriched"
    )


DASHBOARD_FILES = sorted(DASHBOARD_DIR.glob("*.json"))


def test_dashboards_found() -> None:
    assert len(DASHBOARD_FILES) == 6


@pytest.mark.parametrize("path", DASHBOARD_FILES, ids=lambda p: p.name)
def test_dashboard_has_no_municipio_ilike(path: Path) -> None:
    assert not re.search(r"ILIKE\s+'%munic%pio%'", path.read_text(), re.I)


@pytest.mark.parametrize("path", DASHBOARD_FILES, ids=lambda p: p.name)
def test_dashboard_reads_material_view_only(path: Path) -> None:
    text = path.read_text()
    assert "v_contracts_enriched" not in text
    assert "v_contracts_material" in text or "v_adjudicatario_contracts" in text


def test_002_carries_guarded_rename() -> None:
    text = (SQL_DIR / "002_schema.sql").read_text()
    assert re.search(r"IF\s+EXISTS\s*\(\s*SELECT.*?information_schema\.columns", text, re.I | re.S)
    assert "RENAME COLUMN ingested_at TO first_ingested_at" in text


def test_003_rebuilds_contracts_enriched_after_002_column_changes() -> None:
    code = _sql_code(SQL_DIR / "003_views.sql")
    drop = re.search(r"DROP\s+VIEW\s+IF\s+EXISTS\s+procurement\.contracts_enriched\s*;", code, re.I)
    create = re.search(r"CREATE\s+VIEW\s+procurement\.contracts_enriched\b", code, re.I)
    assert drop is not None
    assert create is not None
    assert drop.start() < create.start()


@pytest.mark.parametrize(
    "path", [SQL_DIR / "002_schema.sql", SQL_DIR / "007_ingestion_metadata.sql"]
)
def test_material_index_matches_panel_cast(path: Path) -> None:
    code = _sql_code(path)
    assert re.search(
        r"idx_contracts_data_celebracao_material\s+ON\s+procurement\.contracts\s*"
        r"\(\(data_celebracao::timestamp\)\)\s+WHERE\s+NOT\s+is_below_min_value",
        code,
        re.I,
    )


@pytest.mark.parametrize("name", ["overview-municipios.json", "contracts-table-municipios.json"])
def test_municipios_dashboards_filter_on_is_municipio(name: str) -> None:
    assert "is_municipio" in (DASHBOARD_DIR / name).read_text()


def test_overview_has_freshness_and_excluded_panels() -> None:
    import json

    panels = json.loads((DASHBOARD_DIR / "overview.json").read_text())["panels"]
    titles = {p["title"] for p in panels}
    assert {"Data freshness (last update)", "Excluded below EUR 10"} <= titles


def _load_dashboards() -> dict[str, dict[str, Any]]:
    import json

    return {p.name: json.loads(p.read_text()) for p in DASHBOARD_FILES}


def test_every_dashboard_json_parses() -> None:
    assert len(_load_dashboards()) == len(DASHBOARD_FILES)


@pytest.mark.parametrize("path", DASHBOARD_FILES, ids=lambda p: p.name)
def test_every_dashboard_has_min_contract_value_var(path: Path) -> None:
    import json

    variables = json.loads(path.read_text())["templating"]["list"]
    entry = next((v for v in variables if v["name"] == "min_contract_value"), None)
    assert entry is not None
    assert entry["type"] == "custom"
    assert entry.get("description")


def test_entity_dashboards_exist_with_expected_uids() -> None:
    uids = {d["uid"] for d in _load_dashboards().values()}
    assert {"base-adjudicante", "base-adjudicatario"} <= uids


@pytest.mark.parametrize("name", ["overview.json", "overview-municipios.json"])
def test_overview_links_to_entity_dashboards(name: str) -> None:
    text = (DASHBOARD_DIR / name).read_text()
    assert "/d/base-adjudicante/" in text
    assert "/d/base-adjudicatario/" in text


@pytest.mark.parametrize(
    "name,var",
    [
        ("adjudicante-analysis.json", "adjudicante"),
        ("adjudicatario-analysis.json", "adjudicatario"),
    ],
)
def test_entity_dashboards_are_multi_select(name: str, var: str) -> None:
    import json

    variables = json.loads((DASHBOARD_DIR / name).read_text())["templating"]["list"]
    entry = next(v for v in variables if v["name"] == var)
    assert entry["multi"] is True
    assert entry["includeAll"] is False


@pytest.mark.parametrize("path", DASHBOARD_FILES, ids=lambda p: p.name)
def test_value_filter_uses_absolute_value(path: Path) -> None:
    assert not re.search(r"preco_contratual\s*>=\s*\$\{min_contract_value\}", path.read_text())


def test_010_query_stats_needs_compose_preload() -> None:
    path = SQL_DIR / "010_query_stats.sql"
    if not path.exists():
        pytest.skip("010 not present")
    assert re.search(
        r"CREATE\s+EXTENSION\s+IF\s+NOT\s+EXISTS\s+pg_stat_statements\s+SCHEMA\s+public",
        _sql_code(path),
        re.I,
    )
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    preload = re.search(r"shared_preload_libraries=(\S+)", compose)
    assert preload is not None
    assert "pg_stat_statements" in preload.group(1).split(",")
