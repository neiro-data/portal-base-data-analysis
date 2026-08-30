"""HTTP client for the BASE (base.gov.pt) Base4 search backend.

The portal has no documented API. Its search pages are server-rendered shells
that fold the submitted form into an opaque `query` string, then poll a single
AJAX endpoint for results::

    POST /Base4/pt/resultados/
        type=search_entidades|search_contratos
        version=109.0
        query=<opaque query string, see queries.py>
        sort=+description | -publicationDate
        page=<0-based>
        size=<page size, clamped server-side>

The response body is a JavaScript object literal (the page `eval`s it), which in
practice parses as JSON.

Operational notes, measured against the live site:

* A cold query costs roughly two minutes. Re-running the same query is markedly
  cheaper (~30s), so the backend caches per query. Timeouts must be generous;
  the default here is 300s.
* `size` is only honoured up to 25. Asking for more does not raise; it silently
  returns 20 rows (measured: 25 -> 25, 100 -> 20, 400 -> 20). The paginator
  therefore reads the stride back from the response rather than assuming it.
* The session cookies handed out by any `/Base4/pt/pesquisa/` page are required;
  posting without them hangs rather than erroring.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx

from base_gov.models import SearchPage

logger = logging.getLogger(__name__)

BASE_URL = "https://www.base.gov.pt"
RESULTS_PATH = "/Base4/pt/resultados/"
SEARCH_PATH = "/Base4/pt/pesquisa/"

#: Sent verbatim by the portal's own JavaScript. The server rejects nothing when
#: it drifts, but keeping it in step with the site avoids surprises.
API_VERSION = "109.0"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SORT_ENTITIES = "+description"
SORT_CONTRACTS = "-publicationDate"

SEARCH_ENTITIES = "search_entidades"
SEARCH_CONTRACTS = "search_contratos"


class BaseGovError(RuntimeError):
    """Raised when the portal answers with something unusable."""


@dataclass(slots=True)
class ClientConfig:
    """Tuning knobs for talking to a slow, rate-sensitive server."""

    timeout: float = 300.0
    max_retries: int = 3
    backoff: float = 5.0
    #: Minimum seconds between requests, to stay a polite client.
    min_interval: float = 2.0
    page_size: int = 25
    user_agent: str = DEFAULT_USER_AGENT


class BaseGovClient:
    """Thin, synchronous client over the Base4 results endpoint."""

    def __init__(
        self,
        config: ClientConfig | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config or ClientConfig()
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=self.config.timeout,
            follow_redirects=True,
            transport=transport,
            headers={
                "User-Agent": self.config.user_agent,
                "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
            },
        )
        self._primed: set[str] = set()
        self._last_request_at = 0.0

    def __enter__(self) -> BaseGovClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ---------------------------------------------------------------- requests

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.config.min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _prime_session(self, form: str) -> None:
        """Fetch the search page once so the server hands us its session cookies."""
        if form in self._primed:
            return
        self._throttle()
        response = self._client.get(SEARCH_PATH, params={"type": form})
        response.raise_for_status()
        self._primed.add(form)

    def fetch_page(
        self,
        *,
        search_type: str,
        query: str,
        sort: str,
        page: int = 0,
        size: int | None = None,
    ) -> SearchPage:
        """Fetch a single page of results, retrying on timeouts and 5xx."""
        form = "entidades" if search_type == SEARCH_ENTITIES else "contratos"
        self._prime_session(form)

        payload = {
            "type": search_type,
            "version": API_VERSION,
            "query": query,
            "sort": sort,
            "page": str(page),
            "size": str(size if size is not None else self.config.page_size),
        }

        last_error: Exception | None = None
        for attempt in range(1, self.config.max_retries + 1):
            self._throttle()
            logger.debug("POST %s page=%s attempt=%s", search_type, page, attempt)
            try:
                response = self._client.post(
                    RESULTS_PATH,
                    data=payload,
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": f"{BASE_URL}{SEARCH_PATH}?type={form}",
                    },
                )
                response.raise_for_status()
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise
                last_error = exc
            else:
                return _parse_results(response.text)

            if attempt < self.config.max_retries:
                delay = self.config.backoff * attempt
                logger.warning("page %s failed (%s); retrying in %.0fs", page, last_error, delay)
                time.sleep(delay)

        msg = f"{search_type} page {page} failed after {self.config.max_retries} attempts"
        raise BaseGovError(msg) from last_error

    # -------------------------------------------------------------- pagination

    def iter_pages(
        self,
        *,
        search_type: str,
        query: str,
        sort: str,
        size: int | None = None,
        start_page: int = 0,
        rows_before: int = 0,
    ) -> Iterator[SearchPage]:
        """Yield result pages for a query, walking until exhausted.

        The server clamps `size`, so the stride comes from the first response
        rather than from what was requested. Stops on an empty page so a
        miscounted `total` cannot spin forever.

        `start_page` resumes a partially completed walk, with `rows_before`
        saying how many rows precede it. The caller knows that exactly; deriving
        it from the stride would over-request when the resumed page is the
        short final one.
        """
        page = start_page
        seen = 0
        total: int | None = None

        while True:
            result = self.fetch_page(
                search_type=search_type, query=query, sort=sort, page=page, size=size
            )
            if total is None:
                total = result.total
                logger.info("query matched %s rows", total)
            if result.is_empty:
                return

            yield result
            seen += len(result.items)

            if rows_before + seen >= total:
                return
            page += 1

    def iter_items(
        self,
        *,
        search_type: str,
        query: str,
        sort: str,
        size: int | None = None,
        max_items: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield every result row for a query, flattening `iter_pages`."""
        seen = 0
        pages = self.iter_pages(search_type=search_type, query=query, sort=sort, size=size)
        for result in pages:
            for item in result.items:
                yield item
                seen += 1
                if max_items is not None and seen >= max_items:
                    return


def _parse_results(body: str) -> SearchPage:
    """Parse the endpoint's JavaScript object literal response."""
    text = body.strip()
    if not text:
        msg = "empty response from results endpoint"
        raise BaseGovError(msg)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"could not parse results response: {text[:200]!r}"
        raise BaseGovError(msg) from exc

    if not isinstance(payload, dict):
        msg = f"unexpected results payload of type {type(payload).__name__}"
        raise BaseGovError(msg)

    items = payload.get("items") or []
    if not isinstance(items, list):
        msg = "results payload has a non-list 'items'"
        raise BaseGovError(msg)

    return SearchPage(total=int(payload.get("total", 0)), items=items)
