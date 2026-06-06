# Loop backlog

Direction anchor for the improvement loop (see `PROTOCOL.md` for the rules).

- **Humans** edit the Ranked section: reorder, cut, promote from Proposed, sharpen Done-when.
- **The loop** takes the top unblocked Ranked item, one per iteration, and may only
  *append* to Proposed — with one exception: when Ranked runs dry, the Replenishment
  engine (`PROTOCOL.md`) refills it with up to 3 judge-approved **Tier A** items per
  cycle from renewable sources — bug hunts (reproducing test required), coverage gaps,
  drift sweeps over changed code, journaled debts, and the Ratchets table below — each
  tagged `self-promoted`. Features and API/contract changes always wait for the human.
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

### R9. Tests for `bootstrap.py` (`roost up`) — `open`
Surface: tests. The zero-to-fleet onramp has no test file; regressions break new users silently.
Done-when: unit tests for `build_url`, `wait_for_health`, `wait_for_worker` et al. with a stubbed CP; failure paths covered; pytest green.

### R10. Tests for `service.py` — `open`
Surface: tests. 232 lines of systemd/launchd install logic, zero tests.
Done-when: subprocess boundaries mocked; unit/file-generation logic asserted for both systemd and launchd paths; pytest green.

### R11. Bound the log-append path mid-window — `open`
Surface: backend/robustness. *(Re-scoped 2026-06-05: `_prune_logs` already caps rows per job on a sweep cadence, roost/server.py:1416.)* Residual: between sweeps, unbounded POSTs can still bloat `job_logs` — no append-side size/rate cap.
Done-when: per-append size cap + per-job rate or row ceiling enforced at write time with a clear worker-side error; oversized-append test; pytest green.

### R12. Bare `except Exception` rollback guards — `invalid` *(closed 2026-06-05)*
Survey claim was wrong: the guards at roost/server.py:222, 513, 725, 984 roll back then **re-raise**, with comments explaining the "no transaction is active" masking they prevent. Intentional, correct, nothing to do.

### R13. Fixture drift guard for the mobile contract — `open`
Surface: mobile/tests. Contract verified in sync on 2026-06-05 (all 9 API.md endpoints match server.py) — but nothing *automated* signals when server response shapes drift from `mobile-app/fixtures/*.json`; today it takes a manual audit like that one.
Done-when: a pytest that round-trips live server responses against the golden fixtures' shapes (additive-only rule from API.md §7 enforced: new fields OK, removals/renames fail); wired into the default test run.

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
| Branch coverage of `roost/` | `coverage run -m pytest && coverage report` (dev-only dep, never shipped) | unset | up only |
| Docs drift | confirmed findings per full drift sweep | 0 (2026-06-05, 27-agent pass) | stays 0 |
| Runnable examples | every `examples/*.yaml` accepted by a scratch CP submit | unset | stays 100% |

## Proposed (loop appends here; only humans promote)

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
