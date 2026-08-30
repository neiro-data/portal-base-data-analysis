"""Client and scrapers for Portugal's public procurement portal, base.gov.pt."""

from base_gov.client import BaseGovClient, BaseGovError, ClientConfig
from base_gov.models import Contract, Entity, SearchPage
from base_gov.queries import contracts_query, entities_query, month_windows
from base_gov.scrape import entity_contracts, search_entities

__all__ = [
    "BaseGovClient",
    "BaseGovError",
    "ClientConfig",
    "Contract",
    "Entity",
    "SearchPage",
    "contracts_query",
    "entities_query",
    "entity_contracts",
    "month_windows",
    "search_entities",
]
