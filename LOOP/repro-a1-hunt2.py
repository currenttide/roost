"""A1 hunt #2 (blobs/publish) reproductions.

Each test must fail on current master to qualify a finding for promotion under
LOOP/PROTOCOL.md A1. These are survey evidence, not the eventual regression
tests for the fixes.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from roost import publish as publishlib
from roost import server

TOKEN = "replenish-hunt-token"


@pytest.fixture()
def client(tmp_path: Path):
    app = server.create_app(
        db_path=tmp_path / "roost.db", token=TOKEN, run_sweeper=False
    )
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


def _presigned_parts(url: str) -> tuple[str, dict[str, str]]:
    parsed = urlparse(url)
    return parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}


def test_presigned_put_url_cannot_overwrite_finalized_blob(client: TestClient):
    blob = client.post("/blobs/presign", json={"name": "artifact.bin"}).json()
    path, query = _presigned_parts(blob["put_url"])

    first = client.put(
        path, params=query, content=b"trusted result", headers={"Authorization": ""}
    )
    assert first.status_code == 200

    replay = client.put(
        path, params=query, content=b"attacker overwrite", headers={"Authorization": ""}
    )
    assert replay.status_code == 409
    assert client.get(f"/blobs/{blob['id']}").content == b"trusted result"


def test_rejected_direct_upload_leaves_no_blob_row(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("roost.blobs.BLOB_MAX_BYTES", 3)
    rejected = client.post("/blobs?name=too-big.bin", content=b"four")
    assert rejected.status_code == 413
    assert client.get("/blobs").json() == []


def test_publish_entry_cap_counts_directories_and_links(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(publishlib, "SITE_MAX_FILES", 2)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in ("a", "b", "c"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
        index = b"ok"
        info = tarfile.TarInfo("index.html")
        info.size = len(index)
        tar.addfile(info, io.BytesIO(index))

    response = client.post(
        "/publish",
        params={"name": "entry-cap"},
        content=buf.getvalue(),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 413
