"""roost dash — a full-screen terminal dashboard for the fleet.

The Linux/SSH-native sibling of `mac-app/` (DESIGN.md): the same surfaces the
macOS app exposes — Fleet glance, Runs + live logs, Workers, Transfers, Publish,
Schedules, and a fleet-wired Console — rendered with the Python standard
library's `curses` (zero new dependencies, per CLAUDE.md).

Like the mac app, this is a *mechanical client*: it renders what the control
plane already derives (`GET /derived`) and submits goals; judgment stays in the
agents on the fleet. The pure-logic layer (formatting, staleness, sort,
console-invocation building, the `TuiClient` shapes) is import-safe without a
TTY and unit-tested in `tests/test_tui.py`; only the `run()` event loop touches
`curses`.

The distilled agent-log rendering reuses `cli.distill_log_line` — the canonical
cross-platform contract — rather than forking it.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterator, Optional

import httpx

from .cli import _client, _iter_sse, distill_log_line

# Staleness thresholds — mirror API.md §2a and the mac app exactly so a node
# that dies while a payload sits in hand degrades honestly on screen.
STALE_SEC = 45
OFFLINE_SEC = 120

# The canonical trust-loop phase order, for the run-detail phase rail.
PHASE_RAIL = ["queued", "assigned", "running", "verifying", "succeeded"]

# health.status (closed enum, API.md §2) → a single glyph. Unknown → "·".
HEALTH_GLYPH = {
    "verified": "✓", "done": "✓", "unverified": "⚠", "failed": "✗",
    "cancelled": "−", "running": "▶", "verifying": "▶", "self-healing": "▶",
    "queued": "○", "waiting": "◔", "unplaceable": "⚠", "stuck?": "⚠",
}

# Terminal phases — used to decide live-stream vs. one-shot log fetch.
TERMINAL_STATES = {"succeeded", "failed", "cancelled"}


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested; no curses, no I/O)                                #
# --------------------------------------------------------------------------- #

def relative_time(epoch: Optional[float], now: Optional[float] = None) -> str:
    """"Xs/m/h/d ago" for the past, "in X…" for the future, "now" within a
    second, "—" for missing. Magnitude buckets match the mac app's RelativeTime
    (s<60, m<3600, h<86400, else d) so the two surfaces read identically."""
    if not epoch or epoch <= 0:
        return "—"
    if now is None:
        now = time.time()
    delta = epoch - now
    mag = abs(delta)
    if mag < 1:
        return "now"
    if mag < 60:
        unit = f"{int(mag)}s"
    elif mag < 3600:
        unit = f"{int(mag // 60)}m"
    elif mag < 86_400:
        unit = f"{int(mag // 3600)}h"
    else:
        unit = f"{int(mag // 86_400)}d"
    return f"in {unit}" if delta >= 0 else f"{unit} ago"


def fmt_duration(sec: Optional[float]) -> str:
    """Compact elapsed: "45s", "12m", "1h3m", "2d4h". "—" for missing."""
    if sec is None or sec < 0:
        return "—"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86_400:
        h, m = divmod(sec, 3600)
        m //= 60
        return f"{h}h{m}m" if m else f"{h}h"
    d, h = divmod(sec, 86_400)
    h //= 3600
    return f"{d}d{h}h" if h else f"{d}d"


def worker_live_status(worker: dict, now: Optional[float] = None) -> str:
    """Recompute idle/busy/stale/offline from `last_seen` (API.md §2a). The
    server's word always wins in the offline direction — a row the server marks
    `offline` is offline however fresh the payload — so a node that dies while a
    snapshot sits in hand degrades on screen instead of staying green."""
    server = worker.get("status", "")
    if server == "offline":
        return "offline"
    last_seen = worker.get("last_seen")
    if last_seen:
        if now is None:
            now = time.time()
        gap = now - last_seen
        if gap >= OFFLINE_SEC:
            return "offline"
        if gap >= STALE_SEC:
            return "stale"
    return server or "stale"


def count_live(workers: list[dict], now: Optional[float] = None) -> int:
    """Workers that count as up — idle or busy after the staleness recompute."""
    return sum(1 for w in workers if worker_live_status(w, now) in ("idle", "busy"))


def nodes_chip(workers: list[dict], now: Optional[float] = None) -> str:
    """"3 nodes" / "1 node" — grammar for the header (mirrors server `_node_word`)."""
    n = count_live(workers, now)
    return f"{n} node" if n == 1 else f"{n} nodes"


def sort_runs(runs: list[dict]) -> list[dict]:
    """Display order (API.md §2): running/assigned first, then created_at desc."""
    def key(r: dict) -> tuple:
        active = 0 if r.get("state") in ("running", "assigned") else 1
        return (active, -(r.get("created_at") or 0))
    return sorted(runs, key=key)


def split_runs(runs: list[dict]) -> tuple[list[dict], list[dict]]:
    """(active, recent) — active = non-terminal phase, recent = terminal."""
    active = [r for r in runs if r.get("state") not in TERMINAL_STATES]
    recent = [r for r in runs if r.get("state") in TERMINAL_STATES]
    return active, recent


def worker_caps_short(caps: dict) -> str:
    """Headline capabilities for a one-line worker row: GPU first (a broken GPU
    probe is flagged, not hidden), then os/arch, then cpu/claude/tools."""
    bits: list[str] = []
    if caps.get("gpu_vram_gb"):
        n = caps.get("gpu_count") or 1
        bits.append(f"gpu×{n} {caps['gpu_vram_gb']}GB" if n and n > 1
                    else f"gpu {caps['gpu_vram_gb']}GB")
    elif caps.get("gpu_detection") == "failed":
        bits.append("gpu:DETECTION-FAILED")
    arch = caps.get("arch") or caps.get("os")
    if arch:
        bits.append(str(arch))
    if caps.get("cpus"):
        bits.append(f"{caps['cpus']} cpu")
    tools = caps.get("tools")
    if isinstance(tools, list) and "claude" in tools:
        bits.append("claude ✓")
    return " · ".join(bits[:3])


def fmt_cost(cost: Optional[dict]) -> str:
    """"3.1k tok · $0.04" — empty string when there's nothing to show."""
    if not isinstance(cost, dict):
        return ""
    bits: list[str] = []
    tok = cost.get("tokens_used")
    if tok:
        bits.append(f"{tok / 1000:.1f}k tok" if tok >= 1000 else f"{tok} tok")
    usd = cost.get("cost_est_usd")
    if usd:
        bits.append(f"${usd:.2f}")
    return " · ".join(bits)


def fmt_size(n: Optional[int]) -> str:
    """Human bytes: "512 B", "1.2 KB", "3.4 MB"."""
    if n is None:
        return "—"
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{int(f)} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} GB"


def health_glyph(status: Optional[str]) -> str:
    return HEALTH_GLYPH.get(status or "", "·")


def phase_rail(phase: Optional[str]) -> str:
    """A textual trust-loop rail with the current phase marked, e.g.
    "queued · assigned · ▸running · verifying · done". Terminal phases collapse
    the tail to their verdict."""
    phase = phase or "queued"
    if phase in ("failed", "cancelled"):
        done = "failed" if phase == "failed" else "cancelled"
        return f"queued · assigned · running · verifying · ▸{done}"
    if phase in ("succeeded", "verified", "done"):
        rail = PHASE_RAIL[:-1] + ["✓done"]
        return " · ".join(rail[:-1]) + f" · ▸{rail[-1]}"
    rail = PHASE_RAIL[:]
    if phase == "self-healing" and "self-healing" not in rail:
        rail.insert(rail.index("verifying") + 1, "self-healing")
    out = []
    for p in rail:
        out.append(f"▸{p}" if p == phase else p)
    return " · ".join(out)


def truncate(s: Any, n: int) -> str:
    """Single-line, width-capped string with an ellipsis when clipped."""
    flat = " ".join(str(s if s is not None else "").split())
    if n <= 1:
        return flat[:n]
    return flat if len(flat) <= n else flat[: n - 1] + "…"


def run_title(run: dict) -> str:
    """Best display title for a row, tolerant of both the derived run shape
    (`goal_display`/`goal`) and a raw `/jobs` job object (`spec.task`/`intent`)."""
    spec = run.get("spec") or {}
    return (run.get("goal_display") or run.get("goal") or run.get("intent")
            or spec.get("task") or spec.get("intent")
            or run.get("run_id") or run.get("id") or "—")


def run_status(run: dict) -> Optional[str]:
    """The status to glyph/colour a row by — `health.status` on a derived run,
    else `phase`, else the raw job `state`."""
    return (run.get("health") or {}).get("status") or run.get("phase") or run.get("state")


def run_worker(run: dict) -> str:
    """Worker label, from the derived `worker` or a job object's `worker_id`."""
    return run.get("worker") or run.get("worker_id") or "—"


def run_cost(run: dict) -> dict:
    """A cost dict for fmt_cost, synthesised from a job object's top-level
    `tokens_used` when the derived `cost` block is absent."""
    cost = run.get("cost")
    if isinstance(cost, dict):
        return cost
    return {"tokens_used": run.get("tokens_used")}


class ListCursor:
    """A clamped selection index for a scrollable list. Pure; the screens drive
    it. `move(±1)` and `set_count(n)` keep `index` inside `[0, n)`."""

    def __init__(self) -> None:
        self.index = 0
        self._count = 0

    def set_count(self, n: int) -> None:
        self._count = max(0, n)
        self.clamp()

    def clamp(self) -> None:
        if self._count == 0:
            self.index = 0
        else:
            self.index = max(0, min(self.index, self._count - 1))

    def move(self, delta: int) -> None:
        if self._count:
            self.index = max(0, min(self.index + delta, self._count - 1))

    def selected(self, items: list) -> Optional[Any]:
        if items and 0 <= self.index < len(items):
            return items[self.index]
        return None


# --------------------------------------------------------------------------- #
# Console invocation — fleet-wired Claude Code, nothing global touched         #
# --------------------------------------------------------------------------- #

CONSOLE_CLAUDE_MD = """\
# Roost console

You are running inside `roost dash`'s Console, with this fleet already in your
hands: `ROOST_URL`/`ROOST_TOKEN` are set and the `roost` MCP server is attached,
so you can list workers/runs and dispatch work directly. The `roost` CLI is also
on PATH (`roost workers`, `roost do "<goal>"`, `roost run "<task>"`).

Ask me what's running, route work to the right node, or investigate a failed run.
"""


def build_console_files(url: str, token: str) -> dict[str, str]:
    """The generated, self-contained wiring for the Console (mac DESIGN §13):
    an MCP config pointing at `roost mcp` with this connection's env, and a
    CLAUDE.md giving the agent fleet context from message one. The user's own
    ~/.claude config is never edited."""
    mcp = {
        "mcpServers": {
            "roost": {
                "command": "roost",
                "args": ["mcp"],
                "env": {"ROOST_URL": url, "ROOST_TOKEN": token},
            }
        }
    }
    return {
        "mcp.json": json.dumps(mcp, indent=2),
        "CLAUDE.md": CONSOLE_CLAUDE_MD,
    }


def build_console_argv(mcp_path: str) -> list[str]:
    """The argv to launch Claude Code with the generated fleet MCP config."""
    return ["claude", "--mcp-config", mcp_path]


def console_dir() -> Path:
    return Path(os.path.expanduser("~/.config/roost/console"))


# --------------------------------------------------------------------------- #
# Client — one typed method per endpoint the dashboard consumes               #
# --------------------------------------------------------------------------- #

class TuiError(Exception):
    """A clean, user-facing error string for the footer flash."""


def _detail(resp: httpx.Response, verb: str) -> str:
    try:
        d = resp.json().get("detail")
    except Exception:
        d = None
    return f"{verb} failed: {d or f'HTTP {resp.status_code}'}"


class TuiClient:
    """Thin async-free httpx wrapper. Each method returns parsed JSON or raises
    TuiError with a footer-ready message. Reads are forgiving; writes are
    explicit user actions. The submit shape mirrors `roost run` exactly."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self._c = _client(url, token)

    def close(self) -> None:
        try:
            self._c.close()
        except Exception:
            pass

    def _json(self, resp: httpx.Response, verb: str) -> Any:
        if resp.status_code >= 400:
            raise TuiError(_detail(resp, verb))
        return resp.json()

    # reads -----------------------------------------------------------------
    def healthz(self) -> dict:
        return self._json(self._c.get("/healthz"), "ping")

    def derived(self, limit: int = 40) -> dict:
        return self._json(self._c.get("/derived", params={"limit": limit}), "snapshot")

    def workers(self) -> list[dict]:
        return self._json(self._c.get("/workers"), "list workers")

    def jobs(self, limit: int = 60) -> list[dict]:
        data = self._json(self._c.get("/jobs", params={"limit": limit}), "list jobs")
        # /jobs may return a bare list or {"jobs": [...]} depending on version.
        return data.get("jobs", data) if isinstance(data, dict) else data

    def job(self, job_id: str) -> dict:
        return self._json(self._c.get(f"/jobs/{job_id}"), "load job")

    def job_logs(self, job_id: str, since: int = 0, limit: int = 1000) -> dict:
        return self._json(
            self._c.get(f"/jobs/{job_id}/logs", params={"since": since, "limit": limit}),
            "load logs")

    def job_tree(self, job_id: str) -> Any:
        return self._json(self._c.get(f"/jobs/{job_id}/tree"), "load tree")

    def publish_list(self) -> list[dict]:
        return self._json(self._c.get("/publish"), "list sites")

    def schedules_list(self) -> list[dict]:
        return self._json(self._c.get("/schedules"), "list schedules")

    def blobs_list(self) -> list[dict]:
        data = self._json(self._c.get("/blobs"), "list transfers")
        return data.get("blobs", data) if isinstance(data, dict) else data

    # writes ----------------------------------------------------------------
    def submit_goal(self, goal: str, wallclock_min: int = 15) -> dict:
        body = {"kind": "auto", "task": goal, "verify": True,
                "budget": {"max_wallclock_min": wallclock_min, "max_tokens": 200000}}
        return self._json(self._c.post("/jobs", json=body), "submit")

    def submit_spec(self, spec: dict) -> dict:
        return self._json(self._c.post("/jobs", json=spec), "submit")

    def cancel(self, job_id: str, tree: bool = False) -> dict:
        params = {"tree": "true"} if tree else None
        return self._json(self._c.delete(f"/jobs/{job_id}", params=params), "cancel")

    def send_input(self, job_id: str, text: str) -> dict:
        return self._json(self._c.post(f"/jobs/{job_id}/input", json={"text": text}),
                          "send input")

    def schedule_create(self, goal: str, every: str, name: Optional[str] = None) -> dict:
        body: dict = {"spec": {"kind": "auto", "task": goal}, "every": every}
        if name:
            body["name"] = name
        return self._json(self._c.post("/schedules", json=body), "create schedule")

    def schedule_set_enabled(self, sid: str, enabled: bool) -> dict:
        return self._json(self._c.patch(f"/schedules/{sid}", json={"enabled": enabled}),
                          "update schedule")

    def schedule_delete(self, sid: str) -> dict:
        return self._json(self._c.delete(f"/schedules/{sid}"), "delete schedule")

    def blob_upload(self, name: str, data: bytes, ttl_sec: Optional[int] = None) -> dict:
        params: dict = {"name": name}
        if ttl_sec:
            params["ttl_sec"] = ttl_sec
        resp = self._c.post("/blobs", params=params, content=data,
                            headers={"Content-Type": "application/octet-stream"})
        return self._json(resp, "upload")

    def blob_delete(self, blob_id: str) -> dict:
        return self._json(self._c.delete(f"/blobs/{blob_id}"), "delete transfer")

    # streaming -------------------------------------------------------------
    def stream(self, job_id: str, since: int = 0) -> Iterator[tuple[str, dict]]:
        """Yield (event, data) SSE frames for a run's live feed. Caller runs this
        in a thread and tears it down by stopping iteration / closing."""
        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        with httpx.Client(base_url=self.url,
                          headers={"Authorization": f"Bearer {self.token}"}
                          if self.token else {},
                          timeout=timeout) as c:
            with c.stream("GET", f"/jobs/{job_id}/stream",
                          params={"since": since}) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    raise TuiError(_detail(resp, "stream"))
                yield from _iter_sse(resp)


# --------------------------------------------------------------------------- #
# Curses application                                                           #
# --------------------------------------------------------------------------- #

SCREENS = ["Fleet", "Runs", "Workers", "Transfers", "Publish", "Schedules", "Console"]


def run(ctx) -> None:
    """Entry point for `roost dash`. Resolves the connection, then hands off to
    the curses app. Kept tiny and import-safe: curses is imported here, not at
    module load, so the pure layer tests run without a TTY."""
    import curses
    from .cli import _resolve

    url, token, _ = _resolve(ctx)
    client = TuiClient(url, token)
    try:
        # Fail fast with a clean message if the CP is unreachable / unauthorized.
        client.healthz()
    except (httpx.HTTPError, TuiError) as e:
        client.close()
        import click
        raise click.ClickException(
            f"cannot reach control plane at {url}: {e}\n"
            "Check it's running (`roost up`) and your ROOST_URL/ROOST_TOKEN.")
    try:
        curses.wrapper(lambda scr: App(scr, client, url).loop())
    finally:
        client.close()


class App:
    def __init__(self, scr, client: TuiClient, url: str) -> None:
        import curses
        self.scr = scr
        self.client = client
        self.url = url
        self.screen = 0  # index into SCREENS
        self.lock = threading.Lock()

        # Shared snapshot (poll thread writes, main loop reads under lock).
        self.snapshot: dict = {}
        self.snapshot_at: float = 0.0
        self.snapshot_err: Optional[str] = None

        # Lazily-loaded per-screen lists, with fetch timestamps.
        self.workers: list[dict] = []
        self.publish: list[dict] = []
        self.schedules: list[dict] = []
        self.blobs: list[dict] = []
        self.jobs: list[dict] = []
        self._fetched: dict[str, float] = {}

        # Selection cursors per list.
        self.cur_runs = ListCursor()
        self.cur_workers = ListCursor()
        self.cur_publish = ListCursor()
        self.cur_schedules = ListCursor()
        self.cur_blobs = ListCursor()

        # Run detail (None = list mode).
        self.detail_id: Optional[str] = None
        self.detail_job: dict = {}
        self.log_lines: deque = deque(maxlen=5000)
        self.log_seq = 0
        self.log_follow = True
        self.log_scroll = 0  # lines scrolled up from bottom
        self._stream_stop: Optional[threading.Event] = None
        self._stream_thread: Optional[threading.Thread] = None

        self.flash: str = ""
        self.flash_until: float = 0.0
        self.running = True

        self._poll_stop = threading.Event()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)

    # ---- background poll --------------------------------------------------
    def _poll_loop(self) -> None:
        while not self._poll_stop.is_set():
            try:
                snap = self.client.derived(limit=40)
                with self.lock:
                    self.snapshot = snap
                    self.snapshot_at = time.time()
                    self.snapshot_err = None
            except (httpx.HTTPError, TuiError) as e:
                with self.lock:
                    self.snapshot_err = str(e)
            self._poll_stop.wait(2.0)

    def _set_flash(self, msg: str, secs: float = 3.0) -> None:
        self.flash = msg
        self.flash_until = time.time() + secs

    # ---- lazy list fetches ------------------------------------------------
    def _refresh_current(self, force: bool = False) -> None:
        name = SCREENS[self.screen]
        now = time.time()
        # Refresh a screen's list on entry, on 'r', and at most every 5s.
        if not force and now - self._fetched.get(name, 0) < 5.0:
            return
        try:
            if name == "Workers":
                self.workers = self.client.workers()
            elif name == "Publish":
                self.publish = self.client.publish_list()
            elif name == "Schedules":
                self.schedules = self.client.schedules_list()
            elif name == "Transfers":
                self.blobs = self.client.blobs_list()
            elif name == "Runs":
                self.jobs = self.client.jobs(limit=60)
            else:
                return
            self._fetched[name] = now
        except (httpx.HTTPError, TuiError) as e:
            self._set_flash(str(e))

    def _runs(self) -> list[dict]:
        with self.lock:
            return sort_runs(list(self.snapshot.get("runs", [])))

    def _snapshot_workers(self) -> list[dict]:
        with self.lock:
            return list(self.snapshot.get("workers", []))

    # ---- run detail / streaming ------------------------------------------
    def _open_detail(self, run: dict) -> None:
        job_id = run.get("run_id") or run.get("id")
        if not job_id:
            return
        self.detail_id = job_id
        self.detail_job = dict(run)
        self.log_lines = deque(maxlen=5000)
        self.log_seq = 0
        self.log_follow = True
        self.log_scroll = 0
        # Always page in catch-up logs first (works for terminal + active).
        try:
            page = self.client.job_logs(job_id, since=0, limit=1000)
            for row in page.get("logs", []):
                self._ingest_log(row)
        except (httpx.HTTPError, TuiError) as e:
            self._set_flash(str(e))
        # Live runs additionally attach an SSE stream.
        if run.get("state") not in TERMINAL_STATES:
            self._start_stream(job_id, since=self.log_seq)

    def _ingest_log(self, row: dict) -> None:
        seq = row.get("seq", 0)
        if seq and seq <= self.log_seq:
            return  # dup-suppress across catch-up / stream boundary
        if seq:
            self.log_seq = seq
        stream = row.get("stream")
        if stream == "event":
            return  # roost-internal envelopes — noise in the distilled view
        line = distill_log_line(row.get("data", ""))
        if line:
            for sub in line.split("\n"):
                self.log_lines.append(sub)

    def _start_stream(self, job_id: str, since: int) -> None:
        stop = threading.Event()
        self._stream_stop = stop

        def worker() -> None:
            try:
                for event, data in self.client.stream(job_id, since=since):
                    if stop.is_set():
                        return
                    with self.lock:
                        if event == "log":
                            self._ingest_log(data)
                        elif event == "state":
                            self.detail_job["state"] = data.get("state")
                        elif event == "done":
                            self.detail_job["state"] = data.get("state")
                            self.detail_job["phase"] = data.get("state")
                            return
                        elif event == "error":
                            self._set_flash(data.get("error", "stream error"))
                            return
            except (httpx.HTTPError, TuiError):
                pass  # detail still shows the catch-up page; footer stays calm

        self._stream_thread = threading.Thread(target=worker, daemon=True)
        self._stream_thread.start()

    def _close_detail(self) -> None:
        if self._stream_stop:
            self._stream_stop.set()
        self._stream_stop = None
        self._stream_thread = None
        self.detail_id = None
        self.detail_job = {}

    # ---- main loop --------------------------------------------------------
    def loop(self) -> None:
        import curses
        curses.curs_set(0)
        self.scr.nodelay(True)
        self.scr.timeout(200)
        self._init_colors()
        self._poll_thread.start()
        self._refresh_current(force=True)
        while self.running:
            try:
                self._draw()
            except curses.error:
                pass  # transient resize/overflow — next tick repaints
            ch = self.scr.getch()
            if ch != -1:
                self._handle_key(ch)
        self._poll_stop.set()
        if self._stream_stop:
            self._stream_stop.set()

    def _init_colors(self) -> None:
        import curses
        self.color = {}
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        pairs = [
            ("ok", curses.COLOR_GREEN), ("warn", curses.COLOR_YELLOW),
            ("bad", curses.COLOR_RED), ("run", curses.COLOR_CYAN),
            ("verify", curses.COLOR_MAGENTA), ("dim", curses.COLOR_WHITE),
            ("accent", curses.COLOR_BLUE),
        ]
        for i, (name, fg) in enumerate(pairs, start=1):
            try:
                curses.init_pair(i, fg, -1)
                self.color[name] = curses.color_pair(i)
            except curses.error:
                self.color[name] = 0

    def _c(self, name: str):
        return self.color.get(name, 0)

    # ---- key handling -----------------------------------------------------
    def _handle_key(self, ch: int) -> None:
        import curses
        # Detail mode has its own keymap.
        if self.detail_id is not None:
            self._handle_detail_key(ch)
            return
        if ch in (ord("q"), 27):  # q / ESC
            self.running = False
        elif ch in (ord("\t"), curses.KEY_RIGHT):
            self._switch(self.screen + 1)
        elif ch == curses.KEY_LEFT or ch == curses.KEY_BTAB:
            self._switch(self.screen - 1)
        elif ord("1") <= ch <= ord("7"):
            self._switch(ch - ord("1"))
        elif ch == ord("r"):
            self._refresh_current(force=True)
            self._set_flash("refreshed")
        elif ch == ord("g"):
            self._goal_box()
        elif ch in (curses.KEY_UP, ord("k")):
            self._cursor().move(-1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self._cursor().move(1)
        elif ch in (curses.KEY_ENTER, 10, 13):
            self._activate()
        else:
            self._handle_screen_key(ch)

    def _switch(self, idx: int) -> None:
        idx = max(0, min(idx, len(SCREENS) - 1))
        if SCREENS[idx] == "Console":
            self._launch_console()
            return
        self.screen = idx
        self._refresh_current(force=True)

    def _cursor(self) -> ListCursor:
        name = SCREENS[self.screen]
        return {
            "Fleet": self.cur_runs, "Runs": self.cur_runs,
            "Workers": self.cur_workers, "Publish": self.cur_publish,
            "Schedules": self.cur_schedules, "Transfers": self.cur_blobs,
        }.get(name, self.cur_runs)

    def _activate(self) -> None:
        name = SCREENS[self.screen]
        if name == "Fleet":
            runs = self._runs()
            self.cur_runs.set_count(len(runs))
            run = self.cur_runs.selected(runs)
            if run:
                self._open_detail(run)
        elif name == "Runs":
            self.cur_runs.set_count(len(self.jobs))
            run = self.cur_runs.selected(self.jobs)
            if run:
                self._open_detail(run)

    def _handle_screen_key(self, ch: int) -> None:
        name = SCREENS[self.screen]
        if name == "Schedules":
            if ch == ord("n"):
                self._new_schedule()
            elif ch == ord("e"):
                self._toggle_schedule()
            elif ch == ord("x"):
                self._delete_schedule()
        elif name == "Transfers":
            if ch == ord("s"):
                self._send_file()
            elif ch == ord("x"):
                self._delete_blob()

    def _handle_detail_key(self, ch: int) -> None:
        import curses
        if ch in (ord("q"), 27, curses.KEY_LEFT, ord("h")):
            self._close_detail()
        elif ch in (curses.KEY_UP, ord("k")):
            self.log_follow = False
            self.log_scroll += 1
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.log_scroll = max(0, self.log_scroll - 1)
            if self.log_scroll == 0:
                self.log_follow = True
        elif ch == ord("f"):
            self.log_follow = not self.log_follow
            self.log_scroll = 0
        elif ch == ord("c"):
            self._cancel_detail()
        elif ch == ord("R"):
            self._retry_detail()
        elif ch == ord("i"):
            self._followup_input()

    # ---- actions (modal prompts) -----------------------------------------
    def _goal_box(self) -> None:
        goal = self._prompt("Tell your fleet what to do")
        if not goal:
            return
        try:
            job = self.client.submit_goal(goal)
            self._set_flash(f"submitted {job.get('id', '')[:12]} — verifying on the fleet")
        except (httpx.HTTPError, TuiError) as e:
            self._set_flash(str(e), 5)

    def _cancel_detail(self) -> None:
        if not self.detail_id:
            return
        if not self._confirm(f"Cancel run {self.detail_id[:12]}?"):
            return
        try:
            self.client.cancel(self.detail_id, tree=True)
            self._set_flash("cancel requested")
        except (httpx.HTTPError, TuiError) as e:
            self._set_flash(str(e), 5)

    def _retry_detail(self) -> None:
        # Retry = resubmit the goal as a NEW run (the app never mutates a finished job).
        goal = run_title(self.detail_job)
        try:
            full = self.client.job(self.detail_id)
            spec = full.get("spec") or {}
            goal = spec.get("task") or spec.get("intent") or goal
        except (httpx.HTTPError, TuiError):
            pass
        if not self._confirm(f"Re-run “{truncate(goal, 40)}” as a new job?"):
            return
        try:
            job = self.client.submit_goal(goal)
            self._set_flash(f"resubmitted as {job.get('id', '')[:12]}")
            self._close_detail()
        except (httpx.HTTPError, TuiError) as e:
            self._set_flash(str(e), 5)

    def _followup_input(self) -> None:
        if not self.detail_id:
            return
        text = self._prompt("Follow-up input")
        if not text:
            return
        try:
            self.client.send_input(self.detail_id, text)
            self._set_flash("input queued (command jobs only; agents run stdin-closed)")
        except (httpx.HTTPError, TuiError) as e:
            self._set_flash(str(e), 5)

    def _new_schedule(self) -> None:
        goal = self._prompt("Schedule goal")
        if not goal:
            return
        every = self._prompt("Interval (e.g. 30m, 6h, 1d)")
        if not every:
            return
        name = self._prompt("Name (optional)") or None
        try:
            s = self.client.schedule_create(goal, every, name)
            self._set_flash(f"scheduled {s.get('id', '')[:12]} every {every}")
            self._refresh_current(force=True)
        except (httpx.HTTPError, TuiError) as e:
            self._set_flash(str(e), 5)

    def _toggle_schedule(self) -> None:
        s = self.cur_schedules.selected(self.schedules)
        if not s:
            return
        try:
            self.client.schedule_set_enabled(s["id"], not s.get("enabled", True))
            self._set_flash("schedule toggled (re-enabling restarts the clock)")
            self._refresh_current(force=True)
        except (httpx.HTTPError, TuiError) as e:
            self._set_flash(str(e), 5)

    def _delete_schedule(self) -> None:
        s = self.cur_schedules.selected(self.schedules)
        if not s:
            return
        if not self._confirm(f"Delete schedule {s.get('name') or s['id'][:12]}?"):
            return
        try:
            self.client.schedule_delete(s["id"])
            self._set_flash("schedule deleted")
            self._refresh_current(force=True)
        except (httpx.HTTPError, TuiError) as e:
            self._set_flash(str(e), 5)

    def _send_file(self) -> None:
        path = self._prompt("File to stage (local path)")
        if not path:
            return
        p = Path(os.path.expanduser(path))
        if not p.is_file():
            self._set_flash(f"not a file: {p}", 5)
            return
        try:
            data = p.read_bytes()
            b = self.client.blob_upload(p.name, data)
            self._set_flash(f"staged {p.name} ({fmt_size(b.get('size'))}) — id {b.get('id', '')[:12]}")
            self._refresh_current(force=True)
        except (httpx.HTTPError, TuiError, OSError) as e:
            self._set_flash(str(e), 5)

    def _delete_blob(self) -> None:
        b = self.cur_blobs.selected(self.blobs)
        if not b:
            return
        if not self._confirm(f"Delete staged blob {b.get('name') or b['id'][:12]}?"):
            return
        try:
            self.client.blob_delete(b["id"])
            self._set_flash("blob deleted")
            self._refresh_current(force=True)
        except (httpx.HTTPError, TuiError) as e:
            self._set_flash(str(e), 5)

    def _launch_console(self) -> None:
        """Suspend curses and hand the terminal to a fleet-wired Claude Code
        session; resume the dashboard when it exits (mac DESIGN §13)."""
        import curses
        import shutil
        import subprocess
        cdir = console_dir()
        cdir.mkdir(parents=True, exist_ok=True)
        files = build_console_files(self.url, self.client.token)
        for fname, content in files.items():
            (cdir / fname).write_text(content)
        have_claude = shutil.which("claude") is not None
        curses.endwin()
        if not have_claude:
            print("\n[roost console] `claude` not found on PATH.\n"
                  "Install Claude Code, then reopen the Console. Dropping to a\n"
                  "fleet-wired shell instead (ROOST_URL/ROOST_TOKEN exported).\n")
        env = dict(os.environ)
        env["ROOST_URL"] = self.url
        env["ROOST_TOKEN"] = self.client.token
        try:
            if have_claude:
                argv = build_console_argv(str(cdir / "mcp.json"))
                subprocess.run(argv, cwd=str(cdir), env=env)
            else:
                shell = os.environ.get("SHELL", "/bin/bash")
                subprocess.run([shell], cwd=str(cdir), env=env)
        except (OSError, KeyboardInterrupt):
            pass
        # Resume curses.
        self.scr.clear()
        self.scr.refresh()
        self._set_flash("console closed")

    # ---- modal text / confirm prompts (block the main loop briefly) -------
    def _prompt(self, label: str, initial: str = "") -> Optional[str]:
        import curses
        h, w = self.scr.getmaxyx()
        buf = list(initial)
        self.scr.timeout(-1)
        curses.curs_set(1)
        try:
            while True:
                y = h - 2
                self.scr.move(y, 0)
                self.scr.clrtoeol()
                prompt = f" {label}: "
                text = "".join(buf)
                shown = text[-(w - len(prompt) - 2):]
                self._safe_addstr(y, 0, prompt, self._c("accent") | curses.A_BOLD)
                self._safe_addstr(y, len(prompt), shown)
                self.scr.refresh()
                ch = self.scr.getch()
                if ch in (10, 13, curses.KEY_ENTER):
                    return "".join(buf).strip()
                if ch == 27:  # ESC cancels
                    return None
                if ch in (curses.KEY_BACKSPACE, 127, 8):
                    if buf:
                        buf.pop()
                elif 32 <= ch < 127:
                    buf.append(chr(ch))
        finally:
            curses.curs_set(0)
            self.scr.timeout(200)

    def _confirm(self, question: str) -> bool:
        import curses
        h, w = self.scr.getmaxyx()
        self.scr.timeout(-1)
        try:
            y = h - 2
            self.scr.move(y, 0)
            self.scr.clrtoeol()
            self._safe_addstr(y, 0, f" {question} (y/N) ",
                              self._c("warn") | curses.A_BOLD)
            self.scr.refresh()
            ch = self.scr.getch()
            return ch in (ord("y"), ord("Y"))
        finally:
            self.scr.timeout(200)

    # ---- drawing ----------------------------------------------------------
    def _safe_addstr(self, y: int, x: int, s: str, attr: int = 0) -> None:
        import curses
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        s = s[: max(0, w - x - 1)]
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:
            pass

    def _draw(self) -> None:
        import curses
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        self._draw_header(w)
        self._draw_tabs(w)
        if self.detail_id is not None:
            self._draw_detail(3, h, w)
        else:
            name = SCREENS[self.screen]
            body = {
                "Fleet": self._draw_fleet, "Runs": self._draw_runs,
                "Workers": self._draw_workers, "Transfers": self._draw_transfers,
                "Publish": self._draw_publish, "Schedules": self._draw_schedules,
            }.get(name)
            if body:
                body(3, h, w)
        self._draw_footer(h, w)
        self.scr.noutrefresh()
        curses.doupdate()

    def _draw_header(self, w: int) -> None:
        import curses
        with self.lock:
            snap = self.snapshot
            err = self.snapshot_err
            at = self.snapshot_at
        verdict = snap.get("fleet_verdict", {}) if snap else {}
        level = verdict.get("level", "ok")
        workers = snap.get("workers", []) if snap else []
        if err and not snap:
            self._safe_addstr(0, 0, f" ✗ control plane unreachable — {truncate(err, w - 6)}",
                              self._c("bad") | curses.A_BOLD)
            return
        col = {"ok": "ok", "alert": "bad", "warn": "warn"}.get(level, "warn")
        dot = {"ok": "●", "alert": "✗", "warn": "▲"}.get(level, "▲")
        summary = verdict.get("summary") or f"fleet: {level}"
        chip = nodes_chip(workers)
        stale = ""
        if at and time.time() - at > 10:
            stale = f"  (snapshot {relative_time(at)})"
        left = f" {dot} {summary}"
        self._safe_addstr(0, 0, left, self._c(col) | curses.A_BOLD)
        right = f"🐦 Roost · {chip}{stale} "
        self._safe_addstr(0, max(0, w - len(right)), right, self._c("dim"))

    def _draw_tabs(self, w: int) -> None:
        import curses
        x = 1
        for i, name in enumerate(SCREENS):
            label = f" {i+1} {name} "
            attr = (self._c("accent") | curses.A_REVERSE | curses.A_BOLD
                    if i == self.screen else self._c("dim"))
            self._safe_addstr(1, x, label, attr)
            x += len(label) + 1
        self._safe_addstr(2, 0, "─" * (w - 1), self._c("dim"))

    def _phase_attr(self, status: Optional[str]):
        import curses
        if status in ("verified", "done"):
            return self._c("ok")
        if status in ("failed", "unplaceable"):
            return self._c("bad")
        if status in ("running", "assigned"):
            return self._c("run")
        if status in ("verifying", "self-healing"):
            return self._c("verify")
        if status in ("waiting", "unverified", "stuck?"):
            return self._c("warn")
        return self._c("dim")

    def _draw_run_row(self, y: int, w: int, run: dict, selected: bool) -> None:
        import curses
        status = run_status(run)
        glyph = health_glyph(status)
        title = run_title(run)
        worker = run_worker(run)
        elapsed = ""
        if run.get("created_at"):
            end = run.get("finished_at") or time.time()
            elapsed = fmt_duration(end - run["created_at"])
        prog = run.get("progress")
        prog_s = f" {int(prog)}%" if isinstance(prog, (int, float)) else ""
        cost = fmt_cost(run_cost(run))
        marker = "▌" if selected else " "
        attr = self._phase_attr(status) | (curses.A_BOLD if selected else 0)
        meta = f"{(status or ''):<11} {truncate(worker,10):<10} {elapsed:>5}{prog_s}"
        line = f"{marker}{glyph} {truncate(title, w - 40):<{max(1, w - 40)}} {meta}"
        self._safe_addstr(y, 0, line[: w - 1], attr)
        if cost:
            self._safe_addstr(y, max(0, w - len(cost) - 1), cost, self._c("dim"))

    def _draw_fleet(self, top: int, h: int, w: int) -> None:
        import curses
        runs = self._runs()
        active, recent = split_runs(runs)
        workers = self._snapshot_workers()
        self.cur_runs.set_count(len(runs))
        sel = self.cur_runs.index
        y = top
        ordered: list[dict] = []

        self._safe_addstr(y, 0, " ACTIVE", self._c("accent") | curses.A_BOLD)
        y += 1
        if not active:
            self._safe_addstr(y, 2, "no active runs — press g to give your fleet a goal",
                              self._c("dim"))
            y += 1
        for run in active[: max(0, (h - top) // 3)]:
            if y >= h - 6:
                break
            self._draw_run_row(y, w, run, len(ordered) == sel)
            ordered.append(run)
            y += 1
            narr = run.get("narration")
            if narr:
                self._safe_addstr(y, 4, truncate(narr, w - 6), self._c("run"))
                y += 1
        y += 1
        self._safe_addstr(y, 0, " RECENT", self._c("accent") | curses.A_BOLD)
        y += 1
        for run in recent[: 6]:
            if y >= h - 4:
                break
            self._draw_run_row(y, w, run, len(ordered) == sel)
            ordered.append(run)
            y += 1
        # Workers strip at the bottom.
        wy = h - 3
        self._safe_addstr(wy, 0, " WORKERS  " + truncate(
            "   ".join(self._worker_chip(x) for x in workers[:6]) or "none enrolled",
            w - 12), self._c("dim"))
        # active + recent preserves sort_runs() order, so the cursor index
        # aligns 1:1 with `runs` — _activate() can index it directly.
        self.cur_runs.set_count(len(runs))

    def _worker_chip(self, w: dict) -> str:
        st = worker_live_status(w)
        dot = {"idle": "●", "busy": "●", "stale": "◐", "offline": "○"}.get(st, "○")
        return f"{dot} {truncate(w.get('name', '?'), 10)} {st}"

    def _draw_runs(self, top: int, h: int, w: int) -> None:
        runs = self.jobs
        self.cur_runs.set_count(len(runs))
        sel = self.cur_runs.index
        self._safe_addstr(top, 0, f" RUNS — {len(runs)} jobs (↑↓ select · ⏎ open)",
                          self._c("accent"))
        y = top + 1
        start = max(0, sel - (h - top - 4))
        for i, run in enumerate(runs[start:], start=start):
            if y >= h - 2:
                break
            self._draw_run_row(y, w, run, i == sel)
            y += 1

    def _draw_workers(self, top: int, h: int, w: int) -> None:
        import curses
        workers = self.workers or self._snapshot_workers()
        self.cur_workers.set_count(len(workers))
        sel = self.cur_workers.index
        self._safe_addstr(top, 0,
                          f" {'NAME':<18}{'STATUS':<9}{'LOAD':<8}{'SEEN':<9}CAPABILITIES",
                          self._c("dim") | curses.A_BOLD)
        y = top + 1
        for i, wk in enumerate(workers):
            if y >= h - 2:
                break
            st = worker_live_status(wk)
            attr = {"idle": "ok", "busy": "warn", "stale": "warn",
                    "offline": "dim"}.get(st, "dim")
            load = f"{wk.get('running', 0)}/{wk.get('capacity', 1)}"
            seen = relative_time(wk.get("last_seen"))
            caps = worker_caps_short(wk.get("capabilities", {}))
            marker = "▌" if i == sel else " "
            row = (f"{marker}{truncate(wk.get('name', '?'),17):<17}{st:<9}"
                   f"{load:<8}{seen:<9}{caps}")
            self._safe_addstr(y, 0, row[: w - 1],
                              self._c(attr) | (curses.A_BOLD if i == sel else 0))
            y += 1

    def _draw_transfers(self, top: int, h: int, w: int) -> None:
        import curses
        blobs = self.blobs
        self.cur_blobs.set_count(len(blobs))
        sel = self.cur_blobs.index
        self._safe_addstr(top, 0, " TRANSFERS — staged blobs (s send file · x delete)",
                          self._c("accent"))
        self._safe_addstr(top + 1, 0,
                          f" {'NAME':<26}{'SIZE':<10}{'EXPIRES':<12}ID",
                          self._c("dim") | curses.A_BOLD)
        y = top + 2
        if not blobs:
            self._safe_addstr(y, 2, "nothing staged — press s to stage a local file",
                              self._c("dim"))
            return
        for i, b in enumerate(blobs):
            if y >= h - 2:
                break
            marker = "▌" if i == sel else " "
            row = (f"{marker}{truncate(b.get('name', '?'),25):<25}"
                   f"{fmt_size(b.get('size')):<10}"
                   f"{relative_time(b.get('expires_at')):<12}{b.get('id','')[:12]}")
            self._safe_addstr(y, 0, row[: w - 1],
                              self._c("dim") | (curses.A_BOLD if i == sel else 0))
            y += 1

    def _draw_publish(self, top: int, h: int, w: int) -> None:
        import curses
        sites = self.publish
        self.cur_publish.set_count(len(sites))
        sel = self.cur_publish.index
        self._safe_addstr(top, 0, " PUBLISH — live sites", self._c("accent"))
        self._safe_addstr(top + 1, 0,
                          f" {'SLUG':<20}{'FILES':<7}{'SIZE':<10}{'UPDATED':<10}URL",
                          self._c("dim") | curses.A_BOLD)
        y = top + 2
        if not sites:
            self._safe_addstr(y, 2, "no sites published yet", self._c("dim"))
            return
        for i, s in enumerate(sites):
            if y >= h - 2:
                break
            marker = "▌" if i == sel else " "
            url = s.get("public_url") or s.get("url") or ""
            row = (f"{marker}{truncate(s.get('slug', '?'),19):<19}"
                   f"{str(s.get('files','—')):<7}{fmt_size(s.get('size')):<10}"
                   f"{relative_time(s.get('updated_at')):<10}{url}")
            self._safe_addstr(y, 0, row[: w - 1],
                              self._c("ok") | (curses.A_BOLD if i == sel else 0))
            y += 1

    def _draw_schedules(self, top: int, h: int, w: int) -> None:
        import curses
        scheds = self.schedules
        self.cur_schedules.set_count(len(scheds))
        sel = self.cur_schedules.index
        self._safe_addstr(top, 0,
                          " SCHEDULES — interval jobs (n new · e enable/disable · x delete)",
                          self._c("accent"))
        self._safe_addstr(top + 1, 0,
                          f" {'NAME/ID':<22}{'EVERY':<8}{'ON':<4}{'NEXT':<10}GOAL",
                          self._c("dim") | curses.A_BOLD)
        y = top + 2
        if not scheds:
            self._safe_addstr(y, 2, "no schedules — press n to create one", self._c("dim"))
            return
        for i, s in enumerate(scheds):
            if y >= h - 2:
                break
            marker = "▌" if i == sel else " "
            label = s.get("name") or s.get("id", "")[:12]
            every = fmt_duration(s.get("interval_sec"))
            on = "on" if s.get("enabled") else "off"
            nxt = relative_time(s.get("next_run_at")) if s.get("enabled") else "—"
            spec = s.get("spec", {})
            goal = spec.get("task") or spec.get("intent") or ""
            row = (f"{marker}{truncate(label,21):<21}{every:<8}{on:<4}{nxt:<10}"
                   f"{truncate(goal, max(1, w - 46))}")
            attr = self._c("ok") if s.get("enabled") else self._c("dim")
            self._safe_addstr(y, 0, row[: w - 1],
                              attr | (curses.A_BOLD if i == sel else 0))
            y += 1

    def _draw_detail(self, top: int, h: int, w: int) -> None:
        import curses
        with self.lock:
            job = dict(self.detail_job)
            lines = list(self.log_lines)
        status = run_status(job)
        title = run_title(job)
        self._safe_addstr(top, 0, f" ← {truncate(title, w - 6)}",
                          self._phase_attr(status) | curses.A_BOLD)
        worker = run_worker(job)
        sub = f"   {job.get('state', '?')} on {worker} · {self.detail_id[:12]}"
        self._safe_addstr(top + 1, 0, sub, self._c("dim"))
        self._safe_addstr(top + 2, 0, "   " + phase_rail(job.get("phase") or job.get("state")),
                          self._phase_attr(status))
        narr = job.get("narration")
        ly = top + 4
        if narr:
            self._safe_addstr(top + 3, 3, truncate(narr, w - 6), self._c("run"))
        # Terminal outcome block.
        st = job.get("state")
        if st in TERMINAL_STATES:
            if job.get("verified"):
                ev = job.get("evidence") or job.get("result") or ""
                self._safe_addstr(ly, 2, "✓ Verified — " + truncate(ev, w - 16),
                                  self._c("ok") | curses.A_BOLD)
            elif st == "failed":
                diag = job.get("diagnosis") or job.get("result") or job.get("error") or ""
                self._safe_addstr(ly, 2, "✗ Failed — " + truncate(diag, w - 14),
                                  self._c("bad") | curses.A_BOLD)
            elif st == "cancelled":
                self._safe_addstr(ly, 2, "− Cancelled", self._c("dim"))
            else:
                self._safe_addstr(ly, 2, "✓ Succeeded (not verified)", self._c("warn"))
            ly += 1
        follow = "FOLLOW" if self.log_follow else "PAUSED"
        self._safe_addstr(ly, 0, f" LOGS [{follow}]  (f follow · ↑↓ scroll)",
                          self._c("accent"))
        ly += 1
        avail = h - ly - 1
        if avail < 1:
            return
        if self.log_follow:
            view = lines[-avail:]
        else:
            end = len(lines) - self.log_scroll
            view = lines[max(0, end - avail):max(0, end)]
        for line in view:
            self._safe_addstr(ly, 1, line[: w - 2], self._c("dim"))
            ly += 1

    def _draw_footer(self, h: int, w: int) -> None:
        import curses
        if self.flash and time.time() < self.flash_until:
            self._safe_addstr(h - 1, 0, " " + truncate(self.flash, w - 2),
                              self._c("warn") | curses.A_BOLD)
            return
        if self.detail_id is not None:
            keys = "q back · c cancel · R re-run · i input · f follow · ↑↓ scroll"
        else:
            keys = ("1-7/Tab screens · ↑↓ select · ⏎ open · g goal · "
                    "r refresh · q quit")
        self._safe_addstr(h - 1, 0, " " + keys, self._c("dim"))
