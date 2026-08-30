"""Scrapers map payloads to records; export writes stable CSV columns."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import httpx
import pytest
from pytest_httpx import HTTPXMock

from base_gov.client import BaseGovClient, ClientConfig
from base_gov.export import CONTRACT_COLUMNS, contracts_frame, entities_frame, write_csv
from base_gov.models import Contract, Entity
from base_gov.scrape import entity_contracts, search_entities

RESULTS_URL = "https://www.base.gov.pt/Base4/pt/resultados/"
FAST = ClientConfig(min_interval=0.0, backoff=0.0, timeout=1.0)

# Shapes copied from live responses.
ENTITY_ROW = {
    "nif": "502203323",
    "id": 3024,
    "country": "Portugal",
    "description": "A.M.L.A. - Associação dos Municípios do Litoral Alentejano",
}
CONTRACT_ROW = {
    "id": 12345,
    "objectBriefDescription": "Aquisição de serviços",
    "contractingProcedureType": "Ajuste Direto",
    "contracting": "Município de Lisboa",
    "contracted": "Fornecedor, Lda.",
    "initialContractualPrice": "1.055.006,92 €",
    "publicationDate": "27-02-2025",
}


@pytest.fixture
def primed(httpx_mock: HTTPXMock) -> HTTPXMock:
    httpx_mock.add_response(url=re.compile(r".*/pesquisa/.*"), text="<html></html>")
    return httpx_mock


def _body(total: int, items: list[dict[str, object]]) -> str:
    return json.dumps({"total": total, "items": items})


def test_search_entities_maps_payload_to_records(primed: HTTPXMock) -> None:
    primed.add_response(url=RESULTS_URL, text=_body(1, [ENTITY_ROW]))

    with BaseGovClient(FAST) as client:
        found = list(search_entities(client, "Município"))

    assert found == [
        Entity(
            id=3024,
            nif="502203323",
            description="A.M.L.A. - Associação dos Municípios do Litoral Alentejano",
            country="Portugal",
        )
    ]


def test_entity_contracts_issues_one_search_per_month(primed: HTTPXMock) -> None:
    for _ in range(3):
        primed.add_response(url=RESULTS_URL, text=_body(1, [CONTRACT_ROW]))

    with BaseGovClient(FAST) as client:
        found = list(
            entity_contracts(
                client,
                contracting="500051070",
                start=dt.date(2025, 1, 1),
                end=dt.date(2025, 3, 31),
            )
        )

    assert len(found) == 3
    posts = [r for r in primed.get_requests() if r.method == "POST"]
    queries = [httpx.QueryParams(p.content.decode())["query"] for p in posts]
    assert "desdedatacontrato=2025-01-01&atedatacontrato=2025-01-31" in queries[0]
    assert "desdedatacontrato=2025-02-01&atedatacontrato=2025-02-28" in queries[1]
    assert "desdedatacontrato=2025-03-01&atedatacontrato=2025-03-31" in queries[2]


def test_contract_record_fields(primed: HTTPXMock) -> None:
    primed.add_response(url=RESULTS_URL, text=_body(1, [CONTRACT_ROW]))

    with BaseGovClient(FAST) as client:
        contract = next(
            iter(
                entity_contracts(
                    client,
                    contracting="500051070",
                    start=dt.date(2025, 1, 1),
                    end=dt.date(2025, 1, 31),
                )
            )
        )

    assert contract == Contract(
        id=12345,
        object_description="Aquisição de serviços",
        procedure_type="Ajuste Direto",
        contracting="Município de Lisboa",
        contracted="Fornecedor, Lda.",
        initial_price="1.055.006,92 €",
        publication_date="27-02-2025",
    )


def test_write_csv_round_trips_unicode(tmp_path: Path) -> None:
    frame = entities_frame([Entity.from_payload(ENTITY_ROW)])
    destination = write_csv(frame, tmp_path / "nested" / "entities.csv")

    text = destination.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "id,nif,description,country"
    assert "Municípios do Litoral Alentejano" in text


def test_empty_export_still_writes_headers(tmp_path: Path) -> None:
    destination = write_csv(contracts_frame([]), tmp_path / "contracts.csv")

    assert destination.read_text(encoding="utf-8").strip() == ",".join(CONTRACT_COLUMNS)
