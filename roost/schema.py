"""SQLite schema + migrations for Roost.

The schema is versioned via ``PRAGMA user_version``. New installs jump straight
to the latest version; existing V0 databases are migrated additively (ALTER
TABLE ADD COLUMN + new tables) without rewriting the file.
"""

from __future__ import annotations

import sqlite3

CURRENT_VERSION = 5

# Full V1 schema for fresh installs.
SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS workers (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    capabilities    TEXT NOT NULL,
    registered_at   REAL NOT NULL,
    last_seen       REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'idle',  -- idle | busy | stale | offline
    enroll_id       TEXT,                           -- audit pointer to the enroll_tokens row
    cred_hash       TEXT,                           -- sha256 of the per-worker credential
    policy_json     TEXT NOT NULL DEFAULT '{}',     -- {trust_skip_perms, allow_paths, allow_commands, max_perm}
    last_assigned_at REAL,                          -- last time a job was assigned here (placement spread)
    revoked         INTEGER NOT NULL DEFAULT 0      -- 1 = admin-revoked; never recovers/assigns (V5)
);

CREATE TABLE IF NOT EXISTS jobs (
    id                  TEXT PRIMARY KEY,
    spec                TEXT NOT NULL,
    intent              TEXT,
    requires            TEXT NOT NULL DEFAULT '{}',
    state               TEXT NOT NULL DEFAULT 'queued',  -- queued|assigned|running|succeeded|failed|cancelled
    worker_id           TEXT,
    created_at          REAL NOT NULL,
    assigned_at         REAL,
    started_at          REAL,
    finished_at         REAL,
    exit_code           INTEGER,
    result              TEXT,
    error               TEXT,
    -- lease + retry
    lease_expires_at    REAL,
    attempt             INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 2,
    -- hierarchy
    parent_job_id       TEXT,
    root_job_id         TEXT,
    depth               INTEGER NOT NULL DEFAULT 0,
    max_depth           INTEGER NOT NULL DEFAULT 3,
    -- cost / model routing
    tokens_used         INTEGER NOT NULL DEFAULT 0,
    tree_budget_tokens  INTEGER,
    tree_budget_spent   INTEGER NOT NULL DEFAULT 0,
    model               TEXT,
    subagent_model      TEXT,
    -- liveness / observability (V4)
    last_activity_at    REAL,                            -- last sign of life from the job
    last_activity       TEXT                             -- compact "what it's doing now"
);
CREATE INDEX IF NOT EXISTS idx_jobs_state       ON jobs(state);
CREATE INDEX IF NOT EXISTS idx_jobs_created     ON jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_root        ON jobs(root_job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_parent      ON jobs(parent_job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_lease_sweep ON jobs(state, lease_expires_at);

CREATE TABLE IF NOT EXISTS job_logs (
    job_id    TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    stream    TEXT NOT NULL,    -- stdout | stderr | event
    data      TEXT NOT NULL,
    ts        REAL NOT NULL,
    PRIMARY KEY (job_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_job_logs_job ON job_logs(job_id, seq);
CREATE INDEX IF NOT EXISTS idx_job_logs_ts  ON job_logs(ts);

CREATE TABLE IF NOT EXISTS enroll_tokens (
    token_hash      TEXT PRIMARY KEY,
    label           TEXT,
    policy_json     TEXT NOT NULL DEFAULT '{}',
    created_at      REAL NOT NULL,
    expires_at      REAL NOT NULL,
    used_at         REAL,
    used_by_worker  TEXT
);
CREATE INDEX IF NOT EXISTS idx_enroll_tokens_expiry ON enroll_tokens(expires_at);
"""

# V0 → V1 additive migration. Each entry is (column_name, full DDL fragment).
_WORKER_V1_ADDS = [
    ("enroll_id",   "enroll_id TEXT"),
    ("cred_hash",   "cred_hash TEXT"),
    ("policy_json", "policy_json TEXT NOT NULL DEFAULT '{}'"),
]
# V2 → V3 (placement ranking).
_WORKER_V3_ADDS = [
    ("last_assigned_at", "last_assigned_at REAL"),
]
# V4 → V5 (permanent revocation flag).
_WORKER_V5_ADDS = [
    ("revoked", "revoked INTEGER NOT NULL DEFAULT 0"),
]
_JOB_V1_ADDS = [
    ("lease_expires_at",   "lease_expires_at REAL"),
    ("attempt",            "attempt INTEGER NOT NULL DEFAULT 0"),
    ("max_attempts",       "max_attempts INTEGER NOT NULL DEFAULT 2"),
    ("parent_job_id",      "parent_job_id TEXT"),
    ("root_job_id",        "root_job_id TEXT"),
    ("depth",              "depth INTEGER NOT NULL DEFAULT 0"),
    ("max_depth",          "max_depth INTEGER NOT NULL DEFAULT 3"),
    ("tokens_used",        "tokens_used INTEGER NOT NULL DEFAULT 0"),
    ("tree_budget_tokens", "tree_budget_tokens INTEGER"),
    ("tree_budget_spent",  "tree_budget_spent INTEGER NOT NULL DEFAULT 0"),
    ("model",              "model TEXT"),
    ("subagent_model",     "subagent_model TEXT"),
]
# V3 → V4 (liveness / observability).
_JOB_V4_ADDS = [
    ("last_activity_at", "last_activity_at REAL"),
    ("last_activity",    "last_activity TEXT"),
]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply migrations as needed. Returns the resulting schema version."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables = {
        r["name"] if isinstance(r, sqlite3.Row) else r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    fresh = "workers" not in tables
    if fresh:
        conn.executescript(SCHEMA_V1)
        conn.execute(f"PRAGMA user_version = {CURRENT_VERSION}")
        return CURRENT_VERSION

    def _add_missing(table: str, adds: list[tuple[str, str]]) -> None:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, ddl in adds:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    if version < 2:
        # Bring V0 tables up to V1 shape.
        _add_missing("workers", _WORKER_V1_ADDS)
        _add_missing("jobs", _JOB_V1_ADDS)
        # Backfill root_job_id = id for existing rows (single-node lineage).
        conn.execute("UPDATE jobs SET root_job_id = id WHERE root_job_id IS NULL")
        # New tables + new indexes (CREATE IF NOT EXISTS, so safe to re-run).
        conn.executescript(SCHEMA_V1)

    if version < 3:
        # V2 → V3: placement-ranking column.
        _add_missing("workers", _WORKER_V3_ADDS)

    if version < 4:
        # V3 → V4: per-job liveness columns.
        _add_missing("jobs", _JOB_V4_ADDS)

    if version < 5:
        # V4 → V5: permanent revocation flag.
        _add_missing("workers", _WORKER_V5_ADDS)

    conn.execute(f"PRAGMA user_version = {CURRENT_VERSION}")
    return CURRENT_VERSION
