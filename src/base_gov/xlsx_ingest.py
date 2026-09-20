"""Capped loading of the `contratos2025.xlsx` bulk export.

The workbook is ~66 MB / a single Excel Table; nothing here may read it in
full. Every load goes through `load_contracts`, which always passes an
`n_rows` cap to `pl.read_excel` (default `DEFAULT_ROWS`).
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

DEFAULT_ROWS = 1000

# Observed header (first 1000 rows of data/source/contratos2025.xlsx, see
# plan-xlsx-entities.md step 1) mapped to the names the rest of this package
# uses. Kept as one place to update if a future export renames columns.
CONTRACT_ID = "idcontrato"
ADJUDICANTE = "adjudicante"
ADJUDICATARIOS = "adjudicatarios"
VALUE_EUR = "precoContratual"
CONTRACT_TYPE = "tipoContrato"
OBJECTO = "objectoContrato"
DESCRICAO = "descContrato"
SIGNED_DATE = "dataCelebracaoContrato"
PUBLISHED_DATE = "dataPublicacao"
CPV = "CPV"
NUTS = "NUTs"
YEAR = "Ano"

REQUIRED_COLUMNS = (CONTRACT_ID, ADJUDICANTE, ADJUDICATARIOS, VALUE_EUR, CONTRACT_TYPE)


def load_contracts(path: Path, rows: int = DEFAULT_ROWS) -> pl.DataFrame:
    """Read the first `rows` rows of the contracts workbook.

    Raises a clear error if the expected columns are missing, so a renamed
    export fails loudly instead of silently mis-aggregating.
    """
    df = pl.read_excel(path, read_options={"n_rows": rows})
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"expected columns {list(REQUIRED_COLUMNS)} not found in {path}; "
            f"missing {missing}; observed columns: {df.columns}"
        )
    return df
