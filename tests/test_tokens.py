"""Scoped client-token (front-door) permission tests.

A scoped api_token — minted via POST /pair-tokens with scope "mobile"
(phones, default) or "agent" (Codex / scripts) — authenticates as a non-admin
"client" principal. These tests pin down the scope→verbs matrix: a client token
can observe the fleet, submit/cancel jobs, and use the blob store, but is
rejected (403) from every admin and worker-plane verb. Mobile tokens minted the
old way (no scope) must keep working unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from roost import server


TOKEN = "test-shared-token"


@pytest.fixture()
def client(tmp_path: Path):
    db = tmp_path / "roost.db"
    app = server.create_app(db_path=db, token=TOKEN, run_sweeper=False)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


def _mint(client: TestClient, scope: str | None = None, label: str = "t") -> str:
    """Mint a client token (admin auth from the fixture); return the raw token."""
    body: dict = {"label": label}
    if scope is not None:
        body["scope"] = scope
    r = client.post("/pair-tokens", json=body)
    assert r.status_code == 200, r.text
    return r.json()["token"]


# ---------- minting ----------


def test_mint_default_scope_is_mobile(client: TestClient):
    r = client.post("/pair-tokens", json={"label": "phone"})
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "mobile"


def test_mint_agent_scope(client: TestClient):
    r = client.post("/pair-tokens", json={"label": "codex", "scope": "agent"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "agent"
    assert body["token"].startswith("rst-mob-")  # shares the api_token prefix


def test_mint_unknown_scope_rejected(client: TestClient):
    r = client.post("/pair-tokens", json={"label": "x", "scope": "admin"})
    assert r.status_code == 400, r.text


def test_mint_token_never_echoes_secret_on_list(client: TestClient):
    _mint(client, "agent")
    r = client.get("/pair-tokens")
    assert r.status_code == 200, r.text
    for row in r.json():
        assert "token" not in row
        assert "token_hash" not in row


# ---------- the agent scope: what it CAN do ----------


def test_agent_token_can_observe_and_submit(client: TestClient):
    tok = _mint(client, "agent")
    ah = {"Authorization": f"Bearer {tok}"}

    # Observe the fleet.
    assert client.get("/workers", headers=ah).status_code == 200
    assert client.get("/jobs", headers=ah).status_code == 200
    assert client.get("/derived", headers=ah).status_code == 200

    # Submit a job.
    r = client.post("/jobs", json={"command": "echo hi"}, headers=ah)
    assert r.status_code == 200, r.text
    job_id = r.json()["id"]

    # Read it back, then cancel it.
    assert client.get(f"/jobs/{job_id}", headers=ah).status_code == 200
    assert client.delete(f"/jobs/{job_id}", headers=ah).status_code == 200


def test_client_token_can_send_input_to_running_job(client: TestClient):
    """A client (mobile/agent) token may steer a RUNNING job (R38 INPUT verb).
    Pinned alongside SUBMIT/CANCEL in the client scope→verbs matrix."""
    # Admin (fixture) sets up a worker + a running command job.
    r = client.post("/enroll-tokens", json={"label": "t"})
    etok = r.json()["token"]
    r = client.post(
        "/enroll",
        json={"token": etok, "name": "w1", "capabilities": {"tools": ["python3"]}},
        headers={"Authorization": ""})
    worker_id, cred = r.json()["worker_id"], r.json()["credential"]
    wh = {"Authorization": f"Bearer {cred}"}
    r = client.post("/jobs", json={"command": "cat", "requires": {"tools": ["python3"]}})
    job_id = r.json()["id"]
    client.get(f"/workers/{worker_id}/poll", params={"timeout": 0}, headers=wh)
    client.post(f"/workers/{worker_id}/jobs/{job_id}/event",
                json={"type": "started", "attempt": 1}, headers=wh)

    # The client token sends input.
    tok = _mint(client, "mobile")
    ah = {"Authorization": f"Bearer {tok}"}
    r = client.post(f"/jobs/{job_id}/input", json={"text": "steer me"}, headers=ah)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "queued"
    # And it can read the input queue back.
    r = client.get(f"/jobs/{job_id}/inputs", headers=ah)
    assert r.status_code == 200 and len(r.json()["inputs"]) == 1


def test_client_token_cannot_use_worker_input_plane(client: TestClient):
    """The worker-plane input fetch/ack endpoints reject a client token (they are
    worker-credentialed, like the rest of the lease plane)."""
    tok = _mint(client, "mobile")
    ah = {"Authorization": f"Bearer {tok}"}
    assert client.get("/workers/any/jobs/j/inputs", headers=ah).status_code == 403
    assert client.post("/workers/any/jobs/j/input-ack",
                       json={"input_id": "i", "state": "delivered"},
                       headers=ah).status_code == 403


def test_agent_token_can_use_blobs(client: TestClient):
    tok = _mint(client, "agent")
    ah = {"Authorization": f"Bearer {tok}"}

    # Stage a blob (agents need file transfer).
    r = client.post("/blobs", params={"name": "f.txt"}, content=b"payload", headers=ah)
    assert r.status_code == 200, r.text
    blob = r.json()
    assert blob["state"] == "ready"

    # List + download via bearer.
    assert client.get("/blobs", headers=ah).status_code == 200
    r = client.get(f"/blobs/{blob['id']}", headers=ah)
    assert r.status_code == 200
    assert r.content == b"payload"

    # Presign (the worker-upload flow) is allowed too.
    assert client.post("/blobs/presign", json={"name": "g"}, headers=ah).status_code == 200


# ---------- the agent scope: what it must NOT do (403s) ----------


def test_agent_token_cannot_mint_tokens(client: TestClient):
    tok = _mint(client, "agent")
    ah = {"Authorization": f"Bearer {tok}"}
    assert client.post("/pair-tokens", json={"label": "x"}, headers=ah).status_code == 403
    assert client.get("/pair-tokens", headers=ah).status_code == 403
    assert client.post("/enroll-tokens", json={"label": "x"}, headers=ah).status_code == 403
    assert client.get("/enroll-tokens", headers=ah).status_code == 403


def test_agent_token_cannot_touch_workers(client: TestClient):
    tok = _mint(client, "agent")
    ah = {"Authorization": f"Bearer {tok}"}
    assert client.delete("/workers/nonexistent", headers=ah).status_code == 403
    assert client.post("/workers/prune", headers=ah).status_code == 403


def test_agent_token_cannot_finalize_jobs(client: TestClient):
    tok = _mint(client, "agent")
    ah = {"Authorization": f"Bearer {tok}"}
    # Create a captain-root job as admin, then try to finalize it as the client.
    root = client.post("/jobs", json={
        "intent": "root", "captain_root": True,
        "hierarchy": {"can_dispatch": True},
    }).json()
    r = client.post(f"/jobs/{root['id']}/finalize",
                    json={"state": "succeeded"}, headers=ah)
    assert r.status_code == 403


def test_agent_token_cannot_read_claude_creds(client: TestClient):
    tok = _mint(client, "agent")
    ah = {"Authorization": f"Bearer {tok}"}
    # require_worker rejects the client token before any provisioning check.
    assert client.get("/claude-creds", headers=ah).status_code == 403


def test_agent_token_cannot_delete_blobs(client: TestClient):
    # The blob-delete hole: DELETE /blobs was require_any (any client could wipe
    # another's blob). It must now be admin-only.
    tok = _mint(client, "agent")
    ah = {"Authorization": f"Bearer {tok}"}
    blob = client.post("/blobs", params={"name": "f"}, content=b"x", headers=ah).json()
    assert client.delete(f"/blobs/{blob['id']}", headers=ah).status_code == 403
    # Admin can still delete it.
    assert client.delete(f"/blobs/{blob['id']}").status_code == 200


def test_agent_token_cannot_hit_worker_plane(client: TestClient):
    tok = _mint(client, "agent")
    ah = {"Authorization": f"Bearer {tok}"}
    assert client.get("/workers/anything/poll",
                      params={"timeout": 0}, headers=ah).status_code == 403
    assert client.post("/workers/anything/heartbeat",
                       json={}, headers=ah).status_code == 403
    assert client.get("/triage-prompt", headers=ah).status_code == 403


# ---------- revocation + backward compatibility ----------


def test_revoked_token_dies(client: TestClient):
    r = client.post("/pair-tokens", json={"label": "tmp", "scope": "agent"})
    minted = r.json()
    tok, tid = minted["token"], minted["id"]
    ah = {"Authorization": f"Bearer {tok}"}
    assert client.get("/jobs", headers=ah).status_code == 200
    assert client.delete(f"/pair-tokens/{tid}").status_code == 200
    # A revoked token is no longer a valid bearer at all.
    assert client.get("/jobs", headers=ah).status_code == 401


def test_mobile_token_unchanged(client: TestClient):
    """Old-style mobile tokens (no scope field) keep their exact permissions:
    observe + submit/cancel allowed, admin/worker verbs denied."""
    tok = _mint(client)  # no scope → mobile
    mh = {"Authorization": f"Bearer {tok}"}
    assert client.get("/jobs", headers=mh).status_code == 200
    assert client.get("/workers", headers=mh).status_code == 200
    job = client.post("/jobs", json={"command": "true"}, headers=mh)
    assert job.status_code == 200
    assert client.delete(f"/jobs/{job.json()['id']}", headers=mh).status_code == 200
    # Still denied the admin/worker plane.
    assert client.post("/enroll-tokens", json={}, headers=mh).status_code == 403
    assert client.get("/claude-creds", headers=mh).status_code == 403
