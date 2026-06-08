# Roost UAT findings — 2026-06-07 (4 real-user testers: Mac / iOS / Android / backend)

Standalone report so it does NOT collide with the active loop's BACKLOG/JOURNAL.
Triage each against CURRENT master before promoting — the parallel loop has advanced
the tree well past the UAT baseline (UAT ran against ~dbd229f/823 tests; master is now
704d06d+/941), so some items may already be fixed. Each item notes file:line as observed.

## Already actioned this session (outside the loop)
- **DEPLOYED**: live CP redeployed from current master (was a Jun-06 build, schema V11,
  missing /schedules /jobs/{id}/input one-shot-/publish /metrics). Now current; 16/16
  workers reconnected; 787→ jobs + enrollments preserved; DB backed up to
  /home/yang/roost-fleet/data/roost.db.uat-bak-20260607-205936.
- **FIXED + deployed**: docker/Dockerfile hardcoded `dist/roost-0.1.0-...whl` → broke
  `compose build` after R32 bumped pyproject to 0.2.0. Changed to a version glob
  `roost-*-py3-none-any.whl`. (This commit.)
- **CREDS re-provisioned**: every worker held creds expired Jun-06 and wasn't refreshing
  (stale builds). Rolling `systemctl restart roost-worker.service` made each re-fetch from
  the now-current CP; agent jobs verified working on sevend/pi3/oracle/jetson-orin/
  digitalocean/hubbase-gpu.

## OPEN RECOMMENDATION — fleet worker-build rollout (P1 ops, not yet done)
Workers run stale builds (started Jun-05/06; `roost --version` absent = pre-R42). They
lack R43 cred auto-refresh (creds re-expire ~8h → agent jobs 401 again until next
restart), R24/25/26/30/31 worker fixes, R38 input delivery, R41 GPU-failed state, R72
bounded docker teardown. hubbase-gpu even returns `failed` despite correct agent output
(stale-build artifact). DURABLE FIX: update the roost package on all 16 nodes + restart.
This is a DEPLOY.md rollout across heterogeneous nodes (pis/jetsons/cloud/mac/wsl/docker)
— needs explicit go-ahead on approach (rolling, per-node verify). Until then, agent jobs
work but creds need a periodic worker restart.

## Code bugs (triage vs current master; promote the still-open ones)
- **P1 phone steering UI absent** — `POST /jobs/{id}/input` (R38) has no composer on
  iOS (SessionStore: no sendInput) or Android (ApiClient.kt: no input method). Watch+cancel
  only; can't answer a job. Cross-platform.
- **P1 codex non-git default** — `_build_codex_argv` (worker.py ~1116) omits
  `--skip-git-repo-check`; a fresh worker's non-git cwd fails every codex job.
- **P2 CLI raw tracebacks** — ~19 unwrapped `raise_for_status()` in cli.py (e.g. `roost
  workers`/`cancel` w/ bad token, malformed-YAML submit) dump Python tracebacks; main()
  (cli.py ~2232) has no top-level handler. submit/publish/schedule DO wrap.
- **P2 Mac app no two-step publish fallback** — RoostClient.publishBundle is one-shot only;
  500s against an old CP. CLI degrades; app should mirror or show "upgrade your CP".
- **P2 Android session subtitle hardcodes "claude"** — sessionSubtitle() in SessionScreen.kt
  appends "claude" for every kind incl. command.
- **P3 set**: no workers/GPU screen on either phone; failed-agent rows render raw JSON
  (both phones); "1 nodes" pluralization (panel + Android); publish slug silently
  normalized w/ no notice; Android keyboard occludes Publish/Dispatch/Pair CTAs; Mac app
  no single-instance guard (two menu-bar instances); Mac Info.plist 0.1.0 vs 0.2.0; blob
  DELETE 403 for own blob (token scope); version string 0.2.0 unreliable as a staleness
  signal (bump per-deploy or expose build sha).

## UX themes (cross-surface)
- **Agent-job output is a JSON firehose** on CLI `--follow`, iOS, Android session views —
  the distilled `→ Bash` / `🔎 verifying` / `✓ verified` lines exist but are buried under
  raw stream-json (incl. base64 signature blobs). Highest-leverage UX fix: default to the
  distilled view, raw behind a `--verbose`/toggle. (`_stream` cli.py ~2210 echoes every log.)
- **Verified-state ambiguity** — green ✓ but trailing "verifying result" text lingers
  (iOS 07). Collapse to a crisp "verified" badge once verified==true.
- **Untrusted-worker silent failure** — a hand-enrolled worker (no --trust) fails every
  agent job on permission walls with no submit-time warning. Surface it.
- **Panel/notify polish** — node names truncate w/o tooltip; failed-job notify payload
  lacks the verdict/reason; health banner counts the operator's own benign nonzero exits.

## Verified working end-to-end (current master, scratch CPs)
pairing (all clients, both paths), dashboard, command/auto/claude/docker jobs, the trust
loop (verifier runs + records), live logs, cancel + tree-cancel, schedules full CRUD +
real beats + 30s floor, publish one-shot + slug 400 + served site (Playwright HTTP 200) +
pagination/X-Total-Count, notify webhook (payload + ntfy headers), workers/GPU list,
history, backup→restore (state survives), /metrics (auth-gated, series move), the web
panel (task board = standout), `roost up`. Error states 400/409/413 friendly. Android
first-ever on-device render: clean, dark mode, rotation, zero crashes across 59 screens.

## Honest caps (could not test)
- Mac app pixels/AX: host-screen capture classifier-blocked + System Events needs a macOS
  Automation (TCC) grant unanswerable headlessly. Functionally verified via every HTTP
  call + CLI effects. One manual screenshot or a standing TCC grant unlocks it.
- iOS interactive taps (no idb), GPU-container docker job, live lease-expiry timing.
- Screenshots: /tmp/roost-uat/{ios,android,backend}/ (75 total).
