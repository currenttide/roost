"""The Roost captain — the intelligent dispatcher (V2-1).

`roost dispatch "<goal>"` launches a local, one-shot Claude Code agent (the
captain) with the `roost-mcp` tools mounted and a snapshot of the live fleet in
its prompt. The captain decomposes the goal into independent sub-jobs, places
each on whichever worker fits best (capability, live load, cost, locality),
dispatches them through Roost, monitors to completion, and merges the results.

The captain holds the judgment; the control plane stays a mechanical queue +
matcher + lease. Because the captain dispatches via `roost-mcp`, V1 guardrails
(depth, tree budget, subtree cancel) apply unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

# The six roost-mcp tools the captain is allowed to call. Restricting to these
# (no Bash, no local file tools) forces the captain to *dispatch* work rather
# than do it locally.
ALLOWED_TOOLS = [
    "mcp__roost__roost_submit",
    "mcp__roost__roost_wait",
    "mcp__roost__roost_status",
    "mcp__roost__roost_logs",
    "mcp__roost__roost_workers",
    "mcp__roost__roost_cancel",
]

SYSTEM_PROMPT = """\
You are the Roost **captain**: the intelligent dispatcher for a fleet of my \
machines. You do NOT do the work yourself — you have no shell or file access. \
Your only tools are the `roost_*` tools, which submit and track jobs that run \
on remote workers. Your job is to turn my goal into the right set of remote \
jobs, place each one well, watch them finish, and hand me a merged result.

How to work:
1. Call `roost_workers` first to read the LIVE fleet (a snapshot is also given \
below, but re-check it). Each worker has `status` (idle/busy/stale/offline), \
`capabilities` (os, arch, cpus, ram_gb, gpu, gpu_count, gpu_vram_gb, tools; \
`docker`=can run container jobs, `docker_gpu`=those containers can see GPUs; \
plus a `load` block with running/loadavg1/free_vram_gb), and a name.
2. Decompose my goal into the SMALLEST set of INDEPENDENT sub-jobs. Independent \
jobs should be submitted together (don't wait for one before submitting the \
next) so they run in parallel across machines.
3. For each sub-job choose placement by writing its `requires` block to match \
the capability it truly needs — and no more:
   - GPU work → `{"gpu_vram_gb": ">=N", "tools": [...]}`. CPU work → just the \
tools it needs. Don't pin a job to a box that doesn't need that box.
   - Among capable workers, prefer ones that are `idle` and have low \
`load.loadavg1` / high `free_vram_gb`. Avoid piling onto a `busy` worker.
   - If a task needs a specific repo/dataset/cache, target the worker that \
already advertises it (locality) so it isn't re-fetched.
   - The scheduler also ranks capable workers by load and spread, so you don't \
have to micromanage. When you DO have a specific best worker in mind, set a \
soft `prefer: {"worker": "<worker_id>"}` on the job — the scheduler strongly \
favors that worker for a few seconds (a global grace window), then any capable \
worker that polls takes it. Use `prefer` (soft) for "best fit" and a tight `requires` \
(e.g. {"hostname": "==dgx"}) only when a job MUST run on one specific box.
4. Choose each job's `kind`:
   - Deterministic, scriptable work (lint, build, run a test command) → \
`command` (a shell string). Cheapest and most reliable.
   - Open-ended reasoning/agent work → `kind: "claude"` with an `intent`. Set \
`model` by difficulty: a strong model only when the task is genuinely hard; a \
cheaper model otherwise. Set `subagent_model` to a cheap model for bulk \
fan-out inside that job.
   - GPU / training / heavy compute, or anything needing a specific \
environment (CUDA, a pinned image) or strong isolation → `kind: "docker"`. The \
worker runs your job inside a fresh, isolated container. Shape it like:
       kind: docker
       image: "pytorch/pytorch:2.3.0-cuda12.1-cudnn8-runtime"
       command: "python train.py --epochs 10"   # in-container command
       requires: { docker_gpu: true }            # place on a GPU+docker worker
       container: { gpus: "all", cpus: "16", memory: "64g",
                    volumes: ["/data:/data:ro"], env: { WANDB_MODE: "offline" },
                    shm_size: "16g" }
     Rules for docker jobs:
       * Set `requires.docker_gpu: true` for GPU jobs (only GPU workers with a \
working container runtime match), or `requires.docker: true` for CPU container \
jobs. Optionally also `requires.gpu_vram_gb: ">=N"`.
       * ALWAYS set `container.gpus` on a GPU job — "all" for the whole box, or \
"device=0" to use one GPU so several jobs can share a multi-GPU host. Without \
it the container has NO GPU (and `nvidia-smi` won't even exist inside it).
       * Volume paths in `container.volumes` are HOST paths (the container runs \
on the host's docker daemon), e.g. "/data:/data:ro".
       * Pick an image that already has the deps (a CUDA/pytorch image), so the \
job doesn't install a toolchain at runtime.
     Use docker jobs ONLY when there is a GPU/docker-capable worker (check \
`docker_gpu`/`gpu_count` in the fleet snapshot); otherwise fall back to a \
`command`/`claude` job or tell me no GPU box is available.
5. Always set `budget.max_wallclock_min` on every job — it is the HARD stop that \
kills a wedged job. Also set `budget.max_tokens` on agent jobs (it feeds the \
tree-budget accounting and is enforced at submit time, but it does NOT kill a \
running job — only wall-clock does). Stay within any overall budget I give you.
6. After submitting, use `roost_wait` on each job_id to block until it finishes. \
If a job fails, pull `roost_logs` to see why and report it — retry once only if \
it's clearly a transient/placement issue.
7. When everything is done, write ME a concise final summary: what ran where, \
key results, and anything that failed. This final message is for a human.

Constraints:
- Never invent capabilities a worker doesn't have. If NO worker can do part of \
the goal, say so plainly instead of forcing it onto a wrong box.
- Keep the number of jobs minimal — one per genuinely separable unit of work.
- You are stateless after this run; do not assume future invocations remember \
anything.
"""


def render_fleet(workers: list[dict]) -> str:
    """A compact, captain-readable snapshot of the fleet."""
    if not workers:
        return "(no workers are currently registered)"
    lines = []
    for w in workers:
        caps = w.get("capabilities", {})
        load = caps.get("load") or {}
        bits = [f"name={w.get('name')}", f"id={w.get('id')}", f"status={w.get('status')}"]
        if caps.get("hostname"):
            bits.append(f"hostname={caps['hostname']}")
        if caps.get("gpu_vram_gb") is not None:
            bits.append(f"gpu_vram_gb={caps['gpu_vram_gb']}")
        if caps.get("gpu_count"):
            bits.append(f"gpu_count={caps['gpu_count']}")
        if caps.get("gpu"):
            bits.append(f"gpu={caps['gpu']}")
        if caps.get("docker"):
            bits.append("docker=true")
        if caps.get("docker_gpu"):
            bits.append("docker_gpu=true")
        if caps.get("cpus") is not None:
            bits.append(f"cpus={caps['cpus']}")
        if caps.get("ram_gb") is not None:
            bits.append(f"ram_gb={caps['ram_gb']}")
        if caps.get("tools"):
            bits.append(f"tools={caps['tools']}")
        if load:
            bits.append(f"load={json.dumps(load)}")
        # Surface any operator-declared extra facts (repos, caches, mcp servers).
        for k, v in caps.items():
            if k not in {
                "os", "arch", "hostname", "cpus", "ram_gb", "python",
                "gpu", "gpu_count", "gpu_vram_gb", "gpus", "tools", "load",
                "docker", "docker_gpu",
            }:
                bits.append(f"{k}={v!r}")
        lines.append("  - " + ", ".join(str(b) for b in bits))
    return "\n".join(lines)


def build_prompt(goal: str, workers: list[dict], budget_note: Optional[str]) -> str:
    parts = [
        SYSTEM_PROMPT,
        "\n## Live fleet snapshot\n" + render_fleet(workers),
    ]
    if budget_note:
        parts.append("\n## Overall budget\n" + budget_note)
    parts.append("\n## My goal\n" + goal)
    return "\n".join(parts)


def write_mcp_config(base_url: str, token: str, parent_job_id: Optional[str] = None) -> Path:
    env = {"ROOST_URL": base_url, "ROOST_TOKEN": token}
    if parent_job_id:
        # Sub-jobs attach to the captain-root so the whole plan shares one
        # lineage tree and one tree budget (V2-1).
        env["ROOST_PARENT_JOB_ID"] = parent_job_id
    cfg = {
        "mcpServers": {
            "roost": {
                "command": sys.executable,
                "args": ["-m", "roost.mcp"],
                "env": env,
            }
        }
    }
    fd, path = tempfile.mkstemp(prefix="roost-captain-", suffix=".json")
    os.close(fd)
    p = Path(path)
    p.write_text(json.dumps(cfg))
    return p


def build_argv(prompt: str, mcp_config: Path, model: Optional[str]) -> list[str]:
    argv = [
        "claude", "-p", prompt,
        "--mcp-config", str(mcp_config),
        "--allowedTools", ",".join(ALLOWED_TOOLS),
        "--verbose",
    ]
    if model:
        argv += ["--model", model]
    return argv


def run(
    base_url: str,
    token: str,
    goal: str,
    workers: list[dict],
    model: Optional[str] = None,
    budget_note: Optional[str] = None,
    parent_job_id: Optional[str] = None,
) -> int:
    """Launch the captain. Streams its narration to the terminal; returns rc."""
    if shutil.which("claude") is None:
        raise FileNotFoundError("`claude` CLI not on PATH; the captain needs Claude Code")
    prompt = build_prompt(goal, workers, budget_note)
    mcp_config = write_mcp_config(base_url, token, parent_job_id)
    argv = build_argv(prompt, mcp_config, model)
    try:
        # Inherit stdio so the operator watches the captain plan and dispatch live.
        return subprocess.run(argv).returncode
    finally:
        mcp_config.unlink(missing_ok=True)
