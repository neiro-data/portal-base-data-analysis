"""Offline tests for the dashboard PDF report (no live Grafana or renderer)."""

from __future__ import annotations

import struct
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from typer.testing import CliRunner

from base_gov import report
from base_gov.cli import app

NOW = datetime(2026, 10, 4, 15, 30, tzinfo=report.TZ)


def _png() -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data)
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    raw = zlib.compress(b"\x00\xff\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", raw)
        + chunk(b"IEND", b"")
    )


def _ids(name: str) -> list[int]:
    return [p.id for s in report.select_sections(report.load_dashboard(name)) for p in s.panels]


def _band_ids(name: str) -> list[set[int]]:
    dash = report.load_dashboard(name)
    return [
        {p.id for p in b.panels}
        for s in report.select_sections(dash)
        for b in report.layout_bands(s)
    ]


@pytest.mark.parametrize("stem", sorted(report.REPORTABLE))
def test_selection_excludes_text_rows_and_contracts(stem: str) -> None:
    dash = report.load_dashboard(stem)
    sections = report.select_sections(dash)
    assert all(s.title != "Contracts" for s in sections)
    assert all(p.type not in ("text", "row") for s in sections for p in s.panels)
    for s in sections:
        assert [(p.y, p.x) for p in s.panels] == sorted((p.y, p.x) for p in s.panels)
    if stem.endswith("analysis"):
        assert not {34, 35} & set(_ids(stem))


def test_load_by_uid_and_unknown() -> None:
    assert report.load_dashboard("base-adjudicante")["uid"] == "base-adjudicante"
    with pytest.raises(report.ReportError):
        report.load_dashboard("contracts-table")


def test_section_titles_adjudicante() -> None:
    titles = [
        s.title for s in report.select_sections(report.load_dashboard("adjudicante-analysis"))
    ]
    assert titles == [
        "Overview",
        "Over time",
        "Who it buys from",
        "How it procures",
        "Duration & price",
    ]


def test_overview_summary_and_nested_rows() -> None:
    sections = report.select_sections(report.load_dashboard("overview"))
    assert sections[0].title == "Summary"
    assert set(range(15, 24)) <= set(_ids("overview"))
    assert sections[-1].title == "Procedure types (notebooks)"


def test_bands() -> None:
    adj = _band_ids("adjudicante-analysis")
    assert {13, 14} in adj and {16, 17} in adj and {21, 22, 23, 24} in adj
    assert {9, 10, 12} in _band_ids("overview")


def test_parse_vars() -> None:
    dash = report.load_dashboard("adjudicante-analysis")
    assert report.parse_vars(["adjudicante=1", "adjudicante=2"], dash) == {
        "adjudicante": ["1", "2"]
    }
    with pytest.raises(report.ReportError):
        report.parse_vars(["nope=1"], dash)
    with pytest.raises(report.ReportError):
        report.parse_vars(["min_contract_value=1", "min_contract_value=2"], dash)
    with pytest.raises(report.ReportError):
        report.parse_vars(["adjudicante"], dash)


def test_resolve_time() -> None:
    assert report.resolve_time("now", NOW) == NOW
    assert report.resolve_time("now-2y", NOW).date().isoformat() == "2024-10-04"
    assert report.resolve_time("now-90d", NOW).date().isoformat() == "2026-07-06"
    d = report.resolve_time("now/d", NOW)
    assert (d.hour, d.minute) == (0, 0) and d.day == 4
    assert report.resolve_time("2024-01-02", NOW) == datetime(2024, 1, 2, tzinfo=report.TZ)
    ms = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp() * 1000)
    assert report.resolve_time(str(ms), NOW) == datetime(2025, 1, 1, tzinfo=UTC)
    with pytest.raises(report.ReportError):
        report.resolve_time("yesterday", NOW)


def test_render_url() -> None:
    panel = report.ReportPanel(13, "x", "timeseries", 0, 17, 12, 8)
    t0 = report.resolve_time("now-2y", NOW)
    url = report.render_url(
        "http://g:3001/",
        "base-adjudicante",
        panel,
        {"adjudicante": ["A", "B C"]},
        t0,
        NOW,
        timeout_s=45,
    )
    u = urlparse(url)
    assert u.path == "/render/d-solo/base-adjudicante"
    q = parse_qs(u.query)
    assert q["panelId"] == ["13"] and q["width"] == ["800"] and q["height"] == ["296"]
    assert q["from"] == [str(int(t0.timestamp() * 1000))]
    assert q["to"] == [str(int(NOW.timestamp() * 1000))]
    assert q["timeout"] == ["45"] and q["var-adjudicante"] == ["A", "B C"]
    assert "var-adjudicante=A&var-adjudicante=B%20C" in url


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://g")


def test_fetch_png_ok_and_errors() -> None:
    png = _png()
    ok = _client(lambda r: httpx.Response(200, content=png, headers={"content-type": "image/png"}))
    assert report.fetch_png(ok, "/x") == png
    for resp in (
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="<html>", headers={"content-type": "text/html"}),
    ):
        with pytest.raises(report.RenderError):
            report.fetch_png(_client(lambda r, resp=resp: resp), "/x", "T")


def test_fetch_png_retries_timeout_once() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, content=_png(), headers={"content-type": "image/png"})

    assert report.fetch_png(_client(handler), "/x")
    assert calls == 2


def test_fetch_png_gives_up_after_second_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(report.RenderError):
        report.fetch_png(_client(handler), "/x")


@pytest.mark.parametrize("stem", sorted(report.REPORTABLE))
def test_generate_report_fetches_each_panel_once(stem: str, tmp_path: Path) -> None:
    seen: list[int] = []

    def fake(panel: report.ReportPanel, url: str) -> bytes:
        seen.append(panel.id)
        return _png()

    out = tmp_path / "sub" / "r.pdf"
    extra = ["adjudicante=1", "adjudicante=2"] if stem == "adjudicante-analysis" else []
    report.generate_report(stem, out, variables=extra, fetch=fake, now=NOW)
    data = out.read_bytes()
    assert data.startswith(b"%PDF")
    assert data.count(b"/Type /Page\n") + data.count(b"/Type /Page ") >= 1
    assert sorted(seen) == sorted(_ids(stem)) and len(seen) == len(set(seen))


def test_generate_report_urls_and_unknown(tmp_path: Path) -> None:
    urls: list[str] = []

    def fake(panel: report.ReportPanel, url: str) -> bytes:
        urls.append(url)
        return _png()

    report.generate_report(
        "adjudicante-analysis",
        tmp_path / "r.pdf",
        variables=["adjudicante=A", "adjudicante=B"],
        fetch=fake,
        now=NOW,
    )
    assert all("var-adjudicante=A&var-adjudicante=B" in u for u in urls)
    with pytest.raises(report.ReportError):
        report.generate_report("nope", tmp_path / "x.pdf", fetch=fake)


def test_cli_wiring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def fake(dashboard: str, out: Path, **kw: Any) -> Path:
        captured.update(dashboard=dashboard, out=out, **kw)
        return out

    monkeypatch.setattr(report, "generate_report", fake)
    monkeypatch.delenv("GRAFANA_URL", raising=False)
    target = tmp_path / "o.pdf"
    res = CliRunner().invoke(
        app,
        [
            "report",
            "adjudicante-analysis",
            "--var",
            "adjudicante=1",
            "--from",
            "now-1y",
            "--out",
            str(target),
            "--timeout",
            "30",
        ],
    )
    assert res.exit_code == 0, res.output
    assert captured["variables"] == ["adjudicante=1"] and captured["time_from"] == "now-1y"
    assert captured["out"] == target and captured["timeout_s"] == 30


def test_cli_unknown_dashboard_exit_1() -> None:
    res = CliRunner().invoke(app, ["report", "nope"])
    assert res.exit_code == 1


# ----------------------------------------------------------------------- edge cases


def _gp(x: int, y: int, w: int, h: int) -> dict[str, int]:
    return {"x": x, "y": y, "w": w, "h": h}


def _p(pid: int, x: int, y: int, w: int, h: int, typ: str = "timeseries") -> dict[str, Any]:
    return {"id": pid, "type": typ, "title": f"p{pid}", "gridPos": _gp(x, y, w, h)}


def _rp(pid: int, x: int, y: int, w: int, h: int) -> report.ReportPanel:
    return report.ReportPanel(pid, f"p{pid}", "timeseries", x, y, w, h)


def test_contracts_row_panels_are_excluded_by_id() -> None:
    for stem in ("adjudicante-analysis", "adjudicatario-analysis"):
        dash = report.load_dashboard(stem)
        rows = sorted(
            (p for p in dash["panels"] if p["type"] == "row"), key=lambda p: p["gridPos"]["y"]
        )
        assert rows[-1]["title"] == "Contracts"
        y0 = rows[-1]["gridPos"]["y"]
        contract_ids = {
            p["id"] for p in dash["panels"] if p["type"] != "row" and p["gridPos"]["y"] > y0
        }
        assert contract_ids
        assert not contract_ids & set(_ids(stem))


def test_overview_nested_ids_come_from_collapsed_row() -> None:
    dash = report.load_dashboard("overview")
    nested = [p["id"] for p in dash["panels"] if p["type"] == "row" for p in p["panels"]]
    assert nested == list(range(15, 24))
    sec = report.select_sections(dash)[-1]
    assert sec.title == "Procedure types (notebooks)"
    assert {p.id for p in sec.panels} == set(nested)


def test_select_sections_synthetic_collapsed_and_contracts() -> None:
    dash = {
        "panels": [
            _p(1, 0, 0, 24, 4, "stat"),
            _p(2, 0, 1, 24, 2, "text"),
            {
                "id": 3,
                "type": "row",
                "title": "Coll",
                "gridPos": _gp(0, 5, 24, 1),
                "panels": [_p(5, 12, 6, 12, 4), _p(4, 0, 6, 12, 4)],
            },
            {"id": 6, "type": "row", "title": "Contracts", "gridPos": _gp(0, 10, 24, 1)},
            _p(7, 0, 11, 24, 8, "table"),
            {"id": 8, "type": "row", "title": "Empty", "gridPos": _gp(0, 20, 24, 1)},
        ]
    }
    secs = report.select_sections(dash)
    assert [(s.title, [p.id for p in s.panels]) for s in secs] == [
        ("Summary", [1]),
        ("Coll", [4, 5]),
    ]


def test_parse_vars_edge_cases() -> None:
    dash = report.load_dashboard("adjudicante-analysis")
    with pytest.raises(report.ReportError, match="unknown variable"):
        report.parse_vars(["adjudicatario=1"], dash)
    with pytest.raises(report.ReportError, match="not multi-value"):
        report.parse_vars(["min_contract_value=1", "min_contract_value=2"], dash)
    with pytest.raises(report.ReportError):
        report.parse_vars(["=1"], dash)
    assert report.parse_vars(["min_contract_value=1"], dash) == {"min_contract_value": ["1"]}
    assert report.parse_vars(["adjudicante="], dash) == {"adjudicante": [""]}
    assert report.parse_vars([], dash) == {}


def test_resolve_time_edge_cases() -> None:
    assert report.resolve_time("now/d", NOW) == NOW.replace(hour=0, minute=0)
    assert report.resolve_time("now-1M", NOW).date().isoformat() == "2026-09-04"
    assert report.resolve_time("now-1d/d", NOW).date().isoformat() == "2026-10-03"
    assert report.resolve_time("now+1h", NOW).hour == 16
    jan = datetime(2026, 3, 31, 12, tzinfo=report.TZ)
    assert report.resolve_time("now-1M", jan).date().isoformat() == "2026-02-28"
    assert report.resolve_time("now/M", NOW).day == 1
    assert report.resolve_time("now/w", NOW).weekday() == 0
    for bad in ("now-", "now-2x", "now--1d", "now-1.5d", "", "foo", "2024-13-45"):
        with pytest.raises(report.ReportError):
            report.resolve_time(bad, NOW)


def test_band_overlap_merging() -> None:
    # tall left panel (y0..8) overlaps two stacked right panels; later panel starts at 8 -> new band
    sec = report.Section(
        "s",
        (_rp(1, 0, 0, 12, 8), _rp(2, 12, 0, 12, 4), _rp(3, 12, 4, 12, 4), _rp(4, 0, 8, 24, 4)),
    )
    bands = report.layout_bands(sec)
    assert [{p.id for p in b.panels} for b in bands] == [{1, 2, 3}, {4}]
    assert [b.y0 for b in bands] == [0, 8]
    # running span must extend: 3 only overlaps because 2 ends at 4 but 1 ends at 8
    chain = report.Section("s", (_rp(1, 0, 0, 12, 4), _rp(2, 12, 2, 12, 6), _rp(3, 0, 6, 12, 2)))
    assert [{p.id for p in b.panels} for b in report.layout_bands(chain)] == [{1, 2, 3}]
    assert report.layout_bands(report.Section("e", ())) == []
    # unsorted input is sorted by y,x
    uns = report.Section("s", (_rp(2, 0, 4, 24, 2), _rp(1, 0, 0, 24, 4)))
    assert [{p.id for p in b.panels} for b in report.layout_bands(uns)] == [{1}, {2}]


def _meta() -> report.ReportMeta:
    return report.ReportMeta("T", (("v", "1"),), ("123",), "a to b", NOW, "http://g")


def test_band_taller_than_page_is_scaled_to_fit(tmp_path: Path) -> None:
    sec = report.Section("Big", (_rp(1, 0, 0, 24, 60), _rp(2, 0, 60, 12, 4)))
    pages = report._plan(_meta(), [sec])
    imgs = [op for pg in pages for op in pg.ops if op[0] == "image"]
    assert len(imgs) == 2
    for _, _pid, x, y, w, h in imgs:
        assert x >= 0 and y >= 0
        assert x + w <= report.CW_MM + 1e-6
        assert y + h <= report.CH_MM + 1e-6
    out = tmp_path / "big.pdf"
    n = report.build_pdf(_meta(), [sec], {1: _png(), 2: _png()}, out)
    assert n == len(pages) >= 2
    assert out.read_bytes().startswith(b"%PDF")


def test_oversized_band_gets_own_page_and_page_numbers(tmp_path: Path) -> None:
    sec = report.Section("S", tuple(_rp(i, 0, (i - 1) * 8, 24, 8) for i in range(1, 9)))
    pages = report._plan(_meta(), [sec])
    assert len(pages) > 1
    for pg in pages:
        for op in pg.ops:
            if op[0] == "image":
                assert op[3] + op[5] <= report.CH_MM + 1e-6
    out = tmp_path / "m.pdf"
    n = report.build_pdf(_meta(), [sec], {i: _png() for i in range(1, 9)}, out)
    assert n == len(pages)


def test_generate_report_propagates_render_error(tmp_path: Path) -> None:
    def boom(panel: report.ReportPanel, url: str) -> bytes:
        raise report.RenderError("x")

    with pytest.raises(report.RenderError):
        report.generate_report("overview", tmp_path / "r.pdf", fetch=boom, now=NOW)
    assert not (tmp_path / "r.pdf").exists()


def test_heading_moves_with_first_band_to_new_page() -> None:
    moved = 0
    for rows in range(4, 40):
        filler = report.Section("Filler", (_rp(1, 0, 0, 24, rows),))
        big = report.Section("Heading-Marker", (_rp(2, 0, 0, 24, 14),))
        pages = report._plan(_meta(), [filler, big])
        for pg in pages:
            has_head = any(op[0] == "text" and op[3] == "Heading-Marker" for op in pg.ops)
            has_img = any(op[0] == "image" and op[1] == 2 for op in pg.ops)
            assert has_head == has_img
        moved += len(pages) > 1
    assert moved > 0


def test_heading_not_stranded_at_page_bottom() -> None:
    # many full-width bands, then a section: whichever page it lands on, heading has its image
    secs = [report.Section(f"Sec{i}", (_rp(i, 0, 0, 24, 14 + i),)) for i in range(1, 12)]
    pages = report._plan(_meta(), secs)
    for pg in pages:
        heads = [op[3] for op in pg.ops if op[0] == "text" and str(op[3]).startswith("Sec")]
        imgs = {op[1] for op in pg.ops if op[0] == "image"}
        for h in heads:
            assert int(h[3:]) in imgs


@pytest.mark.parametrize("expr", ["2024", "20241004", "0", "123456789"])
def test_resolve_time_short_digits_rejected(expr: str) -> None:
    with pytest.raises(report.ReportError):
        report.resolve_time(expr, NOW)


def test_resolve_time_end_snaps_to_period_end() -> None:
    d = report.resolve_time("now/d", NOW, end=True)
    assert (d.day, d.hour, d.minute, d.second) == (4, 23, 59, 59)
    m = report.resolve_time("now/M", NOW, end=True)
    assert (m.month, m.day, m.hour) == (10, 31, 23)
    y = report.resolve_time("now/y", NOW, end=True)
    assert (y.year, y.month, y.day) == (2026, 12, 31)
    w = report.resolve_time("now/w", NOW, end=True)
    assert w.weekday() == 6 and w.hour == 23
    assert report.resolve_time("now", NOW, end=True) == NOW


def test_generate_report_rejects_empty_range(tmp_path: Path) -> None:
    def never(panel: report.ReportPanel, url: str) -> bytes:
        raise AssertionError("must not render")

    with pytest.raises(report.ReportError, match="empty time range"):
        report.generate_report(
            "overview",
            tmp_path / "r.pdf",
            time_from="now",
            time_to="now-2y",
            fetch=never,
            now=NOW,
        )


def test_fetch_png_transport_error_hints_renderer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(report.RenderError, match="renderer may be down"):
        report.fetch_png(_client(handler), "/x", "T")

    def handler2(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("eof", request=request)

    with pytest.raises(report.RenderError, match="renderer may be down"):
        report.fetch_png(_client(handler2), "/x", "T")


def test_report_command_registered_before_main_guard() -> None:
    src = Path(report.__file__).with_name("cli.py").read_text()
    assert src.index('@app.command("report")') < src.index('if __name__ == "__main__"')


def test_cli_timeout_min_1() -> None:
    res = CliRunner().invoke(app, ["report", "overview", "--timeout", "0"])
    assert res.exit_code == 2


def test_cli_default_logging_shows_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    import logging

    from base_gov import cli

    seen: dict[str, Any] = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: seen.update(kw))
    cli._configure_logging(False)
    assert seen["level"] == logging.INFO
