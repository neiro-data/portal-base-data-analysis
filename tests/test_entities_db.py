"""Entity extraction, dedup, and idempotent parquet upserts."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from base_gov.entities_db import adjudicantes, adjudicatarios, read_entities, write_entities

ROWS = [
    {
        "idcontrato": 1,
        "adjudicante": "506609553 - Município de Montemor-o-Novo",
        "adjudicatarios": "517304660 - Sítio dos Arados Lda",
        "precoContratual": 1000.0,
        "tipoContrato": "Aquisição de serviços",
    },
    {
        "idcontrato": 2,
        # same adjudicante again -> must dedup down to one row
        "adjudicante": "506609553 - Município de Montemor-o-Novo",
        "adjudicatarios": "500113270 - FERRAZ LYNCE SA",
        "precoContratual": 500.0,
        "tipoContrato": "Outros",
    },
]


def _df() -> pl.DataFrame:
    return pl.DataFrame(ROWS)


def test_adjudicantes_dedups_by_entity_id() -> None:
    df = adjudicantes(_df())
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["entity_id"] == "506609553"
    assert row["name"] == "Município de Montemor-o-Novo"


def test_adjudicatarios_keeps_distinct_entities() -> None:
    df = adjudicatarios(_df())
    assert df.height == 2
    assert set(df["entity_id"].to_list()) == {"517304660", "500113270"}


def test_write_entities_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "entities_adjudicantes.parquet"
    rows = write_entities(adjudicantes(_df()), path)
    assert rows == 1
    assert path.exists()
    reread = read_entities(path)
    assert reread.height == 1
    assert reread.columns == ["entity_id", "name"]


def test_write_entities_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "entities_adjudicantes.parquet"
    write_entities(adjudicantes(_df()), path)
    rows = write_entities(adjudicantes(_df()), path)
    assert rows == 1  # re-running does not duplicate the same entity

    # A genuinely new entity does get added on the next upsert.
    extra = pl.DataFrame({"entity_id": ["123456789"], "name": ["Nova Entidade"]})
    rows = write_entities(extra, path)
    assert rows == 2
