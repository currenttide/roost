"""Schema migrations (roost/schema.py).

The control plane versions its SQLite file via ``PRAGMA user_version`` and
migrates old databases forward additively (ALTER TABLE ADD COLUMN + new tables)
rather than rewriting the file. These tests build synthetic old-version DBs and
migrate each forward to the current version, asserting at every step that the
right columns/tables appear, the version lands on CURRENT_VERSION, existing rows
survive, and re-migrating an already-current DB is a no-op.

Follows the additive-migration test pattern introduced for R19/R38 (see the
V13 → V14 case in tests/test_input.py).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from roost.schema import CURRENT_VERSION, SCHEMA_V1, migrate


# A faithful V0 database: the bare workers/jobs/job_logs/enroll_tokens tables as
# they existed before any migration, WITHOUT the columns/tables that later
# versions add. migrate() must bring this all the way up to CURRENT_VERSION via
# its additive ALTER TABLE / CREATE-IF-NOT-EXISTS path (the `version < N` ladder).
_SCHEMA_V0 = """
CREATE TABLE workers (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    capabilities    TEXT NOT NULL,
    registered_at   REAL NOT NULL,
    last_seen       REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'idle'
);

CREATE TABLE jobs (
    id          TEXT PRIMARY KEY,
    spec        TEXT NOT NULL,
    intent      TEXT,
    requires    TEXT NOT NULL DEFAULT '{}',
    state       TEXT NOT NULL DEFAULT 'queued',
    worker_id   TEXT,
    created_at  REAL NOT NULL,
    assigned_at REAL,
    started_at  REAL,
    finished_at REAL,
    exit_code   INTEGER,
    result      TEXT,
    error       TEXT
);

CREATE TABLE job_logs (
    job_id  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    stream  TEXT NOT NULL,
    data    TEXT NOT NULL,
    ts      REAL NOT NULL,
    PRIMARY KEY (job_id, seq)
);

CREATE TABLE enroll_tokens (
    token_hash  TEXT PRIMARY KEY,
    label       TEXT,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    used_at     REAL,
    used_by_worker TEXT
);
"""


# Columns each worker/job version step is responsible for adding, and the tables
# the later (CREATE-based) steps add. Used to (a) carve a synthetic DB stamped at
# version N that is missing exactly what N+1 introduces and (b) assert that
# migrating forward fills them in. This is the contract the `version < N` ladder
# in schema.py must satisfy.
_WORKER_COLS = {
    1: ["enroll_id", "cred_hash", "policy_json"],
    3: ["last_assigned_at"],
    5: ["revoked"],
    8: ["capacity"],
}
_JOB_COLS = {
    1: ["lease_expires_at", "attempt", "max_attempts", "parent_job_id",
        "root_job_id", "depth", "max_depth", "tokens_used", "tree_budget_tokens",
        "tree_budget_spent", "model", "subagent_model"],
    4: ["last_activity_at", "last_activity"],
    6: ["decline_count", "declined_by"],
    7: ["narration", "narrated_at", "progress", "eta_sec"],
    8: ["diagnosis"],
    13: ["requeued_at"],
}
_NEW_TABLES = {  # version step -> table that step's CREATE introduces
    9: "api_tokens",
    10: "blobs",
    11: "sites",
    12: "schedules",
    14: "job_inputs",
}


def _open(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _build_at_version(db: Path, version: int) -> sqlite3.Connection:
    """Construct a DB shaped exactly like an on-disk file at `version`.

    Start from V0, apply every additive column/table introduced at steps
    1..version, then stamp user_version. The result is missing precisely what
    step version+1 (and beyond) will add, so a subsequent migrate() must add it.
    """
    conn = _open(db)
    conn.executescript(_SCHEMA_V0)
    for v in range(1, version + 1):
        for col in _WORKER_COLS.get(v, []):
            conn.execute(f"ALTER TABLE workers ADD COLUMN {col}")
        for col in _JOB_COLS.get(v, []):
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col}")
        tbl = _NEW_TABLES.get(v)
        if tbl:
            # Reuse the canonical CREATE from the live schema so the synthetic
            # historical table matches what production would have had.
            conn.execute(_create_stmt_for(tbl))
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    return conn


def _create_stmt_for(table: str) -> str:
    """Pull the `CREATE TABLE ... <table> (...)` statement out of SCHEMA_V1."""
    marker = f"CREATE TABLE IF NOT EXISTS {table} ("
    start = SCHEMA_V1.index(marker)
    end = SCHEMA_V1.index(");", start) + 2
    return SCHEMA_V1[start:end]


# ---------- fresh install ----------


def test_fresh_install_jumps_to_current(tmp_path: Path):
    """A brand-new DB skips the ladder entirely and lands on CURRENT_VERSION
    with every table present."""
    conn = _open(tmp_path / "fresh.db")
    version = migrate(conn)
    assert version == CURRENT_VERSION
    assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
    tables = _tables(conn)
    for expected in ("workers", "jobs", "job_logs", "enroll_tokens",
                     "api_tokens", "blobs", "sites", "schedules", "job_inputs"):
        assert expected in tables, f"fresh install missing {expected}"
    conn.close()


# ---------- full V0 -> CURRENT ----------


def test_v0_migrates_all_the_way_to_current(tmp_path: Path):
    """A pre-migration V0 DB walks the entire `version < N` ladder up to
    CURRENT_VERSION, gaining every additive column AND every later table."""
    conn = _build_at_version(tmp_path / "v0.db", 0)
    # Precondition: V0 lacks the V1+ columns and all later tables.
    assert "policy_json" not in _cols(conn, "workers")
    assert "root_job_id" not in _cols(conn, "jobs")
    assert "schedules" not in _tables(conn)

    version = migrate(conn)

    assert version == CURRENT_VERSION
    assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
    wcols, jcols, tables = _cols(conn, "workers"), _cols(conn, "jobs"), _tables(conn)
    for cols in _WORKER_COLS.values():
        for c in cols:
            assert c in wcols, f"workers.{c} not added by migration"
    for cols in _JOB_COLS.values():
        for c in cols:
            assert c in jcols, f"jobs.{c} not added by migration"
    for tbl in _NEW_TABLES.values():
        assert tbl in tables, f"table {tbl} not created by migration"
    conn.close()


def test_v0_backfills_root_job_id(tmp_path: Path):
    """The V0 -> V1 step backfills root_job_id = id for existing rows so legacy
    single-node jobs have a self-rooted lineage."""
    conn = _build_at_version(tmp_path / "v0b.db", 0)
    conn.execute(
        "INSERT INTO jobs(id, spec, requires, state, created_at) "
        "VALUES ('legacy', '{}', '{}', 'succeeded', 1.0)")
    conn.commit()

    migrate(conn)

    row = conn.execute("SELECT root_job_id FROM jobs WHERE id='legacy'").fetchone()
    assert row["root_job_id"] == "legacy"
    conn.close()


def test_v0_preserves_existing_rows(tmp_path: Path):
    """Migration is additive: an existing worker/job row survives untouched."""
    conn = _build_at_version(tmp_path / "v0c.db", 0)
    conn.execute(
        "INSERT INTO workers(id, name, capabilities, registered_at, last_seen) "
        "VALUES ('w1', 'box', '{}', 1.0, 1.0)")
    conn.execute(
        "INSERT INTO jobs(id, spec, requires, state, created_at) "
        "VALUES ('j1', '{}', '{}', 'queued', 1.0)")
    conn.commit()

    migrate(conn)

    assert conn.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 1
    assert conn.execute("SELECT name FROM workers WHERE id='w1'").fetchone()[0] == "box"
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    # New columns default sensibly (capacity defaults to 1; revoked to 0).
    assert conn.execute("SELECT capacity FROM workers WHERE id='w1'").fetchone()[0] == 1
    assert conn.execute("SELECT revoked FROM workers WHERE id='w1'").fetchone()[0] == 0
    conn.close()


# ---------- every single version step ----------


def _added_at(version: int) -> tuple[list[str], list[str], list[str]]:
    """(worker cols, job cols, tables) that the step *into* `version` introduces."""
    return (_WORKER_COLS.get(version, []),
            _JOB_COLS.get(version, []),
            [_NEW_TABLES[version]] if version in _NEW_TABLES else [])


def test_every_version_step_lands_on_current_and_adds_its_delta(tmp_path: Path):
    """For each starting version N in 0..CURRENT-1, a DB stamped at N migrates to
    CURRENT_VERSION and — critically — the specific column/table that step N+1
    introduces is present afterward but absent before. Exercises every rung of
    the `version < N` ladder in schema.py.
    """
    for n in range(0, CURRENT_VERSION):
        nxt = n + 1
        conn = _build_at_version(tmp_path / f"v{n}.db", n)

        wadd, jadd, tadd = _added_at(nxt)
        # Precondition: what step nxt adds is genuinely missing at version n.
        wcols0, jcols0, tables0 = _cols(conn, "workers"), _cols(conn, "jobs"), _tables(conn)
        for c in wadd:
            assert c not in wcols0, f"v{n}: workers.{c} unexpectedly already present"
        for c in jadd:
            assert c not in jcols0, f"v{n}: jobs.{c} unexpectedly already present"
        for t in tadd:
            assert t not in tables0, f"v{n}: table {t} unexpectedly already present"

        version = migrate(conn)

        assert version == CURRENT_VERSION, f"v{n} did not reach current"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
        wcols1, jcols1, tables1 = _cols(conn, "workers"), _cols(conn, "jobs"), _tables(conn)
        for c in wadd:
            assert c in wcols1, f"v{n}->v{nxt}: workers.{c} not added"
        for c in jadd:
            assert c in jcols1, f"v{n}->v{nxt}: jobs.{c} not added"
        for t in tadd:
            assert t in tables1, f"v{n}->v{nxt}: table {t} not created"
        conn.close()


# ---------- specific named steps (readable, mutation-catching) ----------


def test_v8_to_v9_adds_api_tokens(tmp_path: Path):
    conn = _build_at_version(tmp_path / "v8.db", 8)
    assert "api_tokens" not in _tables(conn)
    migrate(conn)
    assert "api_tokens" in _tables(conn)
    # The created table is usable with the expected scope default.
    conn.execute(
        "INSERT INTO api_tokens(id, token_hash, created_at) VALUES ('t', 'h', 1.0)")
    assert conn.execute(
        "SELECT scope FROM api_tokens WHERE id='t'").fetchone()[0] == "mobile"
    conn.close()


def test_v9_to_v10_adds_blobs(tmp_path: Path):
    conn = _build_at_version(tmp_path / "v9.db", 9)
    assert "blobs" not in _tables(conn)
    migrate(conn)
    assert "blobs" in _tables(conn)
    conn.execute(
        "INSERT INTO blobs(id, name, created_at, expires_at) VALUES ('b','n',1.0,2.0)")
    assert conn.execute(
        "SELECT state FROM blobs WHERE id='b'").fetchone()[0] == "ready"
    conn.close()


def test_v10_to_v11_adds_sites(tmp_path: Path):
    conn = _build_at_version(tmp_path / "v10.db", 10)
    assert "sites" not in _tables(conn)
    migrate(conn)
    assert "sites" in _tables(conn)
    conn.close()


def test_v11_to_v12_adds_schedules(tmp_path: Path):
    conn = _build_at_version(tmp_path / "v11.db", 11)
    assert "schedules" not in _tables(conn)
    migrate(conn)
    assert "schedules" in _tables(conn)
    conn.execute(
        "INSERT INTO schedules(id, spec, interval_sec, next_run_at, created_at) "
        "VALUES ('s', '{}', 60.0, 100.0, 1.0)")
    assert conn.execute(
        "SELECT enabled FROM schedules WHERE id='s'").fetchone()[0] == 1
    conn.close()


def test_v12_to_v13_adds_requeued_at(tmp_path: Path):
    conn = _build_at_version(tmp_path / "v12.db", 12)
    assert "requeued_at" not in _cols(conn, "jobs")
    migrate(conn)
    assert "requeued_at" in _cols(conn, "jobs")
    conn.close()


def test_v13_to_v14_adds_job_inputs(tmp_path: Path):
    conn = _build_at_version(tmp_path / "v13.db", 13)
    assert "job_inputs" not in _tables(conn)
    migrate(conn)
    assert "job_inputs" in _tables(conn)
    conn.close()


def test_v7_to_v8_adds_capacity_and_diagnosis(tmp_path: Path):
    """The V7 -> V8 step touches BOTH tables: workers gains capacity, jobs gains
    diagnosis."""
    conn = _build_at_version(tmp_path / "v7.db", 7)
    assert "capacity" not in _cols(conn, "workers")
    assert "diagnosis" not in _cols(conn, "jobs")
    migrate(conn)
    assert "capacity" in _cols(conn, "workers")
    assert "diagnosis" in _cols(conn, "jobs")
    conn.close()


# ---------- idempotency ----------


def test_migrate_already_current_is_noop(tmp_path: Path):
    """Migrating a fresh (already-current) DB again changes nothing: version and
    the full table set are stable, and no error is raised."""
    db = tmp_path / "idem.db"
    conn = _open(db)
    assert migrate(conn) == CURRENT_VERSION
    tables_before = _tables(conn)
    wcols_before = _cols(conn, "workers")
    jcols_before = _cols(conn, "jobs")

    # Re-run twice — must be a clean no-op each time.
    assert migrate(conn) == CURRENT_VERSION
    assert migrate(conn) == CURRENT_VERSION

    assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_VERSION
    assert _tables(conn) == tables_before
    assert _cols(conn, "workers") == wcols_before
    assert _cols(conn, "jobs") == jcols_before
    conn.close()


def test_migrate_preserves_data_on_idempotent_rerun(tmp_path: Path):
    """A no-op re-migration of a populated current DB leaves its rows intact."""
    conn = _open(tmp_path / "idem2.db")
    migrate(conn)
    conn.execute(
        "INSERT INTO jobs(id, spec, requires, state, created_at, root_job_id) "
        "VALUES ('j', '{}', '{}', 'running', 1.0, 'j')")
    conn.commit()

    migrate(conn)  # no-op

    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    assert conn.execute("SELECT state FROM jobs WHERE id='j'").fetchone()[0] == "running"
    conn.close()


def test_add_missing_skips_existing_columns(tmp_path: Path):
    """The additive helper only ADDs columns that aren't already there: a DB that
    already has *some* of a step's columns must migrate without a duplicate-column
    error and end up with the full set."""
    conn = _build_at_version(tmp_path / "partial.db", 0)
    # Hand-add ONE of the V1 job columns so the V0->V1 step meets a pre-existing
    # column and must skip it rather than re-ALTER (which SQLite would reject).
    conn.execute("ALTER TABLE jobs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    assert "attempt" in _cols(conn, "jobs")
    assert "max_attempts" not in _cols(conn, "jobs")

    version = migrate(conn)  # must not raise on the already-present `attempt`

    assert version == CURRENT_VERSION
    jcols = _cols(conn, "jobs")
    assert "attempt" in jcols and "max_attempts" in jcols
    conn.close()
