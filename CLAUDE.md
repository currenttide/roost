# Roost — context for Claude

**Roost** is a pull-based orchestrator for agent jobs across a fleet of heterogeneous
machines. Workers long-poll a control plane, lease jobs, heartbeat while running, and
report results. Jobs are `command`, `claude` (agent), or `docker` (isolated/GPU). See
`README.md` for the full picture.

## Layout
- `roost/server.py` — control plane (FastAPI + SQLite/WAL): enroll, place, lease, heartbeat, creds provisioning.
- `roost/worker.py` — worker loop: capability detection, pull/lease/run/report, command/claude/docker executors.
- `roost/matcher.py` — `requires:` ↔ capability matching.
- `roost/captain.py`, `roost/mcp.py` — captain agent (`roost dispatch`) + its MCP tools.
- `roost/cli.py` — the `roost` command. `roost/config.py` — `~/.config/roost/config.toml`. `roost/schema.py` — DB schema + migrations.
- `roost/tui.py` — `roost dash`, the full-screen curses dashboard (Linux sibling of `mac-app/`). Pure-logic layer (formatting/staleness/sort/console-wiring/`TuiClient`) is TTY-free and tested in `tests/test_tui.py`; reuses `cli.distill_log_line`.
- `roost/service.py` — durable worker/CP services (systemd / launchd).
- `.claude/skills/` — `roost-quickstart` (first-run setup), `roost-onboard` (add a node), `roost-oversee` (monitor).
- `docker/` — worker container + full stack. `mac-app/` — optional macOS control app. `examples/` — runnable job specs.
- `mobile-app/` — iPhone/Android thin clients (`ios/` SwiftUI, `android/` Compose) + the pinned `API.md` contract and golden `fixtures/` both build against (regen: `record_fixtures.py`). Pure-logic layers testable on Linux; see each README.
- `docs/` — `INTEGRATIONS.md` (front-door plug-in recipes per agent app), `DEPLOY.md` (fleet rollout).

## Working here
- Run the tests after any code change: `python -m pytest -q`.
- The control plane defaults to `http://127.0.0.1:8787`; the CLI resolves URL/token via
  flags → `ROOST_URL`/`ROOST_TOKEN` → `~/.config/roost/config.toml`.
- **Pin Python 3.12** for worker installs (`uv tool install --python 3.12 …`) — newer
  interpreters can break the async HTTP client.

## Conventions
- **Never commit credentials, tokens, or a runtime DB** — `.gitignore` blocks `*.db`,
  `admin_token`, `*.credentials.json`, `.roost/`, and `.claude/settings.local.json`.
- Keep code dependency-light and match the surrounding style.
- Credential handling is security-sensitive: any flow that copies real Claude creds to
  another machine must be an explicit, consented choice — never silent.
