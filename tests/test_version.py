"""Version single-sourcing guard (R32).

There must be exactly ONE place the version lives: ``pyproject.toml``
(``[project].version``). Everything that self-reports a version — the package
``__version__``, the FastAPI app, ``/healthz``, ``/readyz``, and the roost-mcp
serverInfo — derives from it. These tests parse pyproject.toml independently
and pin equality against every reporter, and grep the source for leftover
hardcoded version literals so the two-source drift this item fixed can't
silently come back.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

try:                       # tomllib is stdlib on 3.11+; tomli backports it to 3.10
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

import roost
from roost import server

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = REPO / "pyproject.toml"

TOKEN = "test-shared-token"


def _pyproject_version() -> str:
    with open(PYPROJECT, "rb") as f:
        data = tomllib.load(f)
    version = data["project"]["version"]
    assert isinstance(version, str) and version, "pyproject [project].version missing"
    return version


@pytest.fixture()
def client(tmp_path: Path):
    db = tmp_path / "roost.db"
    app = server.create_app(db_path=db, token=TOKEN, run_sweeper=False)
    with TestClient(app) as c:
        c.headers.update({"Authorization": f"Bearer {TOKEN}"})
        yield c


def test_package_version_matches_pyproject():
    assert roost.__version__ == _pyproject_version()


def test_healthz_version_matches_pyproject(client: TestClient):
    r = client.get("/healthz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["version"] == _pyproject_version()


def test_readyz_version_matches_pyproject(client: TestClient):
    r = client.get("/readyz")
    assert r.status_code == 200, r.text
    assert r.json()["version"] == _pyproject_version()


def test_fastapi_app_version_matches_pyproject(client: TestClient):
    # The OpenAPI doc (and Swagger UI) surface app.version too.
    assert client.app.version == _pyproject_version()


def test_mcp_server_version_matches_pyproject():
    from roost import mcp

    assert mcp.SERVER_VERSION == _pyproject_version()


def test_cli_version_flag_matches_pyproject():
    """`roost --version` reports the single-sourced version and exits 0."""
    from click.testing import CliRunner

    from roost.cli import cli

    res = CliRunner().invoke(cli, ["--version"])
    assert res.exit_code == 0, res.output
    assert res.output.strip() == f"roost {_pyproject_version()}"


@pytest.mark.parametrize("module", ["server.py", "mcp.py"])
def test_no_hardcoded_version_literal_in_source(module: str):
    """Regression guard: no bare ``X.Y.Z`` string literal should reappear in the
    version reporters. The version must come from ``roost.__version__`` only."""
    src = (REPO / "roost" / module).read_text()
    bad = re.findall(r"""['"]\d+\.\d+\.\d+['"]""", src)
    assert not bad, f"hardcoded version literal(s) in roost/{module}: {bad}"
