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


def _publish_oneshot(client: TestClient, bundle: bytes, name: str = "demo",
                     **kw):
    return client.post("/publish", params={"name": name}, content=bundle,
                       headers={"Content-Type": "application/octet-stream"},
                       **kw)


def test_one_shot_publish_roundtrip(client: TestClient, db: Path):
    # [R7] The bundle IS the body — one transactional call, no staged blob.
    r = _publish_oneshot(client, _tar_gz({"index.html": b"<h1>one shot</h1>"}),
                         "oneshot")
    assert r.status_code == 200, r.text
    site = r.json()
    assert site["slug"] == "oneshot"
    assert site["files"] == 1 and site["size"] > 0

    assert client.get("/pub/oneshot/").content == b"<h1>one shot</h1>"
    # The whole point: NO blob row ever existed.
    assert client.get("/blobs").json() == []
    # And no temp upload file is left next to the site.
    leftovers = [p.name for p in publishlib.sites_dir(db).iterdir()
                 if p.name.startswith(".")]
    assert leftovers == []


def test_one_shot_republish_replaces(client: TestClient):
    _publish_oneshot(client, _tar_gz({"index.html": b"v1"}), "demo")
    r = _publish_oneshot(client, _tar_gz({"index.html": b"v2"}), "demo")
    assert r.status_code == 200
    assert client.get("/pub/demo/").content == b"v2"


def test_one_shot_failure_injection_leaves_no_residue(client: TestClient, db: Path):
    # [R7] failure injection: a body that is not a tar.gz fails extraction —
    # nothing may survive it: no blob row, no site dir, no temp file, no row.
    r = _publish_oneshot(client, b"definitely not a tar archive", "broken")
    assert r.status_code == 400
    assert "not a valid tar.gz" in r.json()["detail"]
    assert client.get("/blobs").json() == []
    assert client.get("/publish").json() == []
    assert not publishlib.site_path(db, "broken").exists()
    assert list(publishlib.sites_dir(db).iterdir()) == []


def test_one_shot_oversized_leaves_no_residue(client: TestClient, db: Path,
                                              monkeypatch):
    # Body cap enforced mid-stream; temp file cleaned up.
    monkeypatch.setattr("roost.blobs.BLOB_MAX_BYTES", 64)
    r = _publish_oneshot(client, b"x" * 1024, "big")
    assert r.status_code == 413
    assert list(publishlib.sites_dir(db).iterdir()) == []


def test_one_shot_requires_name(client: TestClient):
    r = client.post("/publish", content=_tar_gz({"index.html": b"x"}),
                    headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 400
    assert "name" in r.json()["detail"]


def test_one_shot_bad_name_rejected(client: TestClient, db: Path):
    r = _publish_oneshot(client, _tar_gz({"index.html": b"x"}), "bad/slug!")
    assert r.status_code == 400
    assert list(publishlib.sites_dir(db).iterdir()) == []


def test_one_shot_empty_body_rejected(client: TestClient, db: Path):
    r = _publish_oneshot(client, b"", "empty")
    assert r.status_code == 400
    assert "empty body" in r.json()["detail"]
    assert list(publishlib.sites_dir(db).iterdir()) == []


def test_one_shot_client_token_can_publish(client: TestClient):
    # Same client permission set as the two-step path (scope = audit label).
    tok = client.post("/pair-tokens", json={"label": "phone"}).json()
    r = client.post("/publish", params={"name": "phone-shot"},
                    content=_tar_gz({"index.html": b"hi"}),
                    headers={"Content-Type": "application/octet-stream",
                             "Authorization": f"Bearer {tok['token']}"})
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "phone-shot"


def test_one_shot_unauthenticated_rejected(client: TestClient, db: Path):
    r = client.post("/publish", params={"name": "nope"},
                    content=_tar_gz({"index.html": b"x"}),
                    headers={"Content-Type": "application/octet-stream",
                             "Authorization": ""})
    assert r.status_code == 401
    assert list(publishlib.sites_dir(db).iterdir()) == []


def test_two_step_invalid_json_body(client: TestClient):
    # The JSON path now parses the body manually: garbage JSON → clean 400
    # (was a framework 422), still no publish.
    r = client.post("/publish", content=b"{not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert "invalid JSON" in r.json()["detail"]
    r = client.post("/publish", json=["a", "list"])
    assert r.status_code == 400
    assert "object" in r.json()["detail"]


def test_mobile_scope_publishes_end_to_end(client: TestClient):
    # [R6] The mobile-app contract (mobile-app/API.md §6) pins this: a
    # mobile-scoped pair token publishes through the SAME client permission
    # set as agent scope — scope is an audit label, not a privilege boundary
    # (see the scope→verbs matrix in server.py). Phone agents publish too.
    tok = client.post("/pair-tokens", json={"label": "phone"}).json()
    assert tok["scope"] == "mobile"  # the default scope
    mh = {"Authorization": f"Bearer {tok['token']}"}

    # stage the bundle AS the phone (not as admin)
    r = client.post("/blobs?name=phone-site.tar.gz",
                    content=_tar_gz({"index.html": b"<h1>from phone</h1>"}),
                    headers=mh)
    assert r.status_code == 200, r.text
    blob = r.json()
    assert blob["state"] == "ready"

    # publish AS the phone; response is the §6 site shape
    r = client.post("/publish", json={"blob_id": blob["id"]}, headers=mh)
    assert r.status_code == 200, r.text
    site = r.json()
    assert site["slug"] == "phone-site"  # defaulted from the blob name stem
    assert site["url"].endswith("/pub/phone-site/")
    assert site["files"] == 1 and site["size"] > 0

    # list AS the phone
    r = client.get("/publish", headers=mh)
    assert r.status_code == 200
    assert [s["slug"] for s in r.json()] == ["phone-site"]

    # the published site is live, unauthenticated
    r = client.get("/pub/phone-site/", headers={"Authorization": ""})
    assert r.status_code == 200 and b"from phone" in r.content

    # delete stays admin-only for mobile too
    r = client.delete("/publish/phone-site", headers=mh)
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


# ---------- publish domain: host routing + the public-edge guard ----------


@pytest.fixture()
def domain_client(db: Path):
    """A CP configured with a public publish domain (as behind the tunnel)."""
    app = server.create_app(
        db_path=db, token=TOKEN, run_sweeper=False, publish_domain="roost.pub")
    with TestClient(app, base_url="http://testserver") as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


def _publish_members(client: TestClient, slug: str, members: dict[str, bytes]) -> dict:
    blob_id = _upload(client, _tar_gz(members))
    r = client.post("/publish", json={"name": slug, "blob_id": blob_id})
    assert r.status_code == 200, r.text
    return r.json()


def test_public_url_in_responses(domain_client: TestClient):
    site = _publish_members(domain_client, "demo", {"index.html": b"<h1>hi</h1>"})
    assert site["public_url"] == "https://demo.roost.pub/"
    listed = domain_client.get("/publish").json()
    assert listed[0]["public_url"] == "https://demo.roost.pub/"


def test_host_routing_serves_site_at_root(domain_client: TestClient):
    _publish_members(domain_client, "demo", {
        "index.html": b"<h1>hi</h1>",
        "assets/app.js": b"console.log(1)",
    })
    r = domain_client.get("/", headers={"host": "demo.roost.pub"})
    assert r.status_code == 200 and b"<h1>hi</h1>" in r.content
    r = domain_client.get("/assets/app.js", headers={"host": "demo.roost.pub"})
    assert r.status_code == 200 and b"console.log" in r.content
    # port suffix and case are normalized
    r = domain_client.get("/", headers={"host": "Demo.Roost.PUB:443"})
    assert r.status_code == 200


def test_public_edge_guard_blocks_api(domain_client: TestClient):
    """Under the publish domain the fleet API must be unreachable — even with
    a valid admin bearer token (tunnel traffic is hostile by assumption)."""
    _publish_members(domain_client, "demo", {"index.html": b"x"})
    for path in ("/derived", "/jobs", "/workers", "/blobs", "/publish",
                 "/healthz", "/enroll-tokens", "/claude-creds"):
        r = domain_client.get(path, headers={"host": "demo.roost.pub"})
        assert r.status_code in (200, 404), (path, r.status_code)
        # 200 only if the site happens to have such a file — it doesn't:
        if r.status_code == 200:
            assert b"x" == r.content or b"<" in r.content  # site content, not API JSON
            assert "application/json" not in r.headers.get("content-type", "")
    # writes are flatly refused
    r = domain_client.post("/jobs", headers={"host": "demo.roost.pub"},
                           json={"task": "evil", "kind": "auto"})
    assert r.status_code == 405
    r = domain_client.request("DELETE", "/jobs/abc",
                              headers={"host": "demo.roost.pub"})
    assert r.status_code == 405


def test_apex_and_unknown_hosts(domain_client: TestClient):
    _publish_members(domain_client, "demo", {"index.html": b"x"})
    # apex: landing on /, 404 elsewhere, never the API
    r = domain_client.get("/", headers={"host": "roost.pub"})
    assert r.status_code == 200 and b"roost.pub" in r.content
    assert domain_client.get("/derived", headers={"host": "roost.pub"}).status_code == 404
    # unknown slug subdomain → 404
    assert domain_client.get("/", headers={"host": "nope.roost.pub"}).status_code == 404
    # nested label is not a site
    assert domain_client.get("/", headers={"host": "a.demo.roost.pub"}).status_code == 404
    # unrelated host: API works normally
    assert domain_client.get("/healthz", headers={"host": "hubbase:8787"}).status_code == 200


def test_lan_paths_still_work_with_domain_set(domain_client: TestClient):
    _publish_members(domain_client, "demo", {"index.html": b"<h1>hi</h1>"})
    r = domain_client.get("/pub/demo/")
    assert r.status_code == 200 and b"<h1>hi</h1>" in r.content


def test_slug_for_host():
    f = publishlib.slug_for_host
    assert f("demo.roost.pub", "roost.pub") == "demo"
    assert f("Demo.Roost.Pub:443", "roost.pub") == "demo"
    assert f("roost.pub", "roost.pub") is None
    assert f("a.b.roost.pub", "roost.pub") is None
    assert f("demo.roost.pub.evil.com", "roost.pub") is None
    assert f("demo.notroost.pub", "roost.pub") is None
    assert f("UPPER_bad.roost.pub", "roost.pub") is None
