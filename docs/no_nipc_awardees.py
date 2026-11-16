"""Reproduce the numbers in docs/no-nipc-awardees.md.

Reads the raw yearly exports (data/raw/contracts_raw_*.parquet), explodes the packed
`adjudicatarios` key=value cell into one row per awardee entry, and profiles the entries
whose NIF is `-` by country (`codPaisEntContrato`) and by a name-based kind heuristic.

    uv run python docs/no_nipc_awardees.py [--raw-dir data/raw]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import polars as pl

RAW_COLUMNS = ["idcontrato", "adjudicatarios", "precoContratual", "Ano", "tipoprocedimento"]
DESC_RE = r"\|descEntContrato=(.*?)(?:\|[A-Za-z][A-Za-z0-9]*=|$)"

# Name heuristics, applied to the normalised (upper-case, punctuation -> space) name.
FOREIGN_FORM = (
    r"\b(S ?L ?U?|S ?A ?U|GMBH|GMB H|LTD|LIMITED|B ?V|N ?V|SAS|SARL|S ?R ?L|S ?P ?A|INC|LLC|CORP"
    r"|CORPORATION|PLC|AG|AB|OY|A S|APS|KFT|SUCURSAL|BRANCH|SPA|SRL|SL|SLU|SAU|CO KG|KG|SE|AS"
    r"|ASA|PTY|LLP)$|\b(GMBH|SUCURSAL|BRANCH|LIMITED|LTD|INC|LLC)\b"
)
PT_ORG = (
    r"\b(LDA|LIMITADA|UNIPESSOAL|SA|S A|SGPS|CRL|ACE|SOCIEDADE|COOPERATIVA|ASSOCIA[CÇ][AÃ]O"
    r"|FUNDA[CÇ][AÃ]O|INSTITUTO|CENTRO|CLUBE|MUNIC[IÍ]PIO|FREGUESIA|HOSPITAL|UNIVERSIDADE|ESCOLA"
    r"|EMPRESA|COMPANHIA|CONSTRU[CÇ]|SERVI[CÇ]OS|COM[EÉ]RCIO|IND[UÚ]STRIA|EDI[CÇ][OÕ]ES|EDITORA"
    r"|LIVRARIA|FARM[AÁ]CIA|CL[IÍ]NICA|GRUPO|PORTUGAL|LISBOA|PORTO|EST[UÚ]DIO|ATELIER"
    r"|PRODU[CÇ][OÕ]ES|TRANSPORTES|RESTAURANTE|HOTEL|TEATRO|ORQUESTRA|BANDA|RANCHO|BOMBEIROS"
    r"|SANTA CASA|MISERIC[OÓ]RDIA|IGREJA|PAR[OÓ]QUIA|CONSULTORES|ADVOGADOS|ARQUITE[C]?TOS"
    r"|ENGENHARIA|SOLU[CÇ][OÕ]ES|SISTEMAS|TECNOLOGIA|AGRUPAMENTO|CONS[OÓ]RCIO|ENI|EIRL"
    r"|E FILHOS|IRM[AÃ]OS)\b"
)
# Partial list of common Portuguese first names: validates the "person" bucket (lower bound).
PT_FIRST_NAMES = frozenset(
    """ANA MARIA JOÃO JOAO JOSÉ JOSE ANTÓNIO ANTONIO PAULO CARLOS MANUEL LUÍS LUIS PEDRO RUI NUNO
    JORGE SUSANA SANDRA MIGUEL RICARDO FILIPE SÓNIA SONIA RITA SARA TERESA HELENA CRISTINA PATRÍCIA
    PATRICIA CATARINA MARTA ISABEL FERNANDO FRANCISCO TIAGO BRUNO ANDRÉ ANDRE HUGO SÉRGIO SERGIO
    VÍTOR VITOR RAQUEL CLÁUDIA CLAUDIA INÊS INES JOANA SOFIA DANIEL DAVID RUTE MÁRIO MARIO ARMANDO
    ALEXANDRE ARTUR EDUARDO TELMO HÉLDER HELDER LILIANA VERA DIANA MÓNICA MONICA ALICE FÁTIMA FATIMA
    ROSA LÚCIA LUCIA MARCO GONÇALO GONCALO RODRIGO DIOGO RAFAEL JOAQUIM FERNANDA ELISABETE PAULA
    CARLA SÍLVIA SILVIA ALBERTO ÁLVARO ALVARO AMÉLIA AMELIA ANDREIA ÂNGELA ANGELA CÉLIA CELIA DULCE
    GABRIELA GRAÇA HENRIQUE IVO JÚLIO JULIO LEONOR MARGARIDA NATÁLIA NATALIA OLGA ORLANDO RENATO
    ROGÉRIO ROGERIO SAMUEL SIMÃO SIMAO VANESSA VASCO ZÉLIA ABEL ADRIANO AGOSTINHO ALBINO ALFREDO
    AMÉRICO AMERICO ANABELA ARMINDA AUGUSTO AVELINO BEATRIZ BENILDE CARMEN CONCEIÇÃO CUSTÓDIO
    DOMINGOS EMANUEL ERNESTO FABIO FÁBIO FILOMENA GLÓRIA GUILHERME HORÁCIO IRENE JAIME LAURA LUÍSA
    LUISA MANUELA MARCELO NELSON OCTÁVIO QUITÉRIA RAUL SALOMÉ TÂNIA TANIA VALTER VICTOR
    VIRGÍNIA""".split()
)
SUSPECT_COUNTRIES = ["AFG", "AND", "EGY", "ALB", "AGO"]

PERSON = "person (name-like)"


def kv(key: str) -> pl.Expr:
    return pl.col("e").str.extract(rf"(?:^|\|){key}=([^|]*)").str.strip_chars()


def load_entries(raw_dir: Path) -> pl.DataFrame:
    """One row per awardee entry, contracts deduplicated on idcontrato (last file wins)."""
    files = sorted(raw_dir.glob("contracts_raw_*.parquet"))
    if not files:
        raise SystemExit(f"no contracts_raw_*.parquet under {raw_dir}")
    raw = pl.concat(
        [pl.read_parquet(f, columns=RAW_COLUMNS) for f in files], how="vertical_relaxed"
    ).unique("idcontrato", keep="last")
    return (
        raw.filter(pl.col("adjudicatarios").is_not_null() & (pl.col("adjudicatarios") != ""))
        .with_columns(e=pl.col("adjudicatarios").str.split("\n"))
        .with_columns(n_entries=pl.col("e").list.len())
        .explode("e", empty_as_null=True)
        .with_columns(e=pl.col("e").str.strip_chars())
        .filter(pl.col("e") != "")
        .with_columns(
            nif=kv("nifEntContrato"),
            name=pl.col("e").str.extract(DESC_RE).str.strip_chars(),
            country=kv("codPaisEntContrato"),
        )
        .with_columns(
            norm=pl.col("name")
            .str.to_uppercase()
            .str.replace_all(r"[^\p{L}\p{N} ]", " ")
            .str.replace_all(r"\s+", " ")
            .str.strip_chars()
        )
        .drop("adjudicatarios", "e")
    )


def classify(no_nif: pl.DataFrame) -> pl.DataFrame:
    norm = pl.col("norm")
    return no_nif.with_columns(
        kind=pl.when(norm.str.contains(FOREIGN_FORM))
        .then(pl.lit("foreign legal form"))
        .when(norm.str.contains(PT_ORG) | pl.col("name").str.contains("&", literal=True))
        .then(pl.lit("PT-style company/org"))
        .when(norm.str.contains(r"^[\p{L} ]+$") & norm.str.count_matches(" ").is_between(1, 7))
        .then(pl.lit(PERSON))
        .otherwise(pl.lit("other/unclear")),
        origin=pl.when(pl.col("country") == "PRT")
        .then(pl.lit("PRT"))
        .when(pl.col("country").is_null())
        .then(pl.lit("null"))
        .otherwise(pl.lit("foreign")),
    )


def section(title: str) -> None:
    print(f"\n## {title}")


def report(entries: pl.DataFrame) -> None:
    valid = entries.filter(pl.col("nif").str.contains(r"^\d{9}$"))
    d = classify(entries.filter(pl.col("nif") == "-"))
    total_contracts = entries["idcontrato"].n_unique()

    section("Totals")
    print(f"contracts: {total_contracts:,}  entries: {entries.height:,}")
    print(f"NIF '-': {d.height:,}  other non-9-digit NIFs:")
    print(entries.filter(~pl.col("nif").str.contains(r"^\d{9}$") & (pl.col("nif") != "-"))
          .select("idcontrato", "nif", "name"))  # fmt: skip
    print(f"share that are the contract's only awardee: {(d['n_entries'] == 1).mean():.4f}")

    section("First digit of valid NIPCs (1-4 = individuals)")
    print(valid.group_by(first=pl.col("nif").str.slice(0, 1)).len().sort("first"))

    section("Origin x kind (entries)")
    print(d.pivot(on="origin", index="kind", values="nif", aggregate_function="len").sort("kind"))

    section("Origin: distinct contracts and value")
    print(
        d.unique(["idcontrato", "origin"])
        .group_by("origin")
        .agg(
            contracts=pl.len(),
            value_bn=pl.col("precoContratual").sum() / 1e9,
            median=pl.col("precoContratual").median(),
        )
    )
    affected = d.unique("idcontrato")
    all_value = entries.unique("idcontrato")["precoContratual"].sum()
    print(
        f"affected contracts: {affected.height:,} ({affected.height / total_contracts:.2%}); "
        f"value {affected['precoContratual'].sum() / 1e9:.2f}bn of {all_value / 1e9:.2f}bn"
    )

    section("Affected contracts per year")
    print(affected.group_by("Ano").len().sort("Ano"))

    section("Countries (top 25)")
    print(d.group_by("country").len().sort("len", descending=True).head(25))

    for origin in ["PRT", "foreign"]:
        for kind in d["kind"].unique().sort():
            s = d.filter((pl.col("origin") == origin) & (pl.col("kind") == kind))
            section(f"{origin} / {kind}: {s.height:,} entries, {s['norm'].n_unique():,} names")
            print(s.group_by("name", "country").len().sort("len", descending=True).head(8))

    persons = d.filter((pl.col("origin") == "PRT") & (pl.col("kind") == PERSON))
    section("PRT persons: validation and procedures")
    first = persons["norm"].str.split(" ").list.first()
    print(f"share with a common PT first name: {first.is_in(list(PT_FIRST_NAMES)).mean():.3f}")
    print(persons.unique("idcontrato").group_by("tipoprocedimento").len()
          .sort("len", descending=True).head(5))  # fmt: skip

    section("Largest foreign contracts")
    print(
        d.filter(pl.col("origin") == "foreign")
        .unique("idcontrato")
        .sort("precoContratual", descending=True)
        .select("name", "country", "precoContratual", "Ano")
        .head(8)
    )

    section("Exact-name matches to a valid NIPC elsewhere in the data")
    known = valid.group_by("norm").agg(nipcs=pl.col("nif").n_unique())
    print(
        d.join(known, on="norm", how="left")
        .group_by("origin", "kind")
        .agg(
            n=pl.len(),
            matched=pl.col("nipcs").is_not_null().sum(),
            matched_one_nipc=(pl.col("nipcs") == 1).sum(),
        )
        .sort("origin", "kind")
    )

    section("Suspect country codes (Portuguese companies coded foreign)")
    foreign = d.filter(pl.col("origin") == "foreign")
    pt_forms = foreign.filter(pl.col("norm").str.contains(r"\b(LDA|LIMITADA|UNIPESSOAL)\b"))
    print(f"foreign-coded with Lda/Unipessoal: {pt_forms.height:,} of {foreign.height:,}")
    for code in SUSPECT_COUNTRIES:
        s = d.filter(pl.col("country") == code)
        top = s.group_by("name").len().sort("len", descending=True).head(5)["name"].to_list()
        print(f"{code} {s.height}: {top}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    args = parser.parse_args()
    pl.Config.set_tbl_rows(40)
    pl.Config.set_fmt_str_lengths(70)
    pl.Config.set_tbl_width_chars(170)
    report(load_entries(args.raw_dir))


if __name__ == "__main__":
    main()
