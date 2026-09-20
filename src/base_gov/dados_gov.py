"""Checksum-verified download of the contracts workbooks from the dados.gov.pt API."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

DATASET_SLUG = "contratos-publicos-portal-base-impic-contratos-de-2012-a-2026"
API_BASE = "https://dados.gov.pt/api/1"
DEFAULT_YEARS = 3
FIRST_YEAR = 2012
TITLE_RE = re.compile(r"contratos(\d{4})\.xlsx")

CHUNK_SIZE = 1024 * 1024
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 1.0

logger = logging.getLogger("base_gov")

Manifest = dict[str, dict[str, Any]]
LocalState = Literal["match", "mismatch", "absent"]


class DadosGovError(Exception):
    """Base class for dados.gov.pt download errors."""


class ChecksumError(DadosGovError):
    """The downloaded body does not match the sha1 published by the API."""


@dataclass(frozen=True)
class Resource:
    year: int
    resource_id: str
    title: str
    url: str
    filesize: int
    sha1: str | None
    last_modified: datetime


@dataclass(frozen=True)
class DownloadResult:
    year: int
    path: Path
    status: Literal["downloaded", "skipped_unchanged", "checksum_mismatch", "failed"]
    bytes_written: int
    sha1: str | None


def make_client() -> httpx.Client:
    """Build the HTTP client used for every dados.gov.pt request."""
    return httpx.Client(
        timeout=httpx.Timeout(30.0, read=300.0),
        follow_redirects=True,
        headers={"User-Agent": "portal-base-data-analysis/0.1 (+github)"},
    )


def _with_retry[T](fn: Callable[[], T]) -> T:
    """Run ``fn`` up to MAX_ATTEMPTS times on transport errors and 5xx; never on 4xx."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()
        except httpx.TransportError as exc:
            error: Exception = exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            error = exc
        if attempt == MAX_ATTEMPTS:
            raise error
        logger.warning("attempt %d/%d failed: %s", attempt, MAX_ATTEMPTS, error)
        time.sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
    raise AssertionError("unreachable")  # pragma: no cover


def list_resources(
    slug: str = DATASET_SLUG, *, client: httpx.Client | None = None
) -> list[Resource]:
    """Return the xlsx resources of the dataset, newest year first."""
    owns_client = client is None
    http = client or make_client()
    try:

        def fetch() -> dict[str, Any]:
            response = http.get(f"{API_BASE}/datasets/{slug}/")
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            return payload

        payload = _with_retry(fetch)
    finally:
        if owns_client:
            http.close()

    resources: list[Resource] = []
    for raw in payload.get("resources", []):
        match = TITLE_RE.fullmatch(raw.get("title", ""))
        if raw.get("format") != "xlsx" or match is None:
            continue
        checksum = raw.get("checksum") or {}
        sha1 = None
        if checksum.get("type") == "sha1":
            sha1 = str(checksum.get("value") or "").strip().lower() or None
        if sha1 is None:
            logger.warning("resource %s has no sha1 checksum", raw["title"])
        resources.append(
            Resource(
                year=int(match.group(1)),
                resource_id=raw["id"],
                title=raw["title"],
                url=raw["latest"],
                filesize=int(raw.get("filesize") or 0),
                sha1=sha1,
                last_modified=datetime.fromisoformat(raw["last_modified"]),
            )
        )
    if not resources:
        raise ValueError(f"no xlsx contract resources found in dataset {slug!r} (schema drift?)")
    return sorted(resources, key=lambda r: r.year, reverse=True)


def select_years(
    resources: Iterable[Resource],
    *,
    years: int = DEFAULT_YEARS,
    today: date | None = None,
    only: Sequence[int] | None = None,
    from_year: int | None = None,
) -> list[Resource]:
    """Pick resources: ``only`` wins; else the last ``years`` years, floored at ``from_year``."""
    if only:
        wanted = set(only)
        return [r for r in resources if r.year in wanted]
    current = (today or date.today()).year
    cutoff = current - (years - 1)
    if from_year is not None:
        cutoff = max(cutoff, from_year)
    return [r for r in resources if r.year >= cutoff]


def target_path(resource: Resource, dest_dir: Path) -> Path:
    return dest_dir / f"contratos{resource.year}.xlsx"


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1(usedforsecurity=False)
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def verify_local(resource: Resource, path: Path) -> LocalState:
    """Compare the sha1 of an existing local file to the API. Read-only."""
    if not path.exists():
        return "absent"
    if resource.sha1 is None:
        # No API sha1: fall back to filesize (weaker, but not a false "mismatch" on our own file).
        return (
            "match"
            if resource.filesize and path.stat().st_size == resource.filesize
            else "mismatch"
        )
    return "match" if _sha1_file(path) == resource.sha1 else "mismatch"


def read_manifest(path: Path) -> Manifest:
    if not path.exists():
        return {}
    manifest: Manifest = json.loads(path.read_text(encoding="utf-8"))
    return manifest


def write_manifest(path: Path, entries: Manifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _manifest_entry(resource: Resource, final: Path, sha1: str | None) -> dict[str, Any]:
    return {
        "resource_id": resource.resource_id,
        "sha1": sha1,
        "filesize": resource.filesize,
        "last_modified": resource.last_modified.isoformat(),
        "downloaded_at": datetime.now(UTC).isoformat(),
        "file": final.as_posix(),
    }


def _stream_to_part(http: httpx.Client, resource: Resource, part: Path) -> tuple[int, str]:
    """Stream the body into ``part``; return (bytes written, sha1 hex)."""

    def attempt() -> tuple[int, str]:
        digest = hashlib.sha1(usedforsecurity=False)
        written = 0
        with http.stream("GET", resource.url) as response:
            response.raise_for_status()
            with part.open("wb") as fh:
                for chunk in response.iter_bytes(CHUNK_SIZE):
                    fh.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
        return written, digest.hexdigest()

    return _with_retry(attempt)


def download(
    resource: Resource,
    dest_dir: Path,
    *,
    manifest: Manifest,
    force: bool = False,
    client: httpx.Client | None = None,
) -> DownloadResult:
    """Download one workbook atomically, updating ``manifest`` in place."""
    final = target_path(resource, dest_dir)
    part = final.with_name(final.name + ".part")
    key = str(resource.year)

    if not force and final.exists():
        if resource.sha1 is None:
            logger.warning(
                "%s: no API sha1; re-downloading and verifying filesize only", resource.title
            )
        else:
            entry = manifest.get(key)
            manifest_ok = entry is not None and entry.get("sha1") == resource.sha1
            if manifest_ok or verify_local(resource, final) == "match":
                if not manifest_ok:
                    manifest[key] = _manifest_entry(resource, final, resource.sha1)
                return DownloadResult(resource.year, final, "skipped_unchanged", 0, resource.sha1)
            logger.info("%s: local sha1 differs from the API; replacing %s", resource.title, final)

    dest_dir.mkdir(parents=True, exist_ok=True)
    owns_client = client is None
    http = client or make_client()
    try:
        try:
            written, sha1 = _stream_to_part(http, resource, part)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    finally:
        if owns_client:
            http.close()

    if written == 0:
        part.unlink(missing_ok=True)
        raise ChecksumError(f"{resource.title}: empty response body")
    if resource.sha1 is not None and sha1 != resource.sha1:
        part.unlink(missing_ok=True)
        raise ChecksumError(f"{resource.title}: downloaded sha1 {sha1} != API sha1 {resource.sha1}")
    if resource.sha1 is None and resource.filesize and written != resource.filesize:
        part.unlink(missing_ok=True)
        raise ChecksumError(f"{resource.title}: got {written} bytes, API says {resource.filesize}")

    os.replace(part, final)
    manifest[key] = _manifest_entry(resource, final, sha1)
    return DownloadResult(resource.year, final, "downloaded", written, sha1)
