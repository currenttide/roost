"""On-node steward judgments (agent-driven, not a scheduler).

Roost pushes judgment to the box that can actually see its own resources. Two
judgments live here:

  * CAPACITY — how many jobs can THIS machine run concurrently right now? A small
    haiku agent looks at live machine facts (cpus, free mem, free disk, loadavg,
    GPU free/total VRAM + util, running jobs) and returns a single integer. The
    worker caches it and reports it on every heartbeat as ``load.capacity``.

  * DIAGNOSIS — when a job fails (non-zero exit / build error / timeout), a haiku
    agent reads the spec summary + exit code + stdout/stderr tails and writes one
    short root-cause line, stored on the terminal event as ``diagnosis``.

Both are FAIL-SAFE: this module only builds prompts / facts / fallbacks and parses
responses — it never blocks the worker. The actual ``claude -p`` subprocess call
lives in worker.py (reusing its subprocess plumbing); if ``claude`` is absent or the
call fails/times out/parses wrong, the worker uses the deterministic fallback here:
capacity = 1, diagnosis = a mechanical one-liner. Pure + dependency-light so it's
trivially testable without invoking claude.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Optional

# The fleet already runs this exact contract live — do not change the model id, the
# JSON shape, or the keys without coordinating with the server agent.
STEWARD_MODEL = "claude-haiku-4-5-20251001"

# Hard floor: a worker can always run at least one job, even if the steward is
# unavailable or returns nonsense. Never block the worker on the steward.
FALLBACK_CAPACITY = 1

# Mechanical capacity estimate (used when the claude steward can't run): how many
# CPU cores and how much memory we assume one agent/command job consumes. Agent jobs
# are memory-hungry (a `claude -p` + its toolchain), so we gate on BOTH cores and
# memory and take the min — a many-core but memory-starved box shouldn't over-commit.
_CORES_PER_JOB = 4          # ~4 cores per concurrent job
_MEM_GB_PER_JOB = 4.0       # ~4 GB resident per concurrent job
_MECHANICAL_CAP_MAX = 8     # never auto-estimate above this without the agent's judgment


def mechanical_capacity(facts: dict[str, Any]) -> int:
    """Deterministic capacity estimate from machine facts, used when the claude
    steward is unavailable (no claude / call failed). Replaces the old flat fail-safe
    of 1 so a big idle box isn't capped at 1 just because it has no claude.

    Derived from CPU cores and available memory (both gate concurrency), minus jobs
    already running, bounded to [1, _MECHANICAL_CAP_MAX]. The agentic steward remains
    the primary path when claude IS present — this is only the graceful-degradation
    floor. Pure + total; safe on missing/garbage facts."""
    try:
        cpus = int(facts.get("cpus") or 1)
    except (TypeError, ValueError):
        cpus = 1
    by_cpu = max(1, cpus // _CORES_PER_JOB)

    # Prefer live available memory; fall back to total RAM; else don't gate on memory.
    mem_gb: Optional[float] = None
    for key in ("mem_available_gb", "mem_total_gb"):
        v = facts.get(key)
        if v is not None:
            try:
                mem_gb = float(v)
                break
            except (TypeError, ValueError):
                pass
    by_mem = max(1, int(mem_gb // _MEM_GB_PER_JOB)) if mem_gb is not None else by_cpu

    # MAX concurrency (total), matching the agentic steward's contract — the server
    # gates placement on free slots (capacity - running), so we must NOT subtract
    # running_jobs here or it would be double-counted.
    est = min(by_cpu, by_mem, _MECHANICAL_CAP_MAX)
    return max(FALLBACK_CAPACITY, est)


# ---------- live machine facts (for the capacity prompt) ----------


def _mem_available_gb() -> Optional[float]:
    """Currently-available RAM (Linux /proc/meminfo MemAvailable), in GB."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return round(int(line.split()[1]) / 1024.0 / 1024.0, 1)
    except OSError:
        pass
    return None


def _free_disk_by_mount() -> dict[str, float]:
    """Free space (GB) for the mounts that matter for a job: cwd and /tmp. Keyed by
    mount path. Best-effort; skips paths we can't stat."""
    out: dict[str, float] = {}
    for path in (os.getcwd(), "/tmp"):
        try:
            free = shutil.disk_usage(path).free
        except OSError:
            continue
        out[path] = round(free / 1024.0 / 1024.0 / 1024.0, 1)
    return out


def _gpu_facts(find_nvidia_smi) -> list[dict[str, Any]]:
    """Per-GPU free/total VRAM (GB) + utilization (%), live. ``find_nvidia_smi`` is
    injected (worker._find_nvidia_smi) so we don't duplicate discovery. Empty on no
    GPU or any probe error."""
    smi = find_nvidia_smi()
    if not smi:
        return []
    try:
        out = subprocess.run(
            [smi, "--query-gpu=memory.free,memory.total,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5.0, check=True,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return []
    gpus: list[dict[str, Any]] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            free_gb = round(float(parts[0]) / 1024.0, 1)
            total_gb = round(float(parts[1]) / 1024.0, 1)
        except ValueError:
            continue
        g: dict[str, Any] = {"free_vram_gb": free_gb, "total_vram_gb": total_gb}
        if len(parts) > 2:
            try:
                g["util_pct"] = int(float(parts[2]))
            except ValueError:
                pass
        gpus.append(g)
    return gpus


def machine_facts(
    capabilities: dict[str, Any],
    running_jobs: int,
    *,
    find_nvidia_smi,
) -> dict[str, Any]:
    """Build the live machine-facts dict handed to the capacity steward. Reuses the
    static capability snapshot for cpus and layers live readings on top."""
    facts: dict[str, Any] = {
        "cpus": capabilities.get("cpus"),
        "running_jobs": running_jobs,
    }
    mem = _mem_available_gb()
    if mem is not None:
        facts["mem_available_gb"] = mem
    elif capabilities.get("ram_gb") is not None:
        facts["mem_total_gb"] = capabilities.get("ram_gb")
    disk = _free_disk_by_mount()
    if disk:
        facts["free_disk_gb"] = disk
    try:
        facts["loadavg1"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    gpus = _gpu_facts(find_nvidia_smi)
    if gpus:
        facts["gpus"] = gpus
    return facts


# ---------- capacity prompt + parsing ----------


def capacity_prompt(facts: dict[str, Any]) -> str:
    """The capacity-judgment prompt. The agent returns ONLY the pinned JSON object."""
    return (
        "You are the capacity steward for ONE worker in a Roost fleet. Given this "
        "machine's live resources, decide the MAXIMUM number of agent/command jobs it "
        "can run concurrently RIGHT NOW without thrashing (consider CPU cores, free "
        "memory, free disk, load average, and — for GPU work — free VRAM and GPU "
        "utilization). Be realistic: a busy or memory-starved box should report a low "
        "number; an idle many-core box can report more. The minimum is 1.\n\n"
        "Machine facts (JSON):\n"
        f"{json.dumps(facts, sort_keys=True)}\n\n"
        'Respond with ONLY a JSON object, no prose: '
        '{"max_concurrent": <int >= 1>, "reason": "<=160 chars"}'
    )


def parse_capacity(raw: Optional[str]) -> Optional[int]:
    """Parse the steward's reply into a capacity int (>= 1), or None if it can't be
    parsed (caller then uses the deterministic fallback). Tolerates the JSON being
    embedded in surrounding text / stream output."""
    if not raw:
        return None
    obj = _extract_json_object(raw)
    if obj is None:
        return None
    val = obj.get("max_concurrent")
    try:
        n = int(val)
    except (TypeError, ValueError):
        return None
    if n < 1:
        return None
    return n


def _extract_json_object(raw: str) -> Optional[dict]:
    """Best-effort: parse ``raw`` as JSON, else the first ``{...}`` span within it."""
    raw = raw.strip()
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, TypeError):
            return None
    return None


# ---------- failure diagnosis ----------

DIAGNOSIS_MAX = 300


def _tail(text: Optional[str], n: int = 1500) -> str:
    if not text:
        return ""
    text = text.strip()
    return text[-n:]


def _last_nonempty_line(text: Optional[str]) -> str:
    if not text:
        return ""
    for line in reversed(text.splitlines()):
        s = line.strip()
        if s:
            return s
    return ""


def deterministic_diagnosis(
    *, exit_code: Optional[Any], stderr_tail: Optional[str],
    stdout_tail: Optional[str] = None, error: Optional[str] = None,
) -> str:
    """Mechanical one-liner used when claude is absent/fails: exit code + last
    non-empty stderr line (falling back to stdout / the raw error). <= DIAGNOSIS_MAX."""
    last = _last_nonempty_line(stderr_tail) or _last_nonempty_line(stdout_tail) or (error or "")
    code = exit_code if exit_code is not None else "?"
    msg = f"exit_code={code}" + (f" — {last}" if last else "")
    return msg[:DIAGNOSIS_MAX]


def diagnosis_prompt(
    *, spec_summary: str, exit_code: Optional[Any],
    stdout_tail: Optional[str], stderr_tail: Optional[str],
) -> str:
    """Prompt for the diagnosis steward: one short root-cause line, no fix, no prose."""
    return (
        "A job on this Roost worker FAILED. In ONE short sentence (<= 280 chars), state "
        "the most likely ROOT CAUSE of the failure based on the evidence below. No "
        "preamble, no fix suggestions, no markdown — just the diagnosis line.\n\n"
        f"Job: {spec_summary}\n"
        f"Exit code: {exit_code}\n"
        f"--- stderr tail ---\n{_tail(stderr_tail)}\n"
        f"--- stdout tail ---\n{_tail(stdout_tail)}\n"
    )


def clean_diagnosis(raw: Optional[str]) -> Optional[str]:
    """Trim an agent diagnosis reply to a single short line, or None if empty."""
    if not raw:
        return None
    line = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    if not line:
        return None
    return line[:DIAGNOSIS_MAX]


def spec_summary(spec: dict[str, Any]) -> str:
    """A compact one-line description of the failed job for the diagnosis prompt."""
    kind = (spec.get("kind") or "claude").lower()
    what = (spec.get("intent") or spec.get("task") or spec.get("command")
            or spec.get("image") or "")
    what = str(what).replace("\n", " ").strip()
    return f"kind={kind} {what}"[:300]
