"""Analysis functions over a small in-memory fixture frame."""

from __future__ import annotations

import polars as pl

from base_gov.analysis import (
    by_contract_type,
    contract_id_stats,
    distinct_adjudicantes,
    distinct_adjudicatarios,
    is_municipality,
    municipality_contracts,
    total_value_eur,
)

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
        "adjudicante": "504828576 - MUNICÍPIO DE ÉVORA",
        "adjudicatarios": "500113270 - FERRAZ LYNCE SA",
        "precoContratual": "1.234,56 €",
        "tipoContrato": "Aquisição de bens móveis",
    },
    {
        "idcontrato": 2,  # duplicate id: id column is not globally unique
        "adjudicante": "501507930 - Instituto Superior Técnico",
        "adjudicatarios": "517304660 - Sítio dos Arados Lda",
        "precoContratual": 500.0,
        "tipoContrato": "Aquisição de serviços",
    },
    {
        "idcontrato": 3,
        "adjudicante": "501143530 - municipio de Castelo Branco",
        "adjudicatarios": "506093190 - Penasegur Lda",
        "precoContratual": 250.0,
        "tipoContrato": "Outros",
    },
    {
        "idcontrato": 4,
        "adjudicante": "999999999 - Freguesia de Alcochete",
        "adjudicatarios": "888888888 - Municipal Sports Lda",
        "precoContratual": 300.0,
        "tipoContrato": "Outros",
    },
]


def _df() -> pl.DataFrame:
    return pl.DataFrame(ROWS)


def test_distinct_counts() -> None:
    df = _df()
    assert distinct_adjudicantes(df) == 5
    assert distinct_adjudicatarios(df) == 4


def test_contract_id_stats_detects_duplicates() -> None:
    stats = contract_id_stats(_df())
    assert stats == {"count": 5, "n_unique": 4, "is_unique": False}


def test_total_value_eur_handles_string_and_numeric() -> None:
    # 1000.0 + 1234.56 + 500.0 + 250.0 + 300.0
    assert total_value_eur(_df()) == 3284.56


def test_by_contract_type_sorted_desc() -> None:
    result = by_contract_type(_df())
    assert result["count"].sum() == 5
    values = result["total_value_eur"].to_list()
    assert values == sorted(values, reverse=True)


def test_is_municipality_matches_accented_and_case_variants() -> None:
    df = _df().with_columns(is_municipality().alias("is_muni"))
    flags = df["is_muni"].to_list()
    assert flags[0] is True  # "Município"
    assert flags[1] is True  # "MUNICÍPIO"
    assert flags[3] is True  # "municipio" (no accent)
    assert flags[4] is False  # "Freguesia" adjudicante


def test_municipality_contracts_excludes_negative_lookalikes() -> None:
    df = municipality_contracts(_df())
    assert df.height == 3  # rows 0, 1, 3 (Município / MUNICÍPIO / municipio)
    # "Municipal Sports Lda" (adjudicatario, not adjudicante) must not match.
    assert 4 not in df["idcontrato"].to_list()
