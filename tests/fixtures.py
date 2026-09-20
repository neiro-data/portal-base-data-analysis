"""Small contracts workbooks/frames carrying the real bulk-export header."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import polars as pl

# Header of the real export, in order (sql/ingest/20_stage_contracts.sql).
HEADER: dict[str, Any] = {
    "idcontrato": pl.Int64,
    "nAnuncio": pl.String,
    "TipoAnuncio": pl.String,
    "idINCM": pl.Int64,
    "tipoContrato": pl.String,
    "idprocedimento": pl.Int64,
    "tipoprocedimento": pl.String,
    "objectoContrato": pl.String,
    "descContrato": pl.String,
    "adjudicante": pl.String,
    "adjudicatarios": pl.String,
    "dataPublicacao": pl.Date,
    "dataCelebracaoContrato": pl.Date,
    "precoContratual": pl.Float64,
    "CPV": pl.String,
    "prazoExecucao": pl.Int64,
    "LocalExecucao": pl.String,
    "fundamentacao": pl.String,
    "ProcedimentoCentralizado": pl.String,
    "numAcordoQuadro": pl.Int64,
    "DescrAcordoQuadro": pl.String,
    "precoBaseProcedimento": pl.Float64,
    "dataDecisaoAdjudicacao": pl.Date,
    "dataFechoContrato": pl.Date,
    "PrecoTotalEfetivo": pl.Float64,
    "regime": pl.String,
    "justifNReducEscrContrato": pl.String,
    "tipoFimContrato": pl.String,
    "CritMateriais": pl.String,
    "concorrentes": pl.String,
    "linkPecasProc": pl.String,
    "Observacoes": pl.String,
    "ContratEcologico": pl.String,
    "fundamentAjusteDireto": pl.String,
    "Ano": pl.Int64,
    "adjudicatarioPMEs": pl.String,
    "NUTs": pl.String,
    "Lotes": pl.String,
    "TipoCriterioAdjudicacao": pl.String,
}


def make_rows(
    year: int, n: int, *, start_id: int = 1, price: float = 1000.0, **overrides: Any
) -> pl.DataFrame:
    """`n` rows with the full header. `overrides` are constants or length-n lists per column."""
    data: dict[str, Any] = {c: [None] * n for c in HEADER}
    data.update(
        idcontrato=list(range(start_id, start_id + n)),
        tipoContrato=["Aquisição de serviços"] * n,
        tipoprocedimento=["Ajuste Direto Regime Geral"] * n,
        objectoContrato=["Serviços diversos"] * n,
        descContrato=["Descrição"] * n,
        adjudicante=["506609553 - Município de Montemor-o-Novo"] * n,
        adjudicatarios=["517304660 - Sítio dos Arados Lda"] * n,
        dataPublicacao=[dt.date(year, 1, 2)] * n,
        dataCelebracaoContrato=[dt.date(year, 1, 1)] * n,
        precoContratual=[price + i for i in range(n)],
        CPV=["79000000-8"] * n,
        prazoExecucao=[30] * n,
        concorrentes=["111111111 - A\n222222222 - B"] * n,
        Ano=[year] * n,
        NUTs=["PT18"] * n,
    )
    for col, value in overrides.items():
        data[col] = value if isinstance(value, list) else [value] * n
    return pl.DataFrame(data, schema=HEADER)


def write_workbook(source_dir: Path, year: int, df: pl.DataFrame) -> Path:
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"contratos{year}.xlsx"
    df.write_excel(path)
    return path
