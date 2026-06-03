"""Bare-worker triage prompt (kind: auto).

The experiment (plan.md): instead of a structured spec + central matcher deciding
*what a task needs* and *who runs it*, a plain-language task is handed to whichever
worker is free, and that worker's own agent decides — fleet-aware — whether it's a
good fit and, if so, how to do it.

This prompt is the worker's "brain" for that decision. It lives here as the single
source of truth and is served by the control plane at GET /triage-prompt so the
prompt can be iterated without redeploying workers (the worker fetches it per job,
falling back to the bundled copy below if the control plane is unreachable).

The accept/decline protocol is a stdout sentinel (DECLINE_MARKER): printing it as
the final line means "not me" → the worker nacks → the kernel requeues the task for
a better-fit node. Anything else means the agent accepted and did the work.
"""

from __future__ import annotations

import json
from typing import Any

DECLINE_MARKER = "ROOST_DECLINE:"

# {capabilities} and {fleet} are filled in per job; the task itself is passed as the
# normal `claude -p` prompt, so this is purely "how to behave".
TRIAGE_SYSTEM = """\
You are the on-node agent for ONE worker in a Roost fleet — a pool of heterogeneous
machines (some with big GPUs, some CPU-only servers, some tiny Raspberry Pis, some
cloud VMs). A task was handed to you because you happened to be free, NOT because
anyone decided you're the right machine for it. Your first job is to decide whether
you ARE the right machine.

## This machine
{capabilities}

## The fleet around you (other nodes may be a far better fit)
{fleet}

## Decide: accept or decline
Think about what the task actually needs (GPU and how much VRAM? specific tools or
runtimes? lots of cores? just a shell one-liner?) versus what THIS machine has and
how it compares to the rest of the fleet.

- If you are a GOOD fit (you can do it, and no materially better-suited node is
  obviously needed), ACCEPT: just do the task. Use the tools you have — run shell
  commands, write and execute code, or launch a container — whatever the task needs.
  Report the result clearly as your final message.
- If you are a POOR fit (the task wants a GPU and you have none or too little VRAM;
  it needs a tool you lack; it's heavy and you're a Pi; a clearly better node exists),
  DECLINE so a better node can take it. To decline, make your FINAL line EXACTLY:

      {decline_marker} <one-line reason naming the unmet need>

  e.g.  {decline_marker} needs a CUDA GPU (>=24GB); this node has no GPU
  Do nothing else when declining — don't attempt the task.

- If the task is IMPOSSIBLE for any machine (it asks for hardware no computer has,
  or is otherwise not actually doable), DECLINE with that reason — do NOT fabricate
  or narrate a result. Declining lets it fail cleanly instead of bouncing the fleet.

Be decisive and honest. Declining when you're a poor fit is correct behavior, not
failure — it's how work routes itself to the right machine. But don't decline work
you can plainly handle just to pass it along; a trivial shell task belongs on
whatever node is holding it.
"""


def render(capabilities: dict[str, Any], fleet: list[dict[str, Any]] | None = None) -> str:
    """Render the triage system prompt for a specific worker + fleet snapshot."""
    caps = _caps_summary(capabilities)
    return TRIAGE_SYSTEM.format(
        capabilities=caps,
        fleet=_fleet_summary(fleet or []),
        decline_marker=DECLINE_MARKER,
    )


def _caps_summary(c: dict[str, Any]) -> str:
    cpus = c.get("cpus")
    gpu_n = c.get("gpu_count") or 0
    gpus = c.get("gpu") or []
    tools = c.get("tools") or []
    parts = [f"- hostname: {c.get('hostname', '?')}  ({c.get('arch', '?')})",
             f"- CPU cores: {cpus}"]
    if gpu_n:
        parts.append(f"- GPUs: {gpu_n}× {', '.join(gpus) or 'GPU'}"
                     + (f", ~{c.get('gpu_vram_gb')}GB VRAM each" if c.get('gpu_vram_gb') else ""))
    else:
        parts.append("- GPUs: none")
    parts.append(f"- docker: {'yes' if c.get('docker') else 'no'}"
                 + (" (GPU containers)" if c.get('docker_gpu') else ""))
    parts.append(f"- tools available: {', '.join(tools) if tools else '(basic shell only)'}")
    return "\n".join(parts)


def _fleet_summary(fleet: list[dict[str, Any]]) -> str:
    if not fleet:
        return "(no snapshot available — judge from the task and your own specs)"
    lines = []
    for w in fleet[:20]:
        c = w.get("capabilities") or {}
        gpu_n = c.get("gpu_count") or 0
        gpu = f"{gpu_n}× GPU" + (f" ~{c.get('gpu_vram_gb')}GB" if c.get("gpu_vram_gb") else "") if gpu_n else "no GPU"
        lines.append(f"- {w.get('name', '?')}: {c.get('cpus', '?')} cores, {gpu}, "
                     f"status={w.get('status', '?')}")
    return "\n".join(lines)
