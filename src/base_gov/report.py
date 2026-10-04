"""Render Grafana dashboards to A4 PDFs (renderer PNG per panel, laid out with reportlab).

Pure functions (selection, variables, time, URLs, layout) are kept apart from the thin I/O
(`fetch_png`, `generate_report`), so everything but the HTTP calls is testable offline.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

logger = logging.getLogger("base_gov")

DASHBOARD_DIR = Path(__file__).resolve().parents[2] / "docker" / "grafana" / "dashboards"
REPORTABLE: dict[str, str] = {
    "overview": "base-overview",
    "overview-municipios": "base-overview-municipios",
    "adjudicante-analysis": "base-adjudicante",
    "adjudicatario-analysis": "base-adjudicatario",
}
EXCLUDED_ROWS = frozenset({"Contracts"})
SUMMARY_TITLE = "Summary"
NIF_VARS = ("adjudicante", "adjudicatario")
TZ = ZoneInfo("Europe/Lisbon")

GRID_COLS = 24
ROW_PX = 38  # 30 px grid row unit + 8 px margin
W_PX = 1600  # reference pixel width of a full-width (w=24) panel
PAGE_W_MM, PAGE_H_MM = A4[0] / mm, A4[1] / mm
MARGIN_MM = 12.0
BAND_MM = 8.0  # header / footer strip height inside the margins
CW_MM = PAGE_W_MM - 2 * MARGIN_MM
CH_MM = PAGE_H_MM - 2 * MARGIN_MM - 2 * BAND_MM
K = CW_MM / W_PX  # mm per px
GUTTER_MM = 2.0
BAND_GAP_MM = 2.0
HEADING_MM = 9.0
KEEP_WITH_NEXT_MM = 40.0


class ReportError(Exception):
    """Base error for report generation (the CLI maps it to exit code 1)."""


class RenderError(ReportError):
    """The image renderer did not return a PNG for a panel."""


@dataclass(frozen=True)
class ReportPanel:
    id: int
    title: str
    type: str
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class Section:
    title: str
    panels: tuple[ReportPanel, ...]


@dataclass(frozen=True)
class Band:
    """Panels whose vertical spans overlap; laid out with their exact grid geometry."""

    y0: int
    panels: tuple[ReportPanel, ...]

    @property
    def height_px(self) -> int:
        return max((p.y - self.y0) * ROW_PX + panel_px_h(p) for p in self.panels)


@dataclass(frozen=True)
class ReportMeta:
    title: str
    filters: tuple[tuple[str, str], ...]
    nifs: tuple[str, ...]
    time_text: str
    generated_at: datetime
    grafana_url: str


# --------------------------------------------------------------------------- selection


def load_dashboard(name_or_uid: str, directory: Path = DASHBOARD_DIR) -> dict[str, Any]:
    """Load one of the reportable dashboards by file stem or uid."""
    stem = name_or_uid.removesuffix(".json")
    if stem not in REPORTABLE:
        by_uid = {uid: s for s, uid in REPORTABLE.items()}
        if name_or_uid not in by_uid:
            raise ReportError(
                f"unknown dashboard {name_or_uid!r}; choose one of "
                f"{', '.join(sorted(REPORTABLE))} (or its uid)"
            )
        stem = by_uid[name_or_uid]
    data: dict[str, Any] = json.loads((directory / f"{stem}.json").read_text())
    return data


def _to_panel(p: dict[str, Any]) -> ReportPanel:
    g = p["gridPos"]
    return ReportPanel(
        id=int(p["id"]),
        title=str(p.get("title", "")),
        type=str(p["type"]),
        x=int(g["x"]),
        y=int(g["y"]),
        w=int(g["w"]),
        h=int(g["h"]),
    )


def _sorted_panels(raw: list[dict[str, Any]]) -> tuple[ReportPanel, ...]:
    panels = [_to_panel(p) for p in raw if p["type"] not in ("row", "text")]
    return tuple(sorted(panels, key=lambda p: (p.y, p.x)))


def select_sections(dash: Mapping[str, Any]) -> list[Section]:
    """Flatten rows (incl. collapsed ones) into ordered sections of renderable panels."""
    top: list[dict[str, Any]] = list(dash["panels"])
    top.sort(key=lambda p: (p["gridPos"]["y"], p["gridPos"]["x"]))
    sections: list[Section] = []
    current_title = SUMMARY_TITLE
    current: list[dict[str, Any]] = []

    def flush() -> None:
        if current_title not in EXCLUDED_ROWS:
            panels = _sorted_panels(current)
            if panels:
                sections.append(Section(current_title, panels))

    for p in top:
        if p["type"] == "row":
            flush()
            current_title = str(p.get("title", ""))
            current = list(p.get("panels", []))  # non-empty only for collapsed rows
        else:
            current.append(p)
    flush()
    return sections


def layout_bands(section: Section) -> list[Band]:
    """Greedily merge panels whose vertical spans overlap into bands."""
    bands: list[Band] = []
    group: list[ReportPanel] = []
    span_end = 0
    for p in sorted(section.panels, key=lambda q: (q.y, q.x)):
        if group and p.y < span_end:
            group.append(p)
            span_end = max(span_end, p.y + p.h)
        else:
            if group:
                bands.append(Band(group[0].y, tuple(group)))
            group = [p]
            span_end = p.y + p.h
    if group:
        bands.append(Band(group[0].y, tuple(group)))
    return bands


# --------------------------------------------------------------------------- variables


def parse_vars(items: list[str], dash: Mapping[str, Any]) -> dict[str, list[str]]:
    """Parse repeated `name=value` options; validate against the dashboard's variables."""
    known = {v["name"]: v for v in dash.get("templating", {}).get("list", [])}
    out: dict[str, list[str]] = {}
    for item in items:
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise ReportError(f"invalid --var {item!r}; expected name=value")
        if name not in known:
            raise ReportError(f"unknown variable {name!r}; known: {', '.join(sorted(known))}")
        out.setdefault(name, []).append(value)
    for name, values in out.items():
        if len(values) > 1 and not known[name].get("multi"):
            raise ReportError(f"variable {name!r} is not multi-value; got {len(values)} values")
    return out


def _default_text(var: Mapping[str, Any]) -> str:
    cur = var.get("current") or {}
    text = cur.get("text", "")
    if isinstance(text, list):
        text = ", ".join(str(t) for t in text)
    return str(text) or "(none)"


def describe_filters(
    dash: Mapping[str, Any], chosen: Mapping[str, list[str]]
) -> tuple[tuple[str, str], ...]:
    """Each query variable with its chosen value, or the saved default marked '(default)'."""
    rows: list[tuple[str, str]] = []
    for v in dash.get("templating", {}).get("list", []):
        if v.get("hide") == 2:
            continue
        name = v["name"]
        rows.append(
            (name, ", ".join(chosen[name]) if name in chosen else f"{_default_text(v)} (default)")
        )
    return tuple(rows)


# --------------------------------------------------------------------------- time

_REL_RE = re.compile(
    r"^now(?:(?P<sign>[+-])(?P<n>\d+)(?P<unit>[mhdwMy]))?(?:/(?P<round>[dhmwMy]))?$"
)


def _add_months(dt: datetime, months: int) -> datetime:
    total = dt.year * 12 + (dt.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    for day in (dt.day, 30, 29, 28):
        try:
            return dt.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    raise AssertionError("unreachable")  # pragma: no cover


def resolve_time(expr: str, now: datetime, *, end: bool = False) -> datetime:
    """Resolve a Grafana-style time expression (or ISO date / epoch ms) to an aware datetime.

    `end=True` snaps `/unit` rounding to the end of the period (Grafana's behaviour for `to`).
    All-digit input is epoch ms and must have >= 10 digits ("2024" is rejected, not 1970).
    """
    expr = expr.strip()
    if expr.isdigit():
        if len(expr) < 10:
            raise ReportError(f"cannot parse time {expr!r}")
        return datetime.fromtimestamp(int(expr) / 1000, tz=UTC).astimezone(TZ)
    m = _REL_RE.match(expr)
    if m is None:
        try:
            dt = datetime.fromisoformat(expr)
        except ValueError as exc:
            raise ReportError(f"cannot parse time {expr!r}") from exc
        return dt if dt.tzinfo else dt.replace(tzinfo=TZ)
    dt = now
    if m["n"]:
        n = int(m["n"]) * (-1 if m["sign"] == "-" else 1)
        match m["unit"]:
            case "m":
                dt = dt + timedelta(minutes=n)
            case "h":
                dt = dt + timedelta(hours=n)
            case "d":
                dt = dt + timedelta(days=n)
            case "w":
                dt = dt + timedelta(weeks=n)
            case "M":
                dt = _add_months(dt, n)
            case "y":
                dt = _add_months(dt, 12 * n)
    unit = m["round"]
    if unit is None:
        return dt
    match unit:
        case "m":
            dt = dt.replace(second=0, microsecond=0)
        case "h":
            dt = dt.replace(minute=0, second=0, microsecond=0)
        case "d":
            dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        case "w":
            dt = (dt - timedelta(days=dt.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        case "M":
            dt = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        case "y":
            dt = dt.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if end:
        step = {
            "m": timedelta(minutes=1),
            "h": timedelta(hours=1),
            "d": timedelta(days=1),
            "w": timedelta(weeks=1),
        }.get(unit)
        dt = dt + step if step else _add_months(dt, 12 if unit == "y" else 1)
        dt -= timedelta(milliseconds=1)
    return dt


# --------------------------------------------------------------------------- rendering


def panel_px_w(p: ReportPanel) -> int:
    return round(p.w / GRID_COLS * W_PX)


def panel_px_h(p: ReportPanel) -> int:
    return ROW_PX * p.h - 8


def render_url(
    base: str,
    uid: str,
    panel: ReportPanel,
    variables: Mapping[str, list[str]],
    t0: datetime,
    t1: datetime,
    *,
    timeout_s: int,
    scale: int = 1,
) -> str:
    """Build the image-renderer URL (`/render/d-solo`) for one panel."""
    params: list[tuple[str, str]] = [
        ("orgId", "1"),
        ("panelId", str(panel.id)),
        ("width", str(panel_px_w(panel))),
        ("height", str(panel_px_h(panel))),
        ("from", str(int(t0.timestamp() * 1000))),
        ("to", str(int(t1.timestamp() * 1000))),
        ("tz", "Europe/Lisbon"),
        ("timeout", str(timeout_s)),
    ]
    if scale != 1:
        params.append(("scale", str(scale)))
    params.extend((f"var-{n}", v) for n, vals in variables.items() for v in vals)
    return f"{base.rstrip('/')}/render/d-solo/{quote(uid)}?{urlencode(params, quote_via=quote)}"


def fetch_png(client: httpx.Client, url: str, title: str = "") -> bytes:
    """GET a rendered panel; require a PNG. Retries once on timeout."""
    for attempt in (1, 2):
        try:
            resp = client.get(url)
            break
        except httpx.TimeoutException as exc:
            if attempt == 2:
                raise RenderError(f"panel {title!r}: render timed out twice") from exc
            logger.warning("panel %r timed out, retrying once", title)
        except httpx.TransportError as exc:
            raise RenderError(
                f"panel {title!r}: {type(exc).__name__}: {exc}; the renderer may be down "
                "(crashed or OOM-killed?): check `docker compose --profile dashboard ps`"
            ) from exc
    ctype = resp.headers.get("content-type", "")
    if resp.status_code != 200 or not ctype.startswith("image/png"):
        raise RenderError(
            f"panel {title!r}: HTTP {resp.status_code} {ctype!r}: {resp.text[:200]!r}"
        )
    return resp.content


Fetch = Callable[[ReportPanel, str], bytes]


# --------------------------------------------------------------------------- PDF


def _latin(s: str) -> str:
    """Helvetica is WinAnsi-only; replace anything outside it."""
    return s.encode("cp1252", "replace").decode("cp1252")


def _wrap(text: str, font: str, size: float, width_mm: float) -> list[str]:
    lines: list[str] = []
    line = ""
    for word in _latin(text).split(" "):
        trial = f"{line} {word}" if line else word
        if line and stringWidth(trial, font, size) > width_mm * mm:
            lines.append(line)
            line = word
        else:
            line = trial
    lines.append(line)
    return lines


@dataclass
class _Page:
    ops: list[tuple[Any, ...]] = field(default_factory=list)


class _Planner:
    """Places text and panel images on pages; coordinates are mm from the content-area top-left."""

    def __init__(self) -> None:
        self.pages: list[_Page] = [_Page()]
        self.y = 0.0

    @property
    def remaining(self) -> float:
        return CH_MM - self.y

    def new_page(self) -> None:
        self.pages.append(_Page())
        self.y = 0.0

    def text(self, s: str, font: str, size: float, line_mm: float) -> None:
        for ln in _wrap(s, font, size, CW_MM):
            self.pages[-1].ops.append(("text", 0.0, self.y + line_mm * 0.75, ln, font, size))
            self.y += line_mm

    def gap(self, h: float) -> None:
        self.y += h

    def band(self, band: Band, *, stay: bool = False) -> None:
        h = band.height_px * K
        if h > self.remaining and self.y > 0 and not stay:
            self.new_page()
        s = min(1.0, self.remaining / h)
        for p in band.panels:
            self.pages[-1].ops.append(
                (
                    "image",
                    p.id,
                    p.x / GRID_COLS * CW_MM * s,
                    self.y + (p.y - band.y0) * ROW_PX * K * s,
                    (p.w / GRID_COLS * CW_MM - GUTTER_MM) * s,
                    panel_px_h(p) * K * s,
                )
            )
        self.y += h * s + BAND_GAP_MM


def _plan(meta: ReportMeta, sections: list[Section]) -> list[_Page]:
    pl = _Planner()
    pl.text(meta.title, "Helvetica-Bold", 18, 8)
    pl.gap(1)
    for name, value in meta.filters:
        pl.text(f"{name}: {value}", "Helvetica", 9, 4.5)
    if meta.nifs:
        pl.text("Selected NIF(s): " + ", ".join(meta.nifs), "Helvetica-Bold", 9, 4.5)
    pl.text(f"Time range: {meta.time_text}", "Helvetica", 9, 4.5)
    pl.text(
        f"Generated: {meta.generated_at:%Y-%m-%d %H:%M} | {meta.grafana_url}",
        "Helvetica",
        9,
        4.5,
    )
    pl.gap(4)
    for sec in sections:
        bands = layout_bands(sec)
        # keep the heading with its first band: move both to a new page if they don't fit
        first_h = bands[0].height_px * K if bands else 0.0
        need = HEADING_MM + min(first_h, max(CH_MM - HEADING_MM, 0.0))
        if pl.y > 0 and pl.remaining < need:
            pl.new_page()
        pl.text(sec.title, "Helvetica-Bold", 13, HEADING_MM - 1)
        pl.gap(1)
        for i, band in enumerate(bands):
            pl.band(band, stay=i == 0)
    return pl.pages


def build_pdf(
    meta: ReportMeta, sections: list[Section], images: Mapping[int, bytes], out: Path
) -> int:
    """Write the PDF; returns the page count. No HTTP."""
    pages = _plan(meta, sections)
    out.parent.mkdir(parents=True, exist_ok=True)
    c = Canvas(str(out), pagesize=A4, pageCompression=1)
    c.setTitle(_latin(meta.title))
    top = PAGE_H_MM - MARGIN_MM - BAND_MM  # content-area top, mm from page bottom
    for n, page in enumerate(pages, start=1):
        c.setFont("Helvetica", 8)
        c.drawString(MARGIN_MM * mm, (PAGE_H_MM - MARGIN_MM - 3) * mm, _latin(meta.title))
        c.drawRightString(
            (PAGE_W_MM - MARGIN_MM) * mm, (MARGIN_MM + 2) * mm, f"page {n}/{len(pages)}"
        )
        for op in page.ops:
            if op[0] == "text":
                _, x, y, s, font, size = op
                c.setFont(font, size)
                c.drawString((MARGIN_MM + x) * mm, (top - y) * mm, s)
            else:
                _, pid, x, y, w, h = op
                c.drawImage(
                    ImageReader(BytesIO(images[pid])),
                    (MARGIN_MM + x) * mm,
                    (top - y - h) * mm,
                    w * mm,
                    h * mm,
                )
        c.showPage()
    c.save()
    return len(pages)


# --------------------------------------------------------------------------- orchestration


def preflight(client: httpx.Client, uid: str, grafana_url: str) -> None:
    """Check Grafana is up and has the dashboard provisioned."""
    hint = "start it with: docker compose --profile dashboard up -d"
    try:
        client.get("/api/health").raise_for_status()
        client.get(f"/api/dashboards/uid/{quote(uid)}").raise_for_status()
    except httpx.HTTPError as exc:
        raise ReportError(f"Grafana at {grafana_url} not ready ({exc}); {hint}") from exc


def _time_text(t0: datetime, t1: datetime, from_expr: str, to_expr: str) -> str:
    return f"{t0:%Y-%m-%d} to {t1:%Y-%m-%d} ({from_expr} to {to_expr})"


def generate_report(
    dashboard: str,
    out: Path,
    *,
    variables: list[str] | None = None,
    time_from: str | None = None,
    time_to: str | None = None,
    grafana_url: str = "http://127.0.0.1:3001",
    timeout_s: int = 60,
    dashboard_dir: Path = DASHBOARD_DIR,
    fetch: Fetch | None = None,
    now: datetime | None = None,
) -> Path:
    """Render every selected panel and write the PDF to `out`.

    `fetch` (panel, url) -> PNG bytes is injectable for tests; by default it uses httpx
    against the image renderer behind Grafana, after a preflight check.
    """
    dash = load_dashboard(dashboard, dashboard_dir)
    uid = str(dash["uid"])
    chosen = parse_vars(variables or [], dash)
    from_expr = time_from or str(dash["time"]["from"])
    to_expr = time_to or str(dash["time"]["to"])
    now = now or datetime.now(TZ)
    t0, t1 = resolve_time(from_expr, now), resolve_time(to_expr, now, end=True)
    if not t0 < t1:
        raise ReportError(
            f"empty time range: {from_expr!r} ({t0:%Y-%m-%d %H:%M}) is not before "
            f"{to_expr!r} ({t1:%Y-%m-%d %H:%M})"
        )
    sections = select_sections(dash)
    panels = [p for s in sections for p in s.panels]

    client: httpx.Client | None = None
    if fetch is None:
        client = httpx.Client(base_url=grafana_url, timeout=timeout_s + 30)
        preflight(client, uid, grafana_url)
        c = client

        def fetch(panel: ReportPanel, url: str) -> bytes:
            try:
                return fetch_png(c, url, panel.title)
            except RenderError as exc:
                if panel is panels[0]:
                    raise RenderError(
                        f"{exc}\nThe image renderer is missing or misconfigured: check the "
                        "`renderer` service and GRAFANA_RENDERER_TOKEN, then restart with "
                        "docker compose --profile dashboard up -d"
                    ) from exc
                raise

    try:
        images: dict[int, bytes] = {}
        for i, p in enumerate(panels, start=1):
            started = time.monotonic()
            url = render_url(grafana_url, uid, p, chosen, t0, t1, timeout_s=timeout_s)
            images[p.id] = fetch(p, url)
            logger.info("[%d/%d] %s (%.1fs)", i, len(panels), p.title, time.monotonic() - started)
    finally:
        if client is not None:
            client.close()

    meta = ReportMeta(
        title=str(dash["title"]),
        filters=describe_filters(dash, chosen),
        nifs=tuple(v for n in NIF_VARS for v in chosen.get(n, [])),
        time_text=_time_text(t0, t1, from_expr, to_expr),
        generated_at=now,
        grafana_url=grafana_url,
    )
    pages = build_pdf(meta, sections, images, out)
    logger.info("wrote %s (%d pages)", out, pages)
    return out
