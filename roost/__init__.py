"""Roost package metadata.

Single source of truth for the version. The canonical value lives in
``pyproject.toml`` (``[project].version``); everything that reports a version
— the FastAPI app, ``/healthz`` / ``/readyz``, and the roost-mcp serverInfo —
imports ``roost.__version__`` so the number can only ever drift in one file.

Resolution order:

1. The adjacent source ``pyproject.toml``, when present. Roost is normally run
   from a source checkout / editable install, where the package metadata cached
   in ``*.dist-info`` is frozen at install time and can lag a ``pyproject.toml``
   bump. Reading the source file makes the reported version track the repo
   immediately (and keeps the healthz==pyproject test deterministic).
2. Installed package metadata via ``importlib.metadata`` — the real-wheel case,
   where ``pyproject.toml`` does not ship alongside the package.
3. A literal fallback, only if neither is available.
"""

from __future__ import annotations

from pathlib import Path

try:                       # tomllib is stdlib on 3.11+; tomli backports it to 3.10
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

_FALLBACK_VERSION = "0.2.0"


def _version_from_pyproject() -> str | None:
    """Read ``[project].version`` from the source pyproject next to the package."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    try:
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    version = data.get("project", {}).get("version")
    return version if isinstance(version, str) and version else None


def _version_from_metadata() -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("roost")
    except PackageNotFoundError:  # pragma: no cover - not installed
        return None


def _detect_version() -> str:
    return (
        _version_from_pyproject()
        or _version_from_metadata()
        or _FALLBACK_VERSION
    )


__version__ = _detect_version()
