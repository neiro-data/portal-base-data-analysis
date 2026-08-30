"""High-level scrapes: entity lookup and per-entity contract collection."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator

from base_gov.client import (
    SEARCH_CONTRACTS,
    SEARCH_ENTITIES,
    SORT_CONTRACTS,
    SORT_ENTITIES,
    BaseGovClient,
)
from base_gov.models import Contract, Entity
from base_gov.queries import contracts_query, entities_query, month_windows

logger = logging.getLogger(__name__)


def search_entities(
    client: BaseGovClient, text: str, *, max_items: int | None = None
) -> Iterator[Entity]:
    """Yield every entity whose NIPC, description or country matches `text`.

    This is the programmatic equivalent of typing into "Entidades" and pressing
    "Pesquisar". Unlike the portal's own "Exportar resultados" button — which
    ignores the active query and always dumps the first 250 entities overall —
    this walks the real result set.
    """
    rows = client.iter_items(
        search_type=SEARCH_ENTITIES,
        query=entities_query(text),
        sort=SORT_ENTITIES,
        max_items=max_items,
    )
    for row in rows:
        yield Entity.from_payload(row)


def search_entity_batches(
    client: BaseGovClient, text: str, *, start_page: int = 0, rows_before: int = 0
) -> Iterator[list[Entity]]:
    """Yield entities one result page at a time, for batched ingestion.

    `start_page` resumes a walk that already has batches on disk, and
    `rows_before` is how many rows those batches already hold.
    """
    pages = client.iter_pages(
        search_type=SEARCH_ENTITIES,
        query=entities_query(text),
        sort=SORT_ENTITIES,
        start_page=start_page,
        rows_before=rows_before,
    )
    for page in pages:
        yield [Entity.from_payload(row) for row in page.items]


def entity_contracts(
    client: BaseGovClient,
    *,
    contracting: str,
    start: dt.date,
    end: dt.date,
    max_items_per_window: int | None = None,
) -> Iterator[Contract]:
    """Yield contracts awarded by `contracting` between `start` and `end`.

    `contracting` should be a NIPC where possible — the field also accepts a
    description fragment, but that matches loosely across entities.

    The range is walked one calendar month at a time. The portal answers a
    narrow window far more reliably than a broad one, and a failure then costs
    a single month rather than the whole span.
    """
    for window_start, window_end in month_windows(start, end):
        logger.info("fetching %s .. %s", window_start, window_end)
        query = contracts_query(
            contracting=contracting,
            contract_date_from=window_start,
            contract_date_to=window_end,
        )
        rows = client.iter_items(
            search_type=SEARCH_CONTRACTS,
            query=query,
            sort=SORT_CONTRACTS,
            max_items=max_items_per_window,
        )
        for row in rows:
            yield Contract.from_payload(row)
