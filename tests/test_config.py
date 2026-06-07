"""Tests for roost/config.py — the worker-local config file (R17).

Covers path resolution precedence (ROOST_CONFIG_DIR → XDG_CONFIG_HOME →
~/.config), load/save round-trips through a real tomllib parse, the 0600
permission contract, TOML escaping of hostile strings (the credential must
never corrupt the file), the supported value types, and the
flag → env → config → default resolution chain.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from roost import config


# ---------- default_config_path precedence ----------


def test_path_roost_config_dir_wins(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROOST_CONFIG_DIR", str(tmp_path / "rcd"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert config.default_config_path() == tmp_path / "rcd" / "config.toml"


def test_path_xdg_fallback(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ROOST_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert (config.default_config_path()
            == tmp_path / "xdg" / "roost" / "config.toml")


def test_path_home_dotconfig_last(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ROOST_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert (config.default_config_path()
            == tmp_path / ".config" / "roost" / "config.toml")


# ---------- load / save ----------


def test_load_missing_file_is_empty(tmp_path: Path):
    assert config.load(tmp_path / "nope.toml") == {}


def test_save_load_roundtrip(tmp_path: Path):
    p = tmp_path / "deep" / "config.toml"  # parent must be created
    data = {
        "url": "http://127.0.0.1:8787",
        "credential": "rst-mob-abc",
        "worker_id": "w1",
        "name": "box",
        "trusted": True,
        "capacity": 4,
        "ratio": 0.5,
        "policy": {"trust_skip_perms": True, "max_perm": "write"},
        "tags": ["gpu", "fast"],
    }
    out = config.save(data, p)
    assert out == p
    # Round-trip through the REAL toml parser — not string comparison.
    assert config.load(p) == data


def test_save_is_0600(tmp_path: Path):
    p = tmp_path / "config.toml"
    config.save({"credential": "secret"}, p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_save_truncates_previous_content(tmp_path: Path):
    p = tmp_path / "config.toml"
    config.save({"credential": "a-much-longer-old-credential-value"}, p)
    config.save({"credential": "x"}, p)
    assert config.load(p) == {"credential": "x"}  # no trailing garbage


def test_save_default_path_honors_roost_config_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ROOST_CONFIG_DIR", str(tmp_path / "rcd"))
    p = config.save({"url": "http://cp:1"})
    assert p == tmp_path / "rcd" / "config.toml"
    assert config.load() == {"url": "http://cp:1"}


# ---------- escaping: hostile strings must survive a round-trip ----------


@pytest.mark.parametrize("evil", [
    'quote " inside',
    "back\\slash",
    "new\nline",
    "tab\there",
    "carriage\rreturn",
    '"; url = "http://evil',   # attempted TOML injection
    'a"\\\n"b',
])
def test_hostile_strings_roundtrip(tmp_path: Path, evil: str):
    p = tmp_path / "config.toml"
    config.save({"credential": evil, "name": "n"}, p)
    loaded = config.load(p)
    assert loaded["credential"] == evil      # value intact
    assert loaded["name"] == "n"             # file not corrupted past it
    assert set(loaded) == {"credential", "name"}  # injection added no keys


def test_hostile_strings_in_dicts_and_lists(tmp_path: Path):
    p = tmp_path / "config.toml"
    data = {"policy": {"note": 'say "hi"\n'}, "tags": ['x"y', "a\\b"]}
    config.save(data, p)
    assert config.load(p) == data


def test_unsupported_types_raise(tmp_path: Path):
    with pytest.raises(TypeError):
        config.save({"bad": object()}, tmp_path / "c.toml")
    with pytest.raises(TypeError):
        config.save({"bad": [object()]}, tmp_path / "c.toml")


# ---------- resolve_url_token precedence ----------


def test_resolve_flag_beats_everything(monkeypatch):
    monkeypatch.setenv("ROOST_URL", "http://env:1")
    monkeypatch.setenv("ROOST_TOKEN", "env-tok")
    cfg = {"url": "http://cfg:1", "credential": "cfg-tok", "worker_id": "w9"}
    url, token, wid = config.resolve_url_token("http://flag:1", "flag-tok", cfg)
    assert (url, token, wid) == ("http://flag:1", "flag-tok", "w9")


def test_resolve_env_beats_config(monkeypatch):
    monkeypatch.setenv("ROOST_URL", "http://env:1")
    monkeypatch.setenv("ROOST_TOKEN", "env-tok")
    cfg = {"url": "http://cfg:1", "credential": "cfg-tok"}
    url, token, wid = config.resolve_url_token(None, None, cfg)
    assert (url, token, wid) == ("http://env:1", "env-tok", None)


def test_resolve_config_beats_default(monkeypatch):
    monkeypatch.delenv("ROOST_URL", raising=False)
    monkeypatch.delenv("ROOST_TOKEN", raising=False)
    cfg = {"url": "http://cfg:1", "credential": "cfg-tok", "worker_id": "w2"}
    assert (config.resolve_url_token(None, None, cfg)
            == ("http://cfg:1", "cfg-tok", "w2"))


def test_resolve_defaults(monkeypatch):
    monkeypatch.delenv("ROOST_URL", raising=False)
    monkeypatch.delenv("ROOST_TOKEN", raising=False)
    assert (config.resolve_url_token(None, None, {})
            == ("http://127.0.0.1:8787", "", None))


def test_resolve_loads_config_when_none_passed(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ROOST_URL", raising=False)
    monkeypatch.delenv("ROOST_TOKEN", raising=False)
    monkeypatch.setenv("ROOST_CONFIG_DIR", str(tmp_path))
    config.save({"url": "http://disk:1", "credential": "disk-tok"})
    assert (config.resolve_url_token(None, None)
            == ("http://disk:1", "disk-tok", None))
