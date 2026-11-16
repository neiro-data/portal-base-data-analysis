"""Treated schema, row hash, the below-min-value flag, and the notebook/entity regressions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import polars as pl
import pytest

from base_gov.entities import entity_key, split_packed
from base_gov.stats import below_min_value_expr, filter_min_value
from base_gov.transform import (
    HASH_EXCLUDE,
    TIMESTAMP_SCHEMA,
    TREATED_SCHEMA,
    row_hash,
    treat,
)
from tests.fixtures import make_rows


@pytest.mark.parametrize("fmt", ["packed", "kv"])
def test_treat_pins_columns_and_dtypes(fmt: Literal["packed", "kv"]) -> None:
    out = treat(make_rows(2025, 5, party_format=fmt))
    assert out.columns == list(TREATED_SCHEMA)
    assert dict(out.schema) == TREATED_SCHEMA
    assert not set(TIMESTAMP_SCHEMA) & set(out.columns)
    assert out.height == 5
    assert out["adjudicante_nipc"][0] == "506609553"
    assert out["adjudicante_key"][0] == "506609553"
    assert out["n_concorrentes"][0] == 2
    assert out["is_municipio"][0] is True
    assert out["adjudicante_name"][0] == "Município de Montemor-o-Novo"
    assert out["adjudicante"][0] == "506609553 - Município de Montemor-o-Novo"
    assert out["adjudicatarios"][0] == "517304660 - Sítio dos Arados Lda"
    assert out["valor_por_dia"][0] == pytest.approx(1000.0 / 30)


def test_kv_and_packed_rows_treat_identically() -> None:
    kv = treat(make_rows(2025, 3, party_format="kv"))
    packed = treat(make_rows(2025, 3, party_format="packed"))
    assert kv.drop("row_hash").equals(packed.drop("row_hash"))
    assert kv["row_hash"].to_list() == packed["row_hash"].to_list()


def test_kv_html_entities_unescaped_after_canonicalisation() -> None:
    cell = "nifEntContrato=501000001|descEntContrato=A &amp; B &#124; C|codPaisEntContrato=PRT"
    out = treat(make_rows(2025, 1, adjudicante=cell))
    assert out["adjudicante_name"][0] == "A & B | C"
    assert out["adjudicante"][0] == "501000001 - A & B | C"


def test_treat_is_lazy_in_lazy_out_and_matches_eager() -> None:
    raw = make_rows(2025, 5)
    assert isinstance(treat(raw.lazy()), pl.LazyFrame)
    assert treat(raw.lazy()).collect().equals(treat(raw))


def test_treat_rejects_missing_raw_columns() -> None:
    with pytest.raises(ValueError, match="lack columns"):
        treat(make_rows(2025, 2).drop("CPV"))


def test_null_concorrentes_stays_null() -> None:
    assert treat(make_rows(2025, 2, concorrentes=None))["n_concorrentes"].null_count() == 2


def test_html_entities_are_unescaped() -> None:
    raw = make_rows(
        2025,
        1,
        adjudicante="506609553 - TIMESTAMP Business Intelligence &amp; Co",
        adjudicatarios="517304660 - A &lt;B&gt; C&#x13;",
        objectoContrato="Fornecimento &amp; instalação",
        descContrato="Descrição &quot;especial&quot;",
    )
    out = treat(raw)
    assert out["adjudicante_name"][0] == "TIMESTAMP Business Intelligence & Co"
    assert out["adjudicatarios"][0] == "517304660 - A <B> C"
    assert out["objecto_contrato"][0] == "Fornecimento & instalação"
    assert out["desc_contrato"][0] == 'Descrição "especial"'


def test_double_escaped_html_entities_are_fully_unescaped() -> None:
    # A few thousand source rows are double-escaped (`&amp;amp;`), which a single unescape pass
    # only resolves to `&amp;`.
    out = treat(make_rows(2025, 1, adjudicante="506609553 - Nunes &amp;amp; Cabral, Lda."))
    assert out["adjudicante_name"][0] == "Nunes & Cabral, Lda."


def test_row_hash_stable_and_sensitive() -> None:
    df = treat(make_rows(2025, 4))
    assert df["row_hash"].to_list() == treat(make_rows(2025, 4))["row_hash"].to_list()
    changed = treat(make_rows(2025, 4, objectoContrato=["Serviços diversos"] * 3 + ["Outro"]))
    same = (df["row_hash"] == changed["row_hash"]).to_list()
    assert same == [True, True, True, False]


def test_row_hash_ignores_excluded_columns() -> None:
    df = treat(make_rows(2025, 3))
    noisy = df.with_columns(
        first_ingested_at=pl.lit("x"),
        updated_at=pl.lit("y"),
        is_below_min_value=~pl.col("is_below_min_value"),
    )
    assert HASH_EXCLUDE >= {"first_ingested_at", "updated_at", "row_hash", "is_below_min_value"}
    assert noisy.select(row_hash(noisy.columns))["row_hash"].equals(df["row_hash"])


def test_min_value_changes_flag_but_not_hash() -> None:
    raw = make_rows(2025, 3, price=8.0)  # 8, 9, 10
    strict, lax = treat(raw, min_value=10.0), treat(raw, min_value=0.0)
    assert strict["is_below_min_value"].to_list() == [True, True, False]
    assert lax["is_below_min_value"].to_list() == [False, False, False]
    assert strict["row_hash"].equals(lax["row_hash"])


def test_below_min_value_boundaries() -> None:
    df = pl.DataFrame({"preco_contratual": [-10.0, -9.99, 9.99, 10.0, None]})
    flags = df.select(below_min_value_expr(min_value=10.0))["preco_contratual"].to_list()
    assert flags == [False, True, True, False, True]


def test_filter_min_value_equals_flag_filter() -> None:
    df = pl.DataFrame({"preco_contratual": [-50.0, -9.0, 0.0, 9.99, 10.0, 500.0, None]})
    assert filter_min_value(df).equals(df.filter(~below_min_value_expr()))


def test_noname_key_is_row_order_independent() -> None:
    def keyed(order: list[int]) -> dict[int, str]:
        df = pl.DataFrame(
            {"idcontrato": order, "adjudicante": ["- - .."] * len(order)}
        ).with_columns(split_packed("adjudicante", prefix="a"))
        out = entity_key(df, prefix="a")
        return dict(zip(out["idcontrato"], out["a_key"], strict=True))

    assert keyed([1, 2, 3]) == keyed([3, 1, 2]) == {1: "NONAME::1", 2: "NONAME::2", 3: "NONAME::3"}


def test_no_notebook_asserts_a_bare_row_count() -> None:
    root = Path(__file__).resolve().parent.parent / "notebooks"
    offenders = []
    for nb in root.glob("*.ipynb"):
        for cell in json.loads(nb.read_text())["cells"]:
            if cell["cell_type"] != "code":
                continue
            for line in "".join(cell["source"]).splitlines():
                if line.lstrip().startswith("assert") and re_bare_int(line):
                    offenders.append(f"{nb.name}: {line.strip()}")
    assert not offenders, offenders


def re_bare_int(line: str) -> bool:
    import re

    return bool(re.search(r"(?<![\w.])\d{3,}(?![\w.])", line))
