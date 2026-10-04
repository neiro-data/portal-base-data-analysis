# ruff: noqa: S603, S607, S608  # fixed docker argv lists; SQL is built from module constants
"""CPU, memory and time statistics for the Grafana dashboard panel queries.

Every panel query (`rawSql`) of the six dashboards is run through Grafana's `/api/ds/query`
under fixed scenarios (dashboard, time range, variables). Around each call the tool snapshots
`pg_stat_statements` (rows of `grafana_reader`) and the cgroup counters of the Postgres and Grafana
containers, then stores the deltas. Server metrics are read with `docker exec`, like
`bin/ingest-postgres.sh`; there is no Postgres driver dependency.

Run strictly serially on an otherwise idle stack: the cgroup numbers are container-wide.
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
import subprocess  # noqa: S404
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
import polars as pl

from base_gov.report import (
    DASHBOARD_DIR,
    REPORTABLE,
    TZ,
    ReportError,
    load_dashboard,
    parse_vars,
    resolve_time,
)

logger = logging.getLogger("base_gov")

PG_CONTAINER = "pbd-postgres"
GRAFANA_CONTAINER = "pbd-grafana"
DATASOURCE_USER = "grafana_reader"
DASHBOARDS: tuple[str, ...] = (
    "overview",
    "overview-municipios",
    "contracts-table",
    "contracts-table-municipios",
    "adjudicante-analysis",
    "adjudicatario-analysis",
)
HEAVY_ADJUDICANTE = "506362299"  # the heaviest buyer (52k contracts)
ALL_TOKENS = frozenset({"$__all", "All"})
EXPLAIN_TOP_N = 10

# default time ranges (the `from`; `to` is `now`) per dashboard
DEFAULT_RANGES: dict[str, tuple[str, ...]] = {
    "overview": ("now-90d", "now-2y", "now-10y"),
    "overview-municipios": ("now-90d", "now-2y", "now-10y"),
    "contracts-table": ("now-90d", "now-2y"),
    "contracts-table-municipios": ("now-90d", "now-2y"),
    "adjudicante-analysis": ("now-2y", "now-10y"),
    "adjudicatario-analysis": ("now-2y", "now-10y"),
}


class QueryStatsError(ReportError):
    """Raised for unrecoverable query-stats problems (the CLI maps it to exit code 1)."""


# --------------------------------------------------------------------------- dashboards


@dataclass(frozen=True)
class QueryPanel:
    dashboard: str
    panel_id: int
    title: str
    ref_id: str
    ds_uid: str
    fmt: str
    raw_sql: str


def load_any_dashboard(name: str, directory: Path = DASHBOARD_DIR) -> dict[str, Any]:
    """`report.load_dashboard` for the reportable ones; the contracts tables are read directly."""
    stem = name.removesuffix(".json")
    if stem in REPORTABLE or stem in REPORTABLE.values():
        return load_dashboard(stem, directory)
    if stem not in DASHBOARDS:
        raise QueryStatsError(f"unknown dashboard {name!r}; choose one of {', '.join(DASHBOARDS)}")
    data: dict[str, Any] = json.loads((directory / f"{stem}.json").read_text())
    return data


def _walk_panels(panels: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Top-level panels plus the ones nested in (collapsed) rows; rows themselves are skipped."""
    for p in panels:
        if p.get("type") == "row":
            yield from _walk_panels(list(p.get("panels", [])))
        else:
            yield p


def query_panels(dash: Mapping[str, Any], name: str = "") -> list[QueryPanel]:
    """Every panel target with a `rawSql` (including text-free panels in collapsed rows)."""
    out: list[QueryPanel] = []
    for p in _walk_panels(list(dash["panels"])):
        for t in p.get("targets", []):
            if "rawSql" not in t:
                continue
            ds = t.get("datasource") or p.get("datasource") or {}
            out.append(
                QueryPanel(
                    dashboard=name or str(dash.get("uid", "")),
                    panel_id=int(p["id"]),
                    title=str(p.get("title", "")),
                    ref_id=str(t.get("refId", "A")),
                    ds_uid=str(ds.get("uid", "")),
                    fmt=str(t.get("format", "table")),
                    raw_sql=str(t["rawSql"]),
                )
            )
    return out


# --------------------------------------------------------------------------- variables

_VAR_RE = re.compile(r"\$\{([^}]*)\}")
_VAR_BODY_RE = re.compile(r"^([A-Za-z_]\w*)(?::(\w+))?$")


def _variable_values(var: Mapping[str, Any], chosen: Mapping[str, list[str]]) -> list[str]:
    name = str(var["name"])
    if name in chosen:
        return list(chosen[name])
    value = (var.get("current") or {}).get("value", "")
    values = [str(v) for v in value] if isinstance(value, list) else [str(value)]
    if not values and var.get("type") != "textbox":
        raise QueryStatsError(f"variable {name!r} has no default value; pass --var {name}=...")
    return values or [""]


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def expand_vars(sql: str, dash: Mapping[str, Any], chosen: Mapping[str, list[str]]) -> str:
    """Expand the `${var[:fmt]}` template variables the way Grafana does.

    `/api/ds/query` only expands the `$__` macros, so the tool does the rest. Formats:
    plain (single value; a multi value variable with several values is an error), `:csv`,
    `:sqlstring`, `:text`. The All selection (`$__all`) expands to the variable's `allValue`
    verbatim (Grafana does not format a custom allValue). Unknown variables or formats raise.
    """
    variables = {str(v["name"]): v for v in dash.get("templating", {}).get("list", [])}

    def repl(m: re.Match[str]) -> str:
        body = _VAR_BODY_RE.match(m.group(1))
        if body is None:
            raise QueryStatsError(f"cannot expand {m.group(0)!r}")
        name, fmt = body.group(1), body.group(2)
        var = variables.get(name)
        if var is None:
            raise QueryStatsError(f"unknown variable in {m.group(0)!r}")
        if fmt not in (None, "csv", "sqlstring", "text"):
            raise QueryStatsError(f"unsupported format {fmt!r} in {m.group(0)!r}")
        values = _variable_values(var, chosen)
        if any(v in ALL_TOKENS for v in values):
            if fmt == "text":
                return "All"
            if "allValue" not in var:
                raise QueryStatsError(f"variable {name!r} selects All but defines no allValue")
            return str(var["allValue"])
        match fmt:
            case None:
                if len(values) != 1:
                    raise QueryStatsError(
                        f"multi-value variable {name!r} used plain in {m.group(0)!r}; "
                        "use :csv or :sqlstring"
                    )
                return values[0]
            case "csv":
                return ",".join(values)
            case "sqlstring":
                return ",".join(_quote(v) for v in values)
            case "text":
                return " + ".join(values)
            case _:
                raise QueryStatsError(f"unsupported format {fmt!r} in {m.group(0)!r}")

    return _VAR_RE.sub(repl, sql)


# --------------------------------------------------------------------------- scenarios


@dataclass(frozen=True)
class Scenario:
    dashboard: str
    name: str
    time_from: str
    time_to: str
    variables: Mapping[str, list[str]] = field(default_factory=dict)


def _label(time_from: str, tag: str) -> str:
    return f"{time_from}/{tag}"


def build_scenarios(
    dashboards: list[str],
    *,
    ranges: list[str] | None = None,
    var_items: list[str] | None = None,
    time_to: str = "now",
    heavy_supplier: str | None = None,
    small_buyer: str | None = None,
    contract_family: str | None = None,
    directory: Path = DASHBOARD_DIR,
) -> list[Scenario]:
    """Built-in scenarios, or (dashboard x --range x --var set) when the user narrows them."""
    out: list[Scenario] = []
    for name in dashboards:
        dash = load_any_dashboard(name, directory)
        if var_items:
            chosen = parse_vars(var_items, dash)
            tag = ",".join(f"{k}={'|'.join(v)}" for k, v in sorted(chosen.items()))
            for r in ranges or list(DEFAULT_RANGES[name]):
                out.append(Scenario(name, _label(r, tag), r, time_to, chosen))
            continue
        variants: list[tuple[str, dict[str, list[str]]]] = [("default", {})]
        if name == "adjudicante-analysis":
            variants = [(f"adjudicante={HEAVY_ADJUDICANTE}", {"adjudicante": [HEAVY_ADJUDICANTE]})]
            if small_buyer:
                variants.append((f"adjudicante={small_buyer}", {"adjudicante": [small_buyer]}))
        elif name == "adjudicatario-analysis":
            variants = []
            if heavy_supplier:
                variants.append(
                    (f"adjudicatario={heavy_supplier}", {"adjudicatario": [heavy_supplier]})
                )
        for r in ranges or list(DEFAULT_RANGES[name]):
            out.extend(Scenario(name, _label(r, tag), r, time_to, v) for tag, v in variants)
        if name == "overview" and not ranges:
            if contract_family:
                out.append(
                    Scenario(
                        name,
                        _label("now-2y", f"contract_type={contract_family}"),
                        "now-2y",
                        time_to,
                        {"contract_type": [contract_family]},
                    )
                )
            out.append(
                Scenario(
                    name,
                    _label("now-2y", "show_all_contract_types=on"),
                    "now-2y",
                    time_to,
                    {"show_all_contract_types": ["on"]},
                )
            )
    return out


# --------------------------------------------------------------------------- metric deltas


@dataclass(frozen=True)
class CgroupSnap:
    cpu_usec: int
    peak_bytes: int


@dataclass(frozen=True)
class PgDelta:
    calls: int
    matched: int  # how many pg_stat_statements rows had their calls go up
    exec_ms: float
    plan_ms: float
    shared_blks_hit: int
    shared_blks_read: int
    temp_blks_written: int
    temp_blks_read: int
    blk_read_ms: float
    rows: int


def cgroup_delta(before: CgroupSnap, after: CgroupSnap) -> tuple[float, float]:
    """(cpu_ms spent between the snapshots, peak memory in MB reported by the `after` one)."""
    return (after.cpu_usec - before.cpu_usec) / 1000.0, after.peak_bytes / (1024 * 1024)


def pgss_delta(
    before: Mapping[str, Mapping[str, Any]], after: Mapping[str, Mapping[str, Any]]
) -> PgDelta | None:
    """Delta of the pg_stat_statements row whose `calls` went up (serial runs make it unique).

    With several candidates (should not happen on an idle stack) the one with the largest
    execution time delta wins and `matched` tells how many there were.
    """

    def num(row: Mapping[str, Any] | None, key: str) -> float:
        return float(row[key]) if row and row.get(key) is not None else 0.0

    cands: list[tuple[float, PgDelta]] = []
    for qid, a in after.items():
        b = before.get(qid)
        d_calls = int(num(a, "calls") - num(b, "calls"))
        if d_calls <= 0:
            continue

        def d(key: str, a: Mapping[str, Any] = a, b: Mapping[str, Any] | None = b) -> float:
            return num(a, key) - num(b, key)

        cands.append(
            (
                d("total_exec_time"),
                PgDelta(
                    calls=d_calls,
                    matched=0,
                    exec_ms=d("total_exec_time"),
                    plan_ms=d("total_plan_time"),
                    shared_blks_hit=int(d("shared_blks_hit")),
                    shared_blks_read=int(d("shared_blks_read")),
                    temp_blks_written=int(d("temp_blks_written")),
                    temp_blks_read=int(d("temp_blks_read")),
                    blk_read_ms=d("shared_blk_read_time"),
                    rows=int(d("rows")),
                ),
            )
        )
    if not cands:
        return None
    best = max(cands, key=lambda c: c[0])[1]
    return PgDelta(**{**best.__dict__, "matched": len(cands)})


# --------------------------------------------------------------------------- plan metrics


def plan_metrics(explain_json: list[dict[str, Any]]) -> dict[str, Any]:
    """Memory and spill facts from an `EXPLAIN (ANALYZE, BUFFERS, MEMORY, FORMAT JSON)` result."""
    top = explain_json[0]
    sort_kb = 0
    hash_kb = 0
    spilled = False
    workers = 0

    def visit(node: Mapping[str, Any]) -> None:
        nonlocal sort_kb, hash_kb, spilled, workers
        sort_kb = max(sort_kb, int(node.get("Sort Space Used", 0) or 0))
        hash_kb = max(hash_kb, int(node.get("Peak Memory Usage", 0) or 0))
        spilled = spilled or str(node.get("Sort Method", "")).lower().startswith("external")
        spilled = spilled or int(node.get("Hash Batches", 1) or 1) > 1
        workers += int(node.get("Workers Launched", 0) or 0)
        for child in node.get("Plans", []):
            visit(child)

    visit(top["Plan"])
    planning = top.get("Planning", {})
    return {
        "sort_space_kb": sort_kb,
        "hash_peak_kb": hash_kb,
        "spilled": spilled,
        "workers_launched": workers,
        "plan_mem_kb": int(planning.get("Memory Used", 0) or 0),
        "plan_temp_written": int(top["Plan"].get("Temp Written Blocks", 0) or 0),
    }


# --------------------------------------------------------------------------- backend (docker)


class Backend(Protocol):
    """Server-side measurements. `DockerBackend` is the real one; tests inject a fake."""

    def preflight(self) -> None: ...
    def pgss_snapshot(self) -> dict[str, dict[str, Any]]: ...
    def reset_peak(self, container: str) -> None: ...
    def cgroup(self, container: str) -> CgroupSnap: ...
    def scalar(self, sql: str) -> str | None: ...
    def explain(self, sql: str) -> list[dict[str, Any]]: ...
    def cold_reset(self) -> None: ...
    def close(self) -> None: ...


PGSS_SQL = f"""
SELECT coalesce(json_agg(t), '[]'::json) FROM (
  SELECT s.queryid::text AS queryid, s.calls, s.total_exec_time, s.total_plan_time,
         s.shared_blks_hit, s.shared_blks_read, s.temp_blks_written, s.temp_blks_read,
         s.shared_blk_read_time, s.rows
  FROM pg_stat_statements s JOIN pg_roles r ON r.oid = s.userid
  WHERE r.rolname = '{DATASOURCE_USER}' AND s.toplevel AND s.queryid IS NOT NULL
) t;
"""
CGROUP_CMD = (
    "grep '^usage_usec' /sys/fs/cgroup/cpu.stat; "
    "cat /sys/fs/cgroup/memory.peak 2>/dev/null || echo -1; cat /sys/fs/cgroup/memory.current"
)
SAMPLER_MAX_TICKS = 72_000  # 50 ms ticks: the in-container loop self-terminates after 1 h


def parse_cgroup(stdout: str, container: str = "?") -> tuple[int, int, int]:
    """Parse `CGROUP_CMD` output into (cpu_usec, peak_bytes or -1 if unsupported, current)."""
    tokens = stdout.split()
    try:
        if len(tokens) != 4 or tokens[0] != "usage_usec":
            raise ValueError(tokens)
        return int(tokens[1]), int(tokens[2]), int(tokens[3])
    except ValueError as exc:
        raise QueryStatsError(f"unexpected cgroup output from {container}: {stdout!r}") from exc


_READ_ONLY_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)


class _MemorySampler:
    """Fallback when `memory.peak` cannot be reset: tail `memory.current` every 50 ms."""

    def __init__(self, container: str) -> None:
        self._container = container
        self._stop_file = f"/tmp/pbd-sampler-{uuid.uuid4().hex}.stop"  # noqa: S108
        loop = (
            "i=0; "
            f"while [ $i -lt {SAMPLER_MAX_TICKS} ] && [ ! -e {self._stop_file} ]; do "
            "cat /sys/fs/cgroup/memory.current; sleep 0.05; i=$((i+1)); done; "
            f"rm -f {self._stop_file}"
        )
        self._proc = subprocess.Popen(
            ["docker", "exec", container, "sh", "-c", loop],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._lock = threading.Lock()
        self._last = 0
        self._max = 0
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        for line in self._proc.stdout or ():
            if line.strip().isdigit():
                with self._lock:
                    self._last = int(line)
                    self._max = max(self._max, self._last)

    def reset(self) -> None:
        with self._lock:
            self._max = self._last

    def peak(self) -> int:
        with self._lock:
            return self._max

    def close(self) -> None:
        """Ask the in-container loop to stop (stop-file), then reap the local client."""
        try:
            subprocess.run(
                ["docker", "exec", self._container, "touch", self._stop_file],
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            logger.warning("cannot stop memory sampler in %s; it self-ends in 1 h", self._container)
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
        self._thread.join(timeout=2)


class DockerBackend:
    """Reads Postgres and cgroup counters through `docker exec` (superuser; no GRANT needed)."""

    def __init__(self, user: str | None = None, db: str | None = None) -> None:
        self.user = user or os.environ.get("POSTGRES_USER", "base_gov")
        self.db = db or os.environ.get("POSTGRES_DB", "base_gov")
        self._samplers: dict[str, _MemorySampler] = {}

    # -- plumbing
    def _run(self, argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)

    def _psql(self, sql: str) -> str:
        proc = self._run(
            [
                "docker",
                "exec",
                "-i",
                PG_CONTAINER,
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                self.user,
                "-d",
                self.db,
                "-Atq",
                "-f",
                "-",
            ],
            sql,
        )
        if proc.returncode != 0:
            raise QueryStatsError(f"psql failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout

    # -- Backend
    def preflight(self) -> None:
        try:
            self._psql("SELECT count(*) FROM pg_stat_statements;")
        except QueryStatsError as exc:
            raise QueryStatsError(
                f"pg_stat_statements is not usable ({exc}). Apply sql/010_query_stats.sql "
                "(make init-db) and restart postgres so compose preloads it: "
                "docker compose up -d postgres"
            ) from exc
        busy = self.scalar(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = "
            f"'{DATASOURCE_USER}' AND state <> 'idle' AND pid <> pg_backend_pid()"
        )
        if busy and int(busy) > 0:
            logger.warning(
                "%s active %s session(s): CPU/memory deltas will be polluted", busy, DATASOURCE_USER
            )

    def pgss_snapshot(self) -> dict[str, dict[str, Any]]:
        rows: list[dict[str, Any]] = json.loads(self._psql(PGSS_SQL).strip() or "[]")
        return {str(r["queryid"]): r for r in rows}

    def reset_peak(self, container: str) -> None:
        if container in self._samplers:
            self._samplers[container].reset()
            return
        proc = self._run(
            ["docker", "exec", container, "sh", "-c", "echo 0 > /sys/fs/cgroup/memory.peak"]
        )
        if proc.returncode == 0 and self._peak_was_reset(container):
            return
        logger.warning("memory.peak not resettable in %s; sampling memory.current", container)
        self._samplers[container] = _MemorySampler(container)

    def _peak_was_reset(self, container: str) -> bool:
        """Read memory.peak back in a fresh process: it must be close to memory.current.

        Kernel 6.12 resets per open fd, so a write in one exec is invisible to the next.
        """
        try:
            _, peak, current = self._cgroup_raw(container)
        except QueryStatsError:
            return False
        return 0 <= peak <= current + max(current // 50, 1 << 20)

    def _cgroup_raw(self, container: str) -> tuple[int, int, int]:
        proc = self._run(["docker", "exec", container, "sh", "-c", CGROUP_CMD])
        if proc.returncode != 0:
            raise QueryStatsError(f"cannot read cgroup of {container}: {proc.stderr.strip()}")
        return parse_cgroup(proc.stdout, container)

    def cgroup(self, container: str) -> CgroupSnap:
        cpu, peak, current = self._cgroup_raw(container)
        sampler = self._samplers.get(container)
        if sampler is None and peak < 0:
            sampler = self._samplers[container] = _MemorySampler(container)
        return CgroupSnap(cpu, max(sampler.peak(), current) if sampler else peak)

    def scalar(self, sql: str) -> str | None:
        out = self._psql(sql.rstrip().rstrip(";") + ";").strip()
        return out.splitlines()[0] if out else None

    def explain(self, sql: str) -> list[dict[str, Any]]:
        body = sql.strip().rstrip(";").strip()
        if not _READ_ONLY_RE.match(body) or ";" in body:
            raise QueryStatsError(
                "refusing to EXPLAIN ANALYZE a non-SELECT / multi-statement query"
            )
        out = self._psql(
            "BEGIN READ ONLY;\n"
            f"SET LOCAL ROLE {DATASOURCE_USER};\n"
            "SET LOCAL jit = off;\n"
            f"EXPLAIN (ANALYZE, BUFFERS, MEMORY, SETTINGS, FORMAT JSON) {body};\n"
            "ROLLBACK;\n"
        )
        plan: list[dict[str, Any]] = json.loads(out[out.index("[") :])
        return plan

    def cold_reset(self) -> None:
        """Restart Postgres and drop the VM page cache (the caller asks for confirmation)."""
        # the sampler's docker-exec dies with the restart: stop it now, re-create on next reset
        stale = self._samplers.pop(PG_CONTAINER, None)
        if stale is not None:
            stale.close()
        for argv in (
            ["docker", "compose", "restart", "postgres"],
            [
                "docker",
                "run",
                "--rm",
                "--privileged",
                "alpine",
                "sh",
                "-c",
                "sync; echo 3 > /proc/sys/vm/drop_caches",
            ],
        ):
            proc = self._run(argv)
            if proc.returncode != 0:
                raise QueryStatsError(f"{' '.join(argv[:3])} failed: {proc.stderr.strip()}")
        for _ in range(60):
            if (
                self._run(
                    ["docker", "exec", PG_CONTAINER, "pg_isready", "-U", self.user]
                ).returncode
                == 0
            ):
                return
            time.sleep(1.0)
        raise QueryStatsError("postgres did not become ready after the restart")

    def close(self) -> None:
        for s in self._samplers.values():
            s.close()


# --------------------------------------------------------------------------- collection

RUN_SCHEMA: dict[str, Any] = {
    "dashboard": pl.String,
    "panel_id": pl.Int64,
    "panel_title": pl.String,
    "ref_id": pl.String,
    "scenario": pl.String,
    "rep": pl.Int64,
    "warmup": pl.Boolean,
    "cold": pl.Boolean,
    "time_from": pl.String,
    "time_to": pl.String,
    "wall_ms": pl.Float64,
    "rows_returned": pl.Int64,
    "executed_sql": pl.String,
    "pg_calls": pl.Int64,
    "pg_matched": pl.Int64,
    "pg_exec_ms": pl.Float64,
    "pg_plan_ms": pl.Float64,
    "shared_blks_hit": pl.Int64,
    "shared_blks_read": pl.Int64,
    "temp_blks_written": pl.Int64,
    "temp_blks_read": pl.Int64,
    "blk_read_ms": pl.Float64,
    "pg_cpu_ms": pl.Float64,
    "pg_peak_mb": pl.Float64,
    "grafana_cpu_ms": pl.Float64,
    "grafana_peak_mb": pl.Float64,
    "sort_space_kb": pl.Int64,
    "hash_peak_kb": pl.Int64,
    "spilled": pl.Boolean,
    "workers_launched": pl.Int64,
    "plan_mem_kb": pl.Int64,
    "error": pl.String,
}


def ds_query(
    client: httpx.Client, panel: QueryPanel, sql: str, t0: datetime, t1: datetime
) -> tuple[str, int, str | None]:
    """POST one query to Grafana; returns (executedQueryString, row count, error)."""
    body = {
        "from": str(int(t0.timestamp() * 1000)),
        "to": str(int(t1.timestamp() * 1000)),
        "queries": [
            {
                "refId": panel.ref_id,
                "datasource": {"uid": panel.ds_uid},
                "rawSql": sql,
                "format": panel.fmt,
                "intervalMs": 60_000,
                "maxDataPoints": 1000,
            }
        ],
    }
    try:
        resp = client.post("/api/ds/query", json=body)
        if resp.status_code >= 400 and not resp.headers.get("content-type", "").startswith(
            "application/json"
        ):
            return "", 0, f"HTTP {resp.status_code}"
        result = resp.json().get("results", {}).get(panel.ref_id, {})
    except httpx.HTTPError as exc:
        return "", 0, f"{type(exc).__name__}: {exc}"[:500]
    except (ValueError, AttributeError) as exc:  # non-JSON body / unexpected shape
        return "", 0, f"invalid response: {type(exc).__name__}: {exc}"[:500]
    if result.get("error"):
        return "", 0, str(result["error"])
    if resp.status_code >= 400:
        return "", 0, f"HTTP {resp.status_code}"
    executed = ""
    rows = 0
    for frame in result.get("frames", []):
        meta = (frame.get("schema") or {}).get("meta") or {}
        executed = executed or str(meta.get("executedQueryString", ""))
        values = (frame.get("data") or {}).get("values") or []
        rows += len(values[0]) if values else 0
    return executed, rows, None


def measure(
    backend: Backend,
    client: httpx.Client,
    panel: QueryPanel,
    sql: str,
    t0: datetime,
    t1: datetime,
) -> dict[str, Any]:
    """One measured call: snapshot, POST, snapshot, deltas."""
    for c in (PG_CONTAINER, GRAFANA_CONTAINER):
        backend.reset_peak(c)
    pg_before = backend.pgss_snapshot()
    cg_before = {c: backend.cgroup(c) for c in (PG_CONTAINER, GRAFANA_CONTAINER)}
    started = time.perf_counter()
    executed, rows, error = ds_query(client, panel, sql, t0, t1)
    wall_ms = (time.perf_counter() - started) * 1000.0
    cg_after = {c: backend.cgroup(c) for c in (PG_CONTAINER, GRAFANA_CONTAINER)}
    pg_after = backend.pgss_snapshot()
    pg_cpu, pg_peak = cgroup_delta(cg_before[PG_CONTAINER], cg_after[PG_CONTAINER])
    gf_cpu, gf_peak = cgroup_delta(cg_before[GRAFANA_CONTAINER], cg_after[GRAFANA_CONTAINER])
    row: dict[str, Any] = {
        "wall_ms": wall_ms,
        "rows_returned": rows,
        "executed_sql": executed,
        "pg_cpu_ms": pg_cpu,
        "pg_peak_mb": pg_peak,
        "grafana_cpu_ms": gf_cpu,
        "grafana_peak_mb": gf_peak,
        "error": error,
    }
    delta = pgss_delta(pg_before, pg_after)
    if delta is not None:
        row.update(
            pg_calls=delta.calls,
            pg_matched=delta.matched,
            pg_exec_ms=delta.exec_ms,
            pg_plan_ms=delta.plan_ms,
            shared_blks_hit=delta.shared_blks_hit,
            shared_blks_read=delta.shared_blks_read,
            temp_blks_written=delta.temp_blks_written,
            temp_blks_read=delta.temp_blks_read,
            blk_read_ms=delta.blk_read_ms,
        )
    elif error is None:
        row["error"] = "no pg_stat_statements row matched"
    return row


def collect(
    backend: Backend,
    client: httpx.Client,
    scenarios: list[Scenario],
    *,
    repeat: int,
    cold: bool,
    now: datetime,
    directory: Path = DASHBOARD_DIR,
    sink: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Run every (panel, scenario, repetition) strictly serially.

    Rows are appended to `sink` as they complete so callers keep partial results on failure.
    """
    rows: list[dict[str, Any]] = sink if sink is not None else []
    for sc in scenarios:
        dash = load_any_dashboard(sc.dashboard, directory)
        t0 = resolve_time(sc.time_from, now)
        t1 = resolve_time(sc.time_to, now, end=True)
        if not t0 < t1:
            raise QueryStatsError(f"empty time range in scenario {sc.name!r}")
        for panel in query_panels(dash, sc.dashboard):
            sql = expand_vars(panel.raw_sql, dash, sc.variables)
            for rep in range(1, repeat + 1):
                is_cold = cold and rep == 1
                if is_cold:
                    backend.cold_reset()
                logger.info(
                    "%s %s #%s rep %d/%d", sc.dashboard, sc.name, panel.panel_id, rep, repeat
                )
                row = measure(backend, client, panel, sql, t0, t1)
                row.update(
                    dashboard=sc.dashboard,
                    panel_id=panel.panel_id,
                    panel_title=panel.title,
                    ref_id=panel.ref_id,
                    scenario=sc.name,
                    rep=rep,
                    warmup=rep == 1,
                    cold=is_cold,
                    time_from=sc.time_from,
                    time_to=sc.time_to,
                )
                rows.append(row)
    return rows


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._=-]+", "_", text).strip("_")[:80]


def explain_slowest(
    backend: Backend, rows: list[dict[str, Any]], plans_dir: Path, top_n: int = EXPLAIN_TOP_N
) -> None:
    """EXPLAIN ANALYZE the `top_n` slowest (panel, scenario) pairs; annotate their rows."""
    groups: dict[tuple[str, int, str, str], list[dict[str, Any]]] = {}
    for r in rows:
        key = (r["dashboard"], r["panel_id"], r["ref_id"], r["scenario"])
        groups.setdefault(key, []).append(r)

    def median_exec(g: list[dict[str, Any]]) -> float:
        vals = [r["pg_exec_ms"] for r in g if not r["warmup"] and r.get("pg_exec_ms") is not None]
        return statistics.median(vals) if vals else -1.0

    ranked = sorted(groups.items(), key=lambda kv: median_exec(kv[1]), reverse=True)[:top_n]
    for (dashboard, panel_id, _ref, scenario), g in ranked:
        sql = next((r["executed_sql"] for r in g if r.get("executed_sql")), "")
        if not sql or median_exec(g) < 0:
            continue
        try:
            plan = backend.explain(sql)
        except QueryStatsError as exc:
            logger.warning("EXPLAIN skipped for %s #%s: %s", dashboard, panel_id, exc)
            continue
        plans_dir.mkdir(parents=True, exist_ok=True)
        name = f"{dashboard}-{panel_id}-{_slug(scenario)}.json"
        (plans_dir / name).write_text(json.dumps(plan, indent=2))
        metrics = plan_metrics(plan)
        for r in g:
            r.update({k: v for k, v in metrics.items() if k in RUN_SCHEMA})


# --------------------------------------------------------------------------- output


def runs_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(
        [{k: r.get(k) for k in RUN_SCHEMA} for r in rows], schema=RUN_SCHEMA, strict=False
    )


def _fmt(v: Any, digits: int = 1) -> str:
    if v is None:
        return "-"
    return f"{v:.{digits}f}" if isinstance(v, float) else str(v)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def summarize(df: pl.DataFrame) -> pl.DataFrame:
    """Per (dashboard, panel, scenario) percentiles; warmup rows excluded; sorted by p95 wall."""
    keys = ["dashboard", "panel_id", "panel_title", "ref_id", "scenario"]
    return (
        df.filter(~pl.col("warmup"))
        .group_by(keys)
        .agg(
            pl.len().alias("n"),
            pl.col("wall_ms").median().alias("p50_wall_ms"),
            pl.col("wall_ms").quantile(0.95, "linear").alias("p95_wall_ms"),
            pl.col("pg_exec_ms").median().alias("p50_pg_exec_ms"),
            pl.col("pg_cpu_ms").median().alias("p50_cpu_ms"),
            pl.col("pg_peak_mb").max().alias("peak_mb"),
            pl.col("temp_blks_written").max().alias("temp_blks_written"),
            pl.col("spilled").any().alias("spilled"),
            pl.col("error").is_not_null().sum().alias("errors"),
        )
        .sort(["p95_wall_ms", "dashboard", "panel_id"], descending=[True, False, False])
    )


_HEADERS = [
    "panel",
    "scenario",
    "p50 wall ms",
    "p95 wall ms",
    "p50 pg ms",
    "cpu ms",
    "peak MB",
    "spill",
]


def _summary_rows(s: pl.DataFrame, with_dashboard: bool) -> list[list[str]]:
    out = []
    for r in s.iter_rows(named=True):
        spill = (r["temp_blks_written"] or 0) > 0 or bool(r["spilled"])
        panel = f"#{r['panel_id']} {r['panel_title']}"
        out.append(
            [
                f"{r['dashboard']} {panel}" if with_dashboard else panel,
                r["scenario"],
                _fmt(r["p50_wall_ms"]),
                _fmt(r["p95_wall_ms"]),
                _fmt(r["p50_pg_exec_ms"]),
                _fmt(r["p50_cpu_ms"]),
                _fmt(r["peak_mb"]),
                "yes" if spill else "no",
            ]
        )
    return out


def build_summary(df: pl.DataFrame, *, title: str = "Query statistics") -> str:
    """Markdown summary: top 10 overall, then one table per dashboard, all sorted by p95."""
    s = summarize(df)
    parts = [f"# {title}", "", "Warmup repetitions are excluded from the percentiles.", ""]
    parts += ["## Top 10 overall (by p95 wall time)", ""]
    parts += [_table(["panel", *_HEADERS[1:]], _summary_rows(s.head(10), True)), ""]
    for dash in df["dashboard"].unique(maintain_order=True).to_list():
        parts += [
            f"## {dash}",
            "",
            _table(_HEADERS, _summary_rows(s.filter(pl.col("dashboard") == dash), False)),
            "",
        ]
    cold = df.filter(pl.col("cold"))
    if cold.height:
        parts += ["## Cold-cache first run (top 10 by wall time)", ""]
        top = cold.sort("wall_ms", descending=True).head(10)
        parts += [
            _table(
                ["panel", "scenario", "wall ms", "pg ms", "blks read"],
                [
                    [
                        f"{r['dashboard']} #{r['panel_id']} {r['panel_title']}",
                        r["scenario"],
                        _fmt(r["wall_ms"]),
                        _fmt(r["pg_exec_ms"]),
                        _fmt(r["shared_blks_read"]),
                    ]
                    for r in top.iter_rows(named=True)
                ],
            ),
            "",
        ]
    errors = df.filter(pl.col("error").is_not_null())
    if errors.height:
        parts += [f"## Errors ({errors.height} rows)", ""]
        seen: set[tuple[str, int, str]] = set()
        for r in errors.iter_rows(named=True):
            key = (r["dashboard"], r["panel_id"], str(r["error"]))
            if key not in seen:
                seen.add(key)
                parts.append(f"- {r['dashboard']} #{r['panel_id']} ({r['scenario']}): {r['error']}")
        parts.append("")
    return "\n".join(parts)


def run_query_stats(
    *,
    dashboards: list[str] | None = None,
    ranges: list[str] | None = None,
    var_items: list[str] | None = None,
    repeat: int = 5,
    cold: bool = False,
    out_dir: Path | None = None,
    grafana_url: str = "http://127.0.0.1:3001",
    timeout_s: int = 120,
    explain_top: int = EXPLAIN_TOP_N,
    backend: Backend | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    directory: Path = DASHBOARD_DIR,
) -> Path:
    """Run the scenarios and write `runs.parquet`, `summary.md` and `plans/` under `out_dir`."""
    now = now or datetime.now(TZ)
    out = out_dir or Path("reports") / "query-stats" / f"{now:%Y%m%d-%H%M}"
    names = dashboards or list(DASHBOARDS)
    rows: list[dict[str, Any]] = []
    own_backend = backend is None
    be: Backend = backend or DockerBackend()
    own_client = client is None
    http = client or httpx.Client(base_url=grafana_url, timeout=timeout_s)
    try:
        if own_client:
            try:
                http.get("/api/health").raise_for_status()
            except httpx.HTTPError as exc:
                raise QueryStatsError(
                    f"Grafana at {grafana_url} not ready ({exc}); "
                    "start it with: docker compose --profile dashboard up -d"
                ) from exc
        be.preflight()
        scenarios = build_scenarios(
            names,
            ranges=ranges,
            var_items=var_items,
            heavy_supplier=be.scalar(
                "SELECT entity_id FROM procurement.contract_adjudicatarios "
                "GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
            )
            if "adjudicatario-analysis" in names and not var_items
            else None,
            small_buyer=be.scalar(
                "SELECT adjudicante_id FROM procurement.v_contracts_material GROUP BY 1 "
                "HAVING count(*) BETWEEN 5 AND 50 ORDER BY 1 LIMIT 1"
            )
            if "adjudicante-analysis" in names and not var_items
            else None,
            contract_family=be.scalar(
                "SELECT value FROM procurement.v_contract_type_options "
                "WHERE mode = 'off' ORDER BY sort LIMIT 1"
            )
            if "overview" in names and not var_items
            else None,
            directory=directory,
        )
        logger.info("%d scenarios x %d reps", len(scenarios), repeat)
        collect(
            be, http, scenarios, repeat=repeat, cold=cold, now=now, directory=directory, sink=rows
        )
        explain_slowest(be, rows, out / "plans", explain_top)
    finally:
        if own_client:
            http.close()
        if own_backend:
            be.close()
        if rows:  # also on failure/Ctrl-C: keep partial measurements
            out.mkdir(parents=True, exist_ok=True)
            df = runs_frame(rows)
            df.write_parquet(out / "runs.parquet")
            (out / "summary.md").write_text(build_summary(df))
            logger.info("wrote %s (%d rows)", out, df.height)
    return out
