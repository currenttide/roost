# Loop backlog

Direction anchor for the improvement loop (see `PROTOCOL.md` for the rules).

- **Humans** edit the Ranked section: reorder, cut, promote from Proposed, sharpen Done-when.
- **The loop** takes the top unblocked Ranked item, one per iteration, and may only
  *append* to Proposed — with one exception: when Ranked runs dry, the Replenishment
  engine (`PROTOCOL.md`) refills it with up to 3 judge-approved **Tier A** items per
  cycle from renewable sources — bug hunts (reproducing test required), coverage gaps,
  drift sweeps over changed code, journaled debts, the Ratchets table, and the
  **Product gap survey (A6)** — each tagged `self-promoted`. A6 may also promote items
  from Proposed directly to Ranked when the gap is real and code-verifiable, the fix
  is additive, the Done-when is concrete, and no design decision is required (judge
  must approve). Features needing a direction call, API/contract changes, or new
  dependencies always wait for the human.
  This is what makes the loop continuous: it idles only when a full cycle plus two
  deepening passes find nothing real, and resumes when the repo changes.
- Every iteration is gated by an independent **Sonnet judge** that re-runs the
  evidence itself (autoreview-style) before a PR is opened.
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

### R29. `roost history` and `roost prune-workers` undocumented — `open` `self-promoted`
Surface: docs. A6 survey cycle #4 (judge-approved, fast-tracked per protocol). Both commands fully implemented (cli.py:1916, cli.py:1029) but absent from README.md's "Inspect & control runs" table and docs/INTEGRATIONS.md. `roost history --failed` is the natural "what went wrong this week" entry point and no user can discover it.
Done-when: README.md inspect/control table includes `roost history [--failed]` and `roost prune-workers`; INTEGRATIONS.md CLI section mentions `roost history`; pytest green (docs-drift ratchet stays 0).

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
| Branch coverage of `roost/` | `coverage run --branch -m pytest && coverage report` (dev-only dep, never shipped) | **63% TOTAL** (2026-06-07, 482 tests; judge-verified) | up only |
| Docs drift | confirmed findings per full drift sweep | 0 (2026-06-07 — sweep found 1+2, R15 fixed all three, judge truth-checked) | stays 0 |
| Runnable examples | every `examples/*.yaml` accepted by a scratch CP submit | **3/3** (2026-06-07, scratch CP :8789) | stays 100% |

## Proposed (loop appends here; only humans promote)

<<<<<<< Updated upstream
=======
>>>>>>> Stashed changes
- **A6 (cycle #4, unblocked from Proposed):** Mobile one-shot publish parity — server landed with R7 (PR #15); just needs API.md §6 + iOS/Android decode layers (no server changes)
- **A6 (cycle #4, unblocked from Proposed):** Version drift — `pyproject.toml` says `0.1.0`, server self-reports `0.2.0`; single-source via `importlib.metadata`
- **A1 (cycle #4 hunt #3, repro in tests/test_judge_r4_bugs.py):** `_oneshot_agent` corrupts bwrap argv when inserting `--append-system-prompt` — inserts at `argv[:3]` (inside bwrap flags) instead of finding the `claude` position
- **A1 (cycle #4 hunt #3):** Relay tasks `t1`/`t2` in `_oneshot_agent` not cancelled in `finally` block — float as pending tasks on `CancelledError`, causing asyncio warnings and test interference
- Published-site listing pagination (`/publish` list unbounded, roost/server.py:2150)
- Drop `cred_hash` on worker revoke — make revocation total
- Capability detection: distinguish "no GPU" from "GPU detection failed" (worker logs)
- Worker credential refresh racing lease TTL — sync refresh with lease lifecycle
- Captain split observability: expose sub-job plan + reasoning in `roost tree`
- Cost estimation: configurable per-model pricing instead of fixed rate
- Narration re-render `min_interval` configurable
- MCP tool docstrings: add usage examples for each tool
- Tests for `triage.py` prompt rendering and `config.py` TOML/perms
- Broader e2e coverage for `verify.py` verdict path
- DEPLOY.md: SQLite backup/restore procedure for the hubbase CP
- Mobile: schedule verb parity (after R8)
- Mobile push notifications (DESIGN.md v1.1 — ntfy/UnifiedPush)
- Interactive follow-up to running agent jobs (DESIGN.md §3.2, v2)
- Mac app follow-ups (the native SwiftPM app lands with I1; webview wrapper is the deleted PoC — never resurrect it)
- Version drift: running CP self-reports 0.2.0, pyproject.toml says 0.1.0 — single-source the version (found during I0, 2026-06-06)
- Publish UI wiring: iOS/Android screens for pick-bundle → upload → publish → share-link (decode layers + contract landed with R6, PR #14, 2026-06-06)
- Mobile one-shot publish parity: expose `POST /publish?name=` (raw body) in API.md §6 + fixtures + decode layers (server side landed with R7, PR #15, 2026-06-06)
- Lease-expiry requeue grace analog: should a sweeper requeue also restart the placement-grace window (R19 restarted it for declines only — real failures may deserve different semantics)? (2026-06-07)
