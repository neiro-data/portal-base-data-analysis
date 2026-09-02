"""Load Portugal's public procurement bulk export (base.gov.pt) as parquet and query it."""

from base_gov import analysis, bulk_ingest, entities_db
from base_gov.xlsx_ingest import load_contracts

__all__ = [
    "analysis",
    "bulk_ingest",
    "entities_db",
    "load_contracts",
]
