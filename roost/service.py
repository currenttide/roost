"""Install/start/stop a long-running worker as a supervised user service.

Linux  → systemd user unit at ~/.config/systemd/user/roost-worker.service
        (loginctl enable-linger so it survives logout).
macOS  → launchd LaunchAgent at ~/Library/LaunchAgents/com.roost.worker.plist
        (KeepAlive=true).
Other  → returns exit 2 with a message to run `roost worker` under your own supervisor.

This module shells out to systemctl / launchctl rather than reimplementing
service management. Each entry point returns (rc, message).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

UNIT_NAME = "roost-worker"
LAUNCHD_LABEL = "com.roost.worker"


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{UNIT_NAME}.service"


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _resolve_roost_bin() -> str:
    candidate = shutil.which("roost")
    if candidate:
        return candidate
    # Common uv-tool location.
    fallback = Path.home() / ".local" / "bin" / "roost"
    if fallback.exists():
        return str(fallback)
    # Last resort: invoke via python -m.
    return f"{sys.executable} -m roost.cli"


# Env vars captured at `roost service install` time and baked into the unit, so
# the supervised worker (and the `claude` subprocesses it spawns) see them. Most
# important: CLAUDE_CONFIG_DIR, which points claude at an isolated creds dir on a
# shared box so we use our own auth without touching the node's ~/.claude.
_PROPAGATED_ENV = ("CLAUDE_CONFIG_DIR", "DOCKER_CONFIG")


def _service_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _PROPAGATED_ENV if os.environ.get(k)}


def _render_systemd_unit(roost_bin: str) -> str:
    # Quote the whole KEY=value so a path with spaces (e.g. macOS-style dirs) or
    # special chars isn't word-split by systemd; escape internal " and \.
    env_lines = "".join(
        'Environment="{}={}"\n'.format(k, v.replace("\\", "\\\\").replace('"', '\\"'))
        for k, v in _service_env().items()
    )
    return f"""[Unit]
Description=Roost worker (pull-based agent job runner)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={roost_bin} worker
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
# Include ~/.local/bin so the worker finds uv-installed tools (claude, uv) — the
# systemd default PATH excludes it, which otherwise hides `claude` from capability
# detection. /usr/lib/wsl/lib is where WSL2 exposes the Windows GPU driver's
# nvidia-smi (harmless/absent on non-WSL hosts).
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/lib/wsl/lib
{env_lines}# Drop privileges further if you run as root; default keeps the invoking user.

[Install]
WantedBy=default.target
"""


def _render_launchd_plist(roost_bin: str) -> str:
    from xml.sax.saxutils import escape  # XML-escape all interpolated values
    parts = roost_bin.split()
    program_args = "".join(f"        <string>{escape(p)}</string>\n" for p in parts + ["worker"])
    log_path = escape(str(Path.home() / "Library" / "Logs" / "roost-worker.log"))
    extra_env = "".join(
        f"        <key>{escape(k)}</key><string>{escape(v)}</string>\n"
        for k, v in _service_env().items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{program_args}    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>5</integer>
    <key>StandardOutPath</key><string>{log_path}</string>
    <key>StandardErrorPath</key><string>{log_path}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>/usr/local/bin:/usr/bin:/bin:{Path.home()}/.local/bin</string>
{extra_env}    </dict>
</dict>
</plist>
"""


def install(start: bool = False) -> tuple[int, str]:
    system = platform.system().lower()
    roost_bin = _resolve_roost_bin()
    if system == "linux":
        unit_path = _systemd_unit_path()
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(_render_systemd_unit(roost_bin))
        # Enable linger so the unit survives logout.
        if shutil.which("loginctl"):
            subprocess.run(
                ["loginctl", "enable-linger", os.environ.get("USER", "")],
                check=False, capture_output=True,
            )
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            subprocess.run(
                ["systemctl", "--user", "enable", f"{UNIT_NAME}.service"], check=False
            )
            if start:
                rc = subprocess.run(
                    ["systemctl", "--user", "start", f"{UNIT_NAME}.service"],
                    check=False,
                ).returncode
                return rc, f"installed unit at {unit_path}; started={start}"
            return 0, f"installed unit at {unit_path}; run `systemctl --user start {UNIT_NAME}` to start"
        return 2, f"wrote unit to {unit_path} but systemctl not found"
    if system == "darwin":
        plist_path = _launchd_plist_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(_render_launchd_plist(roost_bin))
        if shutil.which("launchctl") and start:
            uid = os.getuid()
            subprocess.run(
                ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)], check=False
            )
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/{LAUNCHD_LABEL}"],
                check=False,
            )
            return 0, f"installed plist at {plist_path}; started"
        return 0, f"installed plist at {plist_path}; load with launchctl bootstrap"
    return 2, f"unsupported platform: {system}; run `roost worker` under your own supervisor"


def start() -> tuple[int, str]:
    system = platform.system().lower()
    if system == "linux" and shutil.which("systemctl"):
        rc = subprocess.run(
            ["systemctl", "--user", "start", f"{UNIT_NAME}.service"], check=False
        ).returncode
        return rc, "systemctl start dispatched"
    if system == "darwin" and shutil.which("launchctl"):
        uid = os.getuid()
        rc = subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{LAUNCHD_LABEL}"], check=False
        ).returncode
        return rc, "launchctl kickstart dispatched"
    return 2, f"no supervisor available on {system}"


def stop() -> tuple[int, str]:
    system = platform.system().lower()
    if system == "linux" and shutil.which("systemctl"):
        rc = subprocess.run(
            ["systemctl", "--user", "stop", f"{UNIT_NAME}.service"], check=False
        ).returncode
        return rc, "systemctl stop dispatched"
    if system == "darwin" and shutil.which("launchctl"):
        uid = os.getuid()
        rc = subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"], check=False
        ).returncode
        return rc, "launchctl bootout dispatched"
    return 2, f"no supervisor available on {system}"


def status() -> tuple[int, str]:
    system = platform.system().lower()
    if system == "linux" and shutil.which("systemctl"):
        out = subprocess.run(
            ["systemctl", "--user", "status", f"{UNIT_NAME}.service"],
            check=False, capture_output=True, text=True,
        )
        return out.returncode, out.stdout or out.stderr
    if system == "darwin" and shutil.which("launchctl"):
        uid = os.getuid()
        out = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"],
            check=False, capture_output=True, text=True,
        )
        return out.returncode, out.stdout or out.stderr
    return 2, f"no supervisor available on {system}"


def logs(follow: bool = False) -> tuple[int, str]:
    system = platform.system().lower()
    if system == "linux" and shutil.which("journalctl"):
        cmd = ["journalctl", "--user", "-u", f"{UNIT_NAME}.service"]
        if follow:
            cmd.append("-f")
        rc = subprocess.run(cmd, check=False).returncode
        return rc, ""
    if system == "darwin":
        log_file = Path.home() / "Library" / "Logs" / "roost-worker.log"
        if not log_file.exists():
            return 2, f"no log file at {log_file}"
        if follow:
            rc = subprocess.run(["tail", "-f", str(log_file)], check=False).returncode
        else:
            rc = subprocess.run(["tail", "-n", "200", str(log_file)], check=False).returncode
        return rc, ""
    return 2, f"no log source available on {system}"
