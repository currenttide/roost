"""Roost control plane (V1).

A FastAPI app backed by SQLite. Adds, on top of V0:

  * lease-based job execution (renewed by heartbeat; expired leases requeue)
  * enrollment-token + per-worker credential flow (shared token still works
    as LAN convenience)
  * lineage / depth / tree-budget guardrails for hierarchical dispatch via
    roost-mcp
  * subtree cancel (`DELETE /jobs/{id}?tree=true`)
  * background sweeper that marks stale/offline workers and requeues
    abandoned jobs
  * `/install.sh` one-line installer endpoint
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, PlainTextResponse,
    RedirectResponse, StreamingResponse,
)
from pydantic import BaseModel, Field

from . import blobs as blobstore
from . import publish as publishlib
from . import triage
from . import watcher
from .matcher import matches, placement_score
from .schema import migrate

DEFAULT_DB = Path.home() / ".roost" / "roost.db"

# Timing knobs (Decision B3).
HEARTBEAT_INTERVAL = 15.0
STALE_AFTER = 45.0
OFFLINE_AFTER = 120.0
LEASE_TTL = 60.0
POLL_HOLD_MAX = 30.0
SWEEPER_INTERVAL = 5.0
# How long a queued job will wait for a better-fit worker to poll before any
# capable worker may take it (placement grace window, Decision V2-2/V2-4).
PLACEMENT_GRACE = 3.0
# Bare-worker (kind: auto): a worker permanently skips a task it already declined (a
# poor fit won't become a good one), so the task waits for a capable node. It's
# escalated to failed only when every currently-online capable worker has declined
# (the genuine "no node can do this" case) — never while a capable node is just busy.
# A backstop guards against pathological flapping (workers churning in/out).
MAX_DECLINES = 10
# Schedule verb: the CP tick enqueues a job per due schedule. Floor keeps a
# typo'd "every 1s" from turning the queue into a firehose.
SCHEDULE_MIN_INTERVAL_SEC = 30.0
# Log retention (M1): the sweeper prunes job_logs on a throttle so the DB
# doesn't grow unbounded. Keep ~24h of log lines and cap per-job rows.
LOG_PRUNE_INTERVAL = 1800.0   # seconds between prune passes (~30 min)
LOG_MAX_AGE_SEC = 24 * 3600.0  # drop log rows older than 24h
LOG_MAX_ROWS_PER_JOB = 5000   # and cap rows per job
# Hygiene: prune worker rows that have been offline/unseen this long AND own no
# in-flight job. Non-enrolled workers that reconnect after an outage leave orphan
# rows that age to 'offline' but are never deleted, so /derived + the panel
# accumulate stale rows. Generous so a briefly-down node is never deleted — only
# stale, credential-less (shared-token/LAN) duplicate rows are removed.
WORKER_PRUNE_TTL = 6 * 3600.0  # 6 hours since last_seen
ENROLL_TOKEN_TTL = 900.0  # 15 minutes
ENROLL_PREFIX = "rst-enr-"
WORKER_CRED_PREFIX = "rst-wkr-"
MOBILE_TOKEN_PREFIX = "rst-mob-"

TERMINAL_STATES = ("succeeded", "failed", "cancelled")
ACTIVE_STATES = ("queued", "assigned", "running")


# ---------- DB helpers ----------


@contextmanager
def _connect(db_path: Path):
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


def _init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        migrate(conn)


def _row_to_job(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    d = dict(row)
    d["spec"] = json.loads(d["spec"])
    d["requires"] = json.loads(d["requires"])
    if d.get("result"):
        try:
            d["result"] = json.loads(d["result"])
        except (TypeError, json.JSONDecodeError):
            pass
    return d


def _row_to_worker(row: sqlite3.Row | None) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    d = dict(row)
    d["capabilities"] = json.loads(d["capabilities"])
    d["policy"] = json.loads(d.get("policy_json") or "{}")
    d.pop("policy_json", None)
    d.pop("cred_hash", None)  # never leak hash
    return d


def _hash_cred(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------- Submit / lineage ----------


def _insert_job(
    db_path: Path,
    spec: dict[str, Any],
    parent: Optional[dict[str, Any]] = None,
    *,
    as_running: bool = False,
) -> dict[str, Any]:
    """Insert a job. ``as_running`` anchors a captain-root: it starts in
    ``running`` with no worker and no lease (the sweeper ignores it, workers
    never pull it), existing only to root a plan's lineage + tree budget.
    """
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    requires = spec.get("requires") or {}
    hierarchy = spec.get("hierarchy") or {}
    budget = spec.get("budget") or {}
    max_attempts = int(spec.get("max_attempts") or budget.get("max_attempts") or 2)
    max_depth = int(hierarchy.get("max_depth", parent["max_depth"] if parent else 3))
    if parent is not None:
        # A child may never RAISE the depth ceiling above its parent's (prevents a
        # sub-agent escaping the root's bound by declaring a huge max_depth).
        max_depth = min(max_depth, int(parent["max_depth"]))
        depth = parent["depth"] + 1
        if depth > max_depth:
            raise ValueError(
                f"max_depth exceeded: parent depth={parent['depth']} max_depth={max_depth}"
            )
        root_id = parent["root_job_id"] or parent["id"]
    else:
        depth = 0
        root_id = job_id
    tree_budget = budget.get("tree_max_tokens") or budget.get("max_tokens") if parent is None else None
    model = spec.get("model")
    subagent_model = spec.get("subagent_model")

    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Budget enforcement against parent root.
            if parent is not None:
                root_row = conn.execute(
                    "SELECT tree_budget_tokens, tree_budget_spent FROM jobs WHERE id=?",
                    (root_id,),
                ).fetchone()
                if root_row is None:
                    conn.execute("ROLLBACK")
                    raise ValueError(f"root job {root_id} not found")
                root_budget = root_row["tree_budget_tokens"]
                root_spent = root_row["tree_budget_spent"] or 0
                requested = int(budget.get("max_tokens") or 0)
                if root_budget is not None and requested:
                    remaining = root_budget - root_spent
                    if requested > remaining:
                        conn.execute("ROLLBACK")
                        raise ValueError(
                            f"tree budget exhausted: requested {requested} tokens, "
                            f"only {remaining} remaining of root cap {root_budget}"
                        )
            state = "running" if as_running else "queued"
            started_at = now if as_running else None
            conn.execute(
                "INSERT INTO jobs("
                "id, spec, intent, requires, state, created_at, started_at, max_attempts, "
                "parent_job_id, root_job_id, depth, max_depth, "
                "tree_budget_tokens, model, subagent_model"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    json.dumps(spec),
                    spec.get("intent"),
                    json.dumps(requires),
                    state,
                    now,
                    started_at,
                    max_attempts,
                    parent["id"] if parent else None,
                    root_id,
                    depth,
                    max_depth,
                    tree_budget,
                    model,
                    subagent_model,
                ),
            )
            conn.execute("COMMIT")
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        except Exception:
            # Inner error paths may have already rolled back; guard so we
            # don't raise "no transaction is active" over the real error.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return _row_to_job(row)


# ---------- Schedules (the `schedule` verb: interval jobs) ----------


_EVERY_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$")


def parse_every(every: Any) -> Optional[float]:
    """'30m' / '6h' / '90s' / '1d' / bare seconds (number or string) → seconds.

    Returns None when it can't be parsed (caller decides the error)."""
    if isinstance(every, bool):
        return None
    if isinstance(every, (int, float)):
        return float(every)
    if isinstance(every, str):
        m = _EVERY_RE.match(every.lower())
        if m:
            mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
            return float(m.group(1)) * mult
        try:
            return float(every)
        except ValueError:
            return None
    return None


def _validate_job_spec(spec: dict[str, Any]) -> None:
    """The submit-shape rules, shared by POST /jobs and schedule creation."""
    kind = (spec.get("kind") or "").lower()
    if kind == "auto" and not spec.get("task") and not spec.get("intent"):
        raise HTTPException(400, "kind: auto job requires `task`")
    if (kind != "auto" and not spec.get("intent") and not spec.get("command")
            and not (kind == "docker" and spec.get("image"))):
        raise HTTPException(
            400, "job must have either `intent`, `command`, (kind: docker + `image`), "
                 "or (kind: auto + `task`)")


def _schedule_to_public(row: dict[str, Any]) -> dict[str, Any]:
    try:
        spec = json.loads(row["spec"])
    except (TypeError, ValueError):
        spec = {}  # a corrupt row must not 500 the list endpoint
    return {
        "id": row["id"],
        "name": row["name"],
        "spec": spec,
        "interval_sec": row["interval_sec"],
        "enabled": bool(row["enabled"]),
        "next_run_at": row["next_run_at"],
        "last_run_at": row["last_run_at"],
        "last_job_id": row["last_job_id"],
        "created_at": row["created_at"],
    }


def _tick_schedules(db_path: Path) -> int:
    """Enqueue a job for each due schedule; returns how many were enqueued.

    Policies (documented in INTEGRATIONS.md):
    - one job per due schedule per tick — an overdue schedule (CP was down for
      several intervals) does NOT back-fill missed runs; ``next_run_at``
      advances in whole intervals so the original cadence is preserved;
    - no pile-up: if the schedule's previous job is still in flight
      (queued/assigned/running), this beat is skipped and the clock still
      advances;
    - a broken spec logs and skips — it never breaks the sweep loop.
    """
    now = time.time()
    launched = 0
    with _connect(db_path) as conn:
        due = [dict(r) for r in conn.execute(
            "SELECT * FROM schedules WHERE enabled=1 AND next_run_at <= ?",
            (now,),
        ).fetchall()]
    for sched in due:
        interval = float(sched["interval_sec"])
        missed = int((now - sched["next_run_at"]) // interval) + 1
        next_run = sched["next_run_at"] + missed * interval
        new_job_id = None
        in_flight = False
        if sched["last_job_id"]:
            with _connect(db_path) as conn:
                prev = conn.execute(
                    "SELECT state FROM jobs WHERE id=?", (sched["last_job_id"],)
                ).fetchone()
            in_flight = (prev is not None
                         and prev["state"] in ("queued", "assigned", "running"))
        if not in_flight:
            try:
                spec = json.loads(sched["spec"])
                spec["schedule_id"] = sched["id"]  # provenance on every run
                job = _insert_job(db_path, spec)
                new_job_id = job["id"]
                launched += 1
            except Exception as e:  # noqa: BLE001 — never break the sweep loop
                print(f"[roost] schedule {sched['id']} enqueue error: {e}",
                      flush=True)
        with _connect(db_path) as conn:
            if new_job_id is not None:
                conn.execute(
                    "UPDATE schedules SET next_run_at=?, last_run_at=?, "
                    "last_job_id=? WHERE id=?",
                    (next_run, now, new_job_id, sched["id"]),
                )
            else:
                # Skipped beat (in-flight previous run or broken spec): the
                # clock still advances; last_run_at stays = last real enqueue.
                conn.execute(
                    "UPDATE schedules SET next_run_at=? WHERE id=?",
                    (next_run, sched["id"]),
                )
    return launched


def _list_jobs(
    db_path: Path,
    state: Optional[str] = None,
    root: Optional[str] = None,
    parent: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    clauses, params = [], []
    if state:
        clauses.append("state = ?")
        params.append(state)
    if root:
        clauses.append("root_job_id = ?")
        params.append(root)
    if parent:
        clauses.append("parent_job_id = ?")
        params.append(parent)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?", params
        )
        return [_row_to_job(r) for r in cur.fetchall()]


def _get_job(db_path: Path, job_id: str) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_job(row)


def _get_tree(db_path: Path, root_id: str) -> list[dict]:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM jobs WHERE root_job_id = ? ORDER BY depth ASC, created_at ASC",
            (root_id,),
        )
        return [_row_to_job(r) for r in cur.fetchall()]


def _annotate_liveness(db_path: Path, jobs: list[dict]) -> list[dict]:
    """Attach raw liveness FACTS to job dicts (no verdicts — judgment is the
    agent's job). Adds, where meaningful:

      idle_sec        seconds since the job's last sign of life (running/assigned)
      queued_sec      seconds a job has sat queued
      capable_workers count of online workers whose capabilities satisfy `requires`

    `capable_workers == 0` on a queued job is the mechanical fact behind a
    silently-unplaceable plan; an overseer agent decides what it means.
    """
    jobs = [j for j in jobs if j]
    if not jobs:
        return jobs
    now = time.time()
    # Online workers (fresh enough to poll) and their capabilities, fetched once.
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT capabilities FROM workers WHERE last_seen >= ?",
            (now - STALE_AFTER,),
        ).fetchall()
    online_caps = [json.loads(r["capabilities"]) for r in rows]
    for j in jobs:
        state = j.get("state")
        if j.get("last_activity_at") and state in ("running", "assigned"):
            j["idle_sec"] = round(now - float(j["last_activity_at"]), 1)
        if state == "queued":
            j["queued_sec"] = round(now - float(j["created_at"]), 1)
            req = j.get("requires") or {}
            j["capable_workers"] = sum(1 for caps in online_caps if matches(caps, req))
    return jobs


# ---------- Derived observability model (ease-of-use-plan Part II, D0) ----------
# Composes the deterministic facts above into operator-meaningful fields: phase,
# a rule-based health verdict, cost, and a single fleet verdict. The web panel,
# `scripts/fleet`, and the MCP inbox all render this one model. Agents (D2) later
# fill cached narrative slots; this layer stays deterministic + always-available.

STUCK_AFTER = 150.0          # a running job idle this long is a stuck-suspect
WAITING_AFTER = 90.0         # a queued (but placeable) job waiting this long
AGENT_SESSION_BASE_USD = 0.018   # ~fixed per-agent-session floor (cached system prompt)
COST_PER_MTOK_USD = 6.0          # rough marginal $/Mtok on fresh tokens_used (approximate)


def _goal_text(job: dict) -> str:
    spec = job.get("spec") or {}
    g = spec.get("task") or spec.get("intent") or spec.get("command") or ""
    return (g if isinstance(g, str) else " ".join(g))[:140]


def _job_phase(job: dict) -> str:
    state = job.get("state")
    if state in ("succeeded", "failed", "cancelled"):
        return state
    act = job.get("last_activity") or ""
    if "verifying" in act:
        return "verifying"
    if "self-healing" in act:
        return "self-healing"
    return state or "queued"


def _job_health(job: dict) -> dict:
    """Rule-based verdict from facts only (no LLM). {status, reason}."""
    state = job.get("state")
    if state == "failed":
        return {"status": "failed", "reason": (job.get("error") or "failed")[:160]}
    if state == "cancelled":
        return {"status": "cancelled", "reason": "cancelled"}
    if state == "succeeded":
        res = job.get("result") if isinstance(job.get("result"), dict) else {}
        if res.get("verified") is True:
            return {"status": "verified", "reason": (res.get("evidence") or "verified")[:160]}
        if res.get("verified") is False:
            return {"status": "unverified", "reason": "completed but NOT verified"}
        return {"status": "done", "reason": "completed"}
    if state == "queued":
        if job.get("capable_workers") == 0:
            return {"status": "unplaceable", "reason": "no online worker satisfies requires"}
        qs = job.get("queued_sec") or 0
        if qs > WAITING_AFTER:
            return {"status": "waiting", "reason": f"queued {int(qs)}s — capable workers busy"}
        return {"status": "queued", "reason": "queued"}
    # A job in the verify/self-heal phase is legitimately quiet on its own activity
    # line (its verifier/fix runs as a separate subprocess) — don't call it stuck.
    phase = _job_phase(job)
    if phase in ("verifying", "self-healing"):
        return {"status": phase, "reason": (job.get("last_activity") or phase)[:160]}
    idle = job.get("idle_sec")
    if idle is not None and idle > STUCK_AFTER:
        return {"status": "stuck?", "reason": f"no activity for {int(idle)}s — may be stuck"}
    return {"status": "running", "reason": (job.get("last_activity") or "running")[:160]}


def _job_cost(job: dict) -> dict:
    tok = int(job.get("tokens_used") or 0)
    # Rough $ estimate. tokens_used counts only fresh input+output (not the large
    # cached system-prompt reads that actually dominate an agent session's bill), so
    # a near-fixed per-session floor + a small marginal tracks reality far better than
    # a flat per-token rate. Measured ~$0.02 trivial → ~$0.05 multi-step on Sonnet.
    est = round(AGENT_SESSION_BASE_USD + tok / 1_000_000 * COST_PER_MTOK_USD, 4) if tok else 0.0
    out: dict[str, Any] = {"tokens_used": tok, "cost_est_usd": est}
    tb = job.get("tree_budget_tokens")
    if tb:
        out["budget_pct"] = round(100 * (job.get("tree_budget_spent") or 0) / tb, 1)
    return out


def _derive_run(job: dict) -> dict:
    """One job → the operator-meaningful 'story' fields (D0)."""
    res = job.get("result") if isinstance(job.get("result"), dict) else {}
    return {
        "run_id": job.get("id"),
        "goal": _goal_text(job),
        "state": job.get("state"),
        "phase": _job_phase(job),
        "health": _job_health(job),
        "worker": job.get("worker_id"),
        "verified": res.get("verified"),
        "evidence": res.get("evidence"),
        "result": (res.get("output") or job.get("error") or "")[:240],
        "diagnosis": job.get("diagnosis"),
        "last_activity": job.get("last_activity"),
        "idle_sec": job.get("idle_sec"),
        "queued_sec": job.get("queued_sec"),
        "capable_workers": job.get("capable_workers"),
        "decline_count": job.get("decline_count"),
        "cost": _job_cost(job),
        # agentic slots (D2 fills these; empty/deterministic for now)
        "narration": job.get("narration") or job.get("last_activity"),
        "progress": job.get("progress"),
        "eta_sec": job.get("eta_sec"),
        "root_job_id": job.get("root_job_id"),
        "created_at": job.get("created_at"),
        "finished_at": job.get("finished_at"),
    }


FAILURE_RECENT_SEC = 600.0  # a terminal failure older than this is history, not an alert


def _fleet_verdict(workers: list[dict], runs: list[dict], now: Optional[float] = None) -> dict:
    now = now if now is not None else time.time()
    live = [w for w in workers if w.get("status") in ("idle", "busy")]
    # "needs attention" = active problems (a queued job that can't place, a running job
    # that looks stuck) OR a RECENT terminal failure. Ancient failures in history must
    # not keep the fleet perpetually red.
    bad = []
    for r in runs:
        st = r["health"]["status"]
        if st in ("unplaceable", "stuck?"):
            bad.append(r)
        elif st in ("failed", "unverified"):
            fin = r.get("finished_at")
            if not fin or (now - float(fin)) <= FAILURE_RECENT_SEC:
                bad.append(r)
    active = [r for r in runs if r["state"] in ("running", "assigned")]
    verifying = [r for r in runs if r["phase"] in ("verifying", "self-healing")]
    if bad:
        w = bad[0]
        return {"level": "alert",
                "summary": f"{len(bad)} need attention — {w['health']['status']}: {w['goal'][:60]}"}
    if active or verifying:
        return {"level": "ok",
                "summary": f"{len(live)} nodes · {len(active)} running · {len(verifying)} verifying — all healthy"}
    return {"level": "ok", "summary": f"{len(live)} nodes online · fleet idle"}


def _recently_cancelled_for_worker(db_path: Path, worker_id: str) -> list[str]:
    """Job ids assigned to this worker that were cancelled in the last few
    minutes — so a heartbeat can tell the worker to kill the still-running
    process/container. Bounded by a time window so the list stays small; the
    worker ignores ids it isn't actually running."""
    now = time.time()
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE worker_id=? AND state='cancelled' "
            "AND finished_at >= ?",
            (worker_id, now - 300.0),
        ).fetchall()
    return [r["id"] for r in rows]


def _owned_job_ids(db_path: Path, worker_id: str) -> list[str]:
    """[R3] Job ids the control plane currently attributes to this worker
    (assigned/running). Returned on every heartbeat so a worker that kept
    running through a CP outage can abort attempts the sweeper has since
    requeued elsewhere (lease reconciliation) instead of duplicating work."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE worker_id=? AND state IN ('assigned','running')",
            (worker_id,),
        ).fetchall()
    return [r["id"] for r in rows]


def _cancel_job(db_path: Path, job_id: str, cascade: bool) -> int:
    """Cancel a job (and optionally its subtree). Returns count cancelled."""
    now = time.time()
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            target = conn.execute(
                "SELECT id, state, root_job_id FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not target:
                conn.execute("ROLLBACK")
                return 0
            ids: list[str] = []
            if cascade:
                # BFS over children using parent_job_id.
                pending = [job_id]
                visited = set()
                while pending:
                    current = pending.pop()
                    if current in visited:
                        continue
                    visited.add(current)
                    ids.append(current)
                    kids = conn.execute(
                        "SELECT id FROM jobs WHERE parent_job_id=?", (current,)
                    ).fetchall()
                    for k in kids:
                        pending.append(k["id"])
            else:
                ids = [job_id]
            placeholders = ",".join("?" * len(ids))
            cur = conn.execute(
                f"UPDATE jobs SET state='cancelled', finished_at=? "
                f"WHERE id IN ({placeholders}) AND state NOT IN ('succeeded','failed','cancelled')",
                [now, *ids],
            )
            count = cur.rowcount
            # Recompute status for each worker that owned a job we JUST cancelled
            # (finished_at=now): a freed slot may flip it from saturated 'busy'
            # back to assignable 'idle'. We only consider jobs cancelled in THIS
            # call — a cascade that sweeps an already-finished child must not
            # touch the worker its old owner has since moved on from.
            freed = conn.execute(
                f"SELECT DISTINCT worker_id FROM jobs WHERE id IN ({placeholders}) "
                f"AND state='cancelled' AND finished_at=? AND worker_id IS NOT NULL",
                [*ids, now],
            ).fetchall()
            for fr in freed:
                _refresh_worker_status(conn, fr["worker_id"])
            for jid in ids:
                conn.execute(
                    "INSERT INTO job_logs(job_id, seq, stream, data, ts) "
                    "VALUES (?, (SELECT COALESCE(MAX(seq), 0)+1 FROM job_logs WHERE job_id=?), 'event', ?, ?)",
                    (jid, jid, json.dumps({"type": "cancelled", "cascade": cascade}), now),
                )
            conn.execute("COMMIT")
            return count
        except Exception:
            # Inner error paths may have already rolled back; guard so we
            # don't raise "no transaction is active" over the real error.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def _finalize_job(
    db_path: Path,
    job_id: str,
    state: str,
    result: Any,
    error: Optional[str],
) -> Optional[bool]:
    """Terminate a non-worker-owned job (captain root). Returns None if missing,
    False if worker-owned/terminal, True on success."""
    now = time.time()
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT worker_id, state FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            if row["worker_id"] is not None or row["state"] in TERMINAL_STATES:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                "UPDATE jobs SET state=?, finished_at=?, result=?, error=?, "
                "lease_expires_at=NULL WHERE id=?",
                (
                    state,
                    now,
                    json.dumps(result) if result is not None else None,
                    error,
                    job_id,
                ),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


# ---------- Worker registry ----------


def _register_worker(
    db_path: Path,
    name: str,
    capabilities: dict,
    enroll_id: Optional[str] = None,
    cred_hash: Optional[str] = None,
    policy: Optional[dict] = None,
) -> dict:
    worker_id = uuid.uuid4().hex[:12]
    now = time.time()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO workers("
            "id, name, capabilities, registered_at, last_seen, status, "
            "enroll_id, cred_hash, policy_json) "
            "VALUES (?, ?, ?, ?, ?, 'idle', ?, ?, ?)",
            (
                worker_id,
                name,
                json.dumps(capabilities),
                now,
                now,
                enroll_id,
                cred_hash,
                json.dumps(policy or {}),
            ),
        )
        row = conn.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
    return _row_to_worker(row)


def _retire_superseded_workers(
    db_path: Path, new_worker_id: str, name: str, capabilities: dict
) -> list[str]:
    """When a machine re-enrolls, retire the PRIOR non-offline row(s) it
    supersedes so the new enrollment cleanly replaces it (no stale duplicate
    that must be manually revoked/pruned).

    Conservative by construction: a row is only retired when it clearly belongs
    to *this same machine* re-enrolling — same ``name`` AND same host identity
    (``capabilities.hostname`` when both rows report one; otherwise name alone).
    The brand-new row is never touched, already-offline rows are left as-is, and
    unrelated workers (different name or different hostname) are never affected.
    Retirement reuses revocation semantics: mark revoked + offline and drop the
    credential, so the ghost can't heartbeat its way back in.
    """
    new_host = (capabilities or {}).get("hostname")
    retired: list[str] = []
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, capabilities FROM workers "
            "WHERE name=? AND id != ? AND status != 'offline' AND revoked = 0",
            (name, new_worker_id),
        ).fetchall()
        for r in rows:
            try:
                old_host = (json.loads(r["capabilities"]) or {}).get("hostname")
            except (TypeError, json.JSONDecodeError):
                old_host = None
            # Same host identity required when BOTH rows carry a hostname; if
            # either lacks one, fall back to name-only (the best signal we have).
            if new_host and old_host and new_host != old_host:
                continue
            conn.execute(
                "UPDATE workers SET cred_hash=NULL, status='offline', revoked=1 "
                "WHERE id=?",
                (r["id"],),
            )
            retired.append(r["id"])
    return retired


def _heartbeat_worker(
    db_path: Path,
    worker_id: str,
    capabilities: dict | None = None,
) -> bool:
    now = time.time()
    with _connect(db_path) as conn:
        # A heartbeat is proof of life: a worker the sweeper had marked 'stale'
        # or 'offline' (gap-based) has recovered, so flip it back to 'idle'.
        # 'busy'/'idle' are preserved. A `revoked` worker stays offline (it must
        # never heartbeat its way back in, even in no-auth mode).
        recover = ("status = CASE WHEN status IN ('stale','offline') "
                   "AND revoked = 0 THEN 'idle' ELSE status END")
        # Persist the worker-reported concurrency limit from load.capacity (the
        # pinned wire contract). Only write `capacity` when a heartbeat actually
        # carries a valid load.capacity — otherwise preserve the stored value, so
        # a heartbeat that includes capabilities but omits load/load.capacity
        # doesn't clobber a previously-pinned capacity (e.g. 4) back to 1.
        if capabilities is not None:
            capacity = None
            load = capabilities.get("load")
            if isinstance(load, dict):
                cap = load.get("capacity")
                if isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap >= 1:
                    capacity = int(cap)
            if capacity is not None:
                cur = conn.execute(
                    f"UPDATE workers SET last_seen=?, capabilities=?, capacity=?, {recover} WHERE id=?",
                    (now, json.dumps(capabilities), capacity, worker_id),
                )
            else:
                cur = conn.execute(
                    f"UPDATE workers SET last_seen=?, capabilities=?, {recover} WHERE id=?",
                    (now, json.dumps(capabilities), worker_id),
                )
        else:
            cur = conn.execute(
                f"UPDATE workers SET last_seen=?, {recover} WHERE id=?", (now, worker_id)
            )
        if cur.rowcount == 0:
            return False
        # Renew lease on any jobs this worker is currently running.
        conn.execute(
            "UPDATE jobs SET lease_expires_at=? "
            "WHERE worker_id=? AND state IN ('assigned', 'running')",
            (now + LEASE_TTL, worker_id),
        )
        # Recompute display status from in-flight load vs (now-current) capacity:
        # a recovered worker that still owns work and is saturated must read
        # 'busy', a partially-loaded one stays 'idle' and keeps competing.
        _refresh_worker_status(conn, worker_id)
        return True


def _revoke_worker(db_path: Path, worker_id: str) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE workers SET cred_hash=NULL, status='offline', revoked=1 WHERE id=?",
            (worker_id,),
        )
        return cur.rowcount > 0


def _prune_workers(db_path: Path, older_than_sec: float) -> dict:
    """Explicit admin cleanup: delete worker rows not seen in `older_than_sec`.

    More aggressive than the sweeper's automatic prune (which only drops
    credential-less, non-revoked orphans): this removes any long-dead row by
    age — including enrolled or revoked duplicates left behind when a node
    re-enrols — so the fleet view stops accumulating ghosts. A live node is
    never touched (its last_seen is recent), and a worker that still owns an
    in-flight (assigned/running) job is always spared.
    """
    now = time.time()
    cutoff = now - older_than_sec
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT id, name FROM workers WHERE last_seen < ? "
                "AND id NOT IN (SELECT worker_id FROM jobs "
                "  WHERE state IN ('assigned','running') AND worker_id IS NOT NULL)",
                (cutoff,),
            ).fetchall()
            names = [r["name"] for r in rows]
            for r in rows:
                conn.execute("DELETE FROM workers WHERE id=?", (r["id"],))
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return {"pruned": len(names), "names": names}


def _list_workers(db_path: Path) -> list[dict]:
    with _connect(db_path) as conn:
        cur = conn.execute("SELECT * FROM workers ORDER BY registered_at DESC")
        rows = [_row_to_worker(r) for r in cur.fetchall()]
        # In-flight (running/assigned) job count per worker, so the dashboard can
        # render "2/4 running" against capacity.
        inflight = {
            r["worker_id"]: r["n"]
            for r in conn.execute(
                "SELECT worker_id, COUNT(*) AS n FROM jobs "
                "WHERE worker_id IS NOT NULL AND state IN ('assigned','running') "
                "GROUP BY worker_id"
            ).fetchall()
        }
    now = time.time()
    for r in rows:
        # Surface capacity + live in-flight count for the panel/MCP/scripts.
        r["capacity"] = r.get("capacity") or 1
        r["running"] = int(inflight.get(r["id"], 0))
        if r["status"] == "offline":
            continue
        gap = now - r["last_seen"]
        if gap >= OFFLINE_AFTER:
            r["status"] = "offline"
        elif gap >= STALE_AFTER:
            r["status"] = "stale"
    return rows


def _worker_by_cred_hash(db_path: Path, cred_hash: str) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM workers WHERE cred_hash = ?", (cred_hash,)
        ).fetchone()
    return _row_to_worker(row)


def _get_worker(db_path: Path, worker_id: str) -> Optional[dict]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM workers WHERE id=?", (worker_id,)).fetchone()
    return _row_to_worker(row)


# ---------- Assignment / events ----------


def _score_worker_row(row: sqlite3.Row) -> dict[str, Any]:
    """Lift a worker row into the dict shape placement_score expects."""
    return {
        "id": row["id"],
        "status": row["status"],
        "capacity": row["capacity"],
        "capabilities": json.loads(row["capabilities"]),
        "last_assigned_at": row["last_assigned_at"],
    }


def _inflight_count(conn: sqlite3.Connection, worker_id: str) -> int:
    """Number of in-flight jobs currently owned by this worker
    (state IN assigned/running). This is the work in flight that gates
    capacity-based assignment."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs "
        "WHERE worker_id=? AND state IN ('assigned','running')",
        (worker_id,),
    ).fetchone()
    return int(row["n"] or 0)


def _refresh_worker_status(conn: sqlite3.Connection, worker_id: str) -> None:
    """Recompute the DISPLAY status of a worker from its in-flight load vs its
    reported capacity. 'busy' means SATURATED (in-flight >= capacity); a
    partially-loaded worker (running < capacity) stays 'idle' so it remains
    assignable. Never disturbs 'stale'/'offline'/revoked rows — those are owned
    by the liveness sweeper and revocation."""
    row = conn.execute(
        "SELECT status, capacity FROM workers WHERE id=?", (worker_id,)
    ).fetchone()
    if row is None or row["status"] in ("stale", "offline"):
        return
    capacity = row["capacity"] or 1
    inflight = _inflight_count(conn, worker_id)
    new_status = "busy" if inflight >= capacity else "idle"
    if new_status != row["status"]:
        conn.execute(
            "UPDATE workers SET status=? WHERE id=?", (new_status, worker_id)
        )


def _try_assign_one(db_path: Path, worker_id: str) -> Optional[dict]:
    """Pull-side placement: decide whether the polling worker should take a
    queued job *now*, or leave it for a better-fit worker (Decision V2-2/V2-4).

    For each queued job the polling worker is capable of (oldest first), score
    all currently-assignable capable workers; the polling worker takes the job
    iff it is the (tied) best fit OR the job has already waited past
    PLACEMENT_GRACE. A worker is assignable while it has a free concurrency slot
    (in-flight job count < its reported capacity), not merely when fully idle.
    """
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = time.time()
            me_row = conn.execute(
                "SELECT id, name, status, capacity, capabilities, last_assigned_at "
                "FROM workers WHERE id=?",
                (worker_id,),
            ).fetchone()
            if not me_row:
                conn.execute("ROLLBACK")
                return None
            # Capacity gate: refuse only when SATURATED (no free slot). A worker
            # the sweeper marked stale/offline is not assignable either.
            if me_row["status"] in ("stale", "offline"):
                conn.execute("ROLLBACK")
                return None
            my_capacity = me_row["capacity"] or 1
            if _inflight_count(conn, worker_id) >= my_capacity:
                conn.execute("ROLLBACK")
                return None
            me = _score_worker_row(me_row)
            # Reflect this worker's live free slots so placement_score spreads load.
            me["capabilities"].setdefault("load", {})
            me["capabilities"]["load"]["running"] = _inflight_count(conn, worker_id)
            me["capabilities"]["load"]["capacity"] = my_capacity

            # Other recently-seen workers with a free slot that might out-compete
            # us (idle, or busy-but-not-saturated). Saturated workers carry
            # status='busy' and are excluded.
            other_rows = conn.execute(
                "SELECT id, status, capacity, capabilities, last_assigned_at FROM workers "
                "WHERE id != ? AND status='idle' AND last_seen >= ?",
                (worker_id, now - STALE_AFTER),
            ).fetchall()
            others = []
            for r in other_rows:
                w = _score_worker_row(r)
                w_inflight = _inflight_count(conn, r["id"])
                w_cap = r["capacity"] or 1
                if w_inflight >= w_cap:
                    continue  # saturated despite a stale 'idle' label
                w["capabilities"].setdefault("load", {})
                w["capabilities"]["load"]["running"] = w_inflight
                w["capabilities"]["load"]["capacity"] = w_cap
                others.append(w)

            rows = conn.execute(
                "SELECT * FROM jobs WHERE state='queued' ORDER BY created_at ASC"
            ).fetchall()
            chosen = None
            for row in rows:
                requires = json.loads(row["requires"])
                if not matches(me["capabilities"], requires):
                    continue
                spec = json.loads(row["spec"])
                # Hard worker-pin (`target`): a job that names a target may be
                # taken ONLY by the worker whose id == target, or whose name ==
                # target while it is not offline. Every other worker skips it
                # unconditionally (never falls through on placement-grace), so a
                # pinned job stays queued until its target polls — and if the
                # target doesn't exist / is offline, it simply waits.
                target = spec.get("target")
                if target is not None:
                    is_target = me_row["id"] == target or (
                        me_row["name"] == target and me_row["status"] != "offline"
                    )
                    if not is_target:
                        continue
                waited = now - (row["created_at"] or now)
                # bare-worker (kind: auto): never re-grab a task this worker already
                # declined — a poor fit won't become a good one. The task waits for a
                # capable node; it only fails once ALL capable nodes have declined.
                if worker_id in _decliner_set(row["declined_by"]):
                    continue
                job = {"prefer": spec.get("prefer"), "requires": requires}
                if waited >= PLACEMENT_GRACE:
                    chosen = row  # don't starve: take it regardless of fit
                    break
                my_score = placement_score(me, job, now=now)
                # Best competing fit among *capable* idle others.
                best_other = max(
                    (
                        placement_score(w, job, now=now)
                        for w in others
                        if matches(w["capabilities"], requires)
                    ),
                    default=float("-inf"),
                )
                if my_score >= best_other - 1e-9:
                    chosen = row
                    break
                # else: a better-fit worker exists and is expected to poll; skip.
            if chosen is None:
                conn.execute("ROLLBACK")
                return None
            new_attempt = (chosen["attempt"] or 0) + 1
            conn.execute(
                "UPDATE jobs SET state='assigned', worker_id=?, assigned_at=?, "
                "lease_expires_at=?, attempt=? WHERE id=?",
                (worker_id, now, now + LEASE_TTL, new_attempt, chosen["id"]),
            )
            conn.execute(
                "UPDATE workers SET last_seen=?, last_assigned_at=? WHERE id=?",
                (now, now, worker_id),
            )
            # 'busy' only when this assignment SATURATES the worker; a worker with
            # spare capacity stays 'idle' and keeps competing for more work.
            _refresh_worker_status(conn, worker_id)
            conn.execute("COMMIT")
            return _row_to_job(
                conn.execute(
                    "SELECT * FROM jobs WHERE id=?", (chosen["id"],)
                ).fetchone()
            )
        except Exception:
            # Inner error paths may have already rolled back; guard so we
            # don't raise "no transaction is active" over the real error.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def _append_log(db_path: Path, job_id: str, stream: str, data: str) -> int:
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM job_logs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            seq = (row["s"] or 0) + 1
            now = time.time()
            conn.execute(
                "INSERT INTO job_logs(job_id, seq, stream, data, ts) VALUES (?,?,?,?,?)",
                (job_id, seq, stream, data, now),
            )
            # Every log line is a sign of life. Bump the liveness timestamp; for
            # human-readable streams also keep a compact snapshot of the latest
            # line (event lines are raw JSON, so a worker-supplied `activity`
            # string via the event endpoint is preferred for those).
            if stream in ("stdout", "stderr") and data.strip():
                snippet = data.strip().replace("\n", " ")[:160]
                conn.execute(
                    "UPDATE jobs SET last_activity_at=?, last_activity=? WHERE id=?",
                    (now, snippet, job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET last_activity_at=? WHERE id=?", (now, job_id)
                )
            conn.execute("COMMIT")
            return seq
        except Exception:
            # Inner error paths may have already rolled back; guard so we
            # don't raise "no transaction is active" over the real error.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def _read_logs(
    db_path: Path, job_id: str, since_seq: int = 0, limit: int = 1000
) -> list[dict]:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "SELECT seq, stream, data, ts FROM job_logs "
            "WHERE job_id=? AND seq > ? ORDER BY seq ASC LIMIT ?",
            (job_id, since_seq, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def _decliner_set(value: Optional[str]) -> set[str]:
    """Parse a job's ``declined_by`` (a JSON array of worker ids; tolerates a legacy
    bare-id string) into a set."""
    if not value:
        return set()
    try:
        parsed = json.loads(value)
        return set(parsed) if isinstance(parsed, list) else {str(parsed)}
    except (json.JSONDecodeError, TypeError):
        return {value}  # legacy single-id format


def _online_capable_ids(conn: sqlite3.Connection, requires: dict, now: float) -> set[str]:
    """Worker ids that are online (recently seen, not offline/revoked) and whose
    capabilities satisfy ``requires`` — i.e. could run this job now or once free."""
    out: set[str] = set()
    for w in conn.execute(
        "SELECT id, capabilities FROM workers "
        "WHERE revoked=0 AND status != 'offline' AND last_seen >= ?",
        (now - STALE_AFTER,),
    ).fetchall():
        try:
            caps = json.loads(w["capabilities"])
        except (json.JSONDecodeError, TypeError):
            caps = {}
        if matches(caps, requires):
            out.add(w["id"])
    return out


def _apply_event(
    db_path: Path,
    job_id: str,
    worker_id: str,
    event: dict,
) -> tuple[dict, bool]:
    """Apply a worker-reported event. Returns (job, accepted).

    Stale events (from an attempt that the control plane has already
    superseded via lease expiry + requeue) are ignored.
    """
    etype = event.get("type")
    reported_attempt = event.get("attempt")
    now = time.time()
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                conn.execute("ROLLBACK")
                raise KeyError(job_id)
            if row["worker_id"] != worker_id:
                conn.execute("ROLLBACK")
                raise PermissionError("job is not assigned to this worker")
            if reported_attempt is not None and reported_attempt != row["attempt"]:
                # Stale — server has moved on, ignore.
                conn.execute("ROLLBACK")
                return _row_to_job(row), False
            if row["state"] in ("succeeded", "failed", "cancelled"):
                # Already terminal (e.g. cancelled out from under the worker) —
                # don't let a late event resurrect or relabel it.
                conn.execute("ROLLBACK")
                return _row_to_job(row), False
            if etype == "started":
                conn.execute(
                    "UPDATE jobs SET state='running', started_at=?, lease_expires_at=?, "
                    "last_activity_at=?, last_activity=? WHERE id=?",
                    (now, now + LEASE_TTL, now, "started", job_id),
                )
            elif etype in ("succeeded", "failed"):
                tokens_used = int(event.get("tokens_used") or 0)
                # On FAILED, persist the worker's agentic root-cause diagnosis
                # (pinned wire contract). Absent (older workers) → store NULL.
                diagnosis = None
                if etype == "failed":
                    raw_diag = event.get("diagnosis")
                    if raw_diag is not None:
                        diagnosis = str(raw_diag)[:300]
                conn.execute(
                    "UPDATE jobs SET state=?, finished_at=?, exit_code=?, "
                    "result=?, error=?, lease_expires_at=NULL, tokens_used=?, "
                    "diagnosis=? WHERE id=?",
                    (
                        etype,
                        now,
                        event.get("exit_code"),
                        json.dumps(event.get("result")) if event.get("result") is not None else None,
                        event.get("error"),
                        tokens_used,
                        diagnosis,
                        job_id,
                    ),
                )
                if tokens_used:
                    conn.execute(
                        "UPDATE jobs SET tree_budget_spent = COALESCE(tree_budget_spent,0) + ? "
                        "WHERE id = ?",
                        (tokens_used, row["root_job_id"] or job_id),
                    )
                conn.execute(
                    "UPDATE workers SET last_seen=? WHERE id=?", (now, worker_id)
                )
                # Freed a slot — may flip the worker from saturated 'busy' to 'idle'.
                _refresh_worker_status(conn, worker_id)
            elif etype == "declined":
                # Bare-worker self-selection: this worker judged itself a poor fit.
                # Record it in the decliner set and requeue for a capable node. Escalate
                # to failed ONLY when every currently-online capable worker has declined
                # (genuine "no node can do this") — not while a capable node is just busy.
                reason = str(event.get("reason") or "declined")[:200]
                decliners = _decliner_set(row["declined_by"])
                decliners.add(worker_id)
                new_count = (row["decline_count"] or 0) + 1
                requires = json.loads(row["requires"])
                remaining = _online_capable_ids(conn, requires, now) - decliners
                # Escalate to permanent failure only when:
                #   * the hard backstop trips (pathological flapping), OR
                #   * no capable worker remains AND at least two DISTINCT nodes
                #     have declined — a genuine "no node can do this".
                # A single decline with a momentarily-empty capable set (a capable
                # node merely stale/offline for one sweep window) must NOT fail the
                # job permanently — it just requeues and waits for the node to return.
                escalate = (
                    new_count >= MAX_DECLINES
                    or (not remaining and len(decliners) >= 2)
                )
                if escalate:
                    conn.execute(
                        "UPDATE jobs SET state='failed', finished_at=?, "
                        "lease_expires_at=NULL, worker_id=NULL, decline_count=?, "
                        "declined_by=?, error=?, last_activity=? WHERE id=?",
                        (now, new_count, json.dumps(sorted(decliners)),
                         f"declined by all {len(decliners)} capable node(s); "
                         f"last reason: {reason}",
                         "escalated: declined-by-all", job_id),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET state='queued', worker_id=NULL, "
                        "assigned_at=NULL, started_at=NULL, lease_expires_at=NULL, "
                        "decline_count=?, declined_by=?, last_activity=? WHERE id=?",
                        (new_count, json.dumps(sorted(decliners)),
                         f"declined by {worker_id}: {reason}", job_id),
                    )
                conn.execute(
                    "UPDATE workers SET last_seen=? WHERE id=?", (now, worker_id)
                )
                # The declined job no longer counts against this worker — a freed
                # slot may flip it from saturated 'busy' back to 'idle'.
                _refresh_worker_status(conn, worker_id)
            elif etype == "progress":
                # token-meter + liveness checkpoint mid-run
                activity = event.get("activity")
                if activity:
                    conn.execute(
                        "UPDATE jobs SET tokens_used=?, lease_expires_at=?, "
                        "last_activity_at=?, last_activity=? WHERE id=?",
                        (int(event.get("tokens_used") or 0), now + LEASE_TTL,
                         now, str(activity)[:160], job_id),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET tokens_used=?, lease_expires_at=?, "
                        "last_activity_at=? WHERE id=?",
                        (int(event.get("tokens_used") or 0), now + LEASE_TTL, now, job_id),
                    )
            conn.execute("COMMIT")
            updated = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return _row_to_job(updated), True
        except Exception:
            # Inner error paths may have already rolled back; guard so we
            # don't raise "no transaction is active" over the real error.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


# ---------- Sweeper ----------


def _sweep(db_path: Path) -> dict[str, int]:
    """Mark stale/offline workers; requeue jobs whose leases expired."""
    now = time.time()
    counts = {"requeued": 0, "failed_attempts": 0, "stale": 0, "offline": 0, "pruned": 0}
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Worker liveness.
            cur = conn.execute(
                "UPDATE workers SET status='stale' "
                "WHERE status NOT IN ('stale','offline') AND last_seen < ?",
                (now - STALE_AFTER,),
            )
            counts["stale"] = cur.rowcount
            cur = conn.execute(
                "UPDATE workers SET status='offline' "
                "WHERE status != 'offline' AND last_seen < ?",
                (now - OFFLINE_AFTER,),
            )
            counts["offline"] = cur.rowcount
            # Job lease expiry.
            expired = conn.execute(
                "SELECT id, attempt, max_attempts, worker_id FROM jobs "
                "WHERE state IN ('assigned','running') AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at < ?",
                (now,),
            ).fetchall()
            freed_workers: set[str] = set()
            for row in expired:
                # Free the worker that held the expired lease (it's presumed
                # gone): the job leaves its in-flight set below, then we
                # recompute status so a now-unsaturated worker can pick up the
                # requeued job (or anything else) instead of staying 'busy'.
                if row["worker_id"]:
                    freed_workers.add(row["worker_id"])
                if row["attempt"] >= row["max_attempts"]:
                    conn.execute(
                        "UPDATE jobs SET state='failed', finished_at=?, "
                        "error='lease_expired', lease_expires_at=NULL WHERE id=?",
                        (now, row["id"]),
                    )
                    counts["failed_attempts"] += 1
                else:
                    conn.execute(
                        "UPDATE jobs SET state='queued', worker_id=NULL, "
                        "assigned_at=NULL, started_at=NULL, lease_expires_at=NULL "
                        "WHERE id=?",
                        (row["id"],),
                    )
                    counts["requeued"] += 1
                conn.execute(
                    "INSERT INTO job_logs(job_id, seq, stream, data, ts) "
                    "VALUES (?, (SELECT COALESCE(MAX(seq), 0)+1 FROM job_logs WHERE job_id=?), 'event', ?, ?)",
                    (
                        row["id"],
                        row["id"],
                        json.dumps({"type": "lease_expired", "attempt": row["attempt"]}),
                        now,
                    ),
                )
            for wid in freed_workers:
                _refresh_worker_status(conn, wid)
            # Hygiene: prune long-dead orphan worker rows. A non-enrolled worker
            # that reconnects after an outage registers a fresh row and abandons
            # the old one, which ages to 'offline' but is never deleted — so
            # /derived and the panel accumulate stale rows. Conservatively delete
            # ONLY stale, credential-less (shared-token/LAN) duplicate rows:
            #   - offline and unseen past WORKER_PRUNE_TTL (a briefly-down node is
            #     never pruned),
            #   - owns no in-flight (assigned/running) job,
            #   - has no credential (cred_hash IS NULL): an enrolled/credentialed
            #     node that's merely powered off must keep its row so reconnect
            #     re-authenticates instead of failing 401 → forced re-enroll,
            #   - is not revoked (preserve the revocation audit record).
            cur = conn.execute(
                "DELETE FROM workers WHERE status='offline' AND last_seen < ? "
                "AND cred_hash IS NULL AND revoked = 0 "
                "AND id NOT IN (SELECT worker_id FROM jobs "
                "               WHERE state IN ('assigned','running') AND worker_id IS NOT NULL)",
                (now - WORKER_PRUNE_TTL,),
            )
            counts["pruned"] = cur.rowcount
            conn.execute("COMMIT")
        except Exception:
            # Inner error paths may have already rolled back; guard so we
            # don't raise "no transaction is active" over the real error.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return counts


# ---------- Enrollment ----------


def _mint_enroll_token(
    db_path: Path,
    label: Optional[str],
    policy: Optional[dict],
    ttl: float = ENROLL_TOKEN_TTL,
) -> tuple[str, float]:
    raw = ENROLL_PREFIX + secrets.token_urlsafe(24)
    now = time.time()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO enroll_tokens(token_hash, label, policy_json, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (_hash_cred(raw), label, json.dumps(policy or {}), now, now + ttl),
        )
    return raw, now + ttl


def _consume_enroll_token(db_path: Path, token: str) -> dict:
    """Validate + mark used. Returns the row dict on success."""
    th = _hash_cred(token)
    now = time.time()
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM enroll_tokens WHERE token_hash=?", (th,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise PermissionError("invalid enrollment token")
            if row["used_at"] is not None:
                conn.execute("ROLLBACK")
                raise PermissionError("enrollment token already used")
            if row["expires_at"] < now:
                conn.execute("ROLLBACK")
                raise PermissionError("enrollment token expired")
            conn.execute(
                "UPDATE enroll_tokens SET used_at=? WHERE token_hash=?",
                (now, th),
            )
            conn.execute("COMMIT")
            return dict(row)
        except Exception:
            # Inner error paths may have already rolled back; guard so we
            # don't raise "no transaction is active" over the real error.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def _list_enroll_tokens(db_path: Path) -> list[dict]:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "SELECT label, created_at, expires_at, used_at, used_by_worker "
            "FROM enroll_tokens ORDER BY created_at DESC"
        )
        return [dict(r) for r in cur.fetchall()]


# ---------- Scoped client tokens (`roost pair` / `roost token`) ----------
#
# A scoped api_token is a long-lived bearer for a *client* front door — a phone
# app ('mobile'), a Codex / script integration ('agent'), anything that plugs
# into your personal compute backend without being handed the admin token.
# Both scopes authenticate as kind "client" and share ONE permission set; the
# scope column is an audit/label distinction, not a privilege boundary.
#
#   scope → allowed verbs (enforced by require_any + explicit guards below):
#     READ   GET  /derived /jobs* /workers /workers/{id}    (observe the fleet)
#     SUBMIT POST /jobs                                       (queue work)
#     CANCEL DELETE /jobs/{id}                                (cancel own/any job)
#     BLOBS  POST /blobs, POST /blobs/presign, GET /blobs,
#            GET /blobs/{id}                                  (stage/list/download)
#
#   client tokens are explicitly DENIED (require_admin / require_worker reject
#   kind "client"): mint tokens (/enroll-tokens, /pair-tokens), enroll workers,
#   revoke or prune workers, finalize jobs, read /claude-creds, DELETE /blobs,
#   and every worker-plane endpoint (lease/poll/heartbeat/report, /triage-prompt).
#
# Allowed scope values; "mobile" stays the default for backward compatibility.
API_TOKEN_SCOPES = ("mobile", "agent")


def _mint_api_token(db_path: Path, label: Optional[str], scope: str = "mobile") -> dict:
    raw = MOBILE_TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_id = uuid.uuid4().hex[:12]
    now = time.time()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO api_tokens(id, token_hash, label, scope, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token_id, _hash_cred(raw), label, scope, now),
        )
    return {"id": token_id, "token": raw, "label": label, "scope": scope,
            "created_at": now}


def _api_token_by_hash(db_path: Path, token_hash: str) -> Optional[dict]:
    """Active (non-revoked) api_token row by hash; touches last_used_at at
    most once a minute so the auth path stays read-mostly."""
    now = time.time()
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM api_tokens WHERE token_hash=? AND revoked=0",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        if (d.get("last_used_at") or 0) < now - 60:
            conn.execute(
                "UPDATE api_tokens SET last_used_at=? WHERE id=?", (now, d["id"])
            )
        d.pop("token_hash", None)  # never leak hash
        return d


def _list_api_tokens(db_path: Path) -> list[dict]:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "SELECT id, label, scope, created_at, last_used_at, revoked "
            "FROM api_tokens ORDER BY created_at DESC"
        )
        return [dict(r) for r in cur.fetchall()]


def _revoke_api_token(db_path: Path, token_id: str) -> bool:
    with _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE api_tokens SET revoked=1 WHERE id=? AND revoked=0", (token_id,)
        )
        return cur.rowcount > 0


# ---------- Log retention (called periodically) ----------


def _prune_logs(db_path: Path, max_age_sec: float, max_rows_per_job: int) -> int:
    cutoff = time.time() - max_age_sec
    with _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM job_logs WHERE ts < ?", (cutoff,))
        pruned = cur.rowcount
        # Cap per-job log row count.
        conn.execute(
            f"""
            DELETE FROM job_logs WHERE rowid IN (
                SELECT rowid FROM job_logs WHERE (job_id, seq) IN (
                    SELECT job_id, seq FROM (
                        SELECT job_id, seq,
                               ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY seq DESC) AS rn
                        FROM job_logs
                    ) WHERE rn > {int(max_rows_per_job)}
                )
            )
            """
        )
        pruned += conn.execute("SELECT changes()").fetchone()[0]
    return pruned


# ---------- Pydantic models ----------


class JobSubmit(BaseModel):
    intent: Optional[str] = None
    task: Optional[str] = None  # kind: auto — plain-language task; the worker self-assesses
    command: Optional[Any] = None  # str | list[str]
    args: Optional[list[str]] = None
    cwd: Optional[str] = None
    env: Optional[dict[str, str]] = None
    requires: dict[str, Any] = Field(default_factory=dict)
    prefer: Optional[Any] = None  # soft routing hint, e.g. {"worker": "<id>"} (V2-4)
    target: Optional[str] = None  # HARD worker-pin: a worker id OR name; only that worker may take it
    success_criteria: Optional[str] = None
    budget: dict[str, Any] = Field(default_factory=dict)
    permissions: dict[str, Any] = Field(default_factory=dict)
    hierarchy: dict[str, Any] = Field(default_factory=dict)
    kind: Optional[str] = None
    verify: Optional[bool] = None  # trust loop: independent verifier checks the goal (auto: on)
    image: Optional[str] = None  # kind: docker — image to run the job in
    container: Optional[dict[str, Any]] = None  # kind: docker — gpus/cpus/memory/volumes/env/...
    model: Optional[str] = None
    subagent_model: Optional[str] = None
    parent_job_id: Optional[str] = None  # populated by roost-mcp dispatches
    max_attempts: Optional[int] = None
    captain_root: bool = False  # anchor a captain plan's lineage/budget (V2-1)


class ScheduleCreate(BaseModel):
    spec: dict[str, Any]
    every: Any  # seconds (number) or "<N>[smhd]" (e.g. "30m")
    name: Optional[str] = None
    enabled: bool = True


class SchedulePatch(BaseModel):
    enabled: bool


class WorkerRegister(BaseModel):
    name: str
    capabilities: dict[str, Any] = Field(default_factory=dict)


class EnrollRequest(BaseModel):
    token: str
    name: str
    capabilities: dict[str, Any] = Field(default_factory=dict)


class EnrollTokenRequest(BaseModel):
    label: Optional[str] = None
    policy: dict[str, Any] = Field(default_factory=dict)
    ttl_sec: Optional[float] = None


class PairTokenRequest(BaseModel):
    label: Optional[str] = None
    scope: Optional[str] = None  # "mobile" (default) | "agent"


class HeartbeatPayload(BaseModel):
    capabilities: Optional[dict[str, Any]] = None


class JobEvent(BaseModel):
    type: str  # started | succeeded | failed | progress | declined
    attempt: Optional[int] = None
    exit_code: Optional[int] = None
    result: Any = None
    error: Optional[str] = None
    tokens_used: Optional[int] = None
    activity: Optional[str] = None  # compact "what it's doing now" (liveness)
    diagnosis: Optional[str] = None  # root-cause on FAILED (≤~300 chars)


# ---------- App factory ----------


def _read_host_claude_creds() -> Optional[str]:
    """Read the operator's Claude Code credentials (~/.claude/.credentials.json)
    so an enrolling worker can be provisioned with the same auth. Returns the
    raw file text, or None if absent/unreadable. Linux-only path (matches the
    fleet); on macOS Claude stores creds in the Keychain, not this file."""
    path = Path.home() / ".claude" / ".credentials.json"
    try:
        text = path.read_text()
        json.loads(text)  # validate it's well-formed before handing it out
        return text
    except (OSError, json.JSONDecodeError):
        return None


# Default install command a worker runs when it lacks Claude Code. Centralised
# here so the operator controls it fleet-wide via the enroll response.
CLAUDE_INSTALL_CMD = "curl -fsSL https://claude.ai/install.sh | bash"


def create_app(
    db_path: Optional[Path] = None,
    token: Optional[str] = None,
    *,
    run_sweeper: bool = True,
    provision_claude_auth: bool = True,
    publish_domain: Optional[str] = None,
) -> FastAPI:
    db = Path(db_path or os.environ.get("ROOST_DB", DEFAULT_DB))
    shared_token = token if token is not None else os.environ.get("ROOST_TOKEN", "")
    # When set (e.g. "roost.pub"), published sites are addressable as
    # https://<slug>.<domain>/ through a tunnel, and requests arriving under
    # that domain can ONLY reach site content (see the host middleware below).
    publish_domain = (
        publish_domain
        if publish_domain is not None
        else os.environ.get("ROOST_PUBLISH_DOMAIN") or None
    )
    _init_db(db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sweeper_task: Optional[asyncio.Task] = None
        if run_sweeper:
            sweeper_task = asyncio.create_task(_sweep_loop(db))
        yield
        if sweeper_task:
            sweeper_task.cancel()
            try:
                await sweeper_task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="Roost", version="0.2.0", lifespan=lifespan)
    app.state.db_path = db
    app.state.shared_token = shared_token
    app.state.publish_domain = publish_domain

    if publish_domain:
        # PUBLIC-EDGE GUARD + host routing. The tunnel (cloudflared) forwards
        # *.<publish_domain> to this same origin, so the Host header is the
        # only thing separating "the internet" from "the LAN API". Any request
        # arriving under the publish domain is answered HERE — site content
        # only — and never falls through to the fleet API. A request for
        # demo.<domain> serves sites/demo/ at the root.
        @app.middleware("http")
        async def public_host_router(request: Request, call_next):
            host = request.headers.get("host", "")
            hostname = host.split(":", 1)[0].strip().lower().rstrip(".")
            apex = publish_domain.lower()
            if hostname != apex and not hostname.endswith("." + apex):
                return await call_next(request)  # LAN/API traffic: untouched

            if request.method not in ("GET", "HEAD"):
                return PlainTextResponse("method not allowed", status_code=405)

            if hostname == apex:
                # Apex: a one-line landing. Deliberately no site listing —
                # published slugs are shared by their owners, not enumerated.
                if request.url.path == "/":
                    return HTMLResponse(
                        "<!doctype html><title>roost</title>"
                        "<body style='font-family:system-ui;text-align:center;"
                        "padding-top:4rem'><h3>🐦 roost.pub</h3>"
                        "<p>Sites published from someone's own fleet.</p>")
                return PlainTextResponse("not found", status_code=404)

            slug = publishlib.slug_for_host(hostname, publish_domain)
            if slug is None:
                return PlainTextResponse("not found", status_code=404)
            served = await asyncio.to_thread(
                publishlib.resolve_served_path, db, slug, request.url.path)
            if served is None:
                return PlainTextResponse("not found", status_code=404)
            return FileResponse(served)

    def authenticate(request: Request, authorization: Optional[str] = Header(None)) -> dict:
        """Returns dict with at least {kind: 'shared'|'worker'|'client'|'none',
        worker?: dict, token?: dict, scope?: str}.

        A scoped api_token (minted via /pair-tokens, any scope) authenticates as
        kind "client": a non-admin, non-worker front door. require_admin and
        require_worker both reject it; its allowed verbs are documented in the
        scope→verbs matrix above."""
        if not shared_token:
            return {"kind": "none"}
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer token")
        raw = authorization[7:]
        if raw == shared_token:
            return {"kind": "shared"}
        cred_hash = _hash_cred(raw)
        worker = _worker_by_cred_hash(db, cred_hash)
        if worker is not None:
            return {"kind": "worker", "worker": worker}
        api_token = _api_token_by_hash(db, cred_hash)
        if api_token is not None:
            return {
                "kind": "client",
                "scope": api_token.get("scope") or "mobile",
                "token": api_token,
            }
        raise HTTPException(401, "invalid bearer token")

    def require_admin(principal: dict = Depends(authenticate)) -> dict:
        if principal["kind"] not in ("shared", "none"):
            raise HTTPException(403, "admin auth required")
        return principal

    def require_any(principal: dict = Depends(authenticate)) -> dict:
        return principal

    def require_worker(principal: dict = Depends(authenticate)) -> dict:
        if principal["kind"] == "none":
            return principal  # auth disabled
        if principal["kind"] == "shared":
            return principal  # admin can act as any worker
        if principal["kind"] == "worker":
            return principal
        # 'client' (mobile/agent) lands here: front-door tokens never get
        # worker-plane access (creds provisioning, lease/poll, heartbeat).
        raise HTTPException(403, "worker credential required")

    def require_matching_worker(
        worker_id: str, principal: dict = Depends(require_worker)
    ) -> dict:
        """Like require_worker, but a *worker* credential may only act on its OWN
        path worker_id (a worker can't impersonate another). Admin/shared and
        auth-disabled modes are unaffected."""
        if principal["kind"] == "worker" and principal["worker"]["id"] != worker_id:
            raise HTTPException(403, "worker credential does not match the path worker_id")
        return principal

    # ---- public health + installer ----

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "version": "0.2.0"}

    @app.get("/readyz")
    async def readyz():
        """Readiness probe (unauthenticated, like /healthz): does a trivial DB
        read to prove the control plane can actually serve. 503 if the DB is
        unreachable, so a load balancer can drain a broken instance."""
        def _probe() -> int:
            with _connect(db) as conn:
                row = conn.execute("SELECT COUNT(*) AS n FROM workers").fetchone()
                return int(row["n"])
        try:
            n = await asyncio.to_thread(_probe)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(status_code=503, content={"ready": False, "error": str(e)})
        return {"ready": True, "workers": n, "version": "0.2.0"}

    @app.get("/install.sh", response_class=PlainTextResponse)
    async def install_sh(request: Request):
        return render_install_script(str(request.base_url).rstrip("/"))

    _panel_html = Path(__file__).parent / "panel.html"

    @app.get("/panel", response_class=HTMLResponse)
    async def panel():
        # Live fleet dashboard (the data fetches inside carry ?token=… as a
        # bearer header). The HTML itself is static and harmless, so unauthenticated.
        try:
            return _panel_html.read_text()
        except OSError:
            raise HTTPException(404, "panel not available")

    # ---- enrollment ----

    @app.post("/enroll-tokens", dependencies=[Depends(require_admin)])
    async def mint_token(payload: EnrollTokenRequest):
        ttl = payload.ttl_sec or ENROLL_TOKEN_TTL
        raw, exp = await asyncio.to_thread(
            _mint_enroll_token, db, payload.label, payload.policy, ttl
        )
        return {"token": raw, "expires_at": exp}

    @app.get("/enroll-tokens", dependencies=[Depends(require_admin)])
    async def list_tokens():
        return await asyncio.to_thread(_list_enroll_tokens, db)

    # ---- client front-door tokens (scoped api_tokens: phones + agents) ----

    @app.post("/pair-tokens", dependencies=[Depends(require_admin)])
    async def mint_pair_token(payload: PairTokenRequest):
        """Mint a long-lived scoped token for a client front door — a phone
        ('mobile', the default) or a Codex/script integration ('agent'). Both
        scopes share the same client permission set (see the scope→verbs matrix);
        the scope is an audit label, so an operator can tell phones from agents.

        The raw token is returned exactly once; only its hash is stored."""
        scope = payload.scope or "mobile"
        if scope not in API_TOKEN_SCOPES:
            raise HTTPException(
                400, f"unknown scope {scope!r}; allowed: {', '.join(API_TOKEN_SCOPES)}")
        return await asyncio.to_thread(_mint_api_token, db, payload.label, scope)

    @app.get("/pair-tokens", dependencies=[Depends(require_admin)])
    async def list_pair_tokens():
        return await asyncio.to_thread(_list_api_tokens, db)

    @app.delete("/pair-tokens/{token_id}", dependencies=[Depends(require_admin)])
    async def revoke_pair_token(token_id: str):
        ok = await asyncio.to_thread(_revoke_api_token, db, token_id)
        if not ok:
            raise HTTPException(404, "token not found (or already revoked)")
        return {"revoked": True}

    @app.post("/enroll")
    async def enroll(payload: EnrollRequest):
        try:
            tok_row = await asyncio.to_thread(_consume_enroll_token, db, payload.token)
        except PermissionError as e:
            raise HTTPException(403, str(e))
        policy = json.loads(tok_row["policy_json"] or "{}")
        raw_cred = WORKER_CRED_PREFIX + secrets.token_urlsafe(32)
        cred_hash = _hash_cred(raw_cred)
        # Token row's `token_hash` identifies which enrollment was used.
        worker = await asyncio.to_thread(
            _register_worker,
            db,
            payload.name,
            payload.capabilities,
            tok_row["token_hash"],
            cred_hash,
            policy,
        )
        # Link token → worker for audit.
        with _connect(db) as conn:
            conn.execute(
                "UPDATE enroll_tokens SET used_by_worker=? WHERE token_hash=?",
                (worker["id"], tok_row["token_hash"]),
            )
        # Dedup: a node re-enrolling under the same name + host identity cleanly
        # replaces its prior row(s); retire the superseded ghost so it doesn't
        # linger as a stale duplicate needing manual revoke/prune.
        await asyncio.to_thread(
            _retire_superseded_workers,
            db,
            worker["id"],
            payload.name,
            payload.capabilities,
        )
        resp = {"worker_id": worker["id"], "credential": raw_cred, "policy": policy}
        # Onboarding (v1): help the worker run Claude Code. Install it if missing
        # and provision auth by COPYING the operator's credentials. Gated by the
        # serve-level switch and opt-out-able per token (`provision_claude:false`).
        # `auth.method` leaves room for future schemes (api_key, interactive).
        if provision_claude_auth and policy.get("provision_claude", True):
            onboarding: dict[str, Any] = {
                "install_claude": True,
                "install_cmd": CLAUDE_INSTALL_CMD,
            }
            creds = await asyncio.to_thread(_read_host_claude_creds)
            if creds is not None:
                onboarding["auth"] = {
                    "method": "copy",
                    "target": "~/.claude/.credentials.json",
                    "credentials_json": creds,
                }
            resp["onboarding"] = onboarding
        return resp

    @app.get("/claude-creds", dependencies=[Depends(require_worker)])
    async def claude_creds():
        """Current operator Claude credentials, for a worker to refresh its local
        copy before the access token expires (copied OAuth creds rotate, so a
        one-time copy goes stale fleet-wide). Gated by the same provisioning flag."""
        if not provision_claude_auth:
            raise HTTPException(404, "credential provisioning is disabled")
        creds = await asyncio.to_thread(_read_host_claude_creds)
        if creds is None:
            raise HTTPException(404, "no host credentials available")
        return {"credentials_json": creds}

    @app.get("/triage-prompt", dependencies=[Depends(require_worker)])
    async def triage_prompt(principal: dict = Depends(require_worker)):
        """The bare-worker (kind: auto) self-assessment system prompt, rendered for
        THIS worker against the current fleet. Served from here (not baked into the
        worker) so the prompt can be iterated without redeploying workers — the whole
        point of a thin worker. The worker fetches this per auto job."""
        worker = principal.get("worker") or {}
        caps = worker.get("capabilities") or {}
        fleet = await asyncio.to_thread(_list_workers, db)
        fleet = [w for w in fleet if w["status"] in ("idle", "busy")
                 and w.get("id") != worker.get("id")]
        return {"system": triage.render(caps, fleet), "decline_marker": triage.DECLINE_MARKER}

    # ---- job submit / list / status / logs / stream / cancel ----

    @app.post("/jobs")
    async def submit_job(payload: JobSubmit, principal: dict = Depends(require_any)):
        _validate_job_spec(payload.model_dump(exclude_none=False))
        parent_dict: Optional[dict] = None
        if payload.parent_job_id:
            parent_dict = await asyncio.to_thread(_get_job, db, payload.parent_job_id)
            if parent_dict is None:
                raise HTTPException(404, f"parent job {payload.parent_job_id} not found")
            # A worker may only dispatch from a job assigned to it. Server-anchored
            # roots (captain roots: worker_id is NULL) have no worker owner, so any
            # authenticated principal may dispatch from them (single-operator trust).
            if principal["kind"] == "worker" and parent_dict["worker_id"] is not None:
                worker_id = principal["worker"]["id"]
                if parent_dict["worker_id"] != worker_id:
                    raise HTTPException(
                        403, "parent job is not assigned to caller; cannot dispatch from it"
                    )
            # Hierarchy must be enabled on the parent for sub-dispatch.
            parent_hier = (parent_dict["spec"].get("hierarchy") or {})
            if not parent_hier.get("can_dispatch", False):
                raise HTTPException(
                    403, "parent job does not declare hierarchy.can_dispatch=true"
                )
        as_running = bool(payload.captain_root and parent_dict is None)
        try:
            job = await asyncio.to_thread(
                _insert_job, db, payload.model_dump(exclude_none=False), parent_dict,
                as_running=as_running,
            )
        except ValueError as e:
            raise HTTPException(409, str(e))
        return job

    @app.post("/jobs/{job_id}/finalize", dependencies=[Depends(require_admin)])
    async def finalize_job(job_id: str, payload: dict[str, Any]):
        """Move a non-worker-owned job (a captain root) to a terminal state.

        Used by `roost dispatch` to close out the plan anchor when the captain
        exits. Refuses to touch a job currently owned by a worker.
        """
        state = payload.get("state", "succeeded")
        if state not in TERMINAL_STATES:
            raise HTTPException(400, f"state must be one of {TERMINAL_STATES}")
        ok = await asyncio.to_thread(
            _finalize_job, db, job_id, state, payload.get("result"), payload.get("error")
        )
        if ok is None:
            raise HTTPException(404, "job not found")
        if ok is False:
            raise HTTPException(409, "job is worker-owned or already terminal")
        return await asyncio.to_thread(_get_job, db, job_id)

    @app.get("/jobs", dependencies=[Depends(require_any)])
    async def list_jobs_endpoint(
        state: Optional[str] = None,
        root: Optional[str] = None,
        parent: Optional[str] = None,
        limit: int = 100,
    ):
        jobs = await asyncio.to_thread(_list_jobs, db, state, root, parent, limit)
        return await asyncio.to_thread(_annotate_liveness, db, jobs)

    @app.get("/derived", dependencies=[Depends(require_any)])
    async def derived_endpoint(limit: int = 40):
        """The composed observability model (D0): one fleet verdict + workers + the
        operator-meaningful 'story' for recent runs. The web panel, scripts/fleet, and
        the MCP inbox all render THIS, so the surfaces never diverge."""
        jobs = await asyncio.to_thread(_list_jobs, db, None, None, None, limit)
        jobs = await asyncio.to_thread(_annotate_liveness, db, jobs)
        workers = await asyncio.to_thread(_list_workers, db)
        runs = [_derive_run(j) for j in jobs]
        return {"generated_at": time.time(),
                "fleet_verdict": _fleet_verdict(workers, runs),
                "workers": workers, "runs": runs}

    @app.get("/jobs/{job_id}/derived", dependencies=[Depends(require_any)])
    async def derived_job_endpoint(job_id: str):
        job = await asyncio.to_thread(_get_job, db, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        job = (await asyncio.to_thread(_annotate_liveness, db, [job]))[0]
        return _derive_run(job)

    @app.get("/jobs/{job_id}", dependencies=[Depends(require_any)])
    async def get_job_endpoint(job_id: str):
        job = await asyncio.to_thread(_get_job, db, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return (await asyncio.to_thread(_annotate_liveness, db, [job]))[0]

    @app.get("/jobs/{job_id}/tree", dependencies=[Depends(require_any)])
    async def get_tree(job_id: str):
        job = await asyncio.to_thread(_get_job, db, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        root_id = job["root_job_id"] or job["id"]
        tree = await asyncio.to_thread(_get_tree, db, root_id)
        return await asyncio.to_thread(_annotate_liveness, db, tree)

    @app.delete("/jobs/{job_id}", dependencies=[Depends(require_any)])
    async def cancel_job_endpoint(job_id: str, tree: bool = False):
        count = await asyncio.to_thread(_cancel_job, db, job_id, tree)
        if count == 0:
            raise HTTPException(409, "job not found or already terminal")
        return {"cancelled": count}

    @app.get("/jobs/{job_id}/logs", dependencies=[Depends(require_any)])
    async def get_logs_endpoint(job_id: str, since: int = 0, limit: int = 1000):
        job = await asyncio.to_thread(_get_job, db, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        logs = await asyncio.to_thread(_read_logs, db, job_id, since, limit)
        return {"job_id": job_id, "state": job["state"], "logs": logs}

    @app.get("/jobs/{job_id}/stream", dependencies=[Depends(require_any)])
    async def stream_job(job_id: str, since: int = 0):
        async def gen() -> AsyncIterator[bytes]:
            last = since
            terminal = False
            last_state: Optional[str] = None
            while not terminal:
                job = await asyncio.to_thread(_get_job, db, job_id)
                if not job:
                    yield (
                        b"event: error\ndata: "
                        + json.dumps({"error": "job not found"}).encode()
                        + b"\n\n"
                    )
                    return
                if job["state"] != last_state:
                    yield (
                        b"event: state\ndata: "
                        + json.dumps({"state": job["state"]}).encode()
                        + b"\n\n"
                    )
                    last_state = job["state"]
                logs = await asyncio.to_thread(_read_logs, db, job_id, last, 500)
                for log in logs:
                    yield (
                        b"event: log\ndata: "
                        + json.dumps(log, ensure_ascii=False).encode()
                        + b"\n\n"
                    )
                    last = log["seq"]
                if job["state"] in TERMINAL_STATES:
                    final = {
                        "state": job["state"],
                        "exit_code": job["exit_code"],
                        "error": job["error"],
                        "result": job["result"],
                        "tokens_used": job["tokens_used"],
                    }
                    yield b"event: done\ndata: " + json.dumps(final).encode() + b"\n\n"
                    terminal = True
                else:
                    await asyncio.sleep(0.5)

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ---- blob store (fleet file transfer staging — mac-app DESIGN.md §14) ----
    # Presigned URLs let the worker-side leg (a normal command job) curl a
    # blob without carrying credentials; tokens never enter job specs.

    blob_secret = blobstore.get_secret(db)

    def _store_body_stream(blob_id: str):
        """Returns an async receiver that streams a request body to the blob
        file with size-cap + sha256, returning (size, hexdigest)."""
        async def receive(request: Request) -> tuple[int, str]:
            path = blobstore.blob_path(db, blob_id)
            digest = hashlib.sha256()
            size = 0
            with open(path, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > blobstore.BLOB_MAX_BYTES:
                        f.close()
                        path.unlink(missing_ok=True)
                        raise HTTPException(
                            413, f"blob exceeds {blobstore.BLOB_MAX_BYTES} bytes")
                    digest.update(chunk)
                    f.write(chunk)
            return size, digest.hexdigest()
        return receive

    @app.post("/blobs")
    async def upload_blob(
        request: Request,
        name: str = Query("blob"),
        ttl_sec: Optional[float] = Query(None),
        principal: dict = Depends(require_any),
    ):
        """Stage a file (raw body). Returns the blob with a presigned get_url."""
        def _insert() -> dict:
            with _connect(db) as conn:
                return blobstore.insert_blob(
                    conn, name, ttl_sec, "ready", principal["kind"])
        row = await asyncio.to_thread(_insert)
        size, sha = await _store_body_stream(row["id"])(request)
        def _finalize() -> None:
            with _connect(db) as conn:
                blobstore.finalize_blob(conn, row["id"], size, sha)
        await asyncio.to_thread(_finalize)
        row.update(size=size, sha256=sha, state="ready")
        return blobstore.public_dict(row, str(request.base_url), blob_secret)

    @app.post("/blobs/presign")
    async def presign_blob(
        request: Request,
        payload: Optional[dict[str, Any]] = None,
        principal: dict = Depends(require_any),
    ):
        """Mint a pending blob + presigned put_url for a worker-side upload
        (the fetch flow: a job PUTs the file here, the operator downloads)."""
        payload = payload or {}
        def _insert() -> dict:
            with _connect(db) as conn:
                return blobstore.insert_blob(
                    conn, payload.get("name") or "blob",
                    payload.get("ttl_sec"), "pending", principal["kind"])
        row = await asyncio.to_thread(_insert)
        return blobstore.public_dict(row, str(request.base_url), blob_secret)

    @app.put("/blobs/{blob_id}")
    async def put_blob(
        blob_id: str, request: Request,
        exp: int = Query(...), sig: str = Query(...),
    ):
        """Presigned upload leg — no bearer needed; the signature IS the auth."""
        if not blobstore.verify_sig(blob_secret, blob_id, exp, "put", sig):
            raise HTTPException(403, "invalid or expired signature")
        def _get() -> Optional[dict]:
            with _connect(db) as conn:
                return blobstore.get_blob(conn, blob_id)
        row = await asyncio.to_thread(_get)
        if row is None:
            raise HTTPException(404, "blob not found")
        size, sha = await _store_body_stream(blob_id)(request)
        def _finalize() -> None:
            with _connect(db) as conn:
                blobstore.finalize_blob(conn, blob_id, size, sha)
        await asyncio.to_thread(_finalize)
        return {"id": blob_id, "size": size, "sha256": sha, "state": "ready"}

    @app.get("/blobs/{blob_id}")
    async def download_blob(
        blob_id: str,
        request: Request,
        exp: Optional[int] = Query(None),
        sig: Optional[str] = Query(None),
        authorization: Optional[str] = Header(None),
    ):
        """Download: bearer token OR presigned exp/sig (for worker-side jobs)."""
        presigned_ok = (
            exp is not None and sig is not None
            and blobstore.verify_sig(blob_secret, blob_id, exp, "get", sig)
        )
        if not presigned_ok:
            authenticate(request, authorization)  # raises 401 when bad
        def _get() -> Optional[dict]:
            with _connect(db) as conn:
                return blobstore.get_blob(conn, blob_id)
        row = await asyncio.to_thread(_get)
        if row is None or row["expires_at"] <= time.time():
            raise HTTPException(404, "blob not found or expired")
        if row["state"] != "ready":
            raise HTTPException(409, "blob upload not finished")
        path = blobstore.blob_path(db, blob_id)
        if not path.is_file():
            raise HTTPException(410, "blob file missing")
        return FileResponse(
            path, filename=row["name"], media_type="application/octet-stream")

    @app.get("/blobs", dependencies=[Depends(require_any)])
    async def list_blobs_endpoint(request: Request):
        def _list() -> list[dict]:
            with _connect(db) as conn:
                return blobstore.list_blobs(conn)
        rows = await asyncio.to_thread(_list)
        return [blobstore.public_dict(r, str(request.base_url), blob_secret)
                for r in rows]

    # Admin-only: a client token may stage/list/download blobs (it needs file
    # transfer) but not delete them — deletion is fleet janitorial, not a client
    # verb. (Was require_any, which let any client token wipe another's blob.)
    @app.delete("/blobs/{blob_id}", dependencies=[Depends(require_admin)])
    async def delete_blob_endpoint(blob_id: str):
        def _delete() -> bool:
            with _connect(db) as conn:
                return blobstore.delete_blob(db, conn, blob_id)
        ok = await asyncio.to_thread(_delete)
        if not ok:
            raise HTTPException(404, "blob not found")
        return {"deleted": True}

    # ---- static publish (built thing → real URL on your own CP) ----
    # The publishing loop is the bottleneck for people who just built something:
    # `roost publish ./site` tars the dir and POSTs it straight to /publish
    # (one transactional call — the bundle IS the body, nothing staged), which
    # extracts it into <data_dir>/sites/<slug>/ — live at GET /pub/<slug>/.
    # The two-step flow (stage a blob, then publish by blob_id) remains for
    # callers that already have a blob in flight (worker-side jobs, presign).
    # Agents publish too — that's the point — so a client (agent-scoped) token
    # may POST /publish; deletion stays admin-only (janitorial, like blobs).

    @app.post("/publish")
    async def publish_site(
        request: Request,
        name: Optional[str] = Query(None),
        principal: dict = Depends(require_any),
    ):
        """Publish a static site, two ways (dispatch on Content-Type):

        - raw tar.gz body (anything but application/json) + ?name=<site>:
          ONE transactional call — no staged blob exists at any point, so a
          connection flap can't leave residue (the R7 dangling-blob window).
        - application/json {"blob_id", "name"?}: extract a previously-staged
          blob (the original two-step flow).
        """
        if principal["kind"] not in ("shared", "none", "client"):
            raise HTTPException(403, "publish requires admin or a client token")

        ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            # ---- one-shot path: the body IS the bundle ----
            if not name:
                raise HTTPException(
                    400, "name query parameter is required when POSTing the "
                         "bundle directly (e.g. POST /publish?name=my-site)")
            slug = publishlib.normalize_slug(name)
            if slug is None:
                raise HTTPException(
                    400, "invalid site name: slug must match ^[a-z0-9][a-z0-9-]{0,39}$")
            # Stream to a private temp file next to the sites dir; removed in
            # `finally`, so a failure at ANY point (cap, bad tar, extract error,
            # dropped connection) leaves nothing behind.
            tmp_path = (publishlib.sites_dir(db)
                        / f".upload-{slug}-{secrets.token_hex(6)}.tar.gz")
            try:
                size = 0
                with open(tmp_path, "wb") as f:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > blobstore.BLOB_MAX_BYTES:
                            raise HTTPException(
                                413, f"bundle exceeds {blobstore.BLOB_MAX_BYTES} bytes")
                        f.write(chunk)
                if size == 0:
                    raise HTTPException(
                        400, "empty body: send the tar.gz bundle as the request body")

                def _install_oneshot() -> dict:
                    try:
                        bsize, files = publishlib.extract_bundle(db, slug, tmp_path)
                    except publishlib.PublishError as e:
                        raise HTTPException(e.status, e.detail)
                    with _connect(db) as conn:
                        return publishlib.upsert_site(
                            conn, slug, bsize, files, principal["kind"])
                row = await asyncio.to_thread(_install_oneshot)
                return publishlib.public_dict(
                    row, str(request.base_url), publish_domain)
            finally:
                tmp_path.unlink(missing_ok=True)

        # ---- two-step path: JSON referencing a staged blob ----
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(400, "invalid JSON body")
        if not isinstance(payload, dict):
            raise HTTPException(400, "JSON body must be an object")
        blob_id = payload.get("blob_id")
        if not blob_id:
            raise HTTPException(400, "blob_id is required")

        def _load_blob() -> Optional[dict]:
            with _connect(db) as conn:
                return blobstore.get_blob(conn, blob_id)
        blob = await asyncio.to_thread(_load_blob)
        if blob is None or blob["expires_at"] <= time.time():
            raise HTTPException(404, "blob not found or expired")
        if blob["state"] != "ready":
            raise HTTPException(409, "blob upload not finished")

        # Default the name to the blob's stem, dropping a tar.gz/tgz/tar suffix
        # (the CLI uploads "<name>.tar.gz").
        default_name = blob["name"]
        for suffix in (".tar.gz", ".tgz", ".tar"):
            if default_name.endswith(suffix):
                default_name = default_name[: -len(suffix)]
                break
        else:
            default_name = Path(default_name).stem
        # JSON `name` field wins; the ?name= query is honored for symmetry
        # with the one-shot path; else default from the blob name.
        site_name = payload.get("name") or name or default_name or "site"
        slug = publishlib.normalize_slug(site_name)
        if slug is None:
            raise HTTPException(
                400, "invalid site name: slug must match ^[a-z0-9][a-z0-9-]{0,39}$")

        tar_path = blobstore.blob_path(db, blob_id)
        if not tar_path.is_file():
            raise HTTPException(410, "blob file missing")

        def _install() -> dict:
            try:
                size, files = publishlib.extract_bundle(db, slug, tar_path)
            except publishlib.PublishError as e:
                raise HTTPException(e.status, e.detail)
            with _connect(db) as conn:
                return publishlib.upsert_site(
                    conn, slug, size, files, principal["kind"])
        row = await asyncio.to_thread(_install)
        return publishlib.public_dict(row, str(request.base_url), publish_domain)

    # ---- schedules (the `schedule` verb: interval jobs) ----
    # The CP tick (_tick_schedules, riding the sweep loop) enqueues a job from
    # the stored spec every interval. Client tokens may manage schedules (a
    # phone or agent front door scheduling work is the point); the worker
    # plane may not — a job shouldn't mint standing load.

    def _require_scheduler(principal: dict) -> None:
        if principal["kind"] not in ("shared", "none", "client"):
            raise HTTPException(403, "schedules require admin or a client token")

    @app.post("/schedules")
    async def create_schedule(
        payload: ScheduleCreate, principal: dict = Depends(require_any)
    ):
        """Create an interval schedule. First run fires one interval from now."""
        _require_scheduler(principal)
        interval = parse_every(payload.every)
        if interval is None:
            raise HTTPException(
                400, "every must be seconds or '<N>[smhd]' (e.g. '30m')")
        if interval < SCHEDULE_MIN_INTERVAL_SEC:
            raise HTTPException(
                400, f"every must be >= {SCHEDULE_MIN_INTERVAL_SEC:.0f}s")
        spec = dict(payload.spec or {})
        if spec.get("parent_job_id") or spec.get("captain_root"):
            raise HTTPException(
                400, "schedule specs are root jobs: no parent_job_id/captain_root")
        _validate_job_spec(spec)
        sched_id = uuid.uuid4().hex[:12]
        now = time.time()

        def _insert() -> dict:
            with _connect(db) as conn:
                conn.execute(
                    "INSERT INTO schedules(id, name, spec, interval_sec, enabled, "
                    "next_run_at, created_at, created_by) VALUES (?,?,?,?,?,?,?,?)",
                    (sched_id, payload.name, json.dumps(spec), interval,
                     int(payload.enabled), now + interval, now, principal["kind"]),
                )
                row = conn.execute(
                    "SELECT * FROM schedules WHERE id=?", (sched_id,)).fetchone()
            return dict(row)
        return _schedule_to_public(await asyncio.to_thread(_insert))

    @app.get("/schedules")
    async def list_schedules(principal: dict = Depends(require_any)):
        _require_scheduler(principal)

        def _list() -> list[dict]:
            with _connect(db) as conn:
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM schedules ORDER BY created_at DESC").fetchall()]
        return [_schedule_to_public(r) for r in await asyncio.to_thread(_list)]

    @app.patch("/schedules/{sched_id}")
    async def patch_schedule(
        sched_id: str, payload: SchedulePatch,
        principal: dict = Depends(require_any),
    ):
        """Enable/disable. Re-enabling restarts the clock: next run is one
        interval from now (a long-disabled schedule must not fire instantly)."""
        _require_scheduler(principal)

        def _patch() -> Optional[dict]:
            now = time.time()
            with _connect(db) as conn:
                row = conn.execute(
                    "SELECT * FROM schedules WHERE id=?", (sched_id,)).fetchone()
                if row is None:
                    return None
                if payload.enabled and not row["enabled"]:
                    conn.execute(
                        "UPDATE schedules SET enabled=1, next_run_at=? WHERE id=?",
                        (now + row["interval_sec"], sched_id))
                else:
                    conn.execute(
                        "UPDATE schedules SET enabled=? WHERE id=?",
                        (int(payload.enabled), sched_id))
                return dict(conn.execute(
                    "SELECT * FROM schedules WHERE id=?", (sched_id,)).fetchone())
        row = await asyncio.to_thread(_patch)
        if row is None:
            raise HTTPException(404, "schedule not found")
        return _schedule_to_public(row)

    @app.delete("/schedules/{sched_id}")
    async def delete_schedule(
        sched_id: str, principal: dict = Depends(require_any)
    ):
        _require_scheduler(principal)

        def _delete() -> bool:
            with _connect(db) as conn:
                cur = conn.execute(
                    "DELETE FROM schedules WHERE id=?", (sched_id,))
                return cur.rowcount > 0
        if not await asyncio.to_thread(_delete):
            raise HTTPException(404, "schedule not found")
        return {"deleted": True, "id": sched_id}

    @app.get("/publish", dependencies=[Depends(require_any)])
    async def list_sites_endpoint(request: Request):
        def _list() -> list[dict]:
            with _connect(db) as conn:
                return publishlib.list_sites(conn)
        rows = await asyncio.to_thread(_list)
        return [publishlib.public_dict(r, str(request.base_url), publish_domain)
                for r in rows]

    @app.delete("/publish/{slug}", dependencies=[Depends(require_admin)])
    async def unpublish_site(slug: str):
        def _delete() -> bool:
            with _connect(db) as conn:
                return publishlib.delete_site(db, conn, slug)
        ok = await asyncio.to_thread(_delete)
        if not ok:
            raise HTTPException(404, "site not found")
        return {"unpublished": True, "slug": slug}

    # Public static serving. UNAUTHENTICATED on purpose — a published site is
    # meant to be reachable by anyone with the URL (that's the whole point).
    # Tradeoff: on a LAN/Tailscale-exposed CP this exposes the site to that
    # network; keep nothing secret in a published bundle.
    @app.get("/pub/{slug}")
    async def serve_site_root(slug: str):
        return RedirectResponse(url=f"/pub/{slug}/", status_code=307)

    @app.get("/pub/{slug}/{path:path}")
    async def serve_site(slug: str, path: str = ""):
        # resolve_served_path enforces the in-site commonpath check (belt and
        # braces over the filtered extraction) and the index.html / SPA fallback.
        file_path = await asyncio.to_thread(
            publishlib.resolve_served_path, db, slug, path)
        if file_path is None:
            raise HTTPException(404, "not found")
        return FileResponse(file_path)

    # ---- worker plane ----

    @app.post("/workers/register")
    async def register_worker(
        payload: WorkerRegister, principal: dict = Depends(require_any)
    ):
        """Legacy / LAN-convenience register: uses shared token only.

        Workers using per-worker credentials enroll via POST /enroll instead.
        """
        if principal["kind"] not in ("shared", "none"):
            raise HTTPException(
                403, "use POST /enroll for per-worker credentials; "
                     "POST /workers/register only accepts the shared token"
            )
        worker = await asyncio.to_thread(
            _register_worker, db, payload.name, payload.capabilities, None, None, {}
        )
        return worker

    @app.get("/workers", dependencies=[Depends(require_any)])
    async def list_workers_endpoint():
        return await asyncio.to_thread(_list_workers, db)

    @app.get("/workers/{worker_id}", dependencies=[Depends(require_any)])
    async def get_worker_endpoint(worker_id: str):
        worker = await asyncio.to_thread(_get_worker, db, worker_id)
        if not worker:
            raise HTTPException(404, "worker not found")
        return worker

    @app.delete("/workers/{worker_id}", dependencies=[Depends(require_admin)])
    async def revoke_worker_endpoint(worker_id: str):
        ok = await asyncio.to_thread(_revoke_worker, db, worker_id)
        if not ok:
            raise HTTPException(404, "worker not found")
        return {"revoked": True}

    @app.post("/workers/prune", dependencies=[Depends(require_admin)])
    async def prune_workers_endpoint(older_than_days: float = Query(7.0, ge=0)):
        res = await asyncio.to_thread(
            _prune_workers, db, older_than_days * 86400.0
        )
        return res

    @app.post("/workers/{worker_id}/heartbeat", dependencies=[Depends(require_matching_worker)])
    async def heartbeat(worker_id: str, payload: HeartbeatPayload):
        ok = await asyncio.to_thread(
            _heartbeat_worker, db, worker_id, payload.capabilities
        )
        if not ok:
            raise HTTPException(404, "worker not found")
        # Tell the worker which of its jobs were cancelled so it can tear down the
        # running process / docker container (cancel can't push under the pull
        # model; this is the back-channel).
        cancelled = await asyncio.to_thread(_recently_cancelled_for_worker, db, worker_id)
        # [R3] Also report which jobs we still attribute to this worker, so it
        # can abort local attempts whose lease was swept during a CP outage
        # (additive field; older workers ignore it).
        owned = await asyncio.to_thread(_owned_job_ids, db, worker_id)
        return {"ok": True, "cancel": cancelled, "owned": owned}

    @app.get("/workers/{worker_id}/poll", dependencies=[Depends(require_matching_worker)])
    async def poll(worker_id: str, timeout: float = Query(POLL_HOLD_MAX, ge=0, le=POLL_HOLD_MAX)):
        worker = await asyncio.to_thread(_get_worker, db, worker_id)
        if not worker:
            raise HTTPException(404, "worker not found")
        end = asyncio.get_event_loop().time() + min(timeout, POLL_HOLD_MAX)
        while True:
            job = await asyncio.to_thread(_try_assign_one, db, worker_id)
            if job:
                return job
            remaining = end - asyncio.get_event_loop().time()
            if remaining <= 0:
                return JSONResponse(status_code=204, content=None)
            await asyncio.sleep(min(0.5, remaining))

    @app.post(
        "/workers/{worker_id}/jobs/{job_id}/logs",
        dependencies=[Depends(require_matching_worker)],
    )
    async def post_log(worker_id: str, job_id: str, payload: dict[str, Any]):
        stream = payload.get("stream", "stdout")
        data = payload.get("data", "")
        if not isinstance(data, str):
            data = json.dumps(data)
        seq = await asyncio.to_thread(_append_log, db, job_id, stream, data)
        await asyncio.to_thread(_heartbeat_worker, db, worker_id, None)
        return {"seq": seq}

    @app.post(
        "/workers/{worker_id}/jobs/{job_id}/event",
        dependencies=[Depends(require_matching_worker)],
    )
    async def post_event(worker_id: str, job_id: str, event: JobEvent):
        try:
            job, accepted = await asyncio.to_thread(
                _apply_event, db, job_id, worker_id, event.model_dump(exclude_none=False)
            )
        except KeyError:
            raise HTTPException(404, "job not found")
        except PermissionError as e:
            raise HTTPException(403, str(e))
        if not accepted:
            raise HTTPException(409, "stale attempt; event ignored")
        await asyncio.to_thread(
            _append_log,
            db,
            job_id,
            "event",
            json.dumps(event.model_dump(exclude_none=False)),
        )
        return job

    return app


# ---------- Sweeper task ----------


def _log_tail(db_path: Path, job_id: str, chars: int = 1200) -> str:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT data FROM job_logs WHERE job_id=? ORDER BY seq DESC LIMIT 40",
            (job_id,),
        ).fetchall()
    return "\n".join(r["data"] for r in reversed(rows))[-chars:]


def _make_narration_store(db_path: Path):
    def store(job_id: str, payload: dict) -> None:
        with _connect(db_path) as conn:
            conn.execute(
                "UPDATE jobs SET narration=?, narrated_at=?, progress=?, eta_sec=? "
                "WHERE id=? AND state IN ('running','assigned')",
                (payload.get("narration"), payload.get("narrated_at"),
                 payload.get("progress"), payload.get("eta_sec"), job_id),
            )
    return store


async def _narrate_pass(db_path: Path) -> None:
    """D2: refresh agentic narration for running jobs (opt-in via ROOST_NARRATE=1;
    requires `claude` on the control-plane host). Best-effort: deterministic health
    in `/derived` works regardless. Never blocks the sweep loop."""
    jobs = await asyncio.to_thread(_list_jobs, db_path, "running", None, None, 30)
    jobs += await asyncio.to_thread(_list_jobs, db_path, "assigned", None, None, 30)
    if not jobs:
        return
    for j in watcher.jobs_needing_narration(jobs, time.time()):
        j["log_tail"] = await asyncio.to_thread(_log_tail, db_path, j["id"])
    await watcher.watch_once(jobs, watcher.default_claude_runner,
                             _make_narration_store(db_path))


async def _sweep_loop(db_path: Path) -> None:
    last_prune = 0.0
    narrate = os.environ.get("ROOST_NARRATE") == "1"
    while True:
        try:
            await asyncio.to_thread(_sweep, db_path)
        except Exception as e:  # noqa: BLE001
            print(f"[roost] sweeper error: {e}", flush=True)
        # Schedule tick: enqueue jobs for due interval schedules (R8).
        try:
            await asyncio.to_thread(_tick_schedules, db_path)
        except Exception as e:  # noqa: BLE001
            print(f"[roost] schedule tick error: {e}", flush=True)
        if narrate:
            try:
                await _narrate_pass(db_path)
            except Exception as e:  # noqa: BLE001
                print(f"[roost] narration error: {e}", flush=True)
        # Throttled, best-effort log retention (M1): never let a prune error
        # break the sweep loop.
        now = time.time()
        if now - last_prune >= LOG_PRUNE_INTERVAL:
            last_prune = now
            try:
                await asyncio.to_thread(
                    _prune_logs, db_path, LOG_MAX_AGE_SEC, LOG_MAX_ROWS_PER_JOB
                )
            except Exception as e:  # noqa: BLE001
                print(f"[roost] log prune error: {e}", flush=True)
            # Expired staged blobs ride the same throttle (file + row).
            try:
                def _prune_blobs() -> None:
                    with _connect(db_path) as conn:
                        blobstore.prune_expired(db_path, conn)
                await asyncio.to_thread(_prune_blobs)
            except Exception as e:  # noqa: BLE001
                print(f"[roost] blob prune error: {e}", flush=True)
        try:
            await asyncio.sleep(SWEEPER_INTERVAL)
        except asyncio.CancelledError:
            return


# ---------- Install script ----------


# Where a fresh `curl … | sh` install pulls THIS project's code from when the
# operator hasn't configured anything. NOT the unrelated `roost` PyPI package —
# default to a known-good git source so the served one-liner installs Roost.
DEFAULT_INSTALL_SOURCE = "git+https://github.com/roost-sh/roost@main"


def render_install_script(
    control_plane_url: str,
    source: Optional[str] = None,
    claude_install_cmd: Optional[str] = None,
) -> str:
    # The control plane injects its own known-good source so a fresh install
    # gets the right code without the operator passing --source. Operators can
    # still override via ROOST_INSTALL_SOURCE (serve env) or --source (per run).
    install_source = (
        source
        or os.environ.get("ROOST_INSTALL_SOURCE")
        or DEFAULT_INSTALL_SOURCE
    )
    claude_cmd = claude_install_cmd or CLAUDE_INSTALL_CMD
    return f"""#!/usr/bin/env sh
# Roost worker installer. Idempotent: re-running upgrades in place.
# Usage:
#   curl -fsSL {control_plane_url}/install.sh | sh -s -- <enroll-token> \\
#       [--name NAME] [--source SOURCE] [--with-claude] [--no-start]
#
# SOURCE defaults to this control plane's own known-good source. Override with
# a git URL or local path when iterating, e.g.
#   --source 'git+https://github.com/you/roost@branch'.
# Pass --with-claude to also install the Claude CLI (for agent jobs).
set -eu

ROOST_URL="${{ROOST_URL:-{control_plane_url}}}"
ROOST_ENROLL_TOKEN="${{ROOST_ENROLL_TOKEN:-${{1:-}}}}"
ROOST_NAME="${{ROOST_NAME:-$(hostname)}}"
ROOST_SOURCE="${{ROOST_SOURCE:-{install_source}}}"
ROOST_WITH_CLAUDE="${{ROOST_WITH_CLAUDE:-0}}"

shift 2>/dev/null || true
while [ "$#" -gt 0 ]; do
    case "$1" in
        --name) ROOST_NAME="$2"; shift 2 ;;
        --source) ROOST_SOURCE="$2"; shift 2 ;;
        --with-claude) ROOST_WITH_CLAUDE=1; shift ;;
        --no-start) ROOST_NO_START=1; shift ;;
        *) shift ;;
    esac
done

if [ -z "$ROOST_ENROLL_TOKEN" ]; then
    echo "Usage: curl <url>/install.sh | sh -s -- <enroll-token>" >&2
    exit 1
fi

# Install uv if absent.
if ! command -v uv >/dev/null 2>&1; then
    echo "[install] installing uv..."
    curl -fsSL https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

echo "[install] installing roost from $ROOST_SOURCE..."
# Pin Python 3.12: newer interpreters can break the async HTTP client.
uv tool install --force --python 3.12 "$ROOST_SOURCE"

# Ensure tool dir on PATH for this session.
export PATH="$HOME/.local/bin:$PATH"

# Optionally install the Claude CLI so this node can run agent (claude) jobs.
if [ "$ROOST_WITH_CLAUDE" = "1" ]; then
    if command -v claude >/dev/null 2>&1; then
        echo "[install] claude already present; skipping."
    else
        echo "[install] installing claude CLI..."
        {claude_cmd}
    fi
fi

echo "[install] enrolling as $ROOST_NAME ..."
roost enroll --url "$ROOST_URL" --token "$ROOST_ENROLL_TOKEN" --name "$ROOST_NAME"

if [ "${{ROOST_NO_START:-0}}" = "0" ]; then
    echo "[install] installing supervisor unit and starting..."
    roost service install --start
fi

echo "[install] done. Tail logs with: roost service logs"
"""


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"})


def run(
    host: str = "0.0.0.0",
    port: int = 8787,
    db_path: Optional[Path] = None,
    token: Optional[str] = None,
    provision_claude_auth: bool = True,
    insecure: bool = False,
    publish_domain: Optional[str] = None,
) -> None:
    import uvicorn

    # [C2] Refuse to serve unauthenticated on a non-loopback bind. With no shared
    # token, every endpoint is wide open; on a reachable interface that exposes the
    # whole fleet (and copy-creds onboarding) to the LAN/internet. Require either a
    # token or an explicit opt-in.
    effective_token = token if token is not None else os.environ.get("ROOST_TOKEN", "")
    if not effective_token and host not in _LOOPBACK_HOSTS:
        if not insecure:
            raise SystemExit(
                f"[roost] REFUSING to start: no shared token AND binding to a "
                f"non-loopback host ({host!r}) would run the control plane "
                f"UNAUTHENTICATED and reachable off-box. Set a token "
                f"(ROOST_TOKEN=… or --token) — or, only if you truly intend an "
                f"open server on a trusted network, pass --insecure / insecure=True."
            )
        print(
            f"[roost] WARNING: running UNAUTHENTICATED on {host}:{port} "
            f"(--insecure). Every endpoint is open to anything that can reach "
            f"this interface.",
            flush=True,
        )
    elif not effective_token:
        print(
            f"[roost] WARNING: running with NO shared token on loopback "
            f"({host}:{port}) — all endpoints are unauthenticated. Fine for local "
            f"dev; set ROOST_TOKEN before exposing this box.",
            flush=True,
        )

    if provision_claude_auth and _read_host_claude_creds() is not None:
        print(
            "[roost] WARNING: claude-auth provisioning is ON — enrolling workers "
            "will receive a COPY of this host's ~/.claude/.credentials.json. Only "
            "enroll machines you trust; use --no-provision-auth to disable.",
            flush=True,
        )
    app = create_app(db_path=db_path, token=token,
                     provision_claude_auth=provision_claude_auth,
                     publish_domain=publish_domain)
    uvicorn.run(app, host=host, port=port, log_level="info")
