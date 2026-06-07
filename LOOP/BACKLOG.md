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

### R69. A1 hunt #7 — mobile-contract robustness lens (deepening #3, long-idle gate) — `open` `self-promoted`
Surface: hunt. Final queued deepening: the server↔mobile contract under adversarial/degenerate payloads — the decode layers have golden-fixture tests (happy path) but the SERVER side of the contract has never been hunted for responses that would break the documented additive-only guarantees (e.g. fields that can become null where clients assume non-null; SSE event vocabulary under unusual job lifecycles; pagination headers under edge counts). Server-side findings only (client decoders are pure + tested); reproducing test required per finding; an all-clear = deepening-clear #2 → LONG-IDLE.
Done-when: repros merged after judge verification, or an honest all-clear report triggering long-idle.

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
| Branch coverage of `roost/` | `coverage run --branch -m pytest && coverage report` (dev-only dep, never shipped) | **71% TOTAL** (2026-06-07, 664→707 tests, R54; judge re-measured) | up only |
| Docs drift | confirmed findings per full drift sweep | 0 (2026-06-07 — sweep found 1+2, R15 fixed all three, judge truth-checked) | stays 0 |
| Runnable examples | every `examples/*.yaml` accepted by a scratch CP submit | **3/3** (2026-06-07, scratch CP :8789) | stays 100% |

## Proposed (loop appends here; only humans promote)

<<<<<<< Updated upstream
=======
>>>>>>> Stashed changes
- **A6 (cycle #4, unblocked from Proposed):** Version drift — `pyproject.toml` says `0.1.0`, server self-reports `0.2.0`; single-source via `importlib.metadata`
- Drop `cred_hash` on worker revoke — make revocation total *(security-session — credential lifecycle belongs in the dedicated session)*
- Tests for `triage.py` prompt rendering and `config.py` TOML/perms
- Mac app follow-ups (the native SwiftPM app lands with I1; webview wrapper is the deleted PoC — never resurrect it)
- Mac-app verb expansion (A6 survey #2): menu bar covers Runs/Workers/Console/Transfers but none of publish/schedules/send/backup/history — which belong in a menu-bar scope is a product call (2026-06-07)
