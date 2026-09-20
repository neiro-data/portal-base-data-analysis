"""NIPC-first entity keys for the packed ``"<NIPC> - <Name>"`` columns of the contracts export.

An entity is identified by its NIPC. Name similarity is used only for entries whose NIPC is the
``-`` sentinel (no NIF), and never to merge entries that carry a (possibly dirty) NIPC.
"""

import re
import unicodedata
from collections import defaultdict

import polars as pl
from rapidfuzz import fuzz, process

ENTITY_SEP = "\n"
NO_NIPC = "-"
SIMILARITY_THRESHOLD = 92.0
_BLOCK_WIDTH = 8  # names are only compared within the same len(name) // _BLOCK_WIDTH bucket
_NAME_KEY_PREFIX = "NAME::"


def normalize_name(s: str) -> str:
    """Accent-fold, lowercase, drop dots and collapse whitespace."""
    folded = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", folded.lower().replace(".", "")).strip()


def _split_entry(entry: pl.Expr) -> tuple[pl.Expr, pl.Expr]:
    # splitn(" - ", 2) keeps any later " - " inside the name; "- - Name" yields ("-", "Name").
    parts = entry.str.strip_chars().str.splitn(" - ", 2)
    return parts.struct.field("field_0"), parts.struct.field("field_1")


def split_packed(col: str, *, prefix: str) -> list[pl.Expr]:
    """Split a single-valued packed cell into ``<prefix>_nipc`` (raw) and ``<prefix>_name``."""
    nipc, name = _split_entry(pl.col(col))
    return [nipc.alias(f"{prefix}_nipc"), name.alias(f"{prefix}_name")]


def _clean_nipc(nipc: pl.Expr) -> pl.Expr:
    return nipc.str.replace_all(r"(?:\s|\\t)+", "")


def classify_nipc(nipc: pl.Expr) -> pl.Expr:
    """YES | NO_NIF | WRONG_LENGTH | NIF_INCORRECTLY_PARSED, from the raw NIPC expression."""
    clean = _clean_nipc(nipc)
    return (
        pl.when(nipc.is_null())
        .then(pl.lit("NIF_INCORRECTLY_PARSED"))
        .when(nipc.str.strip_chars() == NO_NIPC)
        .then(pl.lit("NO_NIF"))
        .when(clean.str.contains(r"^\d{9}$"))
        .then(pl.lit("YES"))
        .when(clean.str.contains(r"^\d+$"))
        .then(pl.lit("WRONG_LENGTH"))
        .otherwise(pl.lit("NIF_INCORRECTLY_PARSED"))
    )


def explode_entities(df: pl.DataFrame, col: str, *, prefix: str) -> pl.DataFrame:
    """One row per (contract, entity): ``idcontrato``, ``<prefix>_nipc`` and ``<prefix>_name``."""
    nipc, name = _split_entry(pl.col("entry"))
    return (
        df.filter(pl.col(col).is_not_null())
        .select("idcontrato", pl.col(col).str.split(ENTITY_SEP).alias("entry"))
        .explode("entry", empty_as_null=False)
        .filter(pl.col("entry").str.strip_chars() != "")
        .select("idcontrato", nipc.alias(f"{prefix}_nipc"), name.alias(f"{prefix}_name"))
    )


def _cluster_names(names: list[str], threshold: float = SIMILARITY_THRESHOLD) -> dict[str, str]:
    """Map each name to its cluster representative (the longest normalized name in it).

    Names are blocked by (first token, length bucket); inside a block, names are visited longest
    first and join the first representative scoring >= ``threshold`` on ``token_sort_ratio``.
    """
    normalized = {n: normalize_name(n) for n in set(names)}
    blocks: dict[tuple[str, int], list[str]] = defaultdict(list)
    for norm in set(normalized.values()):
        first = norm.split(" ", 1)[0]
        blocks[(first, len(norm) // _BLOCK_WIDTH)].append(norm)

    representative: dict[str, str] = {}
    for members in blocks.values():
        reps: list[str] = []
        for norm in sorted(members, key=lambda s: (-len(s), s)):
            hit = (
                process.extractOne(norm, reps, scorer=fuzz.token_sort_ratio, score_cutoff=threshold)
                if reps
                else None
            )
            if hit is None:
                reps.append(norm)
                representative[norm] = norm
            else:
                representative[norm] = hit[0]
    return {n: representative[norm] for n, norm in normalized.items()}


def entity_key[F: (pl.DataFrame, pl.LazyFrame)](
    df: F, *, prefix: str, threshold: float = SIMILARITY_THRESHOLD
) -> F:
    """Add ``<prefix>_status``, ``<prefix>_key`` and ``<prefix>_key_source``.

    Needs ``<prefix>_nipc`` (raw) and ``<prefix>_name``. Rules, in order: a valid 9-digit NIPC is
    the key (``nipc``); a dirty NIPC is kept cleaned and separate (``nipc_dirty``); only the ``-``
    sentinel falls back to a name-similarity cluster (``name``). A no-name entry gets
    ``NONAME::<idcontrato>``, so the key does not depend on row order
    (two no-name entries of the *same* contract share it — irrelevant for the single-valued
    ``adjudicante``).

    Accepts a ``DataFrame`` or a ``LazyFrame`` and returns the same kind; only the distinct
    ``NO_NIF`` names are collected, the rest of the frame stays lazy.
    """
    nipc, name = f"{prefix}_nipc", f"{prefix}_name"
    status, key, source = f"{prefix}_status", f"{prefix}_key", f"{prefix}_key_source"
    out = df.lazy().with_columns(classify_nipc(pl.col(nipc)).alias(status))

    no_nif_names = (
        out.filter(pl.col(status) == "NO_NIF")
        .select(pl.col(name).drop_nulls().unique())
        .collect()[name]
        .to_list()
    )
    mapping = _cluster_names(no_nif_names, threshold)
    cluster = pl.DataFrame(
        {name: list(mapping), "_rep": list(mapping.values())},
        schema={name: pl.String, "_rep": pl.String},
    )
    keyed = (
        out.join(cluster.lazy(), on=name, how="left")
        .with_columns(
            pl.when(pl.col(status) == "YES")
            .then(_clean_nipc(pl.col(nipc)))
            .when(pl.col(status) == "NO_NIF")
            .then(
                pl.when(pl.col("_rep").fill_null("") == "")
                .then(
                    pl.lit("NONAME::") + pl.col("idcontrato").cast(pl.String)
                )  # never merged, row-stable
                .otherwise(pl.lit(_NAME_KEY_PREFIX) + pl.col("_rep"))
            )
            .otherwise(_clean_nipc(pl.col(nipc)).fill_null(pl.lit("")))
            .alias(key),
            pl.when(pl.col(status) == "YES")
            .then(pl.lit("nipc"))
            .when(pl.col(status) == "NO_NIF")
            .then(pl.lit("name"))
            .otherwise(pl.lit("nipc_dirty"))
            .alias(source),
        )
        .drop("_rep")
    )
    return keyed.collect() if isinstance(df, pl.DataFrame) else keyed


def canonical_names(df: pl.DataFrame, *, prefix: str) -> pl.DataFrame:
    """One display name per ``<prefix>_key``: modal spelling, ties broken by longest then a-z."""
    key, name = f"{prefix}_key", f"{prefix}_name"
    return (
        df.filter(pl.col(name).is_not_null())
        .group_by(key, name)
        .agg(pl.len().alias("_n"))
        .sort(
            [key, "_n", pl.col(name).str.len_chars(), name], descending=[False, True, True, False]
        )
        .group_by(key, maintain_order=True)
        .agg(pl.col(name).first())
    )
