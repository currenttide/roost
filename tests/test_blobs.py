"""Blob store tests (fleet file transfer staging — mac-app DESIGN.md §14).

Covers the operator path (authed upload/list/download/delete), the presigned
path that worker-side jobs use (no credentials), signature integrity, TTL
expiry, and the pending-upload (fetch) flow.
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from roost import blobs as blobstore
from roost import server

TOKEN = "test-shared-token"


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    return tmp_path / "roost.db"


@pytest.fixture()
def client(db: Path):
    app = server.create_app(db_path=db, token=TOKEN, run_sweeper=False)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


def _presigned_parts(url: str) -> tuple[str, dict]:
    parsed = urlparse(url)
    return parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}


def test_upload_download_roundtrip(client: TestClient, db: Path):
    payload = b"hello fleet" * 100
    r = client.post("/blobs?name=report.txt", content=payload)
    assert r.status_code == 200, r.text
    blob = r.json()
    assert blob["name"] == "report.txt"
    assert blob["size"] == len(payload)
    assert blob["state"] == "ready"
    assert blob["sha256"]
    assert "get_url" in blob

    # authed download
    r = client.get(f"/blobs/{blob['id']}")
    assert r.status_code == 200
    assert r.content == payload

    # file is on disk under <data_dir>/blobs/
    assert blobstore.blob_path(db, blob["id"]).is_file()


def test_presigned_get_needs_no_bearer(client: TestClient):
    r = client.post("/blobs?name=x.bin", content=b"abc")
    blob = r.json()
    path, q = _presigned_parts(blob["get_url"])
    r = client.get(path, params=q, headers={"Authorization": ""})
    assert r.status_code == 200
    assert r.content == b"abc"


def test_unauthed_download_without_sig_rejected(client: TestClient):
    blob = client.post("/blobs?name=x", content=b"abc").json()
    r = client.get(f"/blobs/{blob['id']}", headers={"Authorization": ""})
    assert r.status_code == 401


def test_bad_signature_rejected(client: TestClient):
    blob = client.post("/blobs?name=x", content=b"abc").json()
    path, q = _presigned_parts(blob["get_url"])
    q["sig"] = "0" * 64
    r = client.get(path, params=q, headers={"Authorization": ""})
    assert r.status_code == 401  # falls through to bearer auth, which fails


def test_expired_signature_rejected(db: Path):
    secret = blobstore.get_secret(db)
    past = int(time.time()) - 10
    sig = blobstore.sign(secret, "deadbeef", past, "get")
    assert not blobstore.verify_sig(secret, "deadbeef", past, "get", sig)


def test_presign_upload_flow(client: TestClient):
    """The fetch direction: a job PUTs to a presigned URL, operator downloads."""
    r = client.post("/blobs/presign", json={"name": "fetched.log"})
    assert r.status_code == 200, r.text
    blob = r.json()
    assert blob["state"] == "pending"
    assert "put_url" in blob

    # download before upload completes → 409
    r = client.get(f"/blobs/{blob['id']}")
    assert r.status_code == 409

    # the worker-side leg: PUT with signature only, no bearer
    path, q = _presigned_parts(blob["put_url"])
    r = client.put(path, params=q, content=b"remote bytes",
                   headers={"Authorization": ""})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "ready"
    assert r.json()["size"] == len(b"remote bytes")

    r = client.get(f"/blobs/{blob['id']}")
    assert r.status_code == 200
    assert r.content == b"remote bytes"


def test_put_with_get_sig_rejected(client: TestClient):
    """A get signature must not authorize an upload (verb binding)."""
    blob = client.post("/blobs/presign", json={"name": "x"}).json()
    path, getq = _presigned_parts(blob["get_url"])
    r = client.put(path, params=getq, content=b"x", headers={"Authorization": ""})
    assert r.status_code == 403


def test_list_and_delete(client: TestClient):
    a = client.post("/blobs?name=a", content=b"1").json()
    b = client.post("/blobs?name=b", content=b"2").json()
    ids = {x["id"] for x in client.get("/blobs").json()}
    assert {a["id"], b["id"]} <= ids

    r = client.delete(f"/blobs/{a['id']}")
    assert r.status_code == 200
    ids = {x["id"] for x in client.get("/blobs").json()}
    assert a["id"] not in ids
    assert client.delete(f"/blobs/{a['id']}").status_code == 404


def test_expiry_pruning(client: TestClient, db: Path):
    blob = client.post("/blobs?name=ttl&ttl_sec=1", content=b"x").json()
    # expired rows vanish from list + download, and prune removes the file
    with server._connect(db) as conn:
        conn.execute(
            "UPDATE blobs SET expires_at = ? WHERE id = ?",
            (time.time() - 5, blob["id"]),
        )
    assert all(x["id"] != blob["id"] for x in client.get("/blobs").json())
    assert client.get(f"/blobs/{blob['id']}").status_code == 404

    with server._connect(db) as conn:
        removed = blobstore.prune_expired(db, conn)
    assert removed == 1
    assert not blobstore.blob_path(db, blob["id"]).exists()


def test_ttl_clamped(client: TestClient):
    blob = client.post(
        f"/blobs?name=x&ttl_sec={90 * 86400}", content=b"x").json()
    assert blob["expires_at"] - blob["created_at"] <= blobstore.BLOB_TTL_MAX + 1


def test_secret_is_persistent_and_private(db: Path, client: TestClient):
    s1 = blobstore.get_secret(db)
    s2 = blobstore.get_secret(db)
    assert s1 == s2
    secret_file = db.parent / "blob_secret"
    assert secret_file.is_file()
    assert (secret_file.stat().st_mode & 0o777) == 0o600
