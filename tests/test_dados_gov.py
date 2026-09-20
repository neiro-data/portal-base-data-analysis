from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from base_gov import dados_gov
from base_gov.cli import app
from base_gov.dados_gov import (
    ChecksumError,
    Resource,
    download,
    list_resources,
    read_manifest,
    select_years,
    verify_local,
    write_manifest,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dados_gov_dataset.json"
BODY = b"x" * 3_000_000 + b"tail"
BODY_SHA1 = hashlib.sha1(BODY, usedforsecurity=False).hexdigest()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("base_gov.dados_gov.time.sleep", lambda _s: None)


def _resource(year: int = 2024, sha1: str | None = BODY_SHA1) -> Resource:
    return Resource(
        year=year,
        resource_id="rid",
        title=f"contratos{year}.xlsx",
        url=f"https://dados.gov.pt/api/1/datasets/r/rid{year}",
        filesize=len(BODY),
        sha1=sha1,
        last_modified=datetime(2026, 9, 13, tzinfo=UTC),
    )


class _Files:
    """Mock transport serving BODY and recording every request."""

    def __init__(self, body: bytes = BODY) -> None:
        self.body = body
        self.requests: list[httpx.Request] = []

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(200, content=self.body)

        return httpx.Client(transport=httpx.MockTransport(handler))


def _api_client(payload: object) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )


def test_list_resources_parses_and_filters_zip() -> None:
    payload = json.loads(FIXTURE.read_text())
    assert len(payload["resources"]) == 4
    resources = list_resources(client=_api_client(payload))
    assert [r.year for r in resources] == [2025, 2024, 2012]
    r2024 = resources[1]
    assert r2024.sha1 == "1" * 40
    assert r2024.filesize == 57737358
    assert r2024.url.endswith("/datasets/r/344d19b2-7dcf-4716-aa2d-4cec42a5c834")
    assert r2024.last_modified == datetime(2026, 9, 13, 11, 41, 56, 367000, tzinfo=UTC)


@pytest.mark.parametrize(
    "payload",
    [
        {"resources": []},
        {"resources": [{"title": "other.csv", "format": "csv"}]},
        {"resources": [{"title": "contratos2024.zip", "format": "zip"}]},
    ],
)
def test_list_resources_raises_on_schema_drift(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        list_resources(client=_api_client(payload))


def test_select_years_cap() -> None:
    resources = [_resource(y) for y in (2026, 2025, 2024, 2023, 2014)]
    picked = select_years(resources, years=3, today=date(2026, 9, 20))
    assert [r.year for r in picked] == [2026, 2025, 2024]


def test_select_years_only_ignores_cap() -> None:
    resources = [_resource(y) for y in (2026, 2025, 2014)]
    picked = select_years(resources, years=1, today=date(2026, 9, 20), only=[2014])
    assert [r.year for r in picked] == [2014]


def test_select_years_from_year() -> None:
    resources = [_resource(y) for y in (2026, 2013, 2012)]
    picked = select_years(resources, years=99, today=date(2026, 9, 20), from_year=2013)
    assert [r.year for r in picked] == [2026, 2013]


def test_download_skips_when_manifest_matches(tmp_path: Path) -> None:
    res = _resource()
    (tmp_path / "contratos2024.xlsx").write_bytes(BODY)
    files = _Files()
    manifest = {"2024": {"sha1": BODY_SHA1}}
    result = download(res, tmp_path, manifest=manifest, client=files.client())
    assert result.status == "skipped_unchanged"
    assert result.bytes_written == 0
    assert files.requests == []


def test_download_writes_file_and_manifest(tmp_path: Path) -> None:
    files = _Files()
    manifest: dados_gov.Manifest = {}
    result = download(_resource(), tmp_path, manifest=manifest, client=files.client())
    assert result.status == "downloaded"
    assert result.sha1 == BODY_SHA1
    assert (tmp_path / "contratos2024.xlsx").read_bytes() == BODY
    assert not (tmp_path / "contratos2024.xlsx.part").exists()
    assert manifest["2024"]["sha1"] == BODY_SHA1
    assert manifest["2024"]["resource_id"] == "rid"


def test_download_body_checksum_mismatch_leaves_nothing(tmp_path: Path) -> None:
    files = _Files(body=b"corrupted")
    with pytest.raises(ChecksumError):
        download(_resource(), tmp_path, manifest={}, client=files.client())
    assert list(tmp_path.iterdir()) == []


def test_download_replaces_differing_local_file(tmp_path: Path) -> None:
    local = tmp_path / "contratos2024.xlsx"
    local.write_bytes(b"human placed")
    files = _Files()
    manifest: dados_gov.Manifest = {}
    result = download(_resource(), tmp_path, manifest=manifest, client=files.client())
    assert result.status == "downloaded"
    assert local.read_bytes() == BODY
    assert len(files.requests) == 1
    assert manifest["2024"]["sha1"] == BODY_SHA1
    assert [p.name for p in tmp_path.iterdir()] == ["contratos2024.xlsx"]


def test_failed_replacement_leaves_old_file_intact(tmp_path: Path) -> None:
    local = tmp_path / "contratos2024.xlsx"
    local.write_bytes(b"human placed")
    manifest: dados_gov.Manifest = {}
    with pytest.raises(ChecksumError):
        download(_resource(), tmp_path, manifest=manifest, client=_Files(b"bad").client())
    assert local.read_bytes() == b"human placed"
    assert manifest == {}
    assert [p.name for p in tmp_path.iterdir()] == ["contratos2024.xlsx"]


def test_force_overrides_stale_local_file(tmp_path: Path) -> None:
    local = tmp_path / "contratos2024.xlsx"
    local.write_bytes(b"human placed")
    result = download(_resource(), tmp_path, manifest={}, force=True, client=_Files().client())
    assert result.status == "downloaded"
    assert local.read_bytes() == BODY


def test_download_is_atomic(tmp_path: Path) -> None:
    final = tmp_path / "contratos2024.xlsx"
    part = tmp_path / "contratos2024.xlsx.part"
    seen: list[tuple[bool, bool]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        def gen() -> object:
            yield BODY[:1000]
            seen.append((final.exists(), part.exists()))
            yield BODY[1000:]

        return httpx.Response(200, content=gen())  # type: ignore[arg-type]

    client = httpx.Client(transport=httpx.MockTransport(handler))
    download(_resource(), tmp_path, manifest={}, client=client)
    assert seen == [(False, True)]
    assert final.exists()
    assert not part.exists()


def test_download_retries_5xx_but_not_4xx(tmp_path: Path) -> None:
    calls: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, content=BODY)

    client = httpx.Client(transport=httpx.MockTransport(flaky))
    assert download(_resource(), tmp_path, manifest={}, client=client).status == "downloaded"
    assert len(calls) == 3

    calls.clear()

    def not_found(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404)

    client404 = httpx.Client(transport=httpx.MockTransport(not_found))
    with pytest.raises(httpx.HTTPStatusError):
        download(_resource(2023), tmp_path, manifest={}, client=client404)
    assert len(calls) == 1
    assert not (tmp_path / "contratos2023.xlsx.part").exists()


def test_verify_local_states_and_read_only(tmp_path: Path) -> None:
    res = _resource()
    path = tmp_path / "contratos2024.xlsx"
    assert verify_local(res, path) == "absent"
    assert list(tmp_path.iterdir()) == []
    path.write_bytes(BODY)
    assert verify_local(res, path) == "match"
    path.write_bytes(b"other")
    assert verify_local(res, path) == "mismatch"
    assert path.read_bytes() == b"other"
    assert [p.name for p in tmp_path.iterdir()] == ["contratos2024.xlsx"]


def test_manifest_round_trip(tmp_path: Path) -> None:
    entries = {
        "2024": {
            "resource_id": "rid",
            "sha1": BODY_SHA1,
            "filesize": 5,
            "last_modified": "2026-09-13T11:41:56.367000+00:00",
            "downloaded_at": "2026-09-20T10:00:00+00:00",
            "file": "data/source/contratos2024.xlsx",
        }
    }
    path = tmp_path / "sub" / "manifest.json"
    assert read_manifest(path) == {}
    write_manifest(path, entries)
    assert read_manifest(path) == entries


def test_stale_manifest_entry_does_not_block_replacement(tmp_path: Path) -> None:
    local = tmp_path / "contratos2025.xlsx"
    local.write_bytes(b"human placed")
    manifest: dados_gov.Manifest = {"2025": {"sha1": "stale-entry"}}
    files = _Files()
    result = download(_resource(2025), tmp_path, manifest=manifest, client=files.client())
    assert result.status == "downloaded"
    assert local.read_bytes() == BODY
    assert manifest["2025"]["sha1"] == BODY_SHA1
    assert len(files.requests) == 1
    assert [p.name for p in tmp_path.iterdir()] == ["contratos2025.xlsx"]


def test_failed_force_download_leaves_no_part(tmp_path: Path) -> None:
    local = tmp_path / "contratos2024.xlsx"
    local.write_bytes(b"old")
    with pytest.raises(ChecksumError):
        download(_resource(), tmp_path, manifest={}, force=True, client=_Files(b"bad").client())
    assert local.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["contratos2024.xlsx"]


def test_verify_local_without_api_sha1_uses_filesize(tmp_path: Path) -> None:
    path = tmp_path / "contratos2024.xlsx"
    path.write_bytes(BODY)
    assert verify_local(_resource(sha1=None), path) == "match"
    path.write_bytes(b"other")
    assert verify_local(_resource(sha1=None), path) == "mismatch"


def test_download_without_sha1_then_rerun(tmp_path: Path) -> None:
    res = _resource(sha1=None)
    manifest: dados_gov.Manifest = {}
    files = _Files()
    first = download(res, tmp_path, manifest=manifest, client=files.client())
    assert first.status == "downloaded"
    second = download(res, tmp_path, manifest=manifest, client=files.client())
    assert second.status == "downloaded"
    assert (tmp_path / "contratos2024.xlsx").read_bytes() == BODY
    assert len(files.requests) == 2


def test_download_without_sha1_replaces_local_file(tmp_path: Path) -> None:
    local = tmp_path / "contratos2024.xlsx"
    local.write_bytes(b"human placed")
    result = download(_resource(sha1=None), tmp_path, manifest={}, client=_Files().client())
    assert result.status == "downloaded"
    assert local.read_bytes() == BODY


def test_download_without_sha1_bad_size_keeps_old_file(tmp_path: Path) -> None:
    local = tmp_path / "contratos2024.xlsx"
    local.write_bytes(b"human placed")
    with pytest.raises(ChecksumError):
        download(_resource(sha1=None), tmp_path, manifest={}, client=_Files(b"short").client())
    assert local.read_bytes() == b"human placed"
    assert [p.name for p in tmp_path.iterdir()] == ["contratos2024.xlsx"]


def test_download_without_sha1_wrong_filesize(tmp_path: Path) -> None:
    with pytest.raises(ChecksumError):
        download(_resource(sha1=None), tmp_path, manifest={}, client=_Files(b"short").client())
    assert list(tmp_path.iterdir()) == []


def test_zero_byte_body_is_refused(tmp_path: Path) -> None:
    local = tmp_path / "contratos2024.xlsx"
    local.write_bytes(b"old")
    res = Resource(2024, "rid", "contratos2024.xlsx", "https://x/r/rid", 0, None, datetime.now(UTC))
    with pytest.raises(ChecksumError):
        download(res, tmp_path, manifest={}, force=True, client=_Files(b"").client())
    assert local.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["contratos2024.xlsx"]


def test_manifest_adopted_when_local_sha1_matches(tmp_path: Path) -> None:
    (tmp_path / "contratos2024.xlsx").write_bytes(BODY)
    manifest: dados_gov.Manifest = {}
    files = _Files()
    result = download(_resource(), tmp_path, manifest=manifest, client=files.client())
    assert result.status == "skipped_unchanged"
    assert manifest["2024"]["sha1"] == BODY_SHA1
    assert files.requests == []


def test_checksum_value_is_normalised_and_title_fullmatch() -> None:
    payload = json.loads(FIXTURE.read_text())
    for res in payload["resources"]:
        if res["title"] == "contratos2024.xlsx":
            res["checksum"] = {"type": "sha1", "value": " ABCDEF" + "0" * 34 + "\n"}
        if res["title"] == "contratos2012.xlsx":
            res["checksum"] = {"type": "sha1", "value": ""}
    payload["resources"].append({"title": "contratos2020.xlsx\n", "format": "xlsx"})
    by_year = {r.year: r for r in list_resources(client=_api_client(payload))}
    assert by_year[2024].sha1 == "abcdef" + "0" * 34
    assert by_year[2012].sha1 is None
    assert 2020 not in by_year


def _verify_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local: bytes) -> int:
    payload = json.loads(FIXTURE.read_text())
    for res in payload["resources"]:
        if res["title"] == "contratos2024.xlsx":
            res["checksum"] = {"type": "sha1", "value": BODY_SHA1}
    monkeypatch.setattr(
        dados_gov,
        "make_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
        ),
    )
    dest = tmp_path / "source"
    dest.mkdir()
    (dest / "contratos2024.xlsx").write_bytes(local)
    args = ["download-contracts", "--verify-only", "--year", "2024", "--dest", str(dest)]
    return CliRunner().invoke(app, args).exit_code


def test_cli_verify_only_mismatch_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _verify_only(tmp_path, monkeypatch, b"other") == 1


def test_cli_verify_only_match_exits_0(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _verify_only(tmp_path, monkeypatch, BODY) == 0


def _cli_transport(monkeypatch: pytest.MonkeyPatch, requests: list[httpx.Request]) -> None:
    payload = json.loads(FIXTURE.read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/datasets/r/" in request.url.path:
            return httpx.Response(200, content=BODY)
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        dados_gov, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr("base_gov.cli.time.sleep", lambda _s: None)


def test_cli_dry_run_makes_no_file_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[httpx.Request] = []
    _cli_transport(monkeypatch, requests)
    dest = tmp_path / "source"
    result = CliRunner().invoke(
        app,
        [
            "download-contracts",
            "--dry-run",
            "--year",
            "2024",
            "--year",
            "2025",
            "--dest",
            str(dest),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "2024" in result.output and "2025" in result.output
    assert "2012" not in result.output
    assert "would download" in result.output
    assert len(requests) == 1
    assert "/datasets/r/" not in requests[0].url.path
    assert not dest.exists()
    assert not (tmp_path / "manifest.json").exists()


def test_cli_download_then_rerun_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.loads(FIXTURE.read_text())
    for res in payload["resources"]:
        if res["title"] == "contratos2024.xlsx":
            res["checksum"] = {"type": "sha1", "value": BODY_SHA1}
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "/datasets/r/" in request.url.path:
            return httpx.Response(200, content=BODY)
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        dados_gov, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    dest = tmp_path / "source"
    args = ["download-contracts", "--year", "2024", "--dest", str(dest)]
    first = CliRunner().invoke(app, args)
    assert first.exit_code == 0, first.output
    assert "downloaded" in first.output
    assert (dest / "contratos2024.xlsx").read_bytes() == BODY
    assert "2024" in read_manifest(tmp_path / "manifest.json")
    mtime = (dest / "contratos2024.xlsx").stat().st_mtime_ns
    requests.clear()
    second = CliRunner().invoke(app, args)
    assert second.exit_code == 0, second.output
    assert "skipped_unchanged" in second.output
    assert [r for r in requests if "/datasets/r/" in r.url.path] == []
    assert (dest / "contratos2024.xlsx").stat().st_mtime_ns == mtime


def test_cli_replaces_differing_local_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.loads(FIXTURE.read_text())
    for res in payload["resources"]:
        if res["title"] == "contratos2025.xlsx":
            res["checksum"] = {"type": "sha1", "value": BODY_SHA1}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/datasets/r/" in request.url.path:
            return httpx.Response(200, content=BODY)
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        dados_gov, "make_client", lambda: httpx.Client(transport=httpx.MockTransport(handler))
    )
    dest = tmp_path / "source"
    dest.mkdir()
    local = dest / "contratos2025.xlsx"
    local.write_bytes(b"human placed")
    dry = CliRunner().invoke(
        app, ["download-contracts", "--dry-run", "--year", "2025", "--dest", str(dest)]
    )
    assert "would replace" in dry.output
    assert local.read_bytes() == b"human placed"
    result = CliRunner().invoke(app, ["download-contracts", "--year", "2025", "--dest", str(dest)])
    assert result.exit_code == 0, result.output
    assert local.read_bytes() == BODY
    assert read_manifest(tmp_path / "manifest.json")["2025"]["sha1"] == BODY_SHA1
    assert [p.name for p in dest.iterdir()] == ["contratos2025.xlsx"]


def test_backfill_script_refuses_without_env_var() -> None:
    script = Path(__file__).parent.parent / "bin" / "backfill-contracts.sh"
    env = {k: v for k, v in os.environ.items() if k != "BACKFILL_CONFIRM"}
    proc = subprocess.run(  # noqa: S603
        ["/bin/bash", str(script)], env=env, capture_output=True, text=True, check=False, timeout=30
    )
    assert proc.returncode == 2
    assert "refusing" in proc.stderr
