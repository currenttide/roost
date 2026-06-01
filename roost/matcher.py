"""Capability matching: does a worker's advertised capabilities satisfy a job's requirements?"""

from __future__ import annotations

from typing import Any

_OPERATORS = (">=", "<=", "==", "!=", ">", "<")


def _parse_comparator(s: str) -> tuple[str, str] | None:
    for op in _OPERATORS:
        if s.startswith(op):
            return op, s[len(op):].strip()
    return None


def _as_number(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x)
        except ValueError:
            return None
    return None


def _check_one(cap: Any, requirement: Any) -> bool:
    # Comparator string like ">=24" or "<=8".
    if isinstance(requirement, str):
        parsed = _parse_comparator(requirement)
        if parsed is not None:
            op, rhs = parsed
            a = _as_number(cap)
            b = _as_number(rhs)
            if a is None or b is None:
                # Non-numeric operands (e.g. `hostname: ==pi0`). Only equality
                # comparators are meaningful; fall back to string compare so
                # hard pins like `hostname: ==dgx` work as documented.
                # A worker that doesn't advertise the capability at all must NOT
                # satisfy a constraint *about* that capability (else a CPU-only
                # box matches `gpu_vram_gb: "!=0"` via str(None)!="0").
                if cap is None:
                    return False
                if op == "==":
                    return str(cap) == rhs
                if op == "!=":
                    return str(cap) != rhs
                return False
            return {
                ">=": a >= b,
                "<=": a <= b,
                ">": a > b,
                "<": a < b,
                "==": a == b,
                "!=": a != b,
            }[op]
    # List requirement: worker must advertise a list containing every item.
    if isinstance(requirement, list):
        if cap is None:
            return False
        if isinstance(cap, (list, tuple, set)):
            cap_set = set(cap)
            return all(item in cap_set for item in requirement)
        # Single value can satisfy a single-item list.
        return len(requirement) == 1 and requirement[0] == cap
    # Dict requirement: nested match.
    if isinstance(requirement, dict):
        if not isinstance(cap, dict):
            return False
        return all(_check_one(cap.get(k), v) for k, v in requirement.items())
    # Exact match for everything else.
    return cap == requirement


def matches(capabilities: dict[str, Any], requires: dict[str, Any] | None) -> bool:
    """Return True iff every key in ``requires`` is satisfied by ``capabilities``."""
    if not requires:
        return True
    for key, requirement in requires.items():
        if not _check_one(capabilities.get(key), requirement):
            return False
    return True


# ---------- placement ranking (V2-2) ----------
#
# `matches()` is the hard filter (capable? y/n). `placement_score()` is the
# mechanical, LLM-free tie-break among workers that already pass the filter:
# higher score = better fit. The control plane uses it under the pull model to
# decide whether a polling worker should take a job now or let a better-fit
# worker claim it first (see server._try_assign_one). The captain shapes
# `requires`/`prefer`; this function resolves the final pick among equivalents.

# Score weights, largest first so each tier dominates the next.
_W_PREFER = 1000.0   # captain named this exact worker
_W_IDLE = 100.0      # no job currently running on it
_W_LOADAVG = 10.0    # subtracted per unit of 1-min loadavg
_W_VRAM = 0.1        # added per GB of free VRAM
_W_RECENCY = 5.0     # added per second since last assigned (spread)


def placement_score(
    worker: dict[str, Any],
    job: dict[str, Any],
    *,
    now: float,
) -> float:
    """Rank a capable worker for a job. Assumes the hard `requires` already pass.

    ``worker`` is a registry row dict (``capabilities`` incl. live ``load``,
    ``status``, ``last_assigned_at``). ``job`` may carry a ``prefer`` hint
    (currently ``{"worker": "<id>"}``). Pure and deterministic.
    """
    caps = worker.get("capabilities") or {}
    load = caps.get("load") or {}
    prefer = job.get("prefer") or {}

    score = 0.0

    if isinstance(prefer, dict) and prefer.get("worker") == worker.get("id"):
        score += _W_PREFER
    elif isinstance(prefer, str) and prefer == worker.get("id"):
        score += _W_PREFER

    running = load.get("running")
    if running is None:
        running = 0 if worker.get("status") == "idle" else 1
    if running == 0:
        score += _W_IDLE

    loadavg = load.get("loadavg1")
    if isinstance(loadavg, (int, float)):
        score -= _W_LOADAVG * float(loadavg)

    free_vram = load.get("free_vram_gb")
    if isinstance(free_vram, (int, float)):
        score += _W_VRAM * float(free_vram)

    last_assigned = worker.get("last_assigned_at")
    if last_assigned:
        score += _W_RECENCY * max(0.0, now - float(last_assigned))

    return score
