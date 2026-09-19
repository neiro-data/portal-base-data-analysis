import polars as pl

from base_gov.entities import (
    canonical_names,
    classify_nipc,
    entity_key,
    explode_entities,
    normalize_name,
    split_packed,
)


def _keyed(cells: list[str | None]) -> pl.DataFrame:
    df = pl.DataFrame({"idcontrato": range(len(cells)), "adjudicatarios": cells})
    return entity_key(explode_entities(df, "adjudicatarios", prefix="adj"), prefix="adj")


def test_normalize_name() -> None:
    assert normalize_name("  Câmara  Municipal de S. João ") == "camara municipal de s joao"


def test_split_packed_sentinel_and_dash_in_name() -> None:
    df = pl.DataFrame({"a": ["- - Município de Gouveia", "501413197 - Foo - Bar"]})
    out = df.with_columns(split_packed("a", prefix="x"))
    assert out["x_nipc"].to_list() == ["-", "501413197"]
    assert out["x_name"].to_list() == ["Município de Gouveia", "Foo - Bar"]


def test_explode_multi_entity_cell() -> None:
    out = _keyed(["500000001 - A\n500000002 - B | C", None])
    assert out["adj_nipc"].to_list() == ["500000001", "500000002"]
    assert out["adj_name"].to_list() == ["A", "B | C"]


def test_classify_dirty_nipcs() -> None:
    df = pl.DataFrame({"n": ["502 544 180", "507857542_1", "51372670", "-", "123456789"]})
    got = df.select(classify_nipc(pl.col("n")).alias("s"))["s"].to_list()
    assert got == ["YES", "NIF_INCORRECTLY_PARSED", "WRONG_LENGTH", "NO_NIF", "YES"]


def test_same_nipc_two_spellings_one_key() -> None:
    out = _keyed(["501413197 - Acme Lda", "501413197 - ACME, Lda."])
    assert out["adj_key"].unique().to_list() == ["501413197"]
    assert set(out["adj_key_source"]) == {"nipc"}
    assert canonical_names(out, prefix="adj").height == 1


def test_dirty_nipc_not_name_matched() -> None:
    out = _keyed(["51372670 - Acme Lda", "507857542_1 - Acme Lda"])
    assert set(out["adj_key_source"]) == {"nipc_dirty"}
    assert out["adj_key"].n_unique() == 2


def test_similarity_only_for_sentinel() -> None:
    out = _keyed(
        [
            "- - Maria da Silva Santos",
            "- - Maria da Silva  Santos.",
            "- - Joao Pereira",
            "999999999 - Maria da Silva Santos",
        ]
    )
    by_src = out.group_by("adj_key_source").agg(pl.col("adj_key").n_unique())
    counts = dict(zip(by_src["adj_key_source"], by_src["adj_key"], strict=True))
    assert counts == {"name": 2, "nipc": 1}


def test_pipe_and_semicolon_do_not_split() -> None:
    out = _keyed(["500000001 - A | B; C"])
    assert out.height == 1
    assert out["adj_name"].to_list() == ["A | B; C"]


def test_blank_lines_and_null_cells_dropped() -> None:
    out = _keyed(["", None, "\n500000001 - A\n  \n"])
    assert out["adj_nipc"].to_list() == ["500000001"]


def test_empty_frame_does_not_crash() -> None:
    df = pl.DataFrame(
        {"idcontrato": [], "adjudicatarios": []},
        schema={"idcontrato": pl.Int64, "adjudicatarios": pl.String},
    )
    out = entity_key(explode_entities(df, "adjudicatarios", prefix="adj"), prefix="adj")
    assert out.height == 0
    assert {"adj_status", "adj_key", "adj_key_source"} <= set(out.columns)
    assert canonical_names(out, prefix="adj").height == 0


def test_all_null_cells_do_not_crash() -> None:
    df = pl.DataFrame(
        {"idcontrato": [0, 1], "adjudicatarios": [None, None]},
        schema={"idcontrato": pl.Int64, "adjudicatarios": pl.String},
    )
    assert (
        entity_key(explode_entities(df, "adjudicatarios", prefix="adj"), prefix="adj").height == 0
    )


def test_no_sentinel_rows_never_name_source() -> None:
    out = _keyed(["501413197 - A", "51372670 - A"])
    assert "name" not in set(out["adj_key_source"])


def test_sentinel_is_only_name_path() -> None:
    out = _keyed(["- - Foo Lda", "123 - Foo Lda", "abc - Foo Lda", "501413197 - Foo Lda"])
    src = dict(zip(out["adj_nipc"], out["adj_key_source"], strict=True))
    assert src == {"-": "name", "123": "nipc_dirty", "abc": "nipc_dirty", "501413197": "nipc"}


def test_spaced_nipc_keyed_cleaned() -> None:
    out = _keyed(["502 544 180 - Foo"])
    assert out["adj_status"].to_list() == ["YES"]
    assert out["adj_key"].to_list() == ["502544180"]


def test_entity_key_preserves_order_and_count() -> None:
    cells = ["- - Foo Lda", "500000001 - A", "- - Foo Lda", "- - Foo Lda.", "51372670 - Z"]
    df = pl.DataFrame({"idcontrato": range(len(cells)), "adjudicatarios": cells})
    exploded = explode_entities(df, "adjudicatarios", prefix="adj")
    out = entity_key(exploded, prefix="adj")
    assert out.height == exploded.height
    assert out["idcontrato"].to_list() == exploded["idcontrato"].to_list()


def test_canonical_names_one_per_key_modal_then_tiebreak() -> None:
    df = pl.DataFrame(
        {
            "k_key": ["1", "1", "1", "2", "2", "3", "3"],
            "k_name": ["Aa", "Bb", "Bb", "Zz", "Yyy", "bb", "ab"],
        }
    )
    out = canonical_names(df, prefix="k")
    got = dict(zip(out["k_key"], out["k_name"], strict=True))
    assert out.height == 3
    # modal wins; tie -> longest; tie on length -> a-z
    assert got == {"1": "Bb", "2": "Yyy", "3": "ab"}


def test_nameless_sentinel_entries_are_not_merged() -> None:
    out = _keyed(["- - ..", "- - 中文", "- - Foo", "- - Foo"])
    assert out["adj_key"].n_unique() == 3
    assert set(out["adj_key_source"]) == {"name"}
    assert out["adj_key"].to_list()[:2] == ["NONAME::0", "NONAME::1"]


def test_value_per_day_and_impact() -> None:
    from base_gov.stats import value_impact, value_per_day

    df = pl.DataFrame(
        {"preco_contratual": [100.0, 5.0, 60.0], "prazo_execucao": [10, 0, 3_000_000]}
    )
    out = df.with_columns(value_per_day())
    assert out["valor_por_dia"].to_list() == [10.0, None, None]
    kept = df.filter(pl.col("preco_contratual") >= 10)
    imp = value_impact(df, kept)
    assert imp.filter(pl.col("metric") == "n_contracts")["change"].item() == -1.0


def test_filter_min_value_uses_absolute_value() -> None:
    from base_gov.stats import filter_min_value

    df = pl.DataFrame({"preco_contratual": [-500.0, -5.0, 0.0, 9.99, 10.0, 250.0]})
    assert filter_min_value(df)["preco_contratual"].to_list() == [-500.0, 10.0, 250.0]


def test_is_municipio_expr_accent_and_case() -> None:
    from base_gov.contracts import is_municipio_expr

    df = pl.DataFrame(
        {"adjudicante_name": ["Município de Gouveia", "MUNICIPIO do Porto", "Câmara Municipal X"]}
    )
    assert df.select(is_municipio_expr().alias("m"))["m"].to_list() == [True, True, False]
