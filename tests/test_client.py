"""Client behaviour against a mocked portal: parsing, paging, retries."""

from __future__ import annotations

import json

import httpx
import pytest
from pytest_httpx import HTTPXMock

from base_gov.client import (
    SEARCH_ENTITIES,
    SORT_ENTITIES,
    BaseGovClient,
    BaseGovError,
    ClientConfig,
)

SEARCH_URL = "https://www.base.gov.pt/Base4/pt/pesquisa/?type=entidades"
RESULTS_URL = "https://www.base.gov.pt/Base4/pt/resultados/"

# No throttling or backoff sleeps in tests.
FAST = ClientConfig(min_interval=0.0, backoff=0.0, timeout=1.0)


def _page(total: int, ids: list[int]) -> str:
    items = [
        {"id": i, "nif": f"5000000{i:02d}", "country": "Portugal", "description": f"E{i}"}
        for i in ids
    ]
    # The endpoint answers with a JS object literal, indented and newline-padded.
    return "\n\t\t\t" + json.dumps({"total": total, "items": items}) + "\n"


@pytest.fixture
def primed(httpx_mock: HTTPXMock) -> HTTPXMock:
    httpx_mock.add_response(url=SEARCH_URL, text="<html></html>")
    return httpx_mock


def test_fetch_page_parses_padded_js_literal(primed: HTTPXMock) -> None:
    primed.add_response(url=RESULTS_URL, text=_page(2, [1, 2]))

    with BaseGovClient(FAST) as client:
        page = client.fetch_page(
            search_type=SEARCH_ENTITIES, query="texto=Município", sort=SORT_ENTITIES
        )

    assert page.total == 2
    assert [item["id"] for item in page.items] == [1, 2]


def test_fetch_page_sends_the_form_the_portal_expects(primed: HTTPXMock) -> None:
    primed.add_response(url=RESULTS_URL, text=_page(1, [1]))

    with BaseGovClient(FAST) as client:
        client.fetch_page(
            search_type=SEARCH_ENTITIES,
            query="texto=Município",
            sort=SORT_ENTITIES,
            page=3,
            size=25,
        )

    post = [r for r in primed.get_requests() if r.method == "POST"][0]
    body = httpx.QueryParams(post.content.decode())
    assert body["type"] == "search_entidades"
    assert body["version"] == "109.0"
    assert body["query"] == "texto=Município"
    assert body["sort"] == "+description"
    assert body["page"] == "3"
    assert body["size"] == "25"
    assert post.headers["X-Requested-With"] == "XMLHttpRequest"


def test_session_is_primed_once_then_reused(primed: HTTPXMock) -> None:
    primed.add_response(url=RESULTS_URL, text=_page(60, list(range(0, 25))))
    primed.add_response(url=RESULTS_URL, text=_page(60, list(range(25, 50))))
    primed.add_response(url=RESULTS_URL, text=_page(60, list(range(50, 60))))

    with BaseGovClient(FAST) as client:
        rows = list(
            client.iter_items(
                search_type=SEARCH_ENTITIES, query="texto=Município", sort=SORT_ENTITIES
            )
        )

    assert len(rows) == 60
    gets = [r for r in primed.get_requests() if r.method == "GET"]
    assert len(gets) == 1


def test_iter_items_uses_actual_page_length_not_requested_size(primed: HTTPXMock) -> None:
    # Server clamps: 25 asked for, 20 delivered. Paging must still terminate.
    primed.add_response(url=RESULTS_URL, text=_page(30, list(range(0, 20))))
    primed.add_response(url=RESULTS_URL, text=_page(30, list(range(20, 30))))

    with BaseGovClient(FAST) as client:
        rows = list(
            client.iter_items(search_type=SEARCH_ENTITIES, query="q", sort=SORT_ENTITIES, size=25)
        )

    assert [row["id"] for row in rows] == list(range(30))


def test_iter_items_stops_on_empty_page_when_total_overstates(primed: HTTPXMock) -> None:
    primed.add_response(url=RESULTS_URL, text=_page(999, [1, 2]))
    primed.add_response(url=RESULTS_URL, text=_page(999, []))

    with BaseGovClient(FAST) as client:
        rows = list(client.iter_items(search_type=SEARCH_ENTITIES, query="q", sort=SORT_ENTITIES))

    assert len(rows) == 2


def test_iter_items_honours_max_items(primed: HTTPXMock) -> None:
    primed.add_response(url=RESULTS_URL, text=_page(100, list(range(25))))

    with BaseGovClient(FAST) as client:
        rows = list(
            client.iter_items(
                search_type=SEARCH_ENTITIES, query="q", sort=SORT_ENTITIES, max_items=3
            )
        )

    assert len(rows) == 3


def test_timeouts_are_retried_then_succeed(primed: HTTPXMock) -> None:
    primed.add_exception(httpx.ReadTimeout("slow"), url=RESULTS_URL)
    primed.add_response(url=RESULTS_URL, text=_page(1, [1]))

    with BaseGovClient(FAST) as client:
        page = client.fetch_page(search_type=SEARCH_ENTITIES, query="q", sort=SORT_ENTITIES)

    assert page.total == 1


def test_persistent_timeout_raises_base_gov_error(primed: HTTPXMock) -> None:
    for _ in range(3):
        primed.add_exception(httpx.ReadTimeout("slow"), url=RESULTS_URL)

    with BaseGovClient(FAST) as client, pytest.raises(BaseGovError, match="after 3 attempts"):
        client.fetch_page(search_type=SEARCH_ENTITIES, query="q", sort=SORT_ENTITIES)


def test_client_error_is_not_retried(primed: HTTPXMock) -> None:
    primed.add_response(url=RESULTS_URL, status_code=400)

    with BaseGovClient(FAST) as client, pytest.raises(httpx.HTTPStatusError):
        client.fetch_page(search_type=SEARCH_ENTITIES, query="q", sort=SORT_ENTITIES)


def test_unparseable_body_raises_base_gov_error(primed: HTTPXMock) -> None:
    primed.add_response(url=RESULTS_URL, text="<html>error page</html>")

    with BaseGovClient(FAST) as client, pytest.raises(BaseGovError, match="could not parse"):
        client.fetch_page(search_type=SEARCH_ENTITIES, query="q", sort=SORT_ENTITIES)
