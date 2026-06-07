"""Blob store tests (fleet file transfer staging — mac-app DESIGN.md §14).

Covers the operator path (authed upload/list/download/delete), the presigned
path that worker-side jobs use (no credentials), signature integrity, TTL
expiry, and the pending-upload (fetch) flow.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


def test_presigned_put_is_single_use(client: TestClient):
    blob = client.post("/blobs/presign", json={"name": "result.bin"}).json()
    path, q = _presigned_parts(blob["put_url"])

    first = client.put(
        path, params=q, content=b"trusted result", headers={"Authorization": ""}
    )
    assert first.status_code == 200
    metadata = first.json()

    replay = client.put(
        path, params=q, content=b"attacker overwrite",
        headers={"Authorization": ""},
    )
    assert replay.status_code == 409
    assert client.get(f"/blobs/{blob['id']}").content == b"trusted result"

    listed = next(row for row in client.get("/blobs").json()
                  if row["id"] == blob["id"])
    assert listed["size"] == metadata["size"]
    assert listed["sha256"] == metadata["sha256"]


def test_concurrent_presigned_puts_have_one_winner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    blob = client.post("/blobs/presign", json={"name": "race.bin"}).json()
    path, q = _presigned_parts(blob["put_url"])

    real_claim = blobstore.claim_pending_blob
    claim_barrier = threading.Barrier(2)

    def synchronized_claim(conn, blob_id):
        claim_barrier.wait(timeout=5)
        return real_claim(conn, blob_id)

    monkeypatch.setattr(blobstore, "claim_pending_blob", synchronized_claim)
    bodies = (b"first contender", b"second contender")

    def upload(body: bytes):
        response = client.put(
            path, params=q, content=body, headers={"Authorization": ""}
        )
        return response.status_code, body

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(upload, bodies))

    assert sorted(status for status, _ in results) == [200, 409]
    winner = next(body for status, body in results if status == 200)
    assert client.get(f"/blobs/{blob['id']}").content == winner


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


# ---- blob name length cap (R80) ----
# Fuzzing accepted a 32,000-char `name` (HTTP 200, stored verbatim); every other
# fuzz case returned a clean 4xx. The cap (blobstore.BLOB_NAME_MAX_CHARS) closes
# that hole at the single validation seam for every entry point.


def test_blobs_upload_rejects_overlong_name(client: TestClient, db: Path):
    """The reported hole: a 32k name on POST /blobs?name= must now 422, not 200."""
    name = "A" * 32000
    r = client.post(f"/blobs?name={name}", content=b"x")
    assert r.status_code == 422, r.text
    # nothing was stored
    assert client.get("/blobs").json() == []


def test_blobs_presign_rejects_overlong_name(client: TestClient):
    """The other entry point: presign takes the name in JSON — same cap, 422."""
    name = "B" * 32000
    r = client.post("/blobs/presign", json={"name": name})
    assert r.status_code == 422, r.text


def test_blob_name_at_cap_is_accepted(client: TestClient):
    """Boundary: exactly BLOB_NAME_MAX_CHARS is the last accepted length."""
    name = "n" * blobstore.BLOB_NAME_MAX_CHARS
    r = client.post(f"/blobs?name={name}", content=b"x")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == name
    # presign honors the same boundary
    r2 = client.post("/blobs/presign", json={"name": name})
    assert r2.status_code == 200, r2.text
    assert r2.json()["name"] == name


def test_blob_name_over_cap_is_rejected(client: TestClient):
    """Boundary: cap + 1 is the first rejected length (422)."""
    name = "n" * (blobstore.BLOB_NAME_MAX_CHARS + 1)
    assert client.post(f"/blobs?name={name}", content=b"x").status_code == 422
    assert client.post("/blobs/presign", json={"name": name}).status_code == 422


def test_legitimate_long_filename_still_accepted(client: TestClient):
    """A realistic worst case — a 255-char unicode filename plus .tar.gz, the
    shape every client sends (basename / lastPathComponent) — stays well under
    the cap and is accepted unchanged."""
    name = ("ä" * 255) + ".tar.gz"
    assert len(name) <= blobstore.BLOB_NAME_MAX_CHARS
    r = client.post(f"/blobs?name={name}", content=b"x")
    assert r.status_code == 200, r.text
    assert r.json()["name"] == name


def test_validate_name_defaults_empty_to_blob():
    """Empty/None names still default to "blob" (existing client contract)."""
    assert blobstore.validate_name("") == "blob"
    assert blobstore.validate_name(None) == "blob"
    assert blobstore.validate_name("report.txt") == "report.txt"
