"""Read/write the worker's local config at ~/.config/roost/config.toml.

Stores: control plane URL, per-worker credential, worker_id, declared name.
Created by ``roost enroll``; read by ``roost worker``, ``roost-mcp``, and
``roost service`` commands.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

try:                       # tomllib is stdlib on 3.11+; tomli backports it to 3.10
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


def default_config_path() -> Path:
    base = os.environ.get("ROOST_CONFIG_DIR")
    if base:
        return Path(base) / "config.toml"
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "roost" / "config.toml"


def load(path: Optional[Path] = None) -> dict[str, Any]:
    p = path or default_config_path()
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        return tomllib.load(f)


def save(data: dict[str, Any], path: Optional[Path] = None) -> Path:
    p = path or default_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for key, value in data.items():
        lines.append(_format_pair(key, value))
    text = "\n".join(lines) + "\n"
    # Write 0600 so the credential isn't world-readable.
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    return p


def _toml_escape(s: str) -> str:
    """Escape a string for a TOML basic ("...") string, so a credential/name
    with a quote or backslash doesn't corrupt the config file."""
    return (s.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))


def _format_pair(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return f"{key} = {'true' if value else 'false'}"
    if isinstance(value, (int, float)):
        return f"{key} = {value}"
    if isinstance(value, str):
        return f'{key} = "{_toml_escape(value)}"'
    if isinstance(value, dict):
        # Single-level inline-table; keep it simple.
        inner = ", ".join(_format_pair(k, v) for k, v in value.items())
        return f"{key} = {{ {inner} }}"
    if isinstance(value, list):
        formatted = ", ".join(_format_value(v) for v in value)
        return f"{key} = [{formatted}]"
    raise TypeError(f"unsupported config value for {key}: {type(value).__name__}")


def _format_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return f'"{_toml_escape(v)}"'
    raise TypeError(f"unsupported list element: {type(v).__name__}")


def resolve_url_token(
    url_arg: Optional[str], token_arg: Optional[str], cfg: Optional[dict] = None
) -> tuple[str, str, Optional[str]]:
    """Return (url, token, worker_id) using flag → env → config fallback chain."""
    cfg = cfg if cfg is not None else load()
    url = url_arg or os.environ.get("ROOST_URL") or cfg.get("url") or "http://127.0.0.1:8787"
    token = (
        token_arg
        or os.environ.get("ROOST_TOKEN")
        or cfg.get("credential")
        or ""
    )
    worker_id = cfg.get("worker_id")
    return url, token, worker_id
