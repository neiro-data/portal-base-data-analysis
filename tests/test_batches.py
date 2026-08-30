"""Batch ingestion: parts land on disk as they arrive, then join into one CSV."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from base_gov.client import SEARCH_ENTITIES, SORT_ENTITIES, BaseGovClient, ClientConfig
from base_gov.export import (
    ENTITY_COLUMNS,
    discard_parts,
    entities_frame,
    existing_parts,
    join_parts,
    part_path,
    parts_dir,
    rows_in_parts,
    write_part,
)
from base_gov.models import Entity
from base_gov.scrape import search_entity_batches

RESULTS_URL = "https://www.base.gov.pt/Base4/pt/resultados/"
FAST = ClientConfig(min_interval=0.0, backoff=0.0, timeout=1.0)


def _entity(i: int) -> Entity:
    return Entity(id=i, nif=f"5000000{i:02d}", description=f"Município {i}", country="Portugal")


def _body(total: int, ids: list[int]) -> str:
    items = [
        {"id": i, "nif": f"5000000{i:02d}", "country": "Portugal", "description": f"Município {i}"}
        for i in ids
    ]
    return json.dumps({"total": total, "items": items})


@pytest.fixture
def primed(httpx_mock: HTTPXMock) -> HTTPXMock:
    httpx_mock.add_response(url=re.compile(r".*/pesquisa/.*"), text="<html></html>")
    return httpx_mock


def test_parts_are_named_in_lexical_order_matching_numeric_order(tmp_path: Path) -> None:
    out = tmp_path / "entities.csv"
    for index in (0, 2, 10, 11):
        write_part(entities_frame([_entity(index)]), out, index)

    names = [p.name for p in existing_parts(out)]
    assert names == ["part-00000.csv", "part-00002.csv", "part-00010.csv", "part-00011.csv"]


def test_join_concatenates_parts_in_order(tmp_path: Path) -> None:
    out = tmp_path / "entities.csv"
    write_part(entities_frame([_entity(1), _entity(2)]), out, 0)
    write_part(entities_frame([_entity(3)]), out, 1)

    path, rows = join_parts(out, columns=ENTITY_COLUMNS)

    assert rows == 3
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(ENTITY_COLUMNS)
    assert [line.split(",")[0] for line in lines[1:]] == ["1", "2", "3"]


def test_join_with_no_parts_writes_headers_only(tmp_path: Path) -> None:
    out = tmp_path / "entities.csv"

    path, rows = join_parts(out, columns=ENTITY_COLUMNS)

    assert rows == 0
    assert path.read_text(encoding="utf-8").strip() == ",".join(ENTITY_COLUMNS)


def test_join_preserves_leading_zeros_in_nif(tmp_path: Path) -> None:
    out = tmp_path / "entities.csv"
    numeric_looking = Entity(id=1, nif="000892890", description="X", country="Portugal")
    write_part(entities_frame([numeric_looking]), out, 0)

    join_parts(out, columns=ENTITY_COLUMNS)

    assert "000892890" in out.read_text(encoding="utf-8")


def test_write_part_leaves_no_staging_file_behind(tmp_path: Path) -> None:
    out = tmp_path / "entities.csv"
    write_part(entities_frame([_entity(1)]), out, 0)

    assert part_path(out, 0).exists()
    assert list(parts_dir(out).glob("*.tmp")) == []


def test_discard_parts_removes_the_directory(tmp_path: Path) -> None:
    out = tmp_path / "entities.csv"
    write_part(entities_frame([_entity(1)]), out, 0)

    discard_parts(out)

    assert not parts_dir(out).exists()
    assert existing_parts(out) == []


def test_discard_parts_is_safe_when_nothing_exists(tmp_path: Path) -> None:
    discard_parts(tmp_path / "entities.csv")  # must not raise


def test_batches_arrive_one_page_at_a_time(primed: HTTPXMock) -> None:
    primed.add_response(url=RESULTS_URL, text=_body(30, list(range(0, 25))))
    primed.add_response(url=RESULTS_URL, text=_body(30, list(range(25, 30))))

    with BaseGovClient(FAST) as client:
        batches = [len(b) for b in search_entity_batches(client, "Município")]

    assert batches == [25, 5]


def test_resume_starts_from_the_requested_page(primed: HTTPXMock) -> None:
    # Resuming after 1 batch of 25 rows: server is asked for page=1, and the
    # short final page completes the total without a further request.
    primed.add_response(url=RESULTS_URL, text=_body(30, list(range(25, 30))))

    with BaseGovClient(FAST) as client:
        batches = list(search_entity_batches(client, "Município", start_page=1, rows_before=25))

    assert [e.id for e in batches[0]] == list(range(25, 30))
    posts = [r for r in primed.get_requests() if r.method == "POST"]
    assert len(posts) == 1
    assert b"page=1" in posts[0].content


def test_resume_accounts_for_already_fetched_rows_when_stopping(primed: HTTPXMock) -> None:
    # total=60, resuming at page 1. Two more pages of 25 and 10 complete it;
    # the walk must stop rather than requesting a fourth page.
    primed.add_response(url=RESULTS_URL, text=_body(60, list(range(25, 50))))
    primed.add_response(url=RESULTS_URL, text=_body(60, list(range(50, 60))))

    with BaseGovClient(FAST) as client:
        batches = list(
            client.iter_pages(
                search_type=SEARCH_ENTITIES,
                query="q",
                sort=SORT_ENTITIES,
                start_page=1,
                rows_before=25,
            )
        )

    assert [len(b.items) for b in batches] == [25, 10]


def test_rows_in_parts_counts_exactly(tmp_path: Path) -> None:
    out = tmp_path / "entities.csv"
    assert rows_in_parts(out) == 0

    write_part(entities_frame([_entity(1), _entity(2)]), out, 0)
    write_part(entities_frame([_entity(3)]), out, 1)

    assert rows_in_parts(out) == 3
