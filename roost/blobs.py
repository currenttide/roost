"""Blob store: a small staging area on the control plane for fleet file
transfer (mac-app DESIGN.md §14).

Files live in ``<data_dir>/blobs/<id>``; metadata in the ``blobs`` table.
Presigned URLs (HMAC over ``id·exp·verb`` with a per-CP secret persisted next
to the DB) let the worker-side leg of a transfer — a normal command job —
curl a blob without ever carrying credentials. This is a staging area, not a
filesystem: blobs are size-capped and expire (the sweeper deletes them).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

BLOB_TTL_DEFAULT = 24 * 3600.0          # staged files live a day by default
BLOB_TTL_MAX = 7 * 86400.0              # hard ceiling on requested TTLs
BLOB_MAX_BYTES = 512 * 1024 * 1024      # staging cap, not a fileserver

_SECRET_FILE = "blob_secret"


def blob_dir(db_path: Path) -> Path:
    d = db_path.parent / "blobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def blob_path(db_path: Path, blob_id: str) -> Path:
    return blob_dir(db_path) / blob_id


def get_secret(db_path: Path) -> bytes:
    """Per-CP signing secret, created on first use, 0600 next to the DB."""
    path = db_path.parent / _SECRET_FILE
    try:
        return path.read_bytes()
    except OSError:
        secret = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secret)
        finally:
            os.close(fd)
        return secret


def sign(secret: bytes, blob_id: str, exp: int, verb: str) -> str:
    msg = f"{blob_id}.{exp}.{verb}".encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def verify_sig(secret: bytes, blob_id: str, exp: int, verb: str, sig: str) -> bool:
    if exp < time.time():
        return False
    return hmac.compare_digest(sign(secret, blob_id, exp, verb), sig)


def clamp_ttl(ttl_sec: Optional[float]) -> float:
    if not ttl_sec or ttl_sec <= 0:
        return BLOB_TTL_DEFAULT
    return min(float(ttl_sec), BLOB_TTL_MAX)


# ---------- rows ----------


def insert_blob(
    conn: sqlite3.Connection,
    name: str,
    ttl_sec: Optional[float],
    state: str,
    created_by: str,
) -> dict[str, Any]:
    now = time.time()
    row = {
        "id": uuid.uuid4().hex[:12],
        "name": name or "blob",
        "size": 0,
        "sha256": None,
        "state": state,  # 'pending' (awaiting PUT) | 'uploading' | 'ready'
        "created_at": now,
        "expires_at": now + clamp_ttl(ttl_sec),
        "created_by": created_by,
    }
    conn.execute(
        "INSERT INTO blobs(id, name, size, sha256, state, created_at, expires_at, created_by) "
        "VALUES (:id, :name, :size, :sha256, :state, :created_at, :expires_at, :created_by)",
        row,
    )
    return row


def get_blob(conn: sqlite3.Connection, blob_id: str) -> Optional[dict[str, Any]]:
    cur = conn.execute("SELECT * FROM blobs WHERE id=?", (blob_id,))
    row = cur.fetchone()
    return dict(row) if row is not None else None


def list_blobs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    now = time.time()
    cur = conn.execute(
        "SELECT * FROM blobs WHERE expires_at > ? ORDER BY created_at DESC", (now,)
    )
    return [dict(r) for r in cur.fetchall()]


def finalize_blob(
    conn: sqlite3.Connection, blob_id: str, size: int, sha256: str
) -> None:
    conn.execute(
        "UPDATE blobs SET size=?, sha256=?, state='ready' WHERE id=?",
        (size, sha256, blob_id),
    )


def claim_pending_blob(conn: sqlite3.Connection, blob_id: str) -> bool:
    """Atomically reserve a pending blob for one presigned PUT."""
    cur = conn.execute(
        "UPDATE blobs SET state='uploading' WHERE id=? AND state='pending'",
        (blob_id,),
    )
    return cur.rowcount > 0


def finalize_claimed_blob(
    conn: sqlite3.Connection, blob_id: str, size: int, sha256: str
) -> bool:
    """Finalize only the uploader that owns the pending -> uploading claim."""
    cur = conn.execute(
        "UPDATE blobs SET size=?, sha256=?, state='ready' "
        "WHERE id=? AND state='uploading'",
        (size, sha256, blob_id),
    )
    return cur.rowcount > 0


def release_blob_claim(conn: sqlite3.Connection, blob_id: str) -> None:
    """Make a failed claimed upload retryable without disturbing ready blobs."""
    conn.execute(
        "UPDATE blobs SET state='pending' WHERE id=? AND state='uploading'",
        (blob_id,),
    )


def delete_blob(db_path: Path, conn: sqlite3.Connection, blob_id: str) -> bool:
    cur = conn.execute("DELETE FROM blobs WHERE id=?", (blob_id,))
    try:
        blob_path(db_path, blob_id).unlink(missing_ok=True)
    except OSError:
        pass
    return cur.rowcount > 0


def prune_expired(db_path: Path, conn: sqlite3.Connection) -> int:
    """Delete expired blob rows and their files. Returns count removed."""
    now = time.time()
    rows = conn.execute(
        "SELECT id FROM blobs WHERE expires_at <= ?", (now,)
    ).fetchall()
    for r in rows:
        delete_blob(db_path, conn, r["id"] if isinstance(r, sqlite3.Row) else r[0])
    return len(rows)


# ---------- public shapes ----------


def public_dict(
    row: dict[str, Any], base_url: str, secret: bytes
) -> dict[str, Any]:
    """Row → API shape, with presigned get (and put while pending) URLs."""
    base = base_url.rstrip("/")
    exp = int(row["expires_at"])
    out = {
        "id": row["id"],
        "name": row["name"],
        "size": row["size"],
        "sha256": row["sha256"],
        "state": row["state"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "get_url": f"{base}/blobs/{row['id']}?exp={exp}&sig={sign(secret, row['id'], exp, 'get')}",
    }
    if row["state"] == "pending":
        out["put_url"] = (
            f"{base}/blobs/{row['id']}?exp={exp}&sig={sign(secret, row['id'], exp, 'put')}"
        )
    return out
