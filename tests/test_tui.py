"""Tests for the `roost dash` TUI pure-logic layer.

Mirrors the mac app's split: the formatting/staleness/sort/console-wiring
helpers and the `TuiClient` request shapes are exercised here without a TTY
(curses is never imported), pinned against the same golden fixtures the mobile
apps decode (mobile-app/fixtures/), so a server-side shape drift trips here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from roost import tui

FIX = Path(__file__).resolve().parent.parent / "mobile-app" / "fixtures"


def _fix(name: str):
    return json.loads((FIX / name).read_text())


# --------------------------------------------------------------------------- #
# relative_time / fmt_duration                                                #
# --------------------------------------------------------------------------- #

def test_relative_time_past_future_now_missing():
    now = 1_000_000.0
    assert tui.relative_time(None, now) == "—"
    assert tui.relative_time(0, now) == "—"
    assert tui.relative_time(now, now) == "now"
    assert tui.relative_time(now - 30, now) == "30s ago"
    assert tui.relative_time(now - 120, now) == "2m ago"
    assert tui.relative_time(now - 7200, now) == "2h ago"
    assert tui.relative_time(now - 2 * 86400, now) == "2d ago"
    # Future instants (a schedule's next_run_at, a blob's expiry).
    assert tui.relative_time(now + 90, now) == "in 1m"
    assert tui.relative_time(now + 3 * 3600, now) == "in 3h"


def test_fmt_duration_buckets():
    assert tui.fmt_duration(None) == "—"
    assert tui.fmt_duration(-5) == "—"
    assert tui.fmt_duration(45) == "45s"
    assert tui.fmt_duration(12 * 60) == "12m"
    assert tui.fmt_duration(3600 + 3 * 60) == "1h3m"
    assert tui.fmt_duration(3600) == "1h"
    assert tui.fmt_duration(2 * 86400 + 4 * 3600) == "2d4h"


# --------------------------------------------------------------------------- #
# worker staleness — the R75 pattern (API.md §2a)                             #
# --------------------------------------------------------------------------- #

def test_worker_live_status_recomputes_from_last_seen():
    now = 1_000_000.0
    fresh = {"status": "idle", "last_seen": now - 5}
    stale = {"status": "busy", "last_seen": now - 60}      # ≥45s
    gone = {"status": "idle", "last_seen": now - 200}      # ≥120s
    assert tui.worker_live_status(fresh, now) == "idle"
    assert tui.worker_live_status(stale, now) == "stale"
    assert tui.worker_live_status(gone, now) == "offline"


def test_worker_offline_server_word_wins_even_when_fresh():
    now = 1_000_000.0
    row = {"status": "offline", "last_seen": now}  # server says offline
    assert tui.worker_live_status(row, now) == "offline"


def test_nodes_chip_counts_idle_and_busy():
    now = 1_000_000.0
    workers = [
        {"status": "busy", "last_seen": now},
        {"status": "idle", "last_seen": now},
        {"status": "offline", "last_seen": now - 300},
    ]
    assert tui.count_live(workers, now) == 2
    assert tui.nodes_chip(workers, now) == "2 nodes"
    assert tui.nodes_chip(workers[:1], now) == "1 node"
    assert tui.nodes_chip([], now) == "0 nodes"


# --------------------------------------------------------------------------- #
# run sorting / splitting                                                      #
# --------------------------------------------------------------------------- #

def test_sort_runs_active_first_then_created_desc():
    runs = [
        {"run_id": "a", "state": "succeeded", "created_at": 100},
        {"run_id": "b", "state": "running", "created_at": 50},
        {"run_id": "c", "state": "queued", "created_at": 200},
        {"run_id": "d", "state": "assigned", "created_at": 10},
    ]
    order = [r["run_id"] for r in tui.sort_runs(runs)]
    # running + assigned float to the top; then everything by created_at desc.
    assert order[:2] == ["b", "d"]
    assert order[2:] == ["c", "a"]


def test_split_runs_active_vs_recent():
    runs = [
        {"state": "running"}, {"state": "succeeded"},
        {"state": "queued"}, {"state": "failed"}, {"state": "cancelled"},
    ]
    active, recent = tui.split_runs(runs)
    assert [r["state"] for r in active] == ["running", "queued"]
    assert [r["state"] for r in recent] == ["succeeded", "failed", "cancelled"]


# --------------------------------------------------------------------------- #
# formatting helpers                                                           #
# --------------------------------------------------------------------------- #

def test_worker_caps_short_gpu_and_failed_probe():
    assert "gpu 79GB" in tui.worker_caps_short({"gpu_vram_gb": 79})
    assert "gpu×2 40GB" in tui.worker_caps_short({"gpu_vram_gb": 40, "gpu_count": 2})
    # A broken GPU probe is flagged, never silently dropped (R41).
    assert "DETECTION-FAILED" in tui.worker_caps_short({"gpu_detection": "failed"})
    s = tui.worker_caps_short({"arch": "arm64", "cpus": 4, "tools": ["claude"]})
    assert "arm64" in s and "4 cpu" in s and "claude ✓" in s


def test_fmt_cost_and_size():
    assert tui.fmt_cost({"tokens_used": 3100, "cost_est_usd": 0.04}) == "3.1k tok · $0.04"
    assert tui.fmt_cost({"tokens_used": 0, "cost_est_usd": 0}) == ""
    assert tui.fmt_cost(None) == ""
    assert tui.fmt_size(512) == "512 B"
    assert tui.fmt_size(1536) == "1.5 KB"
    assert tui.fmt_size(None) == "—"


def test_health_glyph_closed_enum_and_unknown():
    assert tui.health_glyph("verified") == "✓"
    assert tui.health_glyph("failed") == "✗"
    assert tui.health_glyph("running") == "▶"
    assert tui.health_glyph("queued") == "○"
    assert tui.health_glyph("totally-new-status") == "·"  # render, don't crash
    assert tui.health_glyph(None) == "·"


def test_phase_rail_marks_current_and_collapses_terminal():
    assert "▸running" in tui.phase_rail("running")
    assert "▸verifying" in tui.phase_rail("verifying")
    assert "self-healing" in tui.phase_rail("self-healing")
    assert "▸✓done" in tui.phase_rail("succeeded")
    assert "▸failed" in tui.phase_rail("failed")


def test_truncate_and_run_title():
    assert tui.truncate("a b   c", 80) == "a b c"
    assert tui.truncate("abcdef", 4) == "abc…"
    assert tui.run_title({"goal_display": "X", "goal": "Y"}) == "X"
    assert tui.run_title({"goal": "Y"}) == "Y"
    assert tui.run_title({"run_id": "abc"}) == "abc"
    # A raw /jobs object (no goal_display/goal) falls back to spec.task / intent.
    assert tui.run_title({"spec": {"task": "report uname"}, "id": "x"}) == "report uname"
    assert tui.run_title({"intent": "do a thing", "id": "x"}) == "do a thing"


def test_run_helpers_handle_both_derived_and_job_shapes():
    # Derived run row shape.
    derived = {"health": {"status": "verified"}, "worker": "pi-4",
               "cost": {"tokens_used": 3100, "cost_est_usd": 0.04}}
    assert tui.run_status(derived) == "verified"
    assert tui.run_worker(derived) == "pi-4"
    assert tui.run_cost(derived)["cost_est_usd"] == 0.04
    # Raw /jobs job object shape (no health/worker/cost).
    job = {"state": "running", "worker_id": "c0276d24bb4d", "tokens_used": 534}
    assert tui.run_status(job) == "running"
    assert tui.run_worker(job) == "c0276d24bb4d"
    assert tui.run_cost(job) == {"tokens_used": 534}
    assert tui.run_worker({}) == "—"


# --------------------------------------------------------------------------- #
# ListCursor                                                                   #
# --------------------------------------------------------------------------- #

def test_list_cursor_clamps_and_moves():
    c = tui.ListCursor()
    c.set_count(3)
    c.move(-1)
    assert c.index == 0
    c.move(5)
    assert c.index == 2          # clamped to last
    c.set_count(1)
    assert c.index == 0          # re-clamped when the list shrinks
    c.set_count(0)
    assert c.index == 0
    assert c.selected([]) is None
    c.set_count(2)
    assert c.selected(["x", "y"]) == "x"


# --------------------------------------------------------------------------- #
# console wiring (mac DESIGN §13)                                             #
# --------------------------------------------------------------------------- #

def test_build_console_files_wires_env_and_context():
    files = tui.build_console_files("http://cp:8787", "rst-tok")
    mcp = json.loads(files["mcp.json"])
    server = mcp["mcpServers"]["roost"]
    assert server["command"] == "roost" and server["args"] == ["mcp"]
    assert server["env"] == {"ROOST_URL": "http://cp:8787", "ROOST_TOKEN": "rst-tok"}
    assert "fleet" in files["CLAUDE.md"].lower()


def test_build_console_argv():
    assert tui.build_console_argv("/x/mcp.json") == [
        "claude", "--mcp-config", "/x/mcp.json"]


# --------------------------------------------------------------------------- #
# fixture-pinned decoding — the contract the mobile apps share                 #
# --------------------------------------------------------------------------- #

def test_derived_fixture_renders_without_crashing():
    d = _fix("derived.json")
    runs = tui.sort_runs(d["runs"])
    active, recent = tui.split_runs(runs)
    assert len(active) + len(recent) == len(d["runs"])
    # Every run produces a title, glyph and cost string from real payload fields.
    for r in runs:
        assert tui.run_title(r)
        status = (r.get("health") or {}).get("status")
        assert isinstance(tui.health_glyph(status), str)
        assert isinstance(tui.fmt_cost(r.get("cost")), str)
    # Workers summarize and count.
    assert tui.nodes_chip(d["workers"]) .endswith("node") or \
        tui.nodes_chip(d["workers"]).endswith("nodes")


def test_workers_fixture_status_and_caps():
    workers = _fix("workers.json")
    for w in workers:
        assert tui.worker_live_status(w) in ("idle", "busy", "stale", "offline")
        assert isinstance(tui.worker_caps_short(w.get("capabilities", {})), str)


def test_schedules_and_publish_fixtures():
    scheds = _fix("schedules_list.json")
    for s in scheds:
        assert tui.fmt_duration(s.get("interval_sec"))
        assert isinstance(tui.relative_time(s.get("next_run_at")), str)
    sites = _fix("publish_list.json")
    for site in sites:
        assert site.get("url") or site.get("public_url")
        assert isinstance(tui.fmt_size(site.get("size")), str)


# --------------------------------------------------------------------------- #
# TuiClient request shapes (no network — assert the built request)            #
# --------------------------------------------------------------------------- #

class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def test_submit_goal_shape_matches_roost_run(monkeypatch):
    captured = {}

    client = tui.TuiClient("http://cp:8787", "tok")

    def fake_post(path, json=None, **kw):
        captured["path"] = path
        captured["body"] = json
        return _FakeResp(200, {"id": "abc123", "state": "queued"})

    monkeypatch.setattr(client._c, "post", fake_post)
    client.submit_goal("report free VRAM", wallclock_min=15)
    assert captured["path"] == "/jobs"
    body = captured["body"]
    assert body["kind"] == "auto"
    assert body["task"] == "report free VRAM"
    assert body["verify"] is True
    assert body["budget"]["max_wallclock_min"] == 15
    client.close()


def test_schedule_create_shape(monkeypatch):
    captured = {}
    client = tui.TuiClient("http://cp:8787", "tok")

    def fake_post(path, json=None, **kw):
        captured["path"] = path
        captured["body"] = json
        return _FakeResp(200, {"id": "s1"})

    monkeypatch.setattr(client._c, "post", fake_post)
    client.schedule_create("tidy the repo", "30m", name="nightly")
    assert captured["path"] == "/schedules"
    assert captured["body"] == {
        "spec": {"kind": "auto", "task": "tidy the repo"},
        "every": "30m", "name": "nightly"}
    client.close()


def test_client_raises_clean_error_on_http_error(monkeypatch):
    client = tui.TuiClient("http://cp:8787", "tok")

    def fake_get(path, **kw):
        return _FakeResp(403, {"detail": "admin endpoint"})

    monkeypatch.setattr(client._c, "get", fake_get)
    with pytest.raises(tui.TuiError) as e:
        client.workers()
    assert "admin endpoint" in str(e.value)
    client.close()
