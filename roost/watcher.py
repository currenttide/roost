"""Narration watcher — the always-on observability narrator (ease-of-use-plan
Part II, Phase D2).

For each currently-running agentic job, this produces, on a cheap cadence, a
ONE-SENTENCE plain-language *narration* of what the job is doing and whether it
looks healthy, plus a rough *progress* (0-100) and *eta_sec* estimate. It does
this with a single cheap Sonnet ``claude -p`` call reading the job's
``last_activity`` + a tail of its logs. The results are CACHED onto the job so
the dashboard / panel / MCP inbox render them with zero LLM calls in the request
path — the deterministic D0 health verdict (``_job_health``) stays the
always-available ground truth; this layer is the human-friendly gloss on top.

Design: this module is deliberately self-contained. It has **no** direct DB or
server import — the job source, the persistence ``store`` callback, and the LLM
``run_claude`` callable are all injected. The pure helpers
(:func:`render_narration_prompt`, :func:`parse_narration`,
:func:`jobs_needing_narration`) are unit-tested; the live agent path
(:func:`default_claude_runner`) is validated on the fleet.

Integration is wired by the server's sweep loop (see module docstring of
``roost.server`` / the integration recipe): the loop gathers running jobs, calls
:func:`watch_once`, and the injected ``store`` writes the cached fields back to
the ``jobs`` row.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from typing import Any, Awaitable, Callable, Optional

# Same cheap model the worker uses for one-shot helper agents (verifier, fixer).
NARRATION_MODEL = "claude-sonnet-4-6"

# Default re-narration cadence (seconds). A job is re-narrated only if its cached
# narration is missing or older than this — keeps LLM spend bounded regardless of
# how often the sweep loop runs.
DEFAULT_MIN_INTERVAL = 20.0

# How much of the job's recent log tail to feed the narrator (chars). Small on
# purpose: the narrator only needs the *recent* gist, and small prompts are cheap
# and fast.
LOG_TAIL_CHARS = 1600

# Subprocess timeout for the narration agent (seconds). Narration is best-effort
# and must never wedge the sweep loop, so this is short.
NARRATE_TIMEOUT_S = 45.0

# States we narrate. A job only has live "what is it doing" worth narrating while
# it is actively assigned/running; terminal jobs keep their last cached value.
ACTIVE_STATES = ("running", "assigned")


# ---------- Pure helpers (unit-tested) ----------


def render_narration_prompt(
    goal: str,
    last_activity: Optional[str],
    log_tail: Optional[str],
    elapsed_sec: Optional[float],
) -> str:
    """Build the narrator's prompt. Pure / deterministic.

    Asks the model to act as an observer of a *running* agent job and emit a
    small JSON object: a one-sentence narration, a rough progress percent, and a
    rough ETA. The goal is always included so the narration is grounded in what
    the job was actually asked to do.
    """
    goal = (goal or "").strip() or "(no goal recorded)"
    act = (last_activity or "").strip() or "(no recent activity line)"
    tail = (log_tail or "").strip() or "(no log output yet)"
    if elapsed_sec is None:
        elapsed = "unknown"
    else:
        elapsed = f"{int(max(0, elapsed_sec))}s"

    return (
        "You are observing ONE long-running automated agent job on a compute "
        "fleet. You did not do the work; you only narrate it for a human "
        "watching a dashboard.\n\n"
        "CRITICAL GROUNDING RULE: describe ONLY what is explicitly present in "
        "the input below (the goal, the elapsed time, the most recent activity "
        "line, and the recent log tail). Do NOT invent, speculate about, or "
        "infer facts that are not literally in that input. In particular, never "
        "mention rate limits, quotas, percentages, error counts, causes, "
        "warnings, or any specific numbers unless those exact facts appear in "
        "the provided activity or log tail. If you are tempted to explain WHY "
        "something is happening, stop — only report WHAT the input shows.\n\n"
        f"GOAL (what the job was asked to do):\n{goal}\n\n"
        f"TIME RUNNING SO FAR: {elapsed}\n\n"
        f"MOST RECENT ACTIVITY LINE:\n{act}\n\n"
        f"RECENT LOG TAIL (last output, may be truncated):\n{tail}\n\n"
        "Write the narration as ONE plain factual sentence stating what the job "
        "appears to be doing RIGHT NOW, based only on the above. If the input is "
        "sparse, be honest and minimal — e.g. \"just started, no output yet\" or "
        "\"running; latest step: <quote the actual last activity line>\". You MAY "
        "note the job looks stalled ONLY if the elapsed time is large AND the "
        "most recent activity line is old or unchanged; phrase that as a neutral "
        "observation (\"no new output for a while\"), not a diagnosis of a cause.\n\n"
        "Respond with ONLY a JSON object (no prose, no markdown fences) of "
        "exactly this shape:\n"
        '{"narration": "<ONE grounded, plain-language sentence, no newlines>", '
        '"progress": <integer 0-100, a ROUGH best guess of percent complete, or '
        'null>, '
        '"eta_sec": <integer ROUGH best guess of seconds until done, or null>}\n'
        "progress and eta_sec are rough estimates only; prefer null over a guess "
        "you cannot justify from the input. Never invent facts to fill the "
        "narration."
    )


def _coerce_pct(v: Any) -> Optional[int]:
    """Best-effort coerce a value to an int in [0, 100], else None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, n))


def _coerce_sec(v: Any) -> Optional[int]:
    """Best-effort coerce a value to a non-negative int (seconds), else None."""
    if v is None or isinstance(v, bool):
        return None
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _safe_default() -> dict:
    return {"narration": "", "progress": None, "eta_sec": None}


def _extract_json_object(text: str) -> Optional[dict]:
    """Pull the first plausible JSON object out of arbitrary model output.

    Tolerant of: clean JSON, JSON inside ``` fences, and JSON embedded in prose
    with leading/trailing chatter. Returns the parsed dict, or None if nothing
    parseable is found. Never raises.
    """
    if not text:
        return None
    # Fast path: the whole thing is a JSON object.
    s = text.strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, TypeError):
        pass
    # Scan for balanced {...} spans and try to parse each, preferring the first
    # one that yields a dict (handles fences + surrounding prose). We walk every
    # '{' so a leading non-JSON brace doesn't sink a later valid object.
    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except (json.JSONDecodeError, TypeError):
                        pass
                    break  # this '{' didn't yield a dict; try the next '{'
    return None


def parse_narration(model_output: str) -> dict:
    """Parse narrator output into ``{narration, progress, eta_sec}``.

    Tolerant of prose, JSON-in-fences, and garbage — ALWAYS returns the full
    dict shape with safe defaults and NEVER raises. ``narration`` is a single
    trimmed line (newlines collapsed); ``progress`` is an int in [0,100] or
    None; ``eta_sec`` is a non-negative int or None.
    """
    out = _safe_default()
    obj = _extract_json_object(model_output or "")
    if obj is not None:
        narr = obj.get("narration")
        if isinstance(narr, str) and narr.strip():
            out["narration"] = " ".join(narr.split()).strip()
        out["progress"] = _coerce_pct(obj.get("progress"))
        out["eta_sec"] = _coerce_sec(obj.get("eta_sec"))
        if out["narration"]:
            return out
    # No JSON narration recovered — salvage a one-liner from the raw prose so the
    # dashboard still shows *something* human, rather than an empty cell.
    if not out["narration"]:
        for line in (model_output or "").splitlines():
            line = line.strip().strip("`").strip()
            if line and not line.startswith("{") and not line.startswith("}"):
                out["narration"] = line[:240]
                break
    return out


def jobs_needing_narration(
    jobs: list[dict], now: float, *, min_interval: float = DEFAULT_MIN_INTERVAL
) -> list[dict]:
    """Select active jobs whose cached narration is missing or stale.

    A job qualifies when its ``state`` is running/assigned AND its
    ``narrated_at`` timestamp is missing or older than ``min_interval`` seconds.
    Terminal jobs (succeeded/failed/cancelled) and queued jobs are skipped —
    there is no live activity to narrate.
    """
    out: list[dict] = []
    for j in jobs:
        if not j or j.get("state") not in ACTIVE_STATES:
            continue
        ts = j.get("narrated_at")
        if ts is None:
            out.append(j)
            continue
        try:
            age = now - float(ts)
        except (TypeError, ValueError):
            out.append(j)
            continue
        if age >= min_interval:
            out.append(j)
    return out


# ---------- Job → prompt inputs ----------


def _job_goal(job: dict) -> str:
    """The job's goal text, mirroring the server's ``_goal_text`` precedence."""
    spec = job.get("spec") or {}
    if isinstance(spec, dict):
        g = spec.get("task") or spec.get("intent") or spec.get("command") or ""
    else:
        g = ""
    if not g:
        g = job.get("intent") or ""
    if isinstance(g, (list, tuple)):
        g = " ".join(str(x) for x in g)
    return str(g)[:280]


def _job_elapsed(job: dict) -> Optional[float]:
    started = job.get("started_at") or job.get("created_at")
    if started is None:
        return None
    try:
        return max(0.0, _now() - float(started))
    except (TypeError, ValueError):
        return None


def _now() -> float:
    import time

    return time.time()


# ---------- Agent-call wrapper (LLM injected) ----------


async def narrate_job(job: dict, run_claude: Callable[[str], Awaitable[str]]) -> dict:
    """Narrate a single job using the injected async ``run_claude(prompt)->str``.

    Builds the prompt from the job's goal / last_activity / log_tail / elapsed,
    invokes the model, and parses the result. Best-effort: any error from
    ``run_claude`` collapses to safe defaults rather than propagating, so one
    bad job never breaks a batch. ``job`` may carry a ``log_tail`` key (the
    caller is responsible for fetching it); absent, the narrator works from
    ``last_activity`` alone.
    """
    prompt = render_narration_prompt(
        goal=_job_goal(job),
        last_activity=job.get("last_activity"),
        log_tail=job.get("log_tail"),
        elapsed_sec=_job_elapsed(job),
    )
    try:
        raw = await run_claude(prompt)
    except Exception:
        return _safe_default()
    return parse_narration(raw or "")


MAX_CONCURRENT_NARRATIONS = 4  # cap claude subprocesses spawned per pass (CP host safety)


async def watch_once(
    jobs: list[dict],
    run_claude: Callable[[str], Awaitable[str]],
    store: Callable[[str, dict], Any],
    *,
    min_interval: float = DEFAULT_MIN_INTERVAL,
    now: Optional[float] = None,
    max_concurrency: int = MAX_CONCURRENT_NARRATIONS,
) -> int:
    """Narrate every job that needs it and persist each result via ``store``.

    ``jobs`` is the current job list (already carrying ``state``, ``last_activity``,
    ``narrated_at``, and optionally ``log_tail``). For each selected job this
    calls ``store(job_id, narration_dict)`` where ``narration_dict`` is
    ``{narration, progress, eta_sec}`` plus a ``narrated_at`` stamp. ``store``
    may be sync or async. Returns the number of jobs narrated.

    Narrations run concurrently (one cheap subprocess each); a failure in one
    narration or one store does not abort the others.
    """
    when = _now() if now is None else now
    todo = jobs_needing_narration(jobs, when, min_interval=min_interval)
    if not todo:
        return 0

    # Bound concurrency so a wide running set can't spawn dozens of claude
    # subprocesses at once on the (possibly tight) control-plane host.
    sem = asyncio.Semaphore(max(1, max_concurrency))

    async def _one(j: dict) -> dict:
        async with sem:
            return await narrate_job(j, run_claude)

    results = await asyncio.gather(
        *(_one(j) for j in todo), return_exceptions=True
    )
    count = 0
    for job, res in zip(todo, results):
        if isinstance(res, BaseException):
            continue
        payload = dict(res)
        payload["narrated_at"] = _now()
        try:
            r = store(job.get("id"), payload)
            if asyncio.iscoroutine(r):
                await r
        except Exception:
            continue
        count += 1
    return count


# ---------- Default production claude runner ----------


async def default_claude_runner(prompt: str, model: str = NARRATION_MODEL) -> str:
    """Run a one-shot ``claude -p`` and return its ``.result`` text (best-effort).

    Reuses the robust subprocess pattern from ``worker._oneshot_agent``: own
    session group (so a hung agent's whole tree can be killed), stdin closed, a
    timeout, and process-group SIGKILL on timeout. Returns ``""`` on ANY failure
    (claude missing, non-JSON output, timeout, etc.) — narration must never
    raise into the sweep loop.

    Uses ``--output-format json`` (single JSON object) rather than stream-json,
    since the narrator is one cheap turn and we only want the final ``result``.
    """
    argv = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "json",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=os.getcwd(),
            env=os.environ.copy(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError, OSError):
        return ""

    try:
        out, _err = await asyncio.wait_for(
            proc.communicate(), timeout=NARRATE_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await proc.wait()
        except Exception:
            pass
        return ""
    except Exception:
        return ""

    text = (out or b"").decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    # claude --output-format json prints one JSON object with a "result" field.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and obj.get("result") is not None:
            return str(obj["result"])
    except (json.JSONDecodeError, TypeError):
        pass
    # Fall back to raw stdout; parse_narration is tolerant of it.
    return text
