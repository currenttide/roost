"""Static publish tests (built thing → real URL on your own control plane).

Covers the one-command roundtrip (tar → blob → /publish → /pub/<slug>/),
atomic republish, slug validation/normalization, the safety boundary (tar
escape members, path traversal on serving), caps, who-can-publish (client
tokens yes, delete admin-only), unauthenticated public serving, and the
``sites`` table / list / unpublish bookkeeping.
"""

from __future__ import annotations

import io
import tarfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roost import publish as publishlib
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


def _tar_gz(members: dict[str, bytes]) -> bytes:
    """Build a tar.gz from {arcname: content}."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _upload(client: TestClient, bundle: bytes, name: str = "bundle.tar.gz") -> str:
    r = client.post(f"/blobs?name={name}", content=bundle)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _publish(client: TestClient, bundle: bytes, name: str = "demo"):
    blob_id = _upload(client, bundle)
    return client.post("/publish", json={"name": name, "blob_id": blob_id})


def test_publish_roundtrip(client: TestClient, db: Path):
    bundle = _tar_gz({
        "index.html": b"<h1>hello</h1>",
        "assets/app.js": b"console.log(1)",
    })
    r = _publish(client, bundle, "demo")
    assert r.status_code == 200, r.text
    site = r.json()
    assert site["slug"] == "demo"
    assert site["url"].endswith("/pub/demo/")
    assert site["files"] == 2

    # root serves index.html
    r = client.get("/pub/demo/")
    assert r.status_code == 200
    assert b"hello" in r.content
    assert "text/html" in r.headers["content-type"]

    # nested asset + media type
    r = client.get("/pub/demo/assets/app.js")
    assert r.status_code == 200
    assert r.content == b"console.log(1)"
    assert "javascript" in r.headers["content-type"]

    # files landed on disk under <data_dir>/sites/
    assert (publishlib.site_path(db, "demo") / "index.html").is_file()


def test_root_redirects_to_trailing_slash(client: TestClient):
    _publish(client, _tar_gz({"index.html": b"x"}), "demo")
    r = client.get("/pub/demo", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/pub/demo/"


def test_republish_replaces_atomically(client: TestClient, db: Path):
    _publish(client, _tar_gz({"index.html": b"v1", "old.txt": b"gone"}), "demo")
    assert client.get("/pub/demo/old.txt").status_code == 200

    r = _publish(client, _tar_gz({"index.html": b"v2"}), "demo")
    assert r.status_code == 200
    assert client.get("/pub/demo/").content == b"v2"
    # old file is gone after the atomic swap
    assert client.get("/pub/demo/old.txt").status_code == 404
    assert not (publishlib.site_path(db, "demo") / "old.txt").exists()


def test_slug_normalization(client: TestClient):
    r = _publish(client, _tar_gz({"index.html": b"x"}), "My Site")
    assert r.status_code == 200
    assert r.json()["slug"] == "my-site"
    assert client.get("/pub/my-site/").status_code == 200


def test_bad_slug_rejected(client: TestClient):
    r = _publish(client, _tar_gz({"index.html": b"x"}), "bad/slug!")
    assert r.status_code == 400


def test_default_name_from_blob_stem(client: TestClient):
    blob_id = _upload(client, _tar_gz({"index.html": b"x"}), name="mysite.tar.gz")
    r = client.post("/publish", json={"blob_id": blob_id})
    assert r.status_code == 200, r.text
    # stem of "mysite.tar.gz" → "mysite.tar" → normalize keeps a valid slug
    assert r.json()["slug"]


def test_tar_escape_members_rejected_or_skipped(client: TestClient, db: Path):
    # absolute path + parent-traversal members must never land outside sites/.
    bundle = _tar_gz({
        "index.html": b"ok",
        "../escape.txt": b"PWNED",
        "/abs-escape.txt": b"PWNED",
    })
    sentinel = db.parent / "escape.txt"
    abs_sentinel = Path("/abs-escape.txt")
    r = _publish(client, bundle, "demo")
    # either the publish fails, or the dangerous members are filtered out — but
    # nothing escapes the site dir.
    assert not sentinel.exists()
    assert not abs_sentinel.exists()
    if r.status_code == 200:
        assert not (publishlib.site_path(db, "demo") / ".." / "escape.txt").exists()


def test_path_traversal_on_serving(client: TestClient):
    _publish(client, _tar_gz({"index.html": b"x"}), "demo")
    # traversal in the served path must 404, never escape the site dir.
    r = client.get("/pub/demo/../../roost.db")
    assert r.status_code in (404, 400)
    assert b"sqlite" not in r.content.lower()


def test_spa_fallback(client: TestClient):
    _publish(client, _tar_gz({"index.html": b"app"}), "demo")
    # extension-less route with no matching file → index.html
    assert client.get("/pub/demo/some/deep/route").content == b"app"
    # but a missing file WITH an extension → 404
    assert client.get("/pub/demo/missing.css").status_code == 404


def test_caps_oversized_filecount(client: TestClient, monkeypatch):
    monkeypatch.setattr(publishlib, "SITE_MAX_FILES", 2)
    bundle = _tar_gz({f"f{i}.txt": b"x" for i in range(5)})
    r = _publish(client, bundle, "demo")
    assert r.status_code == 413


def test_caps_oversized_bytes(client: TestClient, monkeypatch):
    monkeypatch.setattr(publishlib, "SITE_MAX_BYTES", 10)
    bundle = _tar_gz({"big.txt": b"x" * 1000})
    r = _publish(client, bundle, "demo")
    assert r.status_code == 413


def test_client_token_can_publish_but_not_delete(client: TestClient, db: Path):
    # mint an agent-scoped client token via the admin endpoint
    tok = client.post("/pair-tokens", json={"label": "agent", "scope": "agent"}).json()
    raw = tok["token"]
    blob_id = _upload(client, _tar_gz({"index.html": b"x"}), "site.tar.gz")

    # client CAN publish (agents publish — that's the point)
    r = client.post("/publish", json={"name": "demo", "blob_id": blob_id},
                    headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200, r.text

    # but DELETE is admin-only → 403 for a client token
    r = client.delete("/publish/demo", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 403


def test_unauthenticated_public_serving(client: TestClient):
    _publish(client, _tar_gz({"index.html": b"public"}), "demo")
    # serving needs no bearer
    r = client.get("/pub/demo/", headers={"Authorization": ""})
    assert r.status_code == 200
    assert r.content == b"public"


def test_unauthenticated_publish_rejected(client: TestClient):
    blob_id = _upload(client, _tar_gz({"index.html": b"x"}))
    r = client.post("/publish", json={"name": "demo", "blob_id": blob_id},
                    headers={"Authorization": ""})
    assert r.status_code == 401


def test_sites_table_list_and_unpublish(client: TestClient, db: Path):
    _publish(client, _tar_gz({"index.html": b"x"}), "demo")

    # row exists in the sites table
    with server._connect(db) as conn:
        row = publishlib.get_site(conn, "demo")
    assert row is not None and row["slug"] == "demo"

    # list endpoint shows it
    sites = client.get("/publish").json()
    assert any(s["slug"] == "demo" for s in sites)

    # unpublish removes dir + row
    r = client.delete("/publish/demo")
    assert r.status_code == 200
    assert not publishlib.site_path(db, "demo").exists()
    with server._connect(db) as conn:
        assert publishlib.get_site(conn, "demo") is None
    assert client.delete("/publish/demo").status_code == 404


def test_publish_missing_blob(client: TestClient):
    r = client.post("/publish", json={"name": "demo", "blob_id": "deadbeef"})
    assert r.status_code == 404


def test_site_survives_fresh_app(client: TestClient, db: Path):
    _publish(client, _tar_gz({"index.html": b"durable"}), "demo")
    # a brand-new app over the same DB + data dir still serves the site
    app2 = server.create_app(db_path=db, token=TOKEN, run_sweeper=False)
    with TestClient(app2) as c2:
        c2.headers.update({"Authorization": f"Bearer {TOKEN}"})
        r = c2.get("/pub/demo/")
        assert r.status_code == 200
        assert r.content == b"durable"
