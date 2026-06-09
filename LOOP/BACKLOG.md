# Loop backlog

Direction anchor for the improvement loop (see `PROTOCOL.md` for the rules).

- **Humans** may edit the Ranked section anytime: reorder, cut, sharpen Done-when.
- **The loop** (standing human direction 2026-06-06: best judgment, feature focus,
  production-readiness goal — see PROTOCOL.md) takes up to 3 unblocked Ranked items
  per iteration, dispatched to parallel isolated worktree agents. It keeps Ranked
  stocked via Replenishment: confirmed bugs (reproducing test required) outrank
  features; features/production items (Tier B, loop judgment) are the focus and
  Ranked should always hold ≥3 of them; coverage/docs ratchets fill the rest.
  Breaking API/contract changes and security-surface items are the two remaining
  human gates (the latter handled in a dedicated session).
- Every PR is gated by an independent judge on a different model that re-runs the
  evidence itself; approved PRs auto-merge (squash).
- Status: `open` → `in-progress` → `done` (or `blocked: <why>`).

---

## Iteration 0 — verification plumbing (do first)

### I0. Prove the verification paths the loop will rely on — `done` *(2026-06-05, journal entry)*
Surface: tooling/fleet. The loop's honesty depends on these working before any feature claims.
- [x] Fix admin auth: token from `~/roost-fleet/admin_token` wired into `~/.config/roost/config.toml` (0600, outside repo); `roost workers` lists 16 nodes.
- [x] Repoint the installed `roost` CLI: editable install now maps to `/workspace/yang/roost-oss/roost` (was `/workspace/yang/agent_fleet`), Python 3.12.8.
- [x] Confirm the Mac node: `roost exec mac-mini-m4` → Xcode 26.2 (17C52), simulator list returned, iPhone 17 Pro booted, exit 0.
- [x] Artifact round-trip: presigned blob `ab1499820fe3`; Mac `simctl` screenshot (280,802 B PNG) PUT from the Mac, downloaded here; sha256 `5381727b…` identical at all three hops.
- [x] Hygiene: add `mac-app/.build/` to `.gitignore` — done 2026-06-05, pre-loop.

Done-when: the four remaining proven with evidence in the journal; the Mac path either works
end-to-end or is marked `blocked` with the concrete obstacle, and every mac/iOS
backlog item below inherits that block honestly.

### I1. Integrate outstanding feature branches into master — `done` *(2026-06-06, PRs #8 #5; #4 auto-resolved, #6 closed superseded)*
Surface: git. The loop targets `master`, but master (27ffdb1) is behind: `feat/agent-substrate`
(blob store + mobile + publish/substrate, tip 406d079) and `feat/mac-app` (native SwiftPM
menu-bar app, 2c41ae7 — deletes the old pywebview wrapper this branch still carries) are
both unmerged and parallel. `feat/mobile-app` (750e3a5) is superseded — close it.
Done-when: PR per branch, full pytest gate on each merge result, `feat/mac-app`'s RoostKit
`swift test` green on Linux (`/tmp/swift-toolchain`), conflicts resolved in favor of the
Swift mac-app (the pywebview files are the deleted PoC); master ends up containing both
lines; stale branch closed with a note. Also heals the known dangling cross-reference at
mobile-app/ios/README.md:137 (points at mac-app for a SwiftPM pattern that only exists on
feat/mac-app) — verify it after the merge.

---

## Ranked

### R24. Auto job crash after decline marker misclassified as `declined` — `done` *(2026-06-06, PR #31)* `self-promoted`
Surface: backend/correctness. A1 hunt #3 (worker executors). `run_job` checks `declined` before `exit_code != 0`, so a `kind:auto` triage subprocess that emits `ROOST_DECLINE:` then crashes with non-zero exit is reported as `type="declined"` (causing the CP to requeue it) instead of `type="failed"`. Causes an infinite retry loop across the fleet.
Repro: `tests/test_judge_r4_bugs.py::test_bug5_auto_decline_then_crash_reported_as_declined_not_failed` — FAILS on master.
Done-when: exit_code check wins over the `declined` flag (non-zero exit is always `failed` regardless of marker); repro test passes; pytest green.

### R25. `_running`/`_active` leaked when `run_job` is cancelled — `done` *(2026-06-07, PR #35)* `self-promoted`
Surface: backend/robustness. A1 hunt #3. `self._running += 1` and `self._active[job_id] = ...` are set early in `run_job` with no enclosing `try/finally`. On `task.cancel()`, `CancelledError` propagates out leaving both counters permanently wrong — capacity accounting corrupts over time.
Repro: `tests/test_judge_r4_bugs.py::test_bug1_running_and_active_not_leaked_on_cancellation` — FAILS on master.
Done-when: `try/finally` wraps the full body after `_running += 1`; decrement and `_active.pop` unconditional on any exit path including `CancelledError`; repro test passes; pytest green.

### R26. `OSError` subclasses escape `run_job` without posting terminal event — `done` *(2026-06-06, PR #34)* `self-promoted`
Surface: backend/robustness. A1 hunt #3. The `except` clause around `asyncio.create_subprocess_exec` catches only `FileNotFoundError` and `PermissionError`. Broader `OSError` subclasses (e.g. `BlockingIOError` EAGAIN, `OSError` EMFILE) propagate uncaught — `_running` stays incremented, no terminal event posted, job stuck at "started" forever.
Repro: `tests/test_judge_r4_bugs.py::test_bug4_other_oserror_does_not_escape_run_job` — FAILS on master.
Done-when: except broadened to `OSError`; spawn failures post `type="failed"` and decrement `_running`; repro test passes; pytest green.

### R27. `roost_submit` MCP schema missing `kind: auto` — `done` *(2026-06-06, PR #33)* `self-promoted`
Surface: MCP/correctness. `roost/mcp.py` defined `kind` enum as `["claude","codex","docker"]`; `"auto"` was absent. Captain agents calling `roost_submit` with `kind: auto` were rejected by MCP schema validation before reaching the server.
Done-when: `kind` enum includes `"auto"` with description; INTEGRATIONS.md updated; test added; pytest green.

### R28. INTEGRATIONS.md MCP tool table missing 6 of 16 tools — `done` *(2026-06-06, PR #37)* `self-promoted`
Surface: docs. A6 survey cycle #4 (judge-approved, fast-tracked per protocol). `docs/INTEGRATIONS.md` tool table lists 9 tools; `roost/mcp.py` TOOL_IMPL defines 16. Missing: `stage_file`, `send_file`, `fetch_file`, `list_staged`, `roost_schedule`, `roost_wait` (collapsed into another row). File transfer and scheduling are invisible to new MCP users.
Done-when: tool table contains every tool in TOOL_IMPL with one-line descriptions matching their mcp.py docstrings; re-verify against current master (R27 already touched the roost_submit row); pytest green (docs-drift ratchet stays 0).

### R29. `roost history` and `roost prune-workers` undocumented — `done` *(2026-06-06, PR #38)* `self-promoted`
Surface: docs. A6 survey cycle #4 (judge-approved, fast-tracked per protocol). Both commands fully implemented (cli.py:1916, cli.py:1029) but absent from README.md's "Inspect & control runs" table and docs/INTEGRATIONS.md. `roost history --failed` is the natural "what went wrong this week" entry point and no user can discover it.
Done-when: README.md inspect/control table includes `roost history [--failed]` and `roost prune-workers`; INTEGRATIONS.md CLI section mentions `roost history`; pytest green (docs-drift ratchet stays 0).

### R30. `_oneshot_agent` corrupts bwrap argv when inserting `--append-system-prompt` — `done` *(2026-06-06, PR #40)* `self-promoted`
Surface: backend/correctness. A1 hunt #3 deferred bug, repro'd + judge-approved in cycle #6 prep (PR #39). `roost/worker.py:2028-2029`: with policy `sandbox: "bwrap"`, the argv is bwrap-wrapped but the code splices `--append-system-prompt` at fixed index `argv[:3]`, landing inside bwrap's flags (`--ro-bind / /` → `--ro-bind / --append-system-prompt … /`). `_build_auto_argv` (line ~1013) does it correctly via `argv.index("claude")`.
Repro: `LOOP/repro-a1-hunt3.py::test_oneshot_agent_keeps_bwrap_argv_intact_with_system_prompt` — FAILS on master.
Done-when: insertion anchored to the `claude` position (parity with `_build_auto_argv`); repro passes; pytest green.

### R31. `_oneshot_agent` leaks relay tasks on CancelledError — `done` *(2026-06-06, PR #42)* `self-promoted`
Surface: backend/robustness. A1 hunt #3 deferred bug, repro'd + judge-approved in cycle #6 prep (PR #39). `roost/worker.py:2076-2091`: relay tasks `t1`/`t2` are gathered inside `try`, not `finally`; a cancel during `asyncio.wait_for(proc.wait(), …)` skips the gather and the tasks float as pending (asyncio warnings, test interference).
Repro: `LOOP/repro-a1-hunt3.py::test_oneshot_agent_cancels_relay_tasks_on_cancellation` — FAILS on master.
Done-when: finally cancels/awaits both relay tasks on every exit path; repro passes; pytest green.

### R32. Single-source the version (pyproject 0.1.0 vs server 0.2.0) — `done` *(2026-06-06, PR #41)* `self-promoted`
Surface: backend/correctness. A6 promotion from Proposed. `__version__` in roost/__init__.py (adjacent-pyproject first → importlib.metadata → documented fallback); pyproject bumped to 0.2.0; healthz/readyz/FastAPI/MCP all import it; equality test parses pyproject independently. Both judge phases passed (gates, then diff).

### R33. Captain observability: sub-job plan + reasoning in `roost tree` — `done` *(2026-06-06, PR #45)* `self-promoted` `feature`
Surface: backend/CLI/feature. Production north star #2 (operability). When the captain splits a goal into sub-jobs, the plan (which sub-jobs, why, what order) is invisible — `roost tree` shows children but not the reasoning. An operator debugging a fleet cannot tell what the captain intended.
Done-when: captain dispatch records a structured plan on the parent job (additive field); `roost tree <root>` renders per-child one-line reasoning; older plan-less jobs render gracefully; tests for plan recording + rendering; pytest green.

### R34. Mobile one-shot publish parity — `done` *(2026-06-06, PR #46)* `self-promoted` `feature`
Surface: mobile/feature. Completes the half-landed R7 feature (north star #3: complete surfaces). Server has `POST /publish?name=` (raw body) since PR #15; mobile API.md §6 still documents only the two-step blob flow; neither client can use the one-shot path.
Done-when: API.md §6 documents the one-shot path; `record_fixtures.py` records it (regen is values-only additive); iOS + Android decode layers + Linux-runnable tests; pytest green + both mobile harnesses green (per evidence table).

### R35. `/metrics` endpoint (Prometheus text format, no new deps) — `done` *(2026-06-06, PR #44)* `self-promoted` `feature`
Surface: backend/feature. North star #2: a production fleet needs scrapeable metrics; today the only visibility is CLI polling. Hand-rolled Prometheus text exposition (no client library — dependency-light rule).
Done-when: `GET /metrics` (admin auth) returns valid Prometheus text with ≥8 meaningful series (jobs by state, queue depth, workers online/total, lease expirations, schedule beats, blob count/bytes); values read from DB so they survive CP restarts; README ops section documents it; format + seeded-value tests; pytest green.

### R36. Published-site listing pagination — `done` *(2026-06-06, PR #47)* `self-promoted` `feature`
Surface: backend/robustness. North star #2 (bounded resources): `/publish` list is unbounded (server.py ~2150) — a fleet that publishes for months returns megabytes per list call.
Done-when: list accepts `limit`/`offset` with a sane default cap; response shape stays additive (existing clients keep working); CLI passes the flags through; boundary tests; mobile contract unaffected or additively extended; pytest green.

### R37. Mobile push notifications (DESIGN.md v1.1 — ntfy/UnifiedPush) — `done` *(2026-06-06, PR #48)* `self-promoted` `feature`
Surface: backend/mobile/feature. North star #3 + the top user-facing ask in the design doc. Read `mobile-app/DESIGN.md` v1.1 first and implement its choice (ntfy-style webhook push). Server side: CP config gains an optional notify endpoint/topic; terminal job events (succeeded/failed/cancelled) POST a notification via httpx; notify failure NEVER affects job state.
Done-when: per DESIGN.md v1.1 — opt-in config documented; stubbed-endpoint tests cover success, failure-isolation, and payload shape; pytest green. Client subscription wiring claimed only as far as Linux-testable (evidence table caps).

### R38. Interactive follow-up to running agent jobs (DESIGN.md §3.2) — `done` *(2026-06-07, PR #49)* `self-promoted` `feature`
Surface: backend/worker/feature. North star #3, v2 design. Read `mobile-app/DESIGN.md` §3.2 first. Where the design leaves choices open, the loop makes the call and documents the rationale (standing direction 2026-06-06). Expected shape: `POST /jobs/{id}/input` queues a message; worker delivers to the running agent job; clients can steer mid-flight.
Done-when: input verb exists end-to-end for at least the `claude` kind on the CLI surface (`roost send <id> <text>` or similar); delivery semantics documented (queued vs dropped when no consumer); tests with a stubbed agent process; pytest green + live smoke (behavior change).

### R39. `roost backup` — online SQLite backup + documented restore — `done` *(2026-06-07, PR #52)* `self-promoted` `feature`
Surface: backend/CLI/feature. Production north star #2 (recoverable state). The CP's SQLite DB is the whole fleet state; today there is no safe way to back it up while the CP runs (file copy under WAL is corruption-prone) and DEPLOY.md has no restore procedure (pre-existing Proposed item).
Done-when: `roost backup <dest.db>` (admin) performs an online backup via the sqlite3 backup API against the live CP (decide the seam: CP endpoint streaming a consistent snapshot vs CLI attaching directly when local — pick what fits deployment reality, document the choice); DEPLOY.md gains backup + restore + verify procedure; tests: backup of a live busy DB is consistent (readable, row counts match a quiesced copy), restore round-trip; pytest green.

### R40. Mobile schedule parity — `done` *(2026-06-07, PR #51)* `self-promoted` `feature`
Surface: mobile/feature. North star #3 (complete surfaces). The schedule verb landed server-side with R8 (PR #16); mobile API.md has no schedule surface and neither client can list/create/toggle schedules. Mirror the proven R34 pattern.
Done-when: API.md documents the schedule endpoints (list/create/enable/disable/delete per server.py's /schedules routes); record_fixtures.py records them (values-only additive regen; drift guard green); iOS + Android decode layers + Linux-runnable tests; pytest + both mobile harnesses green.

### R41. Capability detection: distinguish "no GPU" from "GPU detection failed" — `done` *(2026-06-07, PR #50)* `self-promoted` `feature`
Surface: worker/operability. North star #2. Pre-existing Proposed item: when GPU probing errors (driver hiccup, nvidia-smi missing vs failing), the worker silently advertises no GPU — placement then quietly routes GPU jobs elsewhere and operators can't tell a bare node from a broken one.
Done-when: detection failure is distinguishable from absence in the worker's advertised capabilities and/or logs (additive — e.g. `gpu_detection: "failed"` capability or a loud structured log + worker event); matcher behavior for GPU constraints unchanged for both cases (failed ≠ schedulable); tests for probe-success/absence/failure paths; pytest green.

### R42. Docs truth pass over the feature wave (PRs #40-#52) — `done` *(2026-06-07, PR #55)* `self-promoted`
Surface: docs. A3 drift sweep — eight features landed since the last sweep (metrics, captain plan, one-shot publish mobile, pagination, push notify, interactive input, backup, mobile schedules, GPU detection-failed). README's feature/verb tables, INTEGRATIONS.md's verb table, and the quickstart/oversee skills likely don't mention `roost send`, `roost backup`, `/metrics`, or `--notify-url`. Includes the R32 leftover: add `roost --version` (tiny, fits a docs/CLI-surface truth pass).
Done-when: every user-facing doc surface (README, INTEGRATIONS.md, DEPLOY.md cross-refs, .claude/skills/roost-*) accurately reflects the new verbs/flags — each claim truth-checked against code; `roost --version` exists and reports `__version__`; docs-drift ratchet back to 0; pytest green.

### R43. Worker credential refresh racing lease TTL — `invalid` *(refuted 2026-06-07; regression guards landed PR #54)* `self-promoted`
Surface: backend/robustness. Old survey hypothesis (Proposed since pre-loop): the worker's credential refresh can race the lease lifecycle — a refresh mid-lease may invalidate the credential the CP knows, or a lease renewal may race a rotating token. INVESTIGATE FIRST per A1 rules: trace the actual refresh + lease paths in worker.py/server.py; a fix requires a failing repro. If the race is not real on current code, clear the item honestly (that is a valid outcome — journal it `invalid`).
Done-when: either (a) reproducing test written and FAILS on master, fix makes it pass, pytest green + live smoke; or (b) the hypothesis is refuted with cited code paths and the item closes `invalid`.

### R44. Cost estimation: configurable per-model pricing — `done` *(2026-06-07, PR #53)* `self-promoted` `feature`
Surface: backend/feature. North star #2 (operability). Cost estimates use a fixed rate (find it — grep worker.py/captain.py/server.py for the pricing constant); real fleets run mixed models and the estimate is wrong for most of them.
Done-when: per-model pricing configurable (worker policy or CP config — pick the seam that matches where the estimate is computed; document the choice); sane defaults preserved (zero-config behavior unchanged); estimate uses the job's actual model; tests for default + override + unknown-model fallback; pytest green.

### R45. Fix flaky backup temp-file test (xdist race) — `done` *(2026-06-07, PR #56)* `self-promoted`
Surface: tests/robustness. A4 debt from R42's run: `test_backup_leaves_no_temp_file_behind` globs the SHARED system temp dir and races the adjacent backup test under parallel execution — failed once mid-run, passes in isolation. A flaky suite undermines every future judge gate.
Done-when: the test isolates its temp observation (dedicated tmp_path-scoped dir for backup temps, or filter by this test's own marker); deterministic under repetition (`pytest tests/test_server.py -k backup -p no:randomly --count`-style or a tight loop) and under parallel runs; pytest green.

### R46. MCP tool docstrings: usage examples — `done` *(2026-06-07, PR #57)* `self-promoted` `feature`
Surface: MCP/DX. Pre-existing Proposed item, promoted: the captain agent READS these docstrings to decide how to call tools — examples directly improve every captain run's tool-use accuracy. Add a short worked example to each of the 16 tools' descriptions in roost/mcp.py (inputs + what comes back), truth-checked against the real schemas/server behavior.
Done-when: every TOOLS entry carries an accurate example; examples truth-checked (judge re-checks against schemas + server routes); INTEGRATIONS.md tool table untouched or consistent; pytest green.

### R47. Stuck-job detection masked by activity-text substring — `done` *(2026-06-07, PR #59)* `self-promoted`
Surface: backend/correctness. A1 hunt #4 (PR #58). `_job_phase` (server.py:585) infers the verify/self-heal phase from a bare substring ("verifying"/"self-healing") of the job's own activity text — short-circuiting `_job_health` before the stuck check, so a genuinely-stuck job whose activity legitimately contains that word is never flagged. The worker emits exact markers ("🔎 verifying result" / "🔧 self-healing (attempt N)").
Repro: `LOOP/repro-a1-hunt4.py::test_stuck_job_with_verifying_in_activity_is_still_flagged_stuck` — FAILS on master.
Done-when: phase detection anchored to the exact worker markers; repro passes (promote into tests/); pytest green.

### R48. `target`-pinned jobs never flagged unplaceable — `done` *(2026-06-07, PR #61)* `self-promoted`
Surface: backend/correctness. A1 hunt #4 (PR #58). `_annotate_liveness` (server.py:463-493) computes `capable_workers` from `requires` only, ignoring the hard `target` pin that `_try_assign_one` enforces — a job pinned to a non-existent/offline worker looks placeable forever and the overseer never sees it.
Repro: `LOOP/repro-a1-hunt4.py::test_job_pinned_to_nonexistent_target_is_unplaceable` — FAILS on master.
Done-when: liveness annotation honors the target pin (parity with assignment); repro passes (promote into tests/); LOOP/repro-a1-hunt4.py deleted once both its tests live in the suite; pytest green.

### R49. Narration re-render `min_interval` configurable — `done` *(2026-06-07, PR #60)* `self-promoted` `feature`
Surface: backend/feature. Pre-existing Proposed item: the watcher's narration re-render interval is a fixed constant; busy fleets may want it slower (cost) and demo fleets faster (snappiness).
Done-when: interval configurable via the same config style as ROOST_NARRATE (env var or config sibling — match the existing seam); default preserves today's value exactly; bounds-checked (sane floor); test for default + override; pytest green.

### R50. iOS publish UI wiring (Mac-node verified) — `done` *(2026-06-07, PR #63 — simulator-verified, screenshot blob c41555f048c8)* `self-promoted` `feature`
Surface: mobile/iOS/feature. North star #3. The decode layers + contract landed with R6/R34; the iOS app still has no publish screen — pick-bundle → upload (or one-shot) → publish → share-link. Evidence table mac-path applies: build + test + simctl screenshot via a Roost job on the Mac node (mac-mini-m4, proven in I0); if the Mac is unreachable, cap claims at "compiles, needs-mac-verify" and mark blocked honestly.
Done-when: publish screen wired into the iOS app using the existing RoostKit calls (one-shot preferred); Linux-runnable logic tests for any new view-model; Mac node run: xcodebuild build+test green + simctl screenshot of the publish screen returned as a blob artifact and linked in the PR; pytest green (server untouched or additive only).

### R51. verify.py e2e coverage — `done` *(2026-06-07, PR #64 — verify.py 87→100%, worker 63→72%)* `self-promoted`
Surface: tests. A2: the trust loop is the product's core promise; verify.py sits at 87% with the verdict path under-exercised end-to-end (hunt #4 cleared parse_verdict unit-level; e2e through run_job's verify phase with a stubbed verifier process is the gap).
Done-when: e2e tests drive run_job's verify/self-heal phase with stubbed subprocess(es): verify-pass → succeeded; verify-fail → self-heal attempt(s) → outcome; verifier crash/timeout → documented degradation; budget-exhausted skip path; verify.py + the worker verify-phase branches measurably up, no module down; pytest green.

### R52. Lease-expiry grace analog — investigate, repro-or-clear — `done (cleared)` *(2026-06-07, PR #62 — fast-retry semantics documented + regression-locked)* `self-promoted`
Surface: backend/design-question. R19 restarted the placement-grace window for declines only and filed the analog question: should a SWEEPER requeue (lease expiry — a real failure) also restart it? Investigate the actual competitive-placement behavior after a lease-expiry requeue on current code; decide with evidence (R43 pattern): if the current behavior produces a concrete bad outcome (e.g. anti-starvation override permanently armed after one expiry, starving competitive placement), repro it and fix; if the current behavior is defensible, document the rationale in code and close `invalid` with the analysis.
Done-when: either repro+fix+tests+pytest green, or a judge-verified refutation documented in a code comment at the requeue site; the Proposed question closes either way.

### R53. Android publish UI parity — `done` *(2026-06-07, PR #65 — UI render honestly capped, no emulator in fleet)* `self-promoted` `feature`
Surface: mobile/Android/feature. North star #3. R50 landed the iOS publish screen (PR #63); Android has the decode layers (R34) but no screen. Mirror the iOS UX in the Compose app: pick tar.gz (SAF document picker) → name with slug preview → one-shot publish → show site URL with share intent.
Done-when: Compose screen wired following the app's existing screen/viewmodel patterns; ALL slug/intent/state logic in a Linux-testable layer (kotlinc+JUnitCore harness) with tests mirroring iOS PublishTests; UI-render claims capped honestly per the evidence table (no Android emulator in the fleet — say so in the PR); pytest green (server untouched).

### R54. Coverage ratchet re-measure + cli.py lift — `done` *(2026-06-07, PR #66 — TOTAL 63→71%, cli.py branch 30→50.4%)* `self-promoted`
Surface: tests/ratchet. A5+A2: the branch-coverage ratchet baseline is stale (63% TOTAL at 482 tests, 2026-06-07 early; suite now 664). First re-measure and record the new TOTAL; then lift the weakest module — cli.py was 36% branch at last measure (process-spawning paths were excused, but command surfaces like send/backup/schedule/history/prune-workers have grown since R16 with uneven test reach).
Done-when: fresh `coverage run --branch -m pytest` TOTAL recorded (judge re-measures); targeted tests raise cli.py branch coverage measurably (≥5 points) with real assertions (R16 style: runner + stubbed HTTP, no processes); no module down; pytest green.

### R55. Push-notification client wiring — Linux-testable slice — `done` *(2026-06-07, PR #67 — device-only transport capped, documented in DESIGN.md §8a)* `self-promoted` `feature`
Surface: mobile/feature. R37 landed the CP webhook (ntfy-compatible); the deferred client side has a real codeable slice even without devices: (iOS) a notification-settings screen storing the ntfy topic/URL derived from the CP config + deep-link plumbing from a notification payload to the job detail screen; (Android) the same settings + an UnifiedPush-style receiver whose payload→navigation mapping is pure logic. Read mobile-app/DESIGN.md v1.1's client section first and implement what it actually specifies.
Done-when: settings + payload-routing logic landed on both clients with the logic layer Linux-tested (payload parse → expected deep-link route table); device-only pieces (actual push registration/display) explicitly capped in the PR per the evidence table; API.md/DESIGN.md updated only if the implemented slice needs it (additive); pytest + both mobile harnesses green.

### R56. A6 product gap survey #2 — `done` *(2026-06-07 — 3 promotables found, 2 Proposed, 5 verified-complete; no code)* `self-promoted`
Surface: survey. The product surface roughly doubled this session (metrics, backup, send/input, mobile schedules, publish UIs, push, pagination, captain plans, cost pricing). Re-run the A6 user-lens survey over the grown surface: README/INTEGRATIONS/API.md/DESIGN.md vs code; CLI help vs docs; what would a daily operator or phone user now hit? Apply the four A6 gates per finding; output a judged slate for cycle #14 (promotables + Proposed notes). Survey #1 found the kind:auto schema hole within minutes — the surface has grown 10× since.
Done-when: every user-facing surface swept; each finding gated + judge-verified (re-checkable evidence); slate of ≤3 promotables + Proposed additions reported to the orchestrator; no code changes (survey only).

### R57. mcp.py + schema.py coverage lift — `done` *(2026-06-07, PR #69 — mcp 61→99%, schema 62→100%)* `self-promoted`
Surface: tests. A2: post-R54 the weakest modules are mcp.py 61% and schema.py 62%. mcp.py's untested reach: tool dispatch paths, error mapping, the R46 example-bearing tools' impl branches; schema.py: migration paths V1→V14 (synthetic old-version DBs, the R19/R38 migration pattern from tests).
Done-when: both modules' branch coverage strictly up (≥8 points each); migration tests cover every version step incl. idempotency; no module down; real assertions (judge mutation-probes); pytest green (707 base).

### R58. Config/deploy truth pass for the new env vars — `done` *(2026-06-07, PR #68 — consolidated CP config reference created)* `self-promoted`
Surface: docs/deploy. This session added ROOST_PRICING (R44), ROOST_NARRATE_INTERVAL (R49), ROOST_NOTIFY_URL (R37) and the backup/metrics admin endpoints. Verify each is (a) documented where operators look (DEPLOY.md's config reference, README), (b) passed through docker/stack.yml like ROOST_PUBLISH_DOMAIN is, (c) consistent with the code's actual parsing (truth-check defaults/fallbacks). R37 added its own passthrough — verify; R44/R49 likely did not.
Done-when: every operator-facing env var documented + docker-passthrough'd + truth-checked; gaps fixed additively; pytest green.

### R59. Surface input states on aggregate views (derived + tree) — `done` *(2026-06-07, PR #71 — batched, only-when-nonzero, no fixture regen needed)* `self-promoted` `feature`
Surface: backend/CLI/feature. A6 survey #2 finding 1+3 (judge-approved, merged — same helper, same contract). `_derive_run` (server.py:777-806) — consumed by /panel, roost history, mac-app, both mobile dashboards — has no `inputs` key; the tree endpoint never calls `_input_counts` per node, so `tree --health` (cli.py:1902-1913) can't show what `roost status` already does. An operator can't see dropped/queued input without per-job drilling.
Done-when: `_derive_run` includes `inputs: {queued, delivered, dropped}` present only when any count > 0 (mirroring GET /jobs/{id}); tree endpoint annotates per-node counts; `tree --health` prints `inputs N/N/N` when nonzero; API.md §2 run row additively documents the optional field (fixture regen values-only if needed); tests for both surfaces; pytest green.

### R60. `roost_publish` MCP tool — the agent front door ships sites — `done` *(2026-06-07, PR #70 — 17th tool; one-shot + 422 fallback + blob_id parity with CLI)* `self-promoted` `feature`
Surface: MCP/feature. A6 survey #2 finding 2 (judge-approved). 16 tools, none publishes; the server allows scoped agent tokens to publish (INTEGRATIONS.md:168-170) and CLI + both mobile apps expose it — only the captain can't. "Build a site and publish it" via roost_do dead-ends.
Done-when: `roost_publish` (name + bundle path or blob_id, mirroring the CLI one-shot/two-step) in TOOLS + TOOL_IMPL with an R46-style worked example; INTEGRATIONS.md row; tools/call test returns a Site; pytest green.

### R61. Mobile schedules UI (both platforms) — `done` *(2026-06-07, PR #72 — every-grammar cross-contract pinned; render claims capped)* `self-promoted` `feature`
Surface: mobile/feature. A6 survey #2 PROPOSED→promoted on Tier-B loop judgment: the interaction design is resolved BY PRECEDENT — the dashboard-overflow→sheet pattern established by publish (R50/R53) and notifications (R55). Both ApiClients already implement all four schedule calls (iOS ApiClient.swift:183-211, Android ApiClient.kt:174-209) — unreachable code today. API.md §7 frames phone scheduling as the point.
Done-when: Schedules sheet on both platforms (list + create with every-interval + enable/disable + delete), following the established sheet/viewmodel patterns; pure logic (interval parse/format, state machine) Linux-tested on both harnesses; UI-render claims capped per evidence table; pytest + both harnesses green.

### R62. Mac-app verb expansion — `done` *(2026-06-07, PR #74 — publish/schedules-toggle/send; create deferred as judge-sanctioned slice)* `self-promoted` `feature`
Surface: mac-app/feature. Tier-B loop judgment on the survey-#2 Proposed note: north star #3 says complete surfaces, and the menu bar already submits/cancels — the missing verbs follow. Scope to the menu-bar-natural ones: publish (one-shot, file picker), schedules (list/toggle), send (steer a selected running job). Backup/history stay CLI (operator tasks, not menu-bar). RoostClient.swift + AppModel patterns established; mac-app has a Linux-runnable test suite (I1: swift test 30/30 on /tmp/swift-toolchain).
Done-when: the three verbs reachable from the menu bar following existing section patterns; client calls + pure logic Linux-tested (mac-app swift test); UI-render claims capped per evidence table unless a Mac-node build+screenshot is run (bonus); pytest green (server untouched).

### R63. Drift sweep #3 — PRs #65-#72 — `done` *(2026-06-07, PR #73 — 3 drifts fixed, 6 surfaces verified clean)* `self-promoted`
Surface: docs. A3: eight PRs landed since R42's sweep (Android publish UI, cli coverage, push slice, survey, mcp/schema tests, config reference, input visibility, roost_publish, mobile schedules). Most were doc-disciplined in-PR; sweep for cross-surface misses: README feature claims, INTEGRATIONS verb matrix vs the 17-tool reality, API.md §7 schedules now having UI, DESIGN.md §8a accuracy, skills (oversee/quickstart) vs new capabilities.
Done-when: every confirmed drift fixed additively with claims truth-checked; docs-drift ratchet stays 0; pytest green.

### R64. A1 hunt #5 — server event/lifecycle paths, concurrency lens — `done` *(2026-06-07, PR #75 — 1 confirmed, 6 cleared)* `self-promoted`
Surface: hunt. Deepening per protocol: all core areas hunted once; re-hunt the server's event-ingestion/lifecycle seams (worker event POST → state transitions, finalize, cancel/tree-cancel, sweeper interactions with the new input queue + notify hook) with a CONCURRENCY lens — interleavings the per-area hunts didn't try (e.g. cancel racing finalize, input-ack racing terminal, two workers' stale events crossing). Reproducing test required per finding; cleared hypotheses are valid output.
Done-when: repros (LOOP/repro-a1-hunt5.py) for confirmed bugs merged after judge verification, or an honest all-clear report; pytest green.

### R65. Orphaned interactive input on terminal transitions — `done` *(2026-06-07, PR #76 — race 10/10→12/12; requeue-survival + cascade-scoping pinned)* `self-promoted`
Surface: backend/correctness. A1 hunt #5 (PR #75). No server-side path reconciles `job_inputs` when a job goes terminal: `_apply_event` succeeded/failed (server.py:1546-1560), `_cancel_job` (:879-941), `_finalize_job` (:944-982), and `_sweep` lease-fail (:1887-1891) all leave `queued` rows untouched, and `_pending_input_job_ids` (:1730) only offers delivery for assigned/running jobs — so a queued input is stranded forever, violating R38's "every input ends delivered or dropped" contract (README:364).
Repro: `LOOP/repro-a1-hunt5.py` — 4 tests FAIL on master incl. a genuine cancel-race (orphans ~40%/trial, fails 10/10).
Done-when: each terminal site drops still-queued inputs (`queued`→`dropped` with a reason) inside its existing BEGIN IMMEDIATE; the `_cancel_job` cascade scopes the drop to jobs transitioned in THIS call (not the whole BFS set — children already terminal must be untouched); the lease-expiry REQUEUE path deliberately left alone (job still active); all 4 repros pass, promoted into tests/; repro file deleted; pytest green.

### R66. A1 hunt #6 — worker-side concurrency (this session's own changes) — `done` *(2026-06-07, PR #77 — 1 confirmed, 6 cleared)* `self-promoted`
Surface: hunt (protocol deepening #1). The worker was heavily modified this session (R25 try/finally rewrite, R31 relay-task lifecycle, R38 input fetch/deliver/ack loop, R26 OSError path) and has never been hunted under a concurrency lens. Fresh attack surface created by our own changes: heartbeat-reconcile racing input delivery; _reap_stale_attempt vs the new finally cleanup; input-ack racing job teardown; relay cancellation vs the input writer on the same stdin pipe; capacity slots under cancel storms.
Done-when: repros (LOOP/repro-a1-hunt6.py) for confirmed bugs merged after judge verification, or an honest all-clear (which counts as deepening-clear #1 toward the protocol's long-idle trigger); pytest green.

### R67. Stale done-callback evicts a re-leased job's new task — `done` *(2026-06-07, PR #78 — guard-only, drain rejected with rationale)* `self-promoted`
Surface: backend/correctness. A1 hunt #6 (PR #77). `_spawn_job`'s `_done` callback (worker.py:1688) pops `_job_tasks[_jid]` unconditionally; `_reap_stale_attempt` (:1669) early-returns on `old.done()` WITHOUT awaiting, so the old task's still-queued done-callback fires after the NEW task is installed and evicts it. Harm: capacity gate undercounts → over-lease; shutdown can't cancel the orphan; a later re-lease can't find the running task → two concurrent attempts → double execution.
Repro: `LOOP/repro-a1-hunt6.py` — FAILS ×3 on master; one-line identity guard (`if self._job_tasks.get(_jid) is t:`) proven to fix (non-tautology cycle md5-verified by the hunter).
Done-when: the identity guard (or equivalent — consider also draining the old task's callback in _reap_stale_attempt; pick the minimal correct form and justify) lands; repro promoted into tests/; repro file deleted; pytest green.

### R68. captain.py coverage lift (deepening #2) — `done` *(2026-06-07, PR #79 — 75→100% branch; zero latent bugs)* `self-promoted`
Surface: tests. A2 deepening: captain.py at 75% branch — the weakest remaining module. The dispatch path (goal → plan → roost_submit children with reasons → collect/wait → synthesize) has unit gaps; R51's verify.py e2e precedent showed test-writing on these seams surfaces latent bugs.
Done-when: captain.py branch coverage strictly up ≥10 points with real-behavior assertions (stubbed MCP/HTTP seams, no live LLM); any latent bug found gets a repro note for the next cycle (do not fix in this PR — scope discipline); no module down; pytest green (802 base).

### R69. A1 hunt #7 — mobile-contract robustness lens (deepening #3, long-idle gate) — `done` *(2026-06-07, PR #80 — 2 confirmed, 6 cleared; gate NOT passed, counter reset)* `self-promoted`
Surface: hunt. Final queued deepening: the server↔mobile contract under adversarial/degenerate payloads — the decode layers have golden-fixture tests (happy path) but the SERVER side of the contract has never been hunted for responses that would break the documented additive-only guarantees (e.g. fields that can become null where clients assume non-null; SSE event vocabulary under unusual job lifecycles; pagination headers under edge counts). Server-side findings only (client decoders are pure + tested); reproducing test required per finding; an all-clear = deepening-clear #2 → LONG-IDLE.
Done-when: repros merged after judge verification, or an honest all-clear report triggering long-idle.

### R70. Dashboard 500s on non-string goal/result fields — `done` *(2026-06-07, PR #81 — read-time coercion + non-breaking submit typing)* `self-promoted`
Surface: backend/robustness. A1 hunt #7 (PR #80). One root cause, two surfaces: `_goal_text` (server.py:615) does `" ".join(...)` and `_derive_run` (:800) slices `result.output[:240]` without proving str. (a) `JobSubmit.command` is `Optional[Any]` (:2398) — `POST /jobs` with `command: [1,2,3]` is accepted, then /derived 500s; one poisoned job breaks the dashboard for EVERY job (mobile polls it every 2s). (b) a non-conformant worker's `result: {"output": {...}}` does the same via the event API.
Repro: `LOOP/repro-a1-hunt7.py` — 2 tests FAIL ×3 on master (assert 500 == 200).
Done-when: serializers never raise on any accepted payload (defensive str coercion at both spots — proven by the hunter); ADDITIONALLY tighten submit-side typing only as far as is provably non-breaking (investigate what command shapes are legitimately accepted/used today — a plain str-or-list[str] union validated at submit beats Any, but do NOT break a legitimate caller; document the decision); repros promoted into tests/; repro file deleted; pytest green (812 base).

### R71. A1 hunt #8 — docker executor lens — `done` *(2026-06-07, PR #82 — 1 confirmed, 7 cleared)* `self-promoted`
Surface: hunt (deepening). The docker executor has never been hunted as a unit (R1 touched argv hardening only): container lifecycle (create/wait/kill/timeout interplay), GPU flag plumbing, mount/workdir semantics, exit-code propagation, log relay from container stdio, teardown when docker itself is slow/wedged. SKIP security-surface findings (mount escapes, privilege — security session); this lens is correctness/robustness for legitimate specs.
Done-when: repros (LOOP/repro-a1-hunt8.py) merged after judge verification, or an honest all-clear (= deepening-clear #1 toward long-idle).

### R72. Unbounded docker-kill wait hangs teardown on a wedged daemon — `done` *(2026-06-07, PR #83 — 4s/CLI bound, loud operator message)* `self-promoted`
Surface: backend/robustness. A1 hunt #8 (PR #82). `_kill_active_job` (worker.py ~1226-1234) awaits `docker kill`/`docker rm -f` with a bare `await k.wait()` — the ONLY subprocess wait in worker.py without a timeout. A wedged dockerd (real GPU-box failure mode) hangs the wallclock-timeout path, the server-cancel path, AND graceful `_shutdown_jobs`; the hang sits inside run_job's try before the R25 finally, so no terminal event posts and `_running` leaks permanently.
Repro: `LOOP/repro-a1-hunt8.py` — 2 tests (unit + run_job integration) FAIL ×3 on master (~20s real hang each).
Done-when: both teardown waits bounded (`asyncio.wait_for` + kill the stuck CLI process on expiry — match the proven fix shape; pick the timeout consistent with the docker-info probe's 20s and justify); the failure is loud (log line: daemon unresponsive, container may still be running — operator must intervene); repros promoted into tests/, repro file deleted; pytest green (820 base).

---

## User-testing sweep 2026-06-07 (human-directed)

Four parallel agents real-user-tested every surface against the live fleet:
backend/CLI/panel (Playwright, desktop + iPhone 14 + Pixel 7), Android (Pixel_8 AVD
on mac-mini-m4, adb-driven), iOS (iPhone 17 Pro sim), mac-app (built + run on the
Mac node). Evidence pack: `/workspace/yang/agent_fleet/user-testing/` (SUMMARY.md +
per-surface report.md + 42 inspected screenshots). The items below are human-promoted;
bug items still require a failing repro/test in-PR per A1 before the fix lands (UI
items: evidence-table artifact instead). Fleet-ops findings (live CP container rebuild,
oracle creds, stale Mac clone) are NOT loop items — parked for the human in Proposed.

### R73. mac-app master does not compile (Swift 6.2 ternary type mismatch) — `done` *(2026-06-07, PR #85 — Mac-node verified; Linux gate-hole documented)*
Surface: mac-app/correctness. User-test BLOCKER. `mac-app` `PublishView.swift:181`
`.foregroundStyle(PublishSlug.isValid(pub.name) ? .secondary : .red)` mixes
`HierarchicalShapeStyle` and `Color`; Swift 6.2 rejects it — nobody can build current
master (proven one-line fix: `? Color.secondary : Color.red`). The Linux RoostKit gate
did not catch it because the app target isn't compiled on Linux.
Done-when: master builds on the Mac node (evidence-table mac path: build + test +
artifact); additionally close the gate-hole honestly — either the Linux gate type-checks
the app target too, or mac-app-touching PRs are documented as requiring the Mac-node
build (pick what's real, journal the choice); pytest green.

### R74. Android dashboard TopAppBar never renders — Publish/Notifications/Schedules unreachable — `done` *(2026-06-07, PR #86 — emulator-verified, 6 screenshots; root cause corrected: app-wide inset consumption, not per-screen layout)*
Surface: mobile/Android/correctness. User-test BLOCKER (user-testing/android/03,04).
`uiautomator` dump shows zero app-bar nodes — no title, no ⋮ overflow, the ONLY entry
point to Publish/Notifications/Schedules (DashboardScreen.kt:84-86, 326-336): a third of
the app is unreachable. `MainActivity.onCreate` calls `enableEdgeToEdge()` with no inset
handling; the dashboard's `topBar` slot collapses (content is `VerdictBar` +
`LazyColumn(fillMaxSize())`) while SessionScreen's same pattern renders (content uses
`weight(1f)`). Same root cause, milder: Session back-arrow crowds the status bar.
Done-when: TopAppBar (title + overflow) renders on the dashboard and all three sheets
open; insets fixed app-wide (Session back-arrow too); UI verified via the now-PROVEN
Android evidence path — Roost job on mac-mini-m4: AVD `Pixel_8` (exists), Homebrew
gradle 9.4.1 + `gradle wrapper` (JAR not committed) `assembleDebug`, `adb install` +
`adb shell input` drive + `adb exec-out screencap` artifact in the PR; pytest green
(server untouched).

### R75. Android offline staleness pill never fires (StateFlow dedupe freezes the clock) — `done` *(2026-06-07, PR #88 — hypothesis confirmed; real-outage screenshot proof)*
Surface: mobile/Android/correctness. User-test major (android/11 — 35s confirmed outage,
no pill). `DashboardViewModel.refreshOnce()` sets `_state.copy(error=…)` keeping the same
`derived` ref → consecutive failures produce `equals` states → `MutableStateFlow` dedupes
→ no recomposition → `val nowMs = System.currentTimeMillis()` in DashboardScreen stays
frozen at the first-failure instant → `ageSec > 10` never true. Exactly the DESIGN.md §2
case it exists for.
Done-when: staleness driven by a 1s ticker or an explicit last-success timestamp in state
(pick, justify); failing Linux-harness test first (simulated failed polls → pill state
asserted), fix makes it pass; emulator screenshot of the pill during an induced outage
per the R74 evidence path; pytest green.

### R76. Session follow-up composer missing on mobile (DESIGN §3.2 / API §4) — `done` *(2026-06-07, PR #89 — both platforms; iOS had the same gap; e2e delivered-to-stdin proven)*
Surface: mobile/feature. User-test major. Server + CLI landed with R38
(`POST /jobs/{id}/input`, `roost send`); Android SessionScreen.kt's bottom bar is
Cancel + Tree only — the headline "react fast: follow up" capability is absent. iOS's
session screen was tap-gated during testing (not live-verified) — audit it first;
implement the composer on whichever clients lack it.
Done-when: composer per DESIGN §3.2 on both platforms (text at minimum; voice only if
the design's slice is Linux-testable); send → input lands queued/delivered via the real
endpoint (fixtures additive if needed); pure logic Linux-tested on both harnesses;
Android emulator screenshot per R74 path, iOS per evidence-table mac path; pytest green.

### R77. `roost schedule --list` dumps a raw httpx traceback on non-2xx — `done` *(2026-06-07, PR #84 — audit fixed --rm and --enable/--disable too)*
Surface: CLI/correctness. User-test major (verbatim: `httpx.HTTPStatusError: Client error
'404 Not Found' for url 'http://127.0.0.1:8787/schedules'`). `cli.py:1564
_print_schedules` calls `r.raise_for_status()` bare; the create path handles 404 cleanly,
`--list` doesn't.
Done-when: failing test first (stubbed 404 + 500 → friendly one-line error, parity with
the create path); fix; audit the other schedule subverbs for the same bare pattern;
pytest green.

### R78. `roost publish` fails opaquely against older control planes — `done` *(2026-06-07, PR #87 — non-2xx fallback exc. auth, both-errors surfacing; live old-CP proven)*
Surface: CLI/robustness. User-test major. Against the deployed 0.1.0 CP the one-shot
raw-tar POST gets HTTP 500 (old server requires JSON `{blob_id}`) and the CLI's blob-flow
fallback (`cli.py:1524`) only triggers on 422 → user sees `publish failed: HTTP 500:
Internal Server Error` with no recourse. Redeploying the CP is ops; the CLI should still
degrade gracefully across versions.
Done-when: compat contract decided + documented (broaden the fallback to any non-2xx from
the one-shot attempt, or preflight the server version via healthz — pick, justify);
failing test first (stubbed old-CP responses); clear error text when no path works;
pytest green.

### R79. Job-id prefix lookup: `history` prints 8 chars, every lookup verb needs 12 — `done` *(2026-06-07, PR #92 — server-side ≥6-char prefix on read routes; cancel/send deliberately exact-id)*
Surface: backend/CLI/DX. User-test minor. `roost history` prints truncated ids
(cli.py:316) but `logs`/`status`/`tree` do exact `WHERE id = ?` — pasting an id straight
from history → terse "job not found".
Done-when: unambiguous-prefix lookup server-side (≥6 chars; ambiguous → 409 listing
candidates) OR history prints full ids — pick the better UX, justify; tests for
match/ambiguous/too-short; docs additively updated if the contract changes; pytest green.

### R80. Blob `name` accepts unbounded length — `done` *(2026-06-07, PR #90 — 512-char cap, single validate_name seam)*
Surface: backend/robustness. User-test minor: a 32,000-char `name` was accepted and
stored (HTTP 200). Every other fuzz case returned clean 4xx — this is the one hole.
Done-when: sane cap (match existing field-cap precedent in server.py) returning 422;
boundary tests at/over the cap; existing clients unaffected; pytest green.

### R81. mac-app Schedules pane: contradictory double error against a 404 CP — `done` *(2026-06-07, PR #91 — SchedulesListState decision seam; render proven headless)*
Surface: mac-app/UX. User-test minor (mac-app/mainwindow-schedules.png): against a CP
without `/schedules` the pane stacks a red "Not found: Not Found" banner ON TOP of the
"No schedules" empty state — error and empty-state contradict each other.
Done-when: 404 → a single clear "schedules not available on this control plane (older
server)" state, no contradiction; RoostKit logic Linux-tested; render verified via the
Mac-node path or capped honestly; pytest green (server untouched).

### R82. mac-app Transfers shows "expires 0s from now" for every staged blob — `done` *(2026-06-07, PR #93 — signed RelativeTime in RoostKit; timeAgo delegates)*
Surface: mac-app/correctness. User-test minor (mainwindow-transfers.png): `Format.timeAgo`
is past-tense only; the call site string-swaps "ago"→"from now" and any future timestamp
renders "0s from now" (hours of TTL remain).
Done-when: a real signed/future-relative formatter with Linux swift tests covering past +
future values; Transfers and any other future-time call sites migrated; pytest green.

### R83. iOS pairing to a dead host: silent un-cancellable ~30s spinner — `done` *(2026-06-07, PR #94 — 5s probe + caption + real cancel; measured 30s→~5s)*
Surface: mobile/iOS/UX. User-test minor (ios/07,08): `timeoutIntervalForRequest = 30` in
`AppState.makeClient`; no caption, no cancel until the (excellent) error finally appears.
Done-when: short healthz probe timeout (~5s, justify), a "Contacting <host>…" caption,
and a cancel affordance; RoostKit-layer logic Linux-tested; simulator screenshot per the
evidence-table mac path; pytest green.

### R84. iOS XCUITest smoke suite — close the tap-gap — `done` *(2026-06-07, PR #95 — runs HEADLESS on the fleet Mac; 4 flows flake-free ×2; .xctestrun env-injection gotcha documented)* `feature`
Surface: mobile/iOS/tests. User testing could not live-verify any tap-gated iOS screen
(New-session, Session, Tree, Notifications, Schedules, swipe actions, Unpair): no
XCUITest target exists, no idb, and the Mac worker has no AX/window-server session.
Android's adb-driveable testing is exactly where its blocker was found — parity matters.
Done-when: XCUITest target with smoke flows (launch-arg pairing → dashboard renders live
data → New-session sheet → open a Session → Notifications/Schedules sheets), runnable
headless via `xcodebuild test` on mac-mini-m4 (Roost job) with the result bundle +
screenshots as artifacts; documented in ios/README; flake-free twice consecutively;
pytest green.

### R85. Mobile session subtitle hardcodes "claude" for every job kind — `done` *(2026-06-07, PR #97 — additive `kind` on /derived rows via _job_kind; clients never guess)*
Surface: mobile/correctness. User-test minor (android/05,08): a `command` job renders
"… · claude · succeeded" (Android `sessionSubtitle` + DashboardScreen `subtitle`; audit
iOS for the same).
Done-when: subtitle reflects the job's actual kind on both platforms; Linux-harness tests
with command/claude/docker fixtures; pytest green.

### R86. Long raw shell-command goals make verdict bars unreadable (all surfaces) — `done` *(2026-06-07, PR #98 — additive goal_display, one summarizer, four surfaces + fallback)*
Surface: cross-surface/UX. User-test minor, observed independently on Android (03,10),
iOS rows, and the mac popover: `command` jobs put the full shell text where a glanceable
goal belongs.
Done-when: seam decided (server-side display summary for command kinds in
`_goal_text`/`_derive_run` — additive field preferred — vs per-client truncation rules),
justified, applied consistently to panel + mac-app + both mobiles; fixtures additive;
tests; pytest green.

### R87. Docs: `roost pair --url` ordering wrong in mobile READMEs — `done` *(2026-06-07, PR #96 — 1 real instance fixed; 7 other sites verified legitimately per-verb)*
Surface: docs. User-test confirmed drift: `mobile-app/README.md` and
`mobile-app/ios/README.md` show `roost pair --url http://<LAN>:8787`, but `--url` is a
global option — `pair` rejects it; correct form is `roost --url … pair`.
Done-when: both corrected (plus a grep for the same inverted pattern across docs/skills);
docs-drift ratchet stays 0; pytest green.

## Replenishment 2026-06-07 evening — A1 hunt #9 + A3 drift sweep #4 over PRs #84–#98

Hunt repro file: `/workspace/yang/loop-wt-hunt/LOOP/repro-a1-hunt9.py` (12 failing repros, 4 cleared; uncommitted — implementers copy it in). All three findings judge-confirmed (sonnet) with failing repros on master 141e03b.

### R88. `/derived` 500s on a truthy non-dict spec row — `_goal_text` + `_job_kind` unguarded — `done` *(2026-06-07, PR #99)* `self-promoted`
Surface: backend/robustness. A1 hunt #9. `server.py:689` (`_goal_text`) and `:721` (`_job_kind`, new in R85) use `spec = job.get("spec") or {}` — `or {}` only replaces FALSY specs; a truthy non-dict spec (JSON string/array/number from a legacy/drifted at-rest row via `_row_to_job:183`) survives and `spec.get(...)` raises AttributeError → one bad row 500s `GET /derived`, which every client polls every 2s (the R70 failure mode, two new co-offenders). Properly guarded siblings: `_goal_display:762`, `_job_health:869`, `_annotate_liveness:583`.
Repro: hunt9 file — 7 tests incl. `test_repro_derived_500s_on_one_bad_spec_row`, FAIL on master.
Done-when: both functions coerce non-dict spec → `{}` (isinstance pattern, parity with siblings); `/derived` returns 200 with a bad-spec row present; repros promoted into tests/; pytest green.

### R89. `_goal_display` blanks a non-empty goal made entirely of strippable prefixes — `done` *(2026-06-07, PR #100 — merged after R88, linear)* `self-promoted`
Surface: backend/correctness. A1 hunt #9. `server.py:749-778` (R86): for `cd ~/x && `, `cd /a && cd /b && `, `A=1 B=2 `, `A=1 && B=2 && ` the peel loop strips the entire string → `goal_display: ""` while `goal` is non-empty — violating R86's own "never return empty when goal is non-empty" contract; the verdict bar renders blank. Real setup-only/copy-paste fleet shapes.
Repro: hunt9 file — 4 `test_repro_goal_display_blank_for_nonempty_goal` cases, FAIL on master.
Done-when: when the peel loop empties the string, fall back to the (72-char-truncated) `_goal_text` instead of `""`; repros promoted; existing R86 `test_goal_display_*` green; pytest green. COUPLING: merge after R88 (the fallback calls `_goal_text`, which R88 hardens) — rebase + re-verify.

### R90. `roost publish` loses the one-shot error when the fallback raises a transport exception — `done` *(2026-06-07, PR #101)* `self-promoted`

### R91. README test-count drift — fix structurally — `done` *(2026-06-07, PR #103 — count-free phrasing; ratchet → 0)* `self-promoted`
Surface: docs. Drift sweep #4 F1 (Tier A judged), promoted this cycle. `README.md:474` says `# 792 tests`; the suite is 884 and the number drifts with EVERY PR — the exact-count claim is the structural problem.
Done-when: the line no longer states a hardcoded count (e.g. `# full suite` or a phrasing the judge confirms can't drift); any OTHER hardcoded suite-count claims found by grep get the same treatment; docs-drift ratchet back to 0; pytest green.

### R92. macOS CI job has never compiled the app — fix the toolchain pin — `done` *(2026-06-07, PR #102 — macos-15 runner; first-ever green compile+test run; branch-protection rec'd to human)* `self-promoted`
Surface: CI/mac-app. A4 journaled debt (R73 notes), promoted: `mac-app/Package.swift` pins `swift-tools-version:5.10`, but SwiftTerm 1.13.0 pulls swift-argument-parser 1.8.2 requiring tools 6.0 — the macos-14 runner's Swift 5.10 dies at dependency RESOLUTION, so the `App build + tests (macOS)` job is red on every mac-app PR and catches nothing (it could not have caught R73's compile break).
Done-when: the workflow's macOS job actually compiles RoostMac and runs swift test, GREEN ON THIS PR's own CI run (that run is the proof — judge re-checks it); seam chosen deliberately (bump tools-version — the Mac node's 6.2.3 builds fine — and/or newer Xcode/runner image; justify); local Mac-node build still green; mac-app/README CI claims updated if behavior changed; pytest green. Branch-protection (required checks) is a GitHub-admin action — note it for the human, out of scope.

### R93. mac-app Publish + Transfers panes surface load errors — `done` *(2026-06-07, PR #104 — per-pane seams, RoostKit 115; transport-error state render-proven)* `self-promoted` `feature`
Surface: mac-app/UX. A4 journaled finding (R81 notes), promoted: `(try? client.sites()) ?? sites` and `try? refreshStaged()` swallow load failures entirely — against a CP missing those endpoints the panes look empty with zero feedback (the inverse of the fixed R81 double-stack). Mirror the proven `SchedulesListState` decision-seam pattern (PR #91).
Done-when: each pane gets a RoostKit decision seam (loading/list/empty/error-or-unavailable, mutually exclusive) with Linux swift tests; views become dumb switches; 404-on-old-CP renders a single clear "not available" state, transport errors a retryable error state; Mac-node build + swift test green; render proven via the headless-harness pattern or capped honestly; pytest green (server untouched).
Surface: CLI/robustness. A1 hunt #9. `cli.py:1564-1573` (R78): on one-shot ≥400, `oneshot_err` is saved, but only an HTTP-STATUS fallback failure wraps it — a transport exception (`httpx.ConnectError`, connection refused mid-fallback) from the blob POST propagates raw, so the user loses the original diagnosis, contradicting the R78 docstring promise (cli.py:1514-1515).
Repro: hunt9 file — `test_repro_publish_loses_oneshot_err_on_fallback_connect_error`, FAIL on master.
Done-when: the fallback blob POST and second `/publish` POST wrapped so transport exceptions still surface `oneshot_err` in a ClickException; repro promoted; pytest green.

## Replenishment 2026-06-07 night — A2 coverage re-measure + A6 survey #3

Fresh measure: **80% TOTAL branch @ 884 tests** (judge re-measured); session-added code fully covered; weakest: cli.py 62%, worker.py 72%. Six Tier-A items judged across the two slates; three promoted now, three queued (next slot): A6-2 iOS README count (STAMPED 92/92 vs claimed 58/58), A6-3 panel 401 wording (panel.html:243,412), A2-3 worker process-safety branches (_kill_active_job/_kill_aux_procs, worker.py:1205-1310).

### R94. `roost history` ignores `goal_display` — the last raw-shell-text surface — `done` *(2026-06-07, PR #105)* `self-promoted`
Surface: backend/CLI/consistency. A6 survey #3 rank 1. R86 wired the glanceable summary into panel/mac/iOS/Android and `server.py:985`'s docstring names `roost history` as a consumer — but `_history_row` (cli.py:344) and `_recent_successes` (cli.py:375) read raw `run.get("goal")`. The documented "what went wrong this week" entry point still shows the wall of shell text the R86 nit was filed about.
Done-when: both render `run.get("goal_display") or run.get("goal")` (fallback = old-CP safe); `_history_runs`'s has-a-real-goal filter stays on `goal`; tests extend tests/test_cli.py:367-426 (summary rendered; fallback when absent); pytest green.

### R95. cli.py client-token surface coverage lift — `done` *(2026-06-07, PR #106 — 62→67% branch, mutation-probed)* `self-promoted`
Surface: tests/ratchet. A2 rank 1 (cli.py 62% branch — weakest module). `pair`/`token`/`revoke` + `_list_client_tokens`/`_revoke_client_token`/`_mint_client_token` (cli.py:941-1052): untested 403/404/empty/≥400 dispatch branches, revoked-vs-active + last-used formatting, distinct loopback warnings (phone vs script), QR fallback. Expected +4-6 branch points on cli.py.
Done-when: real-behavior assertions via CliRunner + stubbed `_ctx_client`/MockTransport (R16 style, no processes); every error/dispatch branch asserted on observable output or exception type; cli.py branch coverage strictly up (judge re-measures + mutation-probes); no module down; pytest green.

### R96. worker.py pure argv-builder coverage (`_build_auto_argv` + `_build_codex_argv`) — `done` *(2026-06-07, PR #107 — 72→75%; R30 anchoring verified, no new bug)* `self-promoted`

### R97. iOS README pure-layer harness count stale — stamp it — `done` *(2026-06-07, PR #108 — count-free + stale recipe fixed)* `self-promoted`
Surface: docs/iOS. A6 survey #3 rank 2, Tier A STAMPED (surveyor ran the harness: **92/92** on Swift 6.0.3 vs the claimed 58/58 at mobile-app/ios/README.md:257).
Done-when: README:257 states the verified current count with a re-stamped date, or count-free phrasing the judge confirms can't drift (R91 precedent — prefer this); harness re-run green via the documented recipe on /tmp/swift-toolchain as the evidence; docs-drift ratchet stays 0; pytest green.

### R98. Panel labels auth failures as "control plane unreachable" — `done` *(2026-06-07, PR #109 — 3-state wording, Playwright-proven)* `self-promoted`
Surface: panel/UX. A6 survey #3 rank 3 (user-test n7, re-confirmed). panel.html:243 throws `Error("HTTP " + r.status)` for any non-2xx; the catch at :412 prefixes EVERY error with "control plane unreachable — " → a bad token renders the self-contradictory "control plane unreachable — HTTP 401". The CP answered; it's an auth failure.
Done-when: HTTP-status errors (esp. 401/403) render an auth/permission message distinct from transport failures' "unreachable" wording; transport wording unchanged; verified by rendered verdict text for a 401 response vs a fetch reject (Playwright against a scratch CP — browsers cached — or capped honestly per evidence table); server untouched; pytest green.

### R99. worker.py process-safety branch coverage (`_kill_active_job` / `_kill_aux_procs`) — `done` *(2026-06-07, PR #110 — kill range 0-miss, worker 76%)* `self-promoted`
Surface: tests/ratchet. A2 rank 3 (worker.py now 75% after R96). worker.py:1205-1310: untested two-level kill fallback (`os.killpg` → ProcessLookupError/PermissionError → `proc.kill()`), docker-kill timeout teardown, aux-proc reaping. Judge note from the slate: `_kill_aux_procs` + early-return are trivially sync-testable — do those first; only the docker-timeout async seam is harder (R72's tests are adjacent precedent).
Done-when: stubbed `os.killpg`/proc seams assert the fallback paths; sync wins first, async docker-timeout second; worker.py branch coverage strictly up; judge mutation-probes; no module down; pytest green.
Surface: tests/ratchet. A2 rank 2 (worker.py 72%). Zero existing test references for worker.py:1068-1125: missing task/intent → ValueError; triage-prompt insertion at the CORRECT index incl. bwrap-wrapped argv (security-relevant — R30's bug class); sandbox/model defaulting; codex-missing → FileNotFoundError (monkeypatch shutil.which); args passthrough. Expected +2-3 branch points.
Done-when: direct pure-function assertions on argv shape + raised errors; judge mutation-probes; no module down; pytest green.

## Human-directed 2026-06-08 — distilled-default stream (all platforms) + mac publish fallback

User directives (2026-06-08): (1) make the distilled live-stream view the DEFAULT on every platform; (4) mac-app two-step publish fallback — no blocker, build it on the mac-mini-m4 node. (Branch protection deferred per user; fleet worker rollout handled as ops, not a code item.)

### R107. CLI live-stream distilled-default + `--verbose` raw escape + shared spec/fixtures — `done` *(2026-06-08, PR #119 — distill_log_line + golden fixtures; pytest 1083)* `human-promoted` `feature`
Surface: CLI/feature. The reference implementation + canonical contract for the cross-platform distilled view. Today `_stream` (cli.py ~2304) echoes every raw Claude stream-json log line (incl. base64 signature blobs) — a firehose. The distilled phase markers (🔎/🔧 from worker `last_activity`) are NOT in the log stream; the per-line distillation (assistant text, "→ <Tool>: <summary>", tool results) must be DERIVED by parsing the stream-json `data` field client-side.
Done-when: ground the mapping in a REAL captured agent-job stream (run a tiny `kind:claude` job on a scratch CP, capture raw logs); define the distilled transform (assistant text shown; tool_use→"→ <Tool>: <short>"; tool_result truncated; suppress base64/signature/raw envelopes; keep 🔎/🔧/✓ phase dividers); make distilled the DEFAULT for `roost logs --follow`/`run`/`_stream`, raw behind `--verbose` (or `--raw`; pick + document); commit a SHARED spec doc + golden fixtures (raw stream-json line → expected distilled output) under mobile-app/fixtures/distilled/ so iOS/Android mirror it exactly; pytest green incl. golden-fixture tests; live smoke on a scratch CP.

### R108. iOS session view: distilled-default (mirror R107 spec/fixtures) — `done` *(2026-06-08, PR #121 — DistilledLine.from + 16 shared fixtures; iOS harness 119; sim screenshots)* `human-promoted` `feature`
Surface: mobile/iOS/feature. After R107 lands the spec+fixtures: iOS SessionStore/SessionView default to the distilled rendering of the same stream-json, raw behind a UI toggle (default off). Linux pure-layer tests against the committed golden fixtures + Mac-node sim screenshot.

### R109. Android session view: distilled-default (mirror R107 spec/fixtures) — `done` *(2026-06-08, PR #120 — DistilledLine.from + 16 golden fixtures; harness 100)* `human-promoted` `feature`
Surface: mobile/Android/feature. After R107: Android SessionViewModel/SessionScreen default to distilled rendering, raw behind a UI toggle. kotlinc+JUnit tests against the committed golden fixtures + emulator screenshot.

### R110. mac-app two-step publish fallback (mirror CLI R78/R90) — `done` *(2026-06-08, PR #118 — stageBlob/publishFromBlob; RoostKit 123; Mac-node verified)* `human-promoted`
Surface: mac-app/robustness. `mac-app/Sources/RoostKit/RoostClient.swift` `publishBundle` (~142) is one-shot only → 500s on an older CP. Mirror the CLI fallback (cli.py ~1547-1625): on one-shot non-2xx (except 401/403) → stageBlob (POST /blobs?name=) → publishFromBlob (POST /publish JSON {name,blob_id}); on double-failure surface BOTH errors leading with the one-shot's. RoostClient lacks stageBlob/publishFromBlob — add them.
Done-when: publishBundle degrades across CP versions; new RoostKit methods + a PublishFallbackTests (one-shot 500 → blob path → Site); Linux `swift test` green + Mac-node `swift build`+`swift test` green (we HAVE mac-mini-m4 — no blocker; build there via roost exec); pytest green (server untouched).

## Human-directed continuation 2026-06-08 — harden the distilled cross-platform contract

### R113. Expand distilled golden fixtures to cover SPEC branches + adversarial shapes; verify all 3 platforms agree — `done` *(2026-06-09 retro-bookkeeping: shipped 2026-06-08, PR #124 — 3 cross-platform divergences fixed; implementing session ended before journaling)* `self-promoted` `feature`
Surface: cross-platform/robustness. R111 proved the 3 distilled implementations (Python `distill_log_line`, iOS `DistilledLine.from`, Android `DistilledLine.from`) CAN diverge on inputs the 17 golden fixtures (`mobile-app/fixtures/distilled/cases.json`) don't cover — the non-dict-`message` crash was Python-only and the fixtures missed it. Systematically expand the shared golden fixtures so EVERY SPEC.md branch + the adversarial shapes the hunt cleared (non-dict content blocks, tool_use missing name/input, tool_result number/null/odd-list, unknown top-level type, non-str text/hint coercion, falsy-vs-truthy message boundary, thinking/signature suppression, 72/200-char truncation boundaries, unicode/control chars, malformed/truncated JSON passthrough) are pinned as golden cases — then run the SAME expanded cases.json through all THREE fixture tests to prove they agree. Any divergence found = a real cross-platform bug → fix the outlier to match SPEC (repro-first if it's a crash).
Done-when: cases.json expanded to cover the SPEC branches + the cleared-hypothesis adversarial shapes (document the coverage mapping: case → SPEC rule); all three fixture tests (pytest `tests/test_distilled.py`, iOS `swift test` via /tmp/swift-toolchain + Mac node, Android kotlinc+JUnit) load the expanded file and pass — i.e. all 3 platforms produce identical distilled output for every case; any divergence fixed (outlier → SPEC) with a repro if it's a crash; fixture-shape/drift guard green; pytest green. Cap render claims honestly; the fixture agreement across 3 harnesses IS the deliverable.

## Human-directed 2026-06-09 — production-review fixes (in-session repo review)

The user reviewed the repo with the orchestrator (2026-06-09) and directed: fix all
five flagged findings. R114 is security-adjacent but EXPLICITLY human-directed in
this session — the standing security-session split governs loop *self*-promotion,
not direct human orders; the PR must still flag the security relevance.

### R114. Auth-disabled CP silently accepts worker-plane requests — loud guard — `open` `human-promoted`
Surface: backend/operability (security-adjacent, human-directed). server.py:~2920: when no shared token is configured (`principal.kind == "none"`), `require_worker`/`require_matching_worker` pass ANY request — an operator who forgets the token opens the worker plane (poll/heartbeat/events/results) to the LAN: job theft, result corruption. The code comment acknowledges the fallback; nothing warns the operator.
Done-when: a CP started with auth disabled on a NON-loopback bind either refuses to start or requires an explicit opt-in (e.g. `ROOST_INSECURE_NO_AUTH=1`) — pick the seam where the bind host is actually known, justify; loopback/dev zero-config keeps working unchanged (quickstart + the entire TestClient suite stay green); an unmissable startup log line in the insecure-opt-in case; DEPLOY.md + the config reference document the rule; tests for guarded/opt-in/loopback paths; pytest green + live smoke (scratch CP).

### R115. Sweeper exceptions swallowed — log + surface a failure counter — `open` `human-promoted`
Surface: backend/operability. server.py:~2258: the sweeper's lease-expiry transaction (and sibling sweep phases) catch bare `Exception`, roll back, and continue silently — a persistent DB error means jobs stop being requeued with ZERO operator signal. (Distinct from the R12-cleared rollback-then-reraise guards — these swallow.)
Done-when: every sweep-phase exception is logged with phase context (rate-limited/deduped so a persistent error doesn't flood); a sweeper-failure counter exposed on /metrics (additive, R35 hand-rolled style); a test injects a sweep-phase failure and asserts the log + counter + that the sweeper survives and later iterations still run; audit the other bare-`except` swallows in server.py's periodic paths (notify/prune/schedule tick) for the same treatment where cheap; pytest green.

### R116. Assignment scan unbounded — LIMIT with provably-unchanged semantics — `open` `human-promoted`
Surface: backend/robustness. server.py:~1525: `_try_assign_one` fetches ALL queued jobs and scores each against workers — every poll/assignment pass is O(queue), stacking poll latency and the sweep cadence on a large backlog.
Done-when: the queued-jobs fetch is bounded (LIMIT whose ordering preserves today's priority/anti-starvation semantics — document why the same job wins for queues under the limit, and prove the anti-starvation override still fires for an old job that a naive LIMIT window would miss); existing placement/decline/grace tests untouched and green; a test with > LIMIT queued jobs proves continued progress (all eventually assigned); pytest green + live smoke.

### R117. Steward timeout/failure invisible — structured signal — `open` `human-promoted`
Surface: worker/operability. worker.py:1421-1476: `_run_steward_agent` returns `None` on timeout or failure; callers silently fall back to the heuristic. Repeated steward timeouts on a node are exactly the flaky-host signal an orchestrator should surface, and a diagnosis silently becomes "no diagnosis".
Done-when: timeout / spawn-failure / bad-output are distinguishable outcomes (structured return or equivalent) with a loud worker log per occurrence; a visible aggregate signal (e.g. consecutive-failure count in the worker's advertised state/heartbeat or a worker event — pick the additive seam consistent with R41's `gpu_detection` precedent, justify); heuristic fallback behavior itself unchanged; tests for timeout vs missing-binary vs success; pytest green.

### R118. Recovery-path tests: CP restart over stale leases + concurrent-assignment race — `open` `human-promoted`
Surface: tests. The two most production-critical untested scenarios (review 2026-06-09): no test simulates a control-plane process restart finding stale leases in its DB; no test forces concurrent polls competing for one queued job.
Done-when: (a) a test boots a CP app over a DB file, assigns + leases a job, tears the app down, advances past LEASE_TTL, boots a FRESH app instance over the SAME file, and proves the sweeper requeues (attempt accounting per R19 semantics); (b) a concurrency test fires N simultaneous polls at one queued job and proves exactly-one assignment with consistent bookkeeping (async gather against the real app, no sleeps-as-sync); tests-only PR (a real bug found = repro note for the next cycle — scope discipline); deterministic under repetition and xdist (R45 precedent); pytest green.

## Human-directed 2026-06-09 — mac-app + mobile-app focus (R119–R124)

User direction (2026-06-09): after R114–R118, feature focus shifts to the mac app
and the mobile apps. The fleet has mac-mini-m4 (Xcode 26.2, iPhone 17 Pro sim,
Pixel_8 AVD) — all UI evidence paths proven (R50 iOS sim / R74 Android emulator /
R84 XCUITest headless / R73 mac build). Order: R119 first (it re-bases the whole
mac-app surface), then R120; R121–R123 parallel-safe (different surfaces); R124 last.

### R119. Verify + merge the mac-app multi-window redesign (branch `mac-app-redesign`) — `open` `human-promoted` `feature`
Surface: mac-app/feature. fb6b95a (pushed) restructures the app ground-up (WindowKind registry, console PTY ownership fix, DesignSystem declutter); the commit honestly states "Not yet compiled on macOS". RoostKit untouched per the commit message — verify.
Done-when: branch rebased on master if needed; Linux RoostKit `swift test` green; Mac-node (`mac-mini-m4`) `swift build` + `swift test` green via a roost job; render evidence per the evidence table (headless RenderShots pattern if R120 lands first, else capped honestly); judge reviews the full diff; PR from the branch merged (squash); pytest green (server untouched).

### R120. Commit the headless SwiftUI render harness as a supported mac-app test utility — `open` `human-promoted`
Surface: mac-app/tests. Promotes the standing Proposed item (the product call is now made by the user's mac focus): RenderShots.swift + a 1-line App.swift hook gated on `ROOST_RENDER_DIR`, rendering real views with live `GET /derived` data via `NSHostingView.cacheDisplay` — the only screenshot path on the TCC-less Mac worker.
Done-when: harness committed + documented (mac-app/README: how to run it via a roost job); produces PNGs of ≥3 key views on mac-mini-m4 as blob artifacts linked in the PR; future mac-app PRs can cite it as render evidence; Linux swift test still green; pytest green.

### R121. Phones: fleet/workers screen (both platforms) — `open` `human-promoted` `feature`
Surface: mobile/feature. Top remaining UAT gap: no workers/GPU view on the phones — an operator can't answer "is my fleet up" from the couch. Server `GET /workers` exists; verify the mobile token scope reaches it (if not, that's an additive scope decision documented in API.md).
Done-when: API.md gains the workers surface (additive) + golden fixtures via record_fixtures.py; iOS + Android Fleet screens (name/status/caps/load/last-seen, offline pill consistent with R75's staleness pattern) following the established screen/sheet patterns; pure logic Linux-tested on both harnesses; emulator + sim screenshots per the proven paths; pytest green.

### R122. Phones: failed-agent rows render raw JSON → distilled failure rendering — `open` `human-promoted` `feature`
Surface: mobile/UX. UAT P3 carryover, now cheap: R107's distilled SPEC + fixtures exist; failure results on the phones still show raw JSON walls.
Done-when: failure-result rendering on both platforms reuses the distilled transform/truncation rules (SPEC.md cross-ref; new golden cases only if a new SPEC branch is exercised — values-only additive regen); Linux-harness tests on both platforms; screenshots per evidence paths; pytest green.

### R123. Android: keyboard occludes Publish/Dispatch/Pair CTAs — `open` `human-promoted`
Surface: mobile/Android/UX. UAT P3: the IME pushes the primary CTA off-screen on the three input-heavy sheets (R74 fixed the inset root cause for the TopAppBar; the IME case remains).
Done-when: imePadding/scroll behavior fixed app-wide (the three known sheets + audit siblings); Linux-testable state logic asserted where any exists; emulator screenshots keyboard-up per the R74 path; pytest green.

### R124. mac-app: schedule-create + single-instance guard + version single-sourcing — `open` `human-promoted` `feature`
Surface: mac-app/feature. Bundle of the judge-sanctioned R62 deferral (schedule CREATE from the menu bar; list/toggle landed) + two UAT nits: a second app launch should focus the running instance, and Info.plist hardcodes 0.1.0 while the CP self-reports `__version__` (single-source per R32 precedent — pick the seam, justify).
Done-when: create-schedule UI per existing sheet patterns with RoostKit logic Linux-tested; single-instance guard + version fix; Mac-node build + swift test green; render evidence per the R120 harness or capped honestly; pytest green.

Device-only push transport (R55/R67 leftovers) stays capped — requires physical
devices the fleet doesn't have; revisit if hardware appears.

## Replenishment 2026-06-08 — hunt fresh distilled code + drift sweep #111-121

Hunt over the just-shipped distilled transform (the new DEFAULT view parsing untrusted Claude stream-json) found 1 reachable crash; drift sweep found the mobile docs lagged the R107/R108/R109 rollout (CLI+iOS were disciplined). The SPEC-vs-impl contract verified ACCURATE (the high-impact drift did NOT occur). Both surveys self-judged on opus (sandbox lacked the Agent tool) → binding cross-model Sonnet judge runs at implementation time. Repro: `/tmp/hunt-distill-repros.py` (+ .bak; uncommitted).

### R111. `distill_log_line` crashes the DEFAULT view on a non-dict `message` — `done` *(2026-06-08, PR #123 — isinstance suppress; 17th golden fixture verified on both mobile harnesses; no twins)* `self-promoted`
Surface: backend/CLI/correctness. A1 hunt (fresh code), judge-confirmed. `cli.py:2440-2441`: `msg = obj.get("message") or {}` only rescues FALSY messages; a TRUTHY non-dict `message` (a JSON string/list/number in an `assistant`/`user` envelope) reaches `msg.get("content")` → AttributeError. A worker posting one such line crashes the now-DEFAULT distilled `roost logs`/`--follow`/phone session view for that job (end-to-end reachability proven: server stores str `data` verbatim). Violates the docstring ("never raises") + SPEC rule 2 ("never lose a line — passthrough"). The mobile clients already SUPPRESS this safely (iOS `as? [String:Any]`→nil, Android `optJSONObject`→null) — Python is the outlier; cases.json has NO non-dict-message case so the fixtures miss it.
Repro: /tmp/hunt-distill-repros.py — 5 tests incl. `test_F1_reachable_end_to_end_via_server_logs`, FAIL on master.
Done-when: `distill_log_line` suppresses (returns None) for a non-dict `message`, matching the mobile clients (use `isinstance(msg, dict)` not `or {}`); a non-dict-message golden case added to `mobile-app/fixtures/distilled/cases.json` (expected: suppressed/null) to pin all 3 platforms; repros promoted into tests/; pytest green. While there, sweep `distill_log_line` for the SAME `x or {}`-on-possibly-non-dict pattern elsewhere (R70/R88 class) and fix any twins.

### R112. Mobile docs lag the distilled-default rollout (Android README + DESIGN §3.2 + API §4) — `done` *(2026-06-08, PR #122 — 3 docs additive; ratchet 0)* `self-promoted`
Surface: docs. A3 drift sweep #111-121, 3 Tier-A findings bundled (all doc-only, same theme). (a) `mobile-app/android/README.md:70-71` describes raw-only monospace logs with no "Distilled session view" section though R109 shipped distilled-default + Raw toggle — mirror the iOS README section (ios/README.md:11-22). (b) `mobile-app/DESIGN.md:148-149` (§3.2) still says "Log rendering is plain monospaced text" — describe distilled-by-default + raw toggle, x-ref fixtures/distilled/SPEC.md. (c) `mobile-app/API.md:162-166` (§4 Log rendering) — note agent stream-json is distilled CLIENT-SIDE by default per SPEC.md (the wire `data` row is UNCHANGED — keep it additive, do not imply a server change).
Done-when: all three additively updated to describe the distilled-default + raw-toggle client rendering with SPEC.md cross-refs (the API.md wire contract stays described as unchanged); docs-drift ratchet to 0; pytest green (docs-only).

## Replenishment 2026-06-07 night #2 — A1 hunt #10 + A2 re-measure (loop restarted by human; repo unchanged → deepening)

Hunt #10 repro file: `/workspace/yang/roost-oss/LOOP/repro-a1-hunt10.py` (+ /tmp backup; uncommitted). Coverage re-measured 82% roost-scoped (+2). NOTE: hunt #10's agent self-judged on opus (its sandbox lacked the Agent tool) — the binding cross-model Sonnet judge happens at implementation time (the implementer re-runs the repros under its mandatory judge). A2 rank 3 (worker R38 input-DELIVERY seam `_deliver_inputs`/`_ack_input` + creds error branches) queued Tier-A-judged for the next slot.

### R100. Non-finite schedule interval (`inf`/`nan`) bypasses the floor guard → wedges `GET /schedules` — `done` *(2026-06-07, PR #111 — math.isfinite guard covers both paths; no-poison-row proven)* `self-promoted`
Surface: backend/robustness. A1 hunt #10, judge-confirmed (2 repros). `parse_every` (server.py:323-340) falls through to `float(every)` for unrecognized strings, so `"inf"`/`"nan"`/`"1e400"` (and a bare JSON `1e999` → `inf`) return a NON-FINITE float; the create guard `interval < SCHEDULE_MIN_INTERVAL_SEC` is bypassed (`inf < 30` and `nan < 30` are both False; `-inf` is correctly caught). Symptoms: (a) `inf` commits a poison row (`next_run_at=inf`) then FastAPI's JSON render raises "Out of range float values are not JSON compliant" → `GET /schedules` returns **500 forever** for every client (CLI/mobile/MCP) — one malformed request durably wedges the whole schedule-list surface; (b) `nan` → NULL violates NOT NULL → IntegrityError 500 at create.
Repro: LOOP/repro-a1-hunt10.py — `test_finding_inf_interval_wedges_schedule_list`, `test_finding_nan_interval_500s_create`, FAIL on master (got 500, expect 400).
Done-when: `POST /schedules` with non-finite `every` (`inf`/`nan`/`1e400`/bare `1e999`) returns a clean 400 with a clear message; NO row committed; `GET /schedules` stays 200 throughout; finiteness rejected at the door (e.g. `parse_every` returns None when `not math.isfinite(result)`, or a guard beside the floor check); both repros promoted into tests/; pytest green.

### R101. cli.py inspect/control read commands + SSE stream coverage — `done` *(2026-06-07, PR #113 — 67→77% branch, SSE exit-codes mutation-probed)* `self-promoted`
Surface: tests/ratchet. A2 rank 1 (cli.py 67% — biggest weakest module). Untested non-process surfaces: `_iter_sse` (387-411), `_lookup_error` (269), `submit`/`run` --json/--detach bodies (1748-1763, 1790-1819), `logs` (2018-2029), `cancel` 409+tree-count (2038-2044), `jobs` state/root filters + intent truncation (2114-2126), `_stream` SSE event dispatch + exit-code arithmetic (succeeded / ec>0 / ec<=0 / None) (2284-2311). `send`/`exec`/`status`/`tree`/`history`/`pair`/`token` already tested — excluded. ~129 missing stmts; the core "what is my fleet doing" observability surface.
Done-when: real-behavior assertions via CliRunner + httpx MockTransport (R16/R95 idiom, no processes/live-LLM); every error/dispatch/SSE-event branch asserted on observable output or exit code (the `_stream` exit-code mapping asserted exactly); cli.py branch coverage strictly up (judge re-measures + mutation-probes); no module down; pytest green.

### R102. worker.py `build_command` kind-dispatch router coverage — `done` *(2026-06-07, PR #112 — 76→78% branch, 3 mutation probes)* `self-promoted`
Surface: tests/ratchet. A2 rank 2 (worker.py 76%). The router body of `build_command` (worker.py:508-517, 529-546) is never invoked directly — every test patches it out, while the argv-builders it calls were covered in R96/R99. Untested: `command` string vs list vs invalid-type→ValueError; `kind=claude/codex/unknown`→ValueError; cwd resolution (spec.cwd / default_cwd / os.getcwd()). Security-relevant: this is the seam that routes to the bwrap/argv builders.
Done-when: pure-function assertions on the returned (argv, cwd, tempfiles) per kind branch + ValueError on invalid command-type and unknown kind; cwd precedence asserted; no subprocess; worker.py branch coverage strictly up; judge mutation-probes; no module down; pytest green.

## Replenishment 2026-06-07 night #3 — UAT-findings triage (2nd parallel UAT pass; human deploy redeployed the live CP)

Triaged LOOP/UAT-FINDINGS-2026-06-07.md vs master 711036c. 2 P1 items STALE (R76 composer, R85 subtitle — the loop already shipped them). 3 Tier-A bugs with failing repros (`/tmp/uat-triage-repros.py` + .bak; uncommitted; cross-model sonnet judge confirmed 5/5 fail). Promoted R103-R105. Tier-B → Proposed: C4 mac-app publish fallback (needs Mac gate), C5 live-stream distilled-default (design call). Human-gated: fleet worker-build rollout across 16 nodes (outward ops, out-of-loop-scope). Queued from cycle #2: A2 rank-3 worker input-delivery coverage.

### R103. codex jobs fail in a non-git cwd — `_build_codex_argv` omits `--skip-git-repo-check` — `done` *(2026-06-07, PR #115)* `self-promoted`
Surface: backend/worker/correctness. UAT C1, judge-confirmed bug. `worker.py:1116-1125` builds `["codex","exec",intent]`; a fresh worker's default (non-git) cwd makes `codex exec` abort → every codex job fails on a clean node.
Repro: /tmp/uat-triage-repros.py::test_codex_argv_includes_skip_git_repo_check (FAIL on master).
Done-when: `_build_codex_argv` includes `--skip-git-repo-check`; repro promoted into tests/ (beside R96's argv tests); pytest green. Additive single-line, Linux-testable.

### R104. CLI raw tracebacks: `workers`/`cancel` bad-token + no top-level `main()` handler — `done` *(2026-06-07, PR #116 — workers/cancel + main() wrap + ROOST_DEBUG)* `self-promoted`
Surface: CLI/robustness. UAT C2, judge-confirmed. `cli.py:2190` (`workers`) and `:2042` (`cancel`) bare `raise_for_status()` → raw httpx traceback on a bad token; `main()` (:2314) has no top-level except so any unexpected error dumps a traceback. (R77/R79 already covered schedule subverbs + status/logs/tree — NOT in scope.)
Repro: /tmp/uat-triage-repros.py::{test_workers_bad_token_friendly_not_traceback, test_cancel_bad_token_friendly_not_traceback, test_main_wraps_unexpected_errors} (FAIL on master).
Done-when: `workers`/`cancel` surface a friendly auth error on 401/403 (mirror `_admin_403`/`_lookup_error`) AND `main()` wraps unexpected exceptions into a clean nonzero exit (don't swallow tracebacks for programming errors that aren't user-facing — match the repo's existing ClickException idiom); 3 repros promoted; pytest green.

### R105. "1 nodes online" — fleet-verdict pluralization — `done` *(2026-06-07, PR #114 — _count_noun helper)* `self-promoted`
Surface: backend/UX. UAT C3, judge-confirmed (cosmetic; lowest-leverage of the three). `server.py:1061-1062` `_fleet_verdict` emits `f"{len(live)} nodes …"` unconditionally → "1 nodes" with one node; surfaces on panel + Android too.
Repro: /tmp/uat-triage-repros.py::test_fleet_verdict_singular_node_grammar (FAIL on master).
Done-when: singular renders "1 node"; test asserts both forms; check for the same `N nodes`/`N jobs` pattern elsewhere in _fleet_verdict and fix consistently; pytest green.

### R106. worker.py R38 input-delivery seam + creds error-branch coverage — `done` *(2026-06-07, PR #117 — worker.py 78→80%, 5 mutation probes killed)* `self-promoted`
Surface: tests/ratchet. A2 rank-3 (queued from replenishment #2; worker.py 78% after R102/R96/R99). Untested: `_deliver_inputs` entire body (worker.py:2334-2379 — no-active-entry early return / HTTP≥400 / HTTPError+ValueError fetch-fail / per-input not-live→dropped / exited-or-closed-stdin→dropped / write-OK→delivered / BrokenPipe→dropped); `_ack_input` (2384-2390 incl. HTTPError swallow); real `_send_log` 413/429 log-drop branch (2398-2409); `_post_event` HTTPError path (2412-2417); `_claude_creds_path` CLAUDE_CONFIG_DIR-vs-default (1572-1577); `_refresh_claude_creds` ERROR branches only (1585-1586 HTTPError, 1591 empty-creds, 1609-1610 OSError-print — happy path already tested). Worker side of `send --wait`, distinct from R99's kill-paths.
Done-when: real-behavior assertions via a fake `self.client` (stub async httpx) + a fake `proc` exposing `.stdin`/`.returncode` (async, no real subprocess); assert dropped-vs-delivered ack state + detail strings per branch, the 413/429 log-drop, CLAUDE_CONFIG_DIR override; worker.py branch coverage strictly up; judge mutation-probes; no module down; pytest green.

### R21. Make presigned blob PUT single-use and race-safe — `done` *(2026-06-07, PR #30)* `self-promoted`
Surface: backend/security. A1 hunt #2 reproduced that a presigned `put_url`
remains valid after the first upload finalizes the blob: replaying the same URL
returns 200 and overwrites both the finalized bytes and their size/hash metadata.
Done-when: only a pending blob can accept a PUT; claiming/finalizing is atomic
enough that concurrent PUTs cannot both win; finalized content and metadata stay
immutable; replay + concurrency regression tests; pytest green.

### R22. Roll back failed direct blob uploads — `blocked: security-session` `self-promoted`
Surface: backend/security/robustness. A1 hunt #2 reproduced that `POST /blobs`
inserts a durable `state=ready` row before streaming the body; a 413 rejection
deletes the partial file but leaves a listed ready row with a signed download URL.
Done-when: incomplete uploads are never exposed as ready; any stream or finalize
failure removes both file and row; tests cover oversized rejection and an
injected failure; pytest green.

### R23. Count every publish archive entry against the extraction cap — `blocked: security-session` `self-promoted`
Surface: publish/security. A1 hunt #2 reproduced that `SITE_MAX_FILES` counts
only regular tar members, so arbitrarily many directories/links bypass the
pre-extraction cap and can consume filesystem inodes/CPU.
Done-when: every extracted filesystem entry (regular file, directory, link)
counts toward one clearly named cap before extraction; existing byte and
traversal protections stay intact; non-regular bypass + normal mixed-archive
tests; pytest green.

### R1. Harden docker argv assembly against flag injection — `done` *(2026-06-06)*
Surface: backend/security. *(Re-scoped 2026-06-05: verified no shell injection — `_build_docker_argv` builds an argv list with `_validate_container` mount/network guards and `_sanitize_env`.)* Residual: `argv.append(str(image))` lands after the option flags (roost/worker.py:~640), so a leading-dash `image`, `volumes`, `network`, or `workdir` value (e.g. `image: "--privileged"`) is parsed by `docker run` as a flag, not an argument.
Done-when: leading-dash (and empty) values rejected for all spec-sourced argv positions; malicious-spec tests added; pytest green.

### R2. Default runtime cap for jobs with no wallclock budget — `done` *(2026-06-06)*
Surface: backend/robustness. *(Re-scoped 2026-06-05: `max_wallclock_min`/`_sec` IS enforced — budget→`timeout_s`→`wait_for` + `killpg` on timeout, roost/worker.py:1602–1607, 1916–1924.)* Residual: a job that sets no budget gets `timeout_s=None` and runs unbounded, holding a capacity slot forever.
Done-when: sane default cap (config-overridable, per job kind) applied when no budget is set; timeout reported distinctly from `failed`; tests for both the default and an explicit override; pytest green.

### R3. Reconcile still-running jobs after lease expiry + re-register — `done` *(2026-06-06)*
Surface: backend/robustness. *(Re-scoped 2026-06-05: the worker DOES track consecutive heartbeat failures and forces re-register, roost/worker.py:~1185.)* Residual: during a CP outage longer than LEASE_TTL (60s), the server sweeps the job to `lease_expired`/re-queues it while the original worker is still running it → possible duplicate execution; post-re-register reconciliation semantics are undefined.
Done-when: semantics chosen + documented (abort local work on re-register, or report-and-dedupe on reconnect); test simulates a CP outage past the TTL; pytest green.

### R4. Escape job intent in verifier prompt — `cut` *(human, 2026-06-06 — in-progress work discarded, branch deleted)*
Surface: backend/security. Job `intent` is interpolated raw into the verifier prompt (roost/verify.py + worker.py) — adversarial intents can steer the verdict.
Done-when: intent is delimited/escaped (e.g. fenced with clear instruction framing); a prompt-injection regression test exists; pytest green.

### R5. Blob expiry sweeper — `invalid` *(closed 2026-06-05)*
Survey claim was wrong: `prune_expired` exists (roost/blobs.py:134) and is wired into the server's periodic sweep (roost/server.py:2395–2398). Nothing to do.

### R6. Publish from mobile: API + contract — `done` *(2026-06-06, PR #14; premise re-scoped: mobile scope could already publish server-side — gap was contract+clients)*
Surface: publish/mobile. The publish verb exists server-side and **agent-scoped tokens can already publish** (verified 2026-06-05: scope→verb matrix in server.py); but `mobile-app/API.md` has no publish surface and the `mobile` scope can't reach it — phone agents can't publish, the positioning gap.
Done-when: publish surface added to API.md + golden fixtures (regen via `record_fixtures.py`); scope decision made explicitly (extend `mobile` scope vs. publish-capable token); iOS + Android decode layers implemented with Linux-runnable tests; UI wiring may be a follow-up item, claims capped accordingly.

### R7. Atomic publish call — `done` *(2026-06-06, PR #15 — one-shot `POST /publish?name=` with the bundle as the body; no blob ever staged)*
Surface: publish. Today: upload blobs, then a separate `POST /publish`; a flap between them leaves dangling staged blobs until TTL expiry (the R5 sweeper bounds the damage to the 24h default TTL — this item is about the UX gap, not disk leak).
Done-when: publish either accepts content in one transactional call or reconciles/retries dangling staged blobs; failure-injection test; pytest green.

### R8. `schedule` verb (interval jobs) — `done` *(2026-06-06, PR #16 — schedules table + CP tick + CLI/MCP; no-backfill, no-pile-up semantics)*
Surface: backend/feature. README.md:74 and docs/INTEGRATIONS.md:124 promise "schedule" as a product verb; nothing implements it. Biggest documented-but-missing feature.
Done-when: minimal honest slice — schema for schedules, CP tick that enqueues due jobs, `roost schedule` CLI + MCP tool, docs updated; tests for due/overdue/disabled schedules; pytest green. (Mobile parity goes to Proposed.)

### R9. Tests for `bootstrap.py` (`roost up`) — `done` *(2026-06-07, PR #17)*
Surface: tests. The zero-to-fleet onramp has no test file; regressions break new users silently.
Done-when: unit tests for `build_url`, `wait_for_health`, `wait_for_worker` et al. with a stubbed CP; failure paths covered; pytest green.

### R10. Tests for `service.py` — `done` *(2026-06-07, PR #18)*
Surface: tests. 232 lines of systemd/launchd install logic, zero tests.
Done-when: subprocess boundaries mocked; unit/file-generation logic asserted for both systemd and launchd paths; pytest green.

### R11. Bound the log-append path mid-window — `done` *(2026-06-07, PR #19 — 64KiB/append 413 + 5000-row 429 at write time; relay crash-on-oversize fixed)*
Surface: backend/robustness. *(Re-scoped 2026-06-05: `_prune_logs` already caps rows per job on a sweep cadence, roost/server.py:1416.)* Residual: between sweeps, unbounded POSTs can still bloat `job_logs` — no append-side size/rate cap.
Done-when: per-append size cap + per-job rate or row ceiling enforced at write time with a clear worker-side error; oversized-append test; pytest green.

### R12. Bare `except Exception` rollback guards — `invalid` *(closed 2026-06-05)*
Survey claim was wrong: the guards at roost/server.py:222, 513, 725, 984 roll back then **re-raise**, with comments explaining the "no transaction is active" masking they prevent. Intentional, correct, nothing to do.

### R13. Fixture drift guard for the mobile contract — `done` *(2026-06-07, PR #20 — capture() refactor + per-fixture shape guard on every pytest run)*
Surface: mobile/tests. Contract verified in sync on 2026-06-05 (all 9 API.md endpoints match server.py) — but nothing *automated* signals when server response shapes drift from `mobile-app/fixtures/*.json`; today it takes a manual audit like that one.
Done-when: a pytest that round-trips live server responses against the golden fixtures' shapes (additive-only rule from API.md §7 enforced: new fields OK, removals/renames fail); wired into the default test run.

### R15. Fix confirmed docs drift: publish docstring + default-caps + log-bounds notes — `done` *(2026-06-07, PR #22 — drift ratchet back to 0)* `self-promoted`
Surface: docs. Confirmed: roost/cli.py:~1440 `roost publish` docstring still says "uploads it via the blob store" (one-shot since PR #15; blob path is only the 422 fallback). Omissions: README "Job kinds" never says unbudgeted jobs get per-kind default caps (worker.py:~863, 120/240/240/360m, `default_runtime_cap_exceeded`); mobile-app/API.md §4 logs section doesn't mention the R11 write-time bounds (64KiB/append, 5000-row ceiling, events exempt).
Done-when: three spots corrected (additive, no contract change); Docs-drift ratchet back to 0; pytest green.

### R16. Tests for `roost up` orchestration in cli.py — `done` *(2026-06-07, PR #23 — judge round 2; cli.py 28%→36% branch)* `self-promoted`
Surface: tests. cli.py `up` (≈line 503+) spawns processes and drives boot.ping_ok/wait_for_health/wait_for_worker; zero tests reach it (cli.py at 28% branch coverage).
Done-when: unit tests with bootstrap helpers + process-spawning mocked (R10 style); failure paths covered (CP already up, health timeout, worker never registers); pytest green.

### R17. Tests for config.py + triage.py — `done` *(2026-06-07, PR #24 — config 48%→97%, triage 67%→100%)* `self-promoted`
Surface: tests. Measured: config.py 48% branch (60 stmts), triage.py 67% (30 stmts); no dedicated test file for either (pre-listed in Proposed).
Done-when: dedicated tests asserting real behavior (config TOML read/write/perms/resolution order; triage prompt rendering); both modules' branch coverage strictly up, no other module down; pytest green.

### R18. Matcher: non-numeric caps must not satisfy numeric constraints — `done` *(2026-06-07, PR #26 — incl. nan/inf hardening)* `self-promoted`
Surface: backend/correctness. matcher.py:~48: a numeric comparator with a non-numeric capability falls through to the string branch — `gpu_vram_gb: "N/A"` PASSES `"!=0"`. Repro: /tmp/a1-repro test_non_numeric_cap_does_not_satisfy_numeric_neq (FAILS on master).
Done-when: non-numeric cap never satisfies a numeric-rhs constraint (all operators); string-pin fallback (hostname: ==x) preserved; repro + operator-matrix tests pass; existing matcher tests untouched; pytest green.

### R19. Decline/requeue bookkeeping: grace restart + attempt budget — `done` *(2026-06-07, PR #27 — V13 requeued_at + attempt refund + decliner exclusion from best_other)* `self-promoted`
Surface: backend/robustness. Two bugs, one code region (server.py declined branch): (a) requeue keeps the old created_at, so one decline permanently arms the anti-starvation override — competitive placement/prefer abandoned; (b) declines consume the attempt counter — two declines + default max_attempts=2 means the first REAL execution dies on lease expiry with zero retries. Repro: test_decline_requeue_restarts_placement_grace + test_declines_do_not_consume_the_attempt_budget (both FAIL on master).
Done-when: decline+requeue restarts the grace window and does not consume the attempt budget (semantics documented in code); both repro tests pass; no regression in MAX_DECLINES/declined_by/escalation tests; pytest green + live smoke (placement behavior change).

### R20. prefer-by-name parity with target — `done` *(2026-06-07, PR #28 — two-layer fix: matcher + the server row lifts that never carried `name`)* `self-promoted`
Surface: backend/correctness. placement_score honors prefer.worker only as an ID; `target` resolves id OR name — prefer by name silently no-ops (+0 instead of +1000). README documents prefer with an id only, so also note the name form once supported.
Done-when: prefer matches id or name; repro (against the real signature) + a grace-window routing test pass; README prefer line updated; pytest green.

### R14. Docs truth pass — `done` *(closed 2026-06-05)*
Completed by the 27-agent repo-map workflow: README publish/kinds/test-count fixed, plus 5 stale docstrings/comments across server/worker/service/schema. The other two survey claims were already false — the verb matrix IS documented (docs/INTEGRATIONS.md) and `cancel --tree` IS documented (README.md:276, INTEGRATIONS.md:33). 347/347 tests green after edits.

---

## Ratchets (human-owned — loop may improve, propose, never add)

Monotone quality metrics per PROTOCOL.md §A5. The loop may take an iteration that
strictly improves one without regressing the others. `baseline: unset` means the
first iteration on that ratchet measures and records it here (no code changes).

| Ratchet | Measure | Baseline | Direction |
|---|---|---|---|
| Test pass | `python -m pytest -q` | 347 passed (2026-06-05) | count may grow; failures never tolerated |
| Branch coverage of `roost/` | `coverage run --branch --source=roost -m pytest && coverage report` (dev-only dep, never shipped; `--source=roost` pins the metric to product code so it stops drifting by scope) | **82% TOTAL roost-scoped** (2026-06-07 night #2, 941 tests, A2 re-measure; judge re-measured; 7 modules at 100%). Footnote: the old unpinned command folded in `tests/` → ~90%; 82% is the honest product number and a genuine +2 over the prior roost-scoped 80%. | up only |
| Docs drift | confirmed findings per full drift sweep | 0 (2026-06-07 — sweep found 1+2, R15 fixed all three, judge truth-checked) | stays 0 |
| Runnable examples | every `examples/*.yaml` accepted by a scratch CP submit | **3/3** (2026-06-07, scratch CP :8789) | stays 100% |

## Proposed (loop appends here; only humans promote)

- **A6 (cycle #4, unblocked from Proposed):** Version drift — `pyproject.toml` says `0.1.0`, server self-reports `0.2.0`; single-source via `importlib.metadata`
- Drop `cred_hash` on worker revoke — make revocation total *(security-session — credential lifecycle belongs in the dedicated session)*
- Tests for `triage.py` prompt rendering and `config.py` TOML/perms
- Mac app follow-ups (the native SwiftPM app lands with I1; webview wrapper is the deleted PoC — never resurrect it)
- Mac-app verb expansion (A6 survey #2): menu bar covers Runs/Workers/Console/Transfers but none of publish/schedules/send/backup/history — which belong in a menu-bar scope is a product call (2026-06-07)
- **User-testing sweep 2026-06-07 — polish notes (not promoted):** panel bad-token banner says "control plane unreachable — HTTP 401" (it's an auth failure; reachability is fine); `roost token --scope agent` mints `rst-mob-`-prefixed secrets (cosmetic; scope is correct); Android pairing screen is bottom-heavy with large empty margins (android/01); Android `model/Parsers.kt:21` `optString(key, null)` compiler warning.
- **Commit the headless SwiftUI render harness** from the mac-app user test as a supported test utility *(product call)*: `RenderShots.swift` + 1-line `App.swift` hook gated on `ROOST_RENDER_DIR`, renders real views with live `GET /derived` data via `NSHostingView.cacheDisplay` — the only screenshot path on a TCC-less worker (mac-mini-m4 has no Screen Recording/Automation permission, ungrantable non-interactively).
- ~~README.md:474 test count stale~~ → PROMOTED as R91 (2026-06-07 evening cycle).
- **iOS README:257 Linux pure-layer count stale (claims 58/58; ≥71 today)** — drift sweep #4 F2, Tier B (exact number needs a Swift-toolchain run to stamp; direction proven from journal R83 + Subtitle tests living in Models/).
- **`_resolve_job_id` case inconsistency** (hunt #9, Proposed-grade): uppercase FULL id 404s (`WHERE id=?` case-sensitive) while an uppercase PREFIX resolves (SQLite LIKE is ASCII-case-insensitive). No legitimate caller hits it (ids are lowercase hex) — normalize or document if ever touched.
- ~~mac-app Publish + Transfers panes swallow load errors silently~~ → PROMOTED as R93 (2026-06-07 evening cycle).
- **mac-app `publishBundle` is one-shot only (no two-step blob fallback)** — UAT C4, Tier B: `mac-app/Sources/RoostKit/RoostClient.swift:142` POSTs gzip one-shot; 500s against an old CP. The CLI degrades (R78/R90); the Mac app should mirror begin-blob→finalize or show "upgrade your CP". Mechanical, but the evidence gate needs a Mac node (RoostKit only Linux type-checkable) → stays Tier B / needs-mac-verify.
- **Live-stream JSON firehose → distilled-default view** — UAT C5, Tier B (design call): `cli.py:2282` `_stream` echoes every raw stream-json line (incl. base64 sig blobs); the distilled phase markers (🔎 verifying / ✓ verified) exist server/worker-side but aren't filtered to. Highest-leverage UX fix per UAT, but "what to distill + default-vs-`--verbose`" is a design decision (also applies to iOS/Android session views). R86/R94 already distilled job *rows*; the live *stream* remains.
- **Remaining UAT P3/UX (design/cosmetic, mostly needs-mac or cross-surface)**: no workers/GPU screen on phones; failed-agent rows render raw JSON on phones; publish-slug-silently-normalized notice; Android keyboard occludes Publish/Dispatch/Pair CTAs; Mac single-instance guard; Mac Info.plist 0.1.0-vs-0.2.0; version-string staleness signal (expose build sha); iOS verified-state lingering text; untrusted-worker silent-failure submit-time warning; panel/notify polish (node-name tooltips, verdict in notify payload, health banner counting operator's own benign nonzero exits). `blob DELETE 403 for own blob` → SECURITY-SESSION (excluded).
- **[HUMAN/OPS — out of loop scope] Fleet worker-build rollout across 16 heterogeneous nodes** — UAT P1 ops: workers run stale Jun-05/06 builds (no `roost --version` = pre-R42), lacking R43 cred auto-refresh (creds re-expire ~8h → agent jobs 401 until a worker restart), R24/25/26/30/31, R38 input delivery, R41, R72. DURABLE FIX = update the roost package on all 16 nodes + restart (DEPLOY.md rollout across pis/jetsons/cloud/mac/wsl/docker) — needs explicit human go-ahead on approach (rolling, per-node verify). The live CP itself is now redeployed from current master (2026-06-07, human commit 21a0e2f); this is the worker tier.
- ~~macOS CI job has never compiled the app~~ → PROMOTED as R92 (2026-06-07 evening cycle). The branch-protection half (required status checks on master) remains a HUMAN/GitHub-admin action — recommended once R92 makes the check meaningful.
- **Fleet ops (human — NOT loop work), from the 2026-06-07 sweep:** (a) live CP container (`docker-ec7c1cae…`) runs roost 0.1.0 (installed Jun 5) — rebuild from master to unbreak `backup`/`schedule`/`/metrics`/one-shot publish against the live fleet; (b) oracle is unhealthy for agent jobs — Claude cred 401 + a broken SessionStart Node hook; (c) mac-mini-m4's `~/roost-r50` clone is a stale single-branch checkout (origin lacks a master ref) — re-clone or fix the remote; (d) consider granting Screen Recording TCC on the Mac for real-window capture.
