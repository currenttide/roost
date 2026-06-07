# Loop journal

Append-only evidence log. One entry per iteration, format per `PROTOCOL.md`.
Entries are written by the loop; humans read, never need to edit.

---

## 2026-06-05 — pre-loop: repo truth pass + backlog audit (interactive session)
- Verdict: shipped (uncommitted, on feat/agent-substrate)
- Branch/PR: feat/agent-substrate / - (working tree, user reviewing)
- What changed: 27-agent map→verify→fix workflow corrected 8 stale prose spots
  (README.md ×3, server.py ×2, worker.py, service.py, schema.py — see git diff).
  Then the backlog itself was audited against the verified map: R5 and R12 closed
  as `invalid` (survey claims refuted by code), R14 closed as `done`, R1/R2/R3/R7/
  R11/R13 re-scoped to what the code actually lacks (details inline in BACKLOG.md).
- Evidence:
  - `python -m pytest -q` → 347 passed in 9.53s (after edits)
  - argv check: `_build_docker_argv` is exec-style w/ guards (refutes shell-injection claim);
    `prune_expired` wired at server.py:2395; rollback guards re-raise (server.py:222 etc.)
- Judge: - (predates the loop; fixes were verified by independent skeptic agents in the workflow)
- Notes: original single-agent survey overstated 3 of 14 ranked items — adversarial
  verification before implementation is earning its keep. Known dangling ref
  mobile-app/ios/README.md:137 deliberately left for I1 (merge heals it).

---

## 2026-06-06 04:30 UTC — I0: Prove the verification paths the loop will rely on
- Verdict: shipped
- Branch/PR: loop/i0-verification-plumbing / https://github.com/currenttide/roost/pull/7
- What changed: no product code — environment plumbing + backlog/journal bookkeeping.
  Admin auth fixed, CLI repointed to this checkout, Mac node proven with Xcode,
  artifact round-trip through the blob store verified byte-identical.
- Evidence:
  - `roost workers` → 16 nodes listed (was 401); mac-mini-m4 idle, seen 0s ago
  - `pip install -e .` → editable mapping now `/workspace/yang/roost-oss/roost` (was `/workspace/yang/agent_fleet`); `pip show roost` confirms; Python 3.12.8
  - `roost exec mac-mini-m4 "xcodebuild -version && xcrun simctl list devices | head -15"` → Xcode 26.2 / Build 17C52, device list returned, iPhone 17 Pro (Booted), exit_code=0
  - `POST /blobs/presign` → blob ab1499820fe3; Mac job: `xcrun simctl io booted screenshot` + `curl PUT` → `{"size":280802,"sha256":"5381727b…","state":"ready"}`; local download sha256 identical (`5381727b9276a9e730b84ce762d8e2032ccfecc8a0443186da1d7accfa1021d7`), `file` says PNG 1206x2622 — screenshot shows the Roost iOS app live against this fleet
  - `python -m pytest -q` → 347 passed in 9.46s (no code changed; ratchet held)
- Judge: approve (round 1) — re-ran pytest (347 passed in 9.29s), `roost workers`
  (16 nodes), editable-location check, artifact sha256 + `file`, CP healthz, and
  corroborated the Mac jobs via `roost jobs`; scope/honesty/deviation all passed.
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6
- Notes: DEVIATION (logged, not silent): PR targets `feat/agent-substrate`, not
  `master` — LOOP/ exists only on that branch until I1 merges it; a master-based
  PR would smuggle I1's whole merge in. Admin token lives at
  `~/roost-fleet/admin_token` → `~/.config/roost/config.toml` (0600), never in
  the repo. The CP self-reports v0.2.0 while pyproject.toml says 0.1.0 —
  version drift noted to Proposed.

## 2026-06-06 04:49 UTC — I1: Integrate outstanding feature branches into master
- Verdict: shipped
- Branch/PR: feat/agent-substrate → PR #8 (merged ece2d17); feat/mac-app → PR #5
  retargeted to master (merged 9d5a990); PR #4 auto-resolved MERGED (ancestor);
  PR #6 closed superseded (b7541e3 carries the same work)
- What changed: master now contains both lines — the agent-substrate stack (blob
  store, mobile thin clients, publish + roost.pub, MCP transfer tools, client
  tokens, LOOP harness) and the native SwiftPM mac app (pywebview PoC deleted).
  Conflict resolution: none needed — leg 1 was a fast-forward; leg 2's merge of
  master into feat/mac-app auto-resolved (mac-app's footprint is mac-app/ + its
  CI workflow + .gitignore Swift entries only).
- Evidence:
  - leg 1: `git merge-base --is-ancestor master 1dd840c` → FF; `python -m pytest -q` → 347 passed in 9.91s
  - leg 2 (merge result 998b399): `python -m pytest -q` → 347 passed in 9.65s; `PATH=/tmp/swift-toolchain/usr/bin:$PATH swift test` (mac-app/) → 30/30, 0 failures, Swift 6.0.3 Linux
  - pro-Swift check: mac-app/{panel_window.py,build.sh,launcher.sh} absent; SwiftPM layout present
  - final master (9d5a990): `python -m pytest -q` → 347 passed in 9.79s; mac-app/Package.swift+Sources+Tests present → mobile-app/ios/README.md:137 "see mac-app for the pattern" now resolves
- Judge: approve ×2 (one per merge leg, round 1 each) — leg 1: re-ran pytest
  (347), FF check, secrets scan (rst-mob-* are synthetic fixtures), 0 deletions,
  54 new test functions; leg 2: re-ran pytest (347) + swift test (30/30),
  verified PoC deletions and mac-app-only footprint, no secrets.
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (both legs)
- Notes: merges performed by the loop under the user's standing "Loop merges
  (always)" authorization granted 2026-06-06 (AskUserQuestion). I0's deviation
  is now healed — LOOP/ lives on master; this entry's PR targets master
  normally. feat/mobile-app branch left in place (PR closed with note).

## 2026-06-06 04:56 UTC — R1: Harden docker argv assembly against flag injection
- Verdict: shipped
- Branch/PR: loop/r1-docker-argv-hardening / https://github.com/currenttide/roost/pull/10
- What changed: new `_argv_value(what, value)` guard in roost/worker.py — rejects
  empty/whitespace-only and leading-dash (incl. whitespace-masked) values for every
  spec-sourced `docker run` argv position: image, gpus, cpus, memory, shm_size,
  network, workdir, each volumes entry. In-container `command` elements deliberately
  NOT restricted (they land after the image where docker stops flag parsing;
  leading dashes there are legitimate, e.g. ["ls", "-la"]) — rationale in a code
  comment. 12 new tests: the `image: "--privileged"` escalation, each container
  field, volume entries, whitespace-masking, empties, and a legit-spec regression.
- Evidence:
  - `python -m pytest -q` → 359 passed in 9.60s (was 347; +12 new, none removed)
- Judge: approve (round 1) — re-ran pytest (359 passed in 9.87s), verified no
  test deletions, confirmed all 8 argv positions guarded incl. the
  `container.image` fallback, probed adversarially (whitespace/newline/tab
  masks, bare "-", unicode dashes, NUL byte, env path) and found no bypass;
  command-exemption rationale verified empirically.
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (explicit
  `model: sonnet` override; the judge's verdict text dropped its mandatory
  first-line model ID — a same-override probe re-confirmed claude-sonnet-4-6;
  logged as a formatting slip, not a model substitution)
- Notes: scope held to Done-when; no drive-bys. Env keys/values were already
  positionally safe (consumed by `-e`) and policy-filtered by `_sanitize_env`.

## 2026-06-06 05:10 UTC — R2: Default runtime cap for jobs with no wallclock budget
- Verdict: shipped
- Branch/PR: loop/r2-default-runtime-cap / (PR pending judge)
- What changed: `_resolve_timeout(spec, policy)` in roost/worker.py — explicit
  budget wins; otherwise a per-kind default cap (command 120m / claude 240m /
  auto 240m / docker 360m, unknown kinds 240m), worker-policy-overridable via
  `default_wallclock_min` (scalar or {kind: minutes}; 0/negative = explicit
  unbounded opt-out). Default-cap kills report `default_runtime_cap_exceeded`
  (distinct from `wallclock_exceeded`), with apply/kill event logs telling the
  operator how to override. 15 new tests: 12 unit (incl. garbage budget/policy)
  + 3 driving the REAL run_job with stubbed network (default-cap kill, explicit
  budget kill, quick job unaffected).
- Evidence:
  - `python -m pytest -q` → 374 passed in 11.03s (was 359; +15, none removed)
  - live smoke (scratch CP :8799, enrolled worker w/ policy default_wallclock_min=0.03):
    - unbudgeted `sleep 30` → state `failed`, error `default_runtime_cap_exceeded` (~2s)
    - `sleep 30` + `max_wallclock_sec: 1` → `failed`, `wallclock_exceeded` (unchanged path)
    - unbudgeted `echo quick-ok` → `succeeded`
- Judge: approve (round 1) — re-ran pytest (374 passed in 11.12s) and the live
  smoke on its OWN scratch CP (:8798): default-cap kill, explicit-budget kill,
  quick-job success all reproduced; verified `_budget_remaining`/verify-phase
  semantics untouched, `roost exec` unaffected, zero-budget edge improved;
  judged the distinct-error-token interpretation defensible and the cap values
  (120/240/240/360m, overridable, opt-out) sound; R1 URL backfill ruled
  acceptable bookkeeping.
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (explicit
  `model: sonnet` override; verdict text again omitted the first-line model ID
  despite a strengthened instruction — re-confirmed claude-sonnet-4-6 by probe.
  Recurring formatting slip noted as an A4 debt: the judge prompt needs a
  structural fix, e.g. demand the ID in a fenced first block.)
- Notes: cap values are judgment calls (runaway breakers, not schedulers) —
  flagged for human review in the PR. R1's journal Branch/PR line backfilled
  with PR #10 in this commit (carry-over noted last iteration).

## 2026-06-06 05:31 UTC — R3: Reconcile still-running jobs after lease expiry + re-register
- Verdict: shipped
- Branch/PR: loop/r3-lease-reconciliation / (PR pending judge)
- What changed: semantics CHOSEN and documented — abort orphaned local work on
  reconcile (vs. report-and-dedupe), since the server has already requeued and a
  stale terminal report is rejected anyway. Server: heartbeat response gains an
  additive `owned` field (`_owned_job_ids`). Worker: (1) `_reconcile_owned` —
  after a successful heartbeat, kill active jobs the server no longer attributes
  to us, guarded by LEASE_LOST_GRACE=90s (> LEASE_TTL) so just-leased jobs are
  never reaped; (2) `_reap_stale_attempt` — a re-lease of a job we still run
  kills the stale attempt and waits for it to fully unwind before the new one
  starts (fixes the _active/_job_tasks job_id-key collision, which would have
  cross-wired the attempts' tracking); (3) teardown reasons extended:
  `lease_lost` joins `cancelled` as a report-nothing teardown, prints show the
  real reason. README documents the lease/outage behavior.
- Evidence:
  - `python -m pytest -q` → 379 passed in 11.71s (was 374; +5, none removed)
  - live smoke (scratch CP :8797, REAL 75s CP outage with `sleep 300` running, LEASE_TTL=60s):
    - during outage: job stayed `running` attempt 1 locally; on CP restart the sweeper had requeued it (`queued 1`)
    - worker heartbeat reconcile: `lease lost (server no longer attributes it to us); aborting local attempt` → `torn down (lease_lost)` (worker.log)
    - job re-leased and `running` attempt 2; old attempt-1 process group confirmed dead (pgrep), exactly one fresh process tree
    - no stale terminal event posted (attempt 2 untouched by attempt 1's teardown)
- Judge: approve (round 1) — re-ran pytest (379) + the 5 new tests, walked the
  race analysis (grace window vs heartbeat snapshot: no kill-healthy-work path),
  confirmed killpg closes the relay pipes so _reap can't hang, and ran its OWN
  live smoke (:8796) where the OTHER path fired first (`re-leased while a stale
  local attempt is still running` → torn down → attempt 2) — both reconcile
  mechanisms now verified live. One non-blocking nit: a malformed `owned`
  (non-iterable) would raise TypeError outside the except tuple — same
  exception scope as the pre-existing cancel path; noted, not fixed.
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (fenced
  model-ID block enforced this round — slip fixed; keep the fenced format)
- Notes: one test fix mid-iteration: outage-sim test originally left w1 online,
  and the placer kept preferring it — marking w1 offline (faithful to a real
  outage) fixed placement to w2. Wire change is additive (older workers ignore
  `owned`; older servers send none and the worker skips reconcile).

## 2026-06-06 22:34 UTC — R4: Escape job intent in verifier prompt
- Verdict: blocked (cut by human)
- Branch/PR: loop/r4-verifier-prompt-injection (deleted) / -
- What changed: nothing landed. Human direction mid-iteration: "delete r4, go
  straight to r5". Uncommitted work (50 lines of prompt-injection regression
  tests in tests/test_verify.py asserting FENCE_BEGIN/FENCE_END delimiters and
  marker redaction in render_user — implementation not yet written) discarded;
  branch deleted.
- Evidence:
  - `git checkout -- tests/test_verify.py && git branch -D loop/r4-verifier-prompt-injection` → clean master @ d150da1
- Judge: n/a — human cut precedes the judge gate.
- Models: implementer claude-opus-4-8 / judge n/a
- Notes: R5 was already closed `invalid` (2026-06-05 — prune_expired exists,
  roost/blobs.py:134, wired into the server sweep), so the next live Ranked
  item is R6 (publish from mobile). Bookkeeping rides the R6 branch (direct
  master push is permission-gated in this session).

## 2026-06-06 23:05 UTC — rescue: two live-CP fixes off the deleted R4 branch
- Verdict: shipped (PR open, awaiting HUMAN merge)
- Branch/PR: fix/cp-204-publish-router / https://github.com/currenttide/roost/pull/13
- What changed: nothing new — d150da1 (bare 204 on idle worker poll; was
  JSONResponse(204, content=None) → body b"null" w/ Content-Length 4) and
  af35c8d (publish host guard rewritten BaseHTTPMiddleware → pure-ASGI
  _PublicHostRouter; stops the ~12k/90min Content-Length crash storm) were
  riding the R4 branch but were never R4 scope. Rescued to their own branch
  before the human-ordered branch delete; remote R4 branch then deleted.
- Evidence:
  - `python -m pytest -q` (on the rescue branch) → 380 passed in 12.33s
- Judge: approve (round 1) — re-ran pytest (380), confirmed both bugs on
  master (server.py:2305 JSONResponse-204; :1585 BaseHTTPMiddleware), zero
  test deletions, and verified the ASGI router's security equivalence (a
  publish-domain Host can never reach API routes; test pins /workers → 404).
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6
- Notes: merge left to the human — the standing loop-merge authorization
  covers judge-approved backlog-item PRs, and the permission classifier
  (correctly) flagged this rescue as outside it. The rescue itself was forced
  by "delete r4" colliding with unmerged human fixes on that branch.

## 2026-06-06 23:20 UTC — R6: Publish from mobile: API + contract
- Verdict: shipped
- Branch/PR: loop/r6-mobile-publish / https://github.com/currenttide/roost/pull/14
- What changed: scope decision made explicit and PINNED — mobile+agent scopes
  share one client permission set (scope = audit label, server.py:~1353), so a
  mobile pair token already publishes; zero server changes. New
  test_mobile_scope_publishes_end_to_end drives upload→publish→list as the
  phone and pins DELETE at 403. API.md gains §6 Publish (staging → publish →
  list, error matrix, blob-TTL ≠ site-TTL) + extended §1 verb table.
  record_fixtures.py records the flow as the mobile token → 3 new goldens.
  iOS: BlobUploadResponse+Site, uploadBlob/publish/sites, testPublish.
  Android: StagedBlob+Site, parseBlob/parseSite/parseSites (+lngOrNull),
  uploadBlob/publish/sites, publishFlow. UI wiring deferred per Done-when.
- Evidence:
  - `python -m pytest -q` → 380 passed in 11.77s (was 379; +1, none removed)
  - iOS Linux: `ROOST_FIXTURES=… swift test` → 33 tests, 0 failures (was 32)
  - Android pure-layer: kotlinc + JUnitCore → OK (27 tests) (was 26)
  - Android full: `gradle :app:testDebugUnitTest` (JDK17) → 32 tests, 0 failures
- Judge: approve (round 1) — re-ran pytest (380), iOS (33/33), Android
  pure-layer (27 OK) itself; verified the scope test is a real end-to-end pin,
  fixture regen additive-only (no keys removed), API.md §6 cross-read against
  server.py:2135–2230, zero assertions removed, claims capped (no UI claim).
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (explicit
  `model: sonnet` override; verdict text omitted the fenced first-line model
  ID again — re-confirmed by same-override probe. The A4 debt from R2 stands:
  the fenced-ID instruction holds only intermittently.)
- Notes: the backlog's premise ("mobile scope can't reach publish") was
  refuted — the gap was contract+clients only; honest re-scope, not server
  work. Full fixture regen churns sibling fixtures (values-only; verified).
  Proposed: publish UI wiring (iOS/Android screens) as follow-up. PR #13
  (rescued CP fixes) still awaits the human; R6 branched before it — merge
  order is safe (no overlapping edits).

## 2026-06-06 23:55 UTC — R7: Atomic publish call
- Verdict: shipped
- Branch/PR: loop/r7-atomic-publish / https://github.com/currenttide/roost/pull/15
- What changed: Done-when option (a) — ONE transactional call. POST /publish
  dispatches on Content-Type: a non-JSON body + ?name= streams the tar.gz to a
  private temp file (`.upload-<slug>-<hex>` under sites/, removed in finally),
  extract_bundle installs atomically, and NO blob row ever exists — the
  dangling-blob flap window is eliminated structurally, not reconciled. JSON
  {blob_id,name?} two-step stays byte-compatible (worker presign path), now
  parsed manually (garbage JSON → clean 400, was framework 422). `roost
  publish` uses the one-shot call with a two-step fallback on 422 (pre-one-shot
  CPs reject a raw body with exactly 422; a current server can never emit it
  on this path — judge-verified). INTEGRATIONS.md + publish.py docstring
  updated. 10 new tests incl. failure injections (bad tar, oversized
  mid-stream, empty, bad/missing name) each asserting zero residue.
- Evidence:
  - `python -m pytest -q` → 390 passed in 12.14s (was 380; +10, none removed)
  - live smoke (scratch CP :8795, real CLI): one-shot publish → live at
    /pub/r7-live/, `GET /blobs` → [] (zero rows ever); garbage-body injection
    → 400 "not a valid tar.gz", sites dir holds ONLY r7-live; two-step
    blob→publish regression OK (slug from blob stem, served)
- Judge: approve (round 1) — re-ran pytest (390), ran its OWN live smoke
  (:8794: CLI one-shot, failure injection, two-step regression), probed
  adversarially: temp-file lifecycle covered on every failure path (finally
  placement vs async stream), dot-prefixed temp can't be served or collide
  with the slug regex, no other POST /publish callers exist (mcp/captain/
  worker clean), 422-fallback scoping proven sound, auth identical + body
  capped mid-stream at BLOB_MAX_BYTES.
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (fenced
  first-line model-ID block present this round)
- Notes: mobile one-shot parity deliberately NOT claimed — API.md §6 keeps
  the two-step as the documented mobile flow; parity filed to Proposed.
  R5's sweeper remains useful for the presign/worker path.

## 2026-06-07 00:50 UTC — R8: `schedule` verb (interval jobs)
- Verdict: shipped
- Branch/PR: loop/r8-schedule-verb / https://github.com/currenttide/roost/pull/16
- What changed: the biggest documented-but-missing verb now exists. Schema V12
  `schedules` table (fresh + V11→V12 additive migration). `_tick_schedules`
  rides the sweep loop in its own try/except: one job per due schedule per
  tick, NO back-fill (next_run_at advances whole intervals on the original
  cadence grid), NO pile-up (beat skipped while the previous job is in flight;
  clock advances), broken spec logs+skips; every run carries schedule_id in
  its spec. API: POST/GET/PATCH/DELETE /schedules (every = seconds|<N>[smhd],
  30s floor; spec shape shared with POST /jobs via new _validate_job_spec —
  submit_job refactored onto it; root jobs only; client tokens may manage,
  worker plane 403; re-enable restarts the clock). CLI `roost schedule`
  (goal → kind:auto task, --spec/--list/--rm/--enable/--disable). MCP
  `roost_schedule` tool. README + INTEGRATIONS.md document the semantics —
  the verb table row no longer punts to external cron.
- Evidence:
  - `python -m pytest -q` → 404 passed in 13.78s (was 390; +14, none removed)
  - live smoke (scratch CP :8793, sweeper ON, REAL `roost worker`): 30s
    schedule via CLI → beat 1 enqueued by the tick and RUN (succeeded,
    stdout `r8-beat-1780789486`, spec.schedule_id set) → beat 2 at +30s
    (2 jobs) → `--disable` held a 70s window at zero new beats → `--enable`
    restarted the clock ("next run in 30s") → `--rm` → "no schedules"
- Judge: approve (round 1) — re-ran pytest (404), ran its OWN live smoke
  (:8792, own worker: beat ran succeeded with provenance; disable/enable/rm
  exercised), verified the V11→V12 migration against a synthetic V11 DB and
  SCHEMA_V1/_SCHEDULES_DDL column parity, proved the cadence math can never
  yield next_run <= now (100k randomized cases + the on-grid edge), confirmed
  the _validate_job_spec refactor is byte-identical on all 9 shape cases,
  schedule_id extra key is harmless downstream (pydantic extra='ignore'),
  permissions hold (worker 403 / unauth 401 / client 200), interval immutable
  post-create.
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (fenced
  first-line model-ID block present)
- Notes: scheduling a plain goal uses the roost-do shape (kind:auto task) so
  scheduled runs get self-selection + the verifier. Mobile parity stays in
  Proposed (pre-existing entry). Cosmetic mid-iteration fix: CLI interval
  display rounded 30s to "0m" — _fmt_interval added before the judge round.

## 2026-06-07 01:15 UTC — R9: Tests for bootstrap.py (`roost up`)
- Verdict: shipped
- Branch/PR: loop/r9-bootstrap-tests / https://github.com/currenttide/roost/pull/17
- What changed: tests-only — new tests/test_bootstrap.py (24 tests, no
  processes/sockets). Pure helpers asserted by exact value (build_url
  0.0.0.0 rewrite, panel_url, is_loopback ±, config_payload `credential`
  contract, env_file_text, write_env_file 0600/mkdir/ROOST_HOME/overwrite,
  gen_admin_token, default_worker_name). Pollers run against a stubbed CP
  (httpx.MockTransport monkeypatched over bootstrap.httpx.Client): ping_ok
  auth-header present/absent + 500 + ConnectError; wait_for_health immediate /
  comes-up-late (exact call count) / timeout; wait_for_worker found /
  offline→busy awaited / worker_id filter / empty-list timeout / 401+transport
  flap tolerated / persistent-401 timeout.
- Evidence:
  - `python -m pytest -q` → 428 passed in 13.74s (was 404; +24, none removed)
  - new file alone: 24 passed in 0.36s (tiny poll intervals; no real sleeps)
- Judge: approve (round 1) — re-ran pytest (428) + the file in isolation
  (0.34s), confirmed tests-only scope (single added file), zero removed
  tests, and did mutation analysis: build_url verbatim-0.0.0.0 mutation and
  wait_for_worker drop-the-id-filter mutation are each caught by a specific
  test; no tautological tests found; monkeypatch fixture auto-undo rules out
  stub leakage.
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (fenced
  first-line model-ID block present)
- Notes: tests-only change → pytest gate is the full evidence-table
  requirement; no live smoke claimed. The `up` orchestration in cli.py
  remains untested (process spawning) — noted for a possible future item,
  not promised.

## 2026-06-07 01:40 UTC — R10: Tests for service.py
- Verdict: shipped
- Branch/PR: loop/r10-service-tests / https://github.com/currenttide/roost/pull/18
- What changed: tests-only — new tests/test_service.py (26 tests, zero real
  subprocesses: FakeRun records every systemctl/launchctl/loginctl/journalctl/
  tail argv; HOME redirected to tmp for all file writes). Covers
  _resolve_roost_bin's three-tier fallback, the _service_env allowlist (an
  AWS secret asserted NOT to propagate; empty values skipped), the systemd
  unit renderer (incl. the exact KEY=value quoting contract for
  space/backslash/quote env values), the launchd plist renderer validated via
  plistlib (multi-word bin → argv split; XML escaping round-trips), install()
  on linux/darwin/unsupported (file content + argv + rc propagation +
  systemctl-missing rc 2 with the unit still written), and
  start/stop/status/logs per platform with their failure paths.
- Evidence:
  - `python -m pytest -q` → 454 passed in 14.32s (was 428; +26, none removed)
  - new file alone: 26 passed in 0.07s
- Judge: approve (round 1) — re-ran pytest (454) + the file alone; verified
  single-file scope; ran three REAL mutation probes on /tmp copies (always-
  start install, dropped XML escape, swapped bootout/kickstart — all caught);
  safety-audited that no test can reach a live subprocess (every lifecycle
  test takes fake_run; helpers have no subprocess sites) or write outside
  tmp (home fixture on every file-writing path).
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (fenced
  first-line model-ID block present)
- Notes: with R9+R10 the two zero-test modules from the survey are closed.
  Remaining Ranked: R11 (log-append bounds), R13 (fixture drift guard).

## 2026-06-07 02:20 UTC — R11: Bound the log-append path mid-window
- Verdict: shipped
- Branch/PR: loop/r11-log-append-bounds / https://github.com/currenttide/roost/pull/19
- What changed: write-time bounds on job_logs (the sweep prunes only every
  ~30min). Per-append cap LOG_APPEND_MAX_BYTES=64KiB (bytes, not chars) →
  413 with a clear client-side instruction; per-job row ceiling (reuses
  LOG_MAX_ROWS_PER_JOB=5000) inside the append transaction → 429, stdout/
  stderr only — lifecycle `event` rows exempt so the terminal divider
  survives stdout spam. A rejected append still bumps last_activity_at
  (capped ≠ stuck) and still heartbeats the worker. Oversize EVENT payloads
  slimmed to parseable {"type","truncated":true}, never rejected (the state
  change already happened). Worker _send_log now surfaces 4xx with detail.
  BONUS BUG (found BY the live smoke): one line > asyncio's 64KiB stream
  limit raised ValueError out of readline() and KILLED the relay task,
  silently losing all later output — both relay sites now drop the line
  with a loud marker and keep relaying.
- Evidence:
  - `python -m pytest -q` → 459 passed in 15.31s (was 454; +5, none removed)
  - live smoke (scratch CP :8791 + real worker): 70000-char line job →
    "oversized output line dropped" event + "after-line" stdout row +
    succeeded (pre-fix run on old code: line vanished, NO marker — the bug);
    direct posts: 413 oversize, 429 at exactly append #5000 (real 5000-row
    spam, 51.5s), terminal event 200 at the ceiling
- Judge: approve (round 1) — re-ran pytest (459), ran its OWN live smoke
  (:8790: marker + after-line + succeeded; 413 at 65537 bytes; 200 at exactly
  65536; 413 on multibyte overrun), probed the COMMIT-then-raise transaction
  state (no 'no transaction active' path), COUNT(*) cost (indexed, bounded by
  the ceiling), the event-exemption tradeoff (documented, proportionate to
  the fleet-worker threat model), CPython readline ValueError semantics, and
  the mobile contract (read path unchanged).
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (fenced
  first-line model-ID block present)
- Notes: the relay crash was only discoverable live — the unit suite never
  pushes a 64KiB line through a real pipe. Evidence-table discipline
  (live smoke for behavior changes) is earning its keep.

## 2026-06-07 02:55 UTC — R13: Fixture drift guard for the mobile contract
- Verdict: shipped
- Branch/PR: loop/r13-fixture-drift-guard / https://github.com/currenttide/roost/pull/20
- What changed: record_fixtures.py refactored — the canonical scenario now
  lives in capture(db_path) → {fixture_name: payload}; main() writes goldens
  from it (regen verified values-only; goldens NOT regenerated in this PR).
  New tests/test_fixture_drift.py (23 tests): per-golden parametrized shape
  comparison, additive-only per API.md §8 — extra live keys pass; removed/
  renamed keys, JSON-type changes, and non-null→null fail with path-precise
  messages + the regen command. Best-fit list matching (ordering ≠ drift),
  bool-before-int typing, null-pins-existence, orphaned-golden detection,
  SSE event-vocabulary coverage, and self-tests for the checker itself.
- Evidence:
  - `python -m pytest -q` → 482 passed in 15.43s (was 459; +23, none removed)
  - negative check: fake key injected into healthz.json → guard failed with
    "$.future_field_the_server_dropped: REMOVED (additive-only contract)";
    golden restored, fixtures dir clean
- Judge: approve (round 1) — re-ran pytest (482) + the file alone; verified
  goldens untouched in the PR and regen equivalence by ACTUALLY regenerating
  (values-only) then restoring; ran its OWN two negative checks (injected key
  → REMOVED; ok:true→"yes" → type change); probed best-fit list matching
  with a field missing from EVERY live element (still caught per golden
  element — not bypassable by reordering); capture() measured 0.30s/session.
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (fenced
  first-line model-ID block present)
- Notes: RANKED IS NOW DRY — R1-R3 done pre-cut, R4 cut, R5/R12 invalid,
  R6-R11+R13+R14 done. Next iteration runs the Replenishment protocol
  (PROTOCOL.md): survey A4 journal debts + A5 ratchets first, then A3 drift
  sweep scoped to changes since the last sweep, A2 coverage, A1 bug hunt.
  Known A4 candidates already journaled: judge fenced-ID instruction holds
  only intermittently (R2 note); `roost up` orchestration in cli.py untested
  (R9 note). A5: branch-coverage ratchet has baseline unset (measure-only
  iteration available).

## 2026-06-07 03:30 UTC — Replenishment cycle #1
- Verdict: shipped (slate promoted; no implementation this iteration)
- Branch/PR: loop/replenish-1 (bookkeeping) / (PR on land)
- What changed: Ranked refilled with 3 judge-approved Tier A items, tagged
  self-promoted: R15 (A3 — confirmed drift: cli.py publish docstring still
  says blob-store; omissions: README default caps, API.md log bounds),
  R16 (A4 — R9's journaled debt: `roost up` orchestration untested, cli.py
  28% branch), R17 (A2 — config.py 48% / triage.py 67%, no dedicated test
  files). Ratchet baselines RECORDED (A5 first-measure): branch coverage of
  roost/ = 63% TOTAL (482 tests); runnable examples = 3/3.
- Evidence (survey):
  - A3: two Explore agents swept everything merged since the 2026-06-05
    sweep (PRs #10–#20 surfaces) → 1 confirmed drift + 2 omissions; schedule
    + publish contract + log read-path docs otherwise clean. One claimed
    finding REJECTED in triage: "API.md §6 should present one-shot publish"
    is the deliberate human-gated Proposed item, not drift.
  - A2/A5: `coverage run --branch -m pytest` → 482 passed, TOTAL 63%
    (cli.py 28%, config.py 48%, worker.py 55%, mcp.py 59%)
  - A5: scratch CP :8789, `roost submit --detach` per examples/*.yaml →
    3/3 accepted (job ids returned)
  - A1: not surveyed — slate filled from cheaper sources per protocol.
- Judge: slate approved (round 1) — re-verified the cli.py docstring drift
  against the code, the README/API.md omissions against worker.py/server.py
  constants, `up`'s zero test reach + the 28% figure, config/triage coverage
  + missing test files, and re-ran the full coverage measurement itself
  (TOTAL 63%, exact match). Confirmed the §6 exclusion correct (contract
  change → stays Proposed/human) and S1's API.md note additive, not a
  contract change. Slate = exactly 3, within the cap.
- Models: surveyor claude-opus-4-8 / judge claude-sonnet-4-6 (fenced
  first-line model-ID block present)
- Notes: baselines recorded in the same cycle (the A5 "first iteration
  measures and records" compressed into replenishment since the judge had
  already re-verified the numbers — logged here, not silent). A1 bug-hunt
  rotation untouched; first hunt area when needed: matcher/placement (never
  hunted). Human notification in the session summary.

## 2026-06-07 04:10 UTC — R15: Fix confirmed docs drift (publish docstring, default caps, log bounds)
- Verdict: shipped
- Branch/PR: loop/r15-docs-drift / https://github.com/currenttide/roost/pull/22
- What changed: docs-only, three spots. cli.py publish docstring now
  describes the one-shot transactional POST (+ automatic 422 fallback);
  README gains a "Budgets & runtime caps" paragraph (per-kind defaults
  120/240/240/360m, distinct default_runtime_cap_exceeded token,
  default_wallclock_min override incl. {kind: minutes} / 0 = opt-out);
  API.md §4 gains an additive "Log bounds" note (64KiB/line + drop marker,
  5000-row stdout/stderr cap, event exemption, capped ≠ stalled guidance).
- Evidence:
  - `python -m pytest -q` → 482 passed in 15.65s (docs-only; fixture drift
    guard unaffected — prose, not shapes)
- Judge: approve (round 1) — re-ran pytest (482), confirmed exactly three
  files / docstring-only cli.py diff, and TRUTH-CHECKED every new claim
  against code (cap values vs DEFAULT_WALLCLOCK_MIN, error token line,
  policy override semantics, LOG_* constants, worker drop-marker text,
  ~24h retention). One nit accepted: README says "0 opts out" where code
  treats any <=0 the same — degenerate input, not meaningful drift.
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (fenced
  first-line model-ID block present)
- Notes: Docs-drift ratchet honestly back to 0. Next: R16 (up-orchestration
  tests), then R17 (config/triage tests).

## 2026-06-07 04:55 UTC — R16: Tests for the `roost up` orchestration
- Verdict: shipped (judge round 2)
- Branch/PR: loop/r16-up-tests / https://github.com/currenttide/roost/pull/23
- What changed: tests-only — new tests/test_up.py (13 tests, no processes/
  sockets; _spawn_detached records argv, service.install / enroll /
  _smoke_test / _worker_already_running stubbed, bootstrap pollers patched,
  HTTP via MockTransport, ROOST_CONFIG_DIR+ROOST_HOME → tmp). Covers the
  fresh-CP full path (serve argv + minted token, detached worker when
  isolated, enroll → config/env persistence incl. the real
  enroll-replaces-credential behavior), reuse/skip paths, supervised-service
  branch + detached fallback, and the failure paths (explicit-url
  unreachable, CP never healthy, enroll 403, worker never registers, smoke
  warns-but-exit-0), plus _roost_argv and _smoke_test units.
- Evidence:
  - `python -m pytest -q` → 495 passed in 16.04s (was 482; +13, none removed)
  - coverage: cli.py 28% → 36% branch, TOTAL 63% → 65% (ratchet up)
- Judge: revise (round 1) → approve (round 2). ROUND-1 CATCH (real, safety):
  the two non-isolated service tests delenv'd ROOST_CONFIG_DIR without
  pinning XDG_CONFIG_HOME, so the orchestration's config writes CLOBBERED
  the operator's real ~/.config/roost/config.toml — the judge proved it
  empirically. INCIDENT: my earlier local runs had already done exactly
  that; the live fleet config was restored from ~/roost-fleet/admin_token
  and re-verified (`roost workers` lists the fleet). Fix: _deisolate_config_dir
  pins XDG_CONFIG_HOME→tmp BEFORE the delenv. Round 2 re-proved with sha256
  before/after two runs (byte-identical) + a default_config_path() probe.
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (fenced
  first-line model-ID block present, both rounds)
- Notes: the judge gate caught a test-suite side effect the implementer ran
  TWICE without noticing — exactly the failure mode the different-model
  judge exists for. Real-config hygiene now has a reusable helper + loud
  comment. Next: R17 (config/triage tests, last Ranked item).

## 2026-06-07 05:30 UTC — R17: Tests for config.py + triage.py
- Verdict: shipped
- Branch/PR: loop/r17-config-triage-tests / https://github.com/currenttide/roost/pull/24
- What changed: tests-only — tests/test_config.py (22 collected: path
  precedence ROOST_CONFIG_DIR→XDG→HOME, round-trips through the REAL toml
  parser for every supported type, 0600 contract, truncate-on-overwrite,
  hostile-string escaping incl. a TOML-injection attempt asserting no keys
  added, TypeError paths, resolve_url_token full flag→env→config→default
  chain) + tests/test_triage.py (8 collected: placeholders, exact machine-
  spec lines for GPU/CPU-only shapes, decline sentinel + FINAL-line protocol
  + no-delegation rule, fleet rows, empty-snapshot, 20-row cap, graceful
  degradation). R16's lesson applied: every default-path test pins env to
  tmp; judge hash-verified the real config untouched.
- Evidence:
  - `python -m pytest -q` → 525 passed in 15.69s (was 495; +30, none removed)
  - coverage: config.py 48% → 95-97%, triage.py 67% → 100%, TOTAL ≥65%
    (judge measured 68%); no module down (bootstrap/service hold 100%)
- Judge: approve (round 1) — re-ran both gates, hash-checked
  ~/.config/roost/config.toml across the run (byte-identical), ran a REAL
  mutation probe on a /tmp copy (removing quote-escaping from _toml_escape
  → TOMLDecodeError → test fails: not tautological), verified triage
  assertions quote actual rendered text. Two non-blocking notes: my claimed
  21+9 split was actually 22+8 collected (total 30 correct); one triage
  assertion uses a weaker disjunction fallback.
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (fenced
  first-line model-ID block present)
- Notes: RANKED DRY AGAIN — replenishment cycle #2 next wake. Survey order
  per protocol: A4 debts (judge fenced-ID flakiness largely self-resolved;
  none new), A5 ratchets (coverage 63%→~68% already up; examples 3/3),
  A3 drift sweep (scope: PRs #21–#24, small), then A2 (next-worst modules:
  worker.py 55%, mcp.py 59%, schema.py 60%) and A1 (first hunt area:
  matcher/placement — never hunted).

## 2026-06-07 06:15 UTC — Replenishment cycle #2 (first A1 bug hunt)
- Verdict: shipped (slate promoted; no implementation this iteration)
- Branch/PR: loop/replenish-2 (bookkeeping) / (PR on land)
- What changed: Ranked refilled with 3 judge-approved Tier A items from the
  FIRST A1 bug hunt (area: matcher/placement — rotation start), all with
  reproducing tests that FAIL on master (/tmp/a1-repro/test_a1_findings.py,
  4 failed): R18 matcher non-numeric-vs-numeric false positive ("N/A"
  passes "!=0"); R19 decline/requeue bookkeeping (grace window permanently
  bypassed after one decline + declines consume the attempt budget so real
  executions get zero retries); R20 prefer-by-name silently ignored
  (target resolves id|name, prefer only id).
- Evidence (survey):
  - A4 clean; A5 all moving (coverage 63→~68, examples 3/3, drift 0);
    A3 trivially clean (PRs #21–#24 = bookkeeping/docs-fix/tests only).
  - A1: two adversarial finder agents (matcher lens + placement lens),
    every claim REPRODUCED by running code before reporting; 4 confirmed
    bugs, 7+ hypotheses honestly cleared (capacity gating, target
    isolation, escalation, decliner bookkeeping all verified correct).
  - Reproducing tests: 4 failed on master in 0.56s — the qualifying bar.
- Judge: slate approved (round 1) — re-ran the failing tests (exactly 4),
  source-verified R18 (string-branch fallthrough) and R19 (requeue UPDATE
  keeps created_at; attempt++ never undone; sweeper kills at
  attempt>=max_attempts), confirmed no doc anywhere blesses the current
  behavior, grouping of R19's two bugs legitimate (one code region), all
  three user-reachable in normal operation. One judge note: the R20 repro
  was written against a proposed signature (TypeError before assertion) —
  re-anchor it to the real placement_score signature when implementing.
- Models: surveyor claude-opus-4-8 (+2 sonnet finder agents) / judge
  claude-sonnet-4-6 (fenced first-line model-ID block present)
- Notes: A2 candidates (worker.py 55%, mcp.py 59%, schema.py 60%) deferred
  — bug fixes outrank coverage; they're first in line for cycle #3.
  Hunt rotation record: #1 matcher/placement (4 findings). Next areas:
  blobs/publish serving, worker executors, captain/steward.

## 2026-06-07 06:50 UTC — R18: Matcher — non-numeric caps vs numeric constraints
- Verdict: shipped
- Branch/PR: loop/r18-matcher-numeric / https://github.com/currenttide/roost/pull/26
- What changed: _check_one — when the rhs parses as a number, a non-numeric
  capability now fails ALL operators (was: fell through to string compare, so
  gpu_vram_gb:"N/A" passed "!=0"); the string fallback fires only when BOTH
  sides are non-numeric (hostname pins preserved, incl. numeric-looking cap
  vs string rhs). Proactive same-class hardening: _as_number rejects
  non-finite floats ("nan" passed "!=0" since nan!=0 is True; "inf" passed
  any ">=") via math.isfinite. 5 new tests incl. the A1 repro trio, the full
  6-operator matrix, coercion preservation, pin preservation, nan/inf.
- Evidence:
  - `python -m pytest -q` → 530 passed in 15.69s (was 525; +5, none removed)
  - original A1 repro test now PASSES (failed on master pre-fix)
- Judge: approve (round 1) — re-ran both gates + the original repro, ran 15
  adversarial bypass probes (whitespace-masked rhs, unicode digits, hex,
  scientific notation, +/- signs, bool/list/None caps — all correctly
  handled), verified fleet safety (real worker probes emit gpu_vram_gb as
  round(float,1), never strings — no legitimate placement changes) and that
  no caller outside matcher.py depends on the old fallthrough.
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (fenced
  first-line model-ID block present)
- Notes: next R19 (decline/requeue bookkeeping — needs live smoke).

## 2026-06-07 07:35 UTC — R19: Decline/requeue bookkeeping
- Verdict: shipped
- Branch/PR: loop/r19-decline-bookkeeping / https://github.com/currenttide/roost/pull/27
- What changed: (a) V13 adds jobs.requeued_at, set on decline-requeue; the
  placement-grace clock runs from COALESCE(requeued_at, created_at) so a
  decline hands the job back for a FRESH competitive round while created_at
  stays truthful. DESIGN DEVIATION (logged): the /tmp repro asserted
  created_at-reset; the chosen semantics supersede it — behavior verified
  live instead. Interaction the fix exposed (3 existing tests caught it):
  the decliner still counted in best_other, so a worse-fit poller deferred
  to a competitor that can never take the job — decliners now excluded from
  the competing set (fast handoff among equals preserved; better-fit
  non-decliners get their window). (b) decline-requeue refunds the attempt
  (MAX(0, attempt-1)) — declines no longer eat the retry budget; late
  events from the decliner are still rejected by worker-ownership.
- Evidence:
  - `python -m pytest -q` → 533 passed in 16.03s (was 530; +3, none removed;
    all 7 existing decline tests green)
  - live smoke (scratch CP :8788, real wire, 3 synthetic workers): the
    hunt's exact Bug-A scenario — decliner grabs via anti-starvation →
    declines → worse-fit immediate poll gets 204 (window LIVE; pre-fix it
    got the job) → preferred worker takes it at attempt 1 (refunded)
- Judge: approve (round 1) — re-ran pytest (533) + the A1 repro file
  (matcher/attempt PASS, prefer-by-name fails as expected for R20, grace
  repro fails ONLY on the superseded created_at design — verified), ran its
  OWN Bug-A smoke + a synthetic V12→V13 migration (column + version,
  idempotent), probed: sweeper attempt arithmetic across
  decline→run→expiry correct, stale-attempt 403 with reused numbers,
  escalation path intentionally no-refund, decliner exclusion can't
  promote a worse worker over a better non-decliner, mobile contract
  additive (drift guard 23/23 in the run).
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (fenced
  first-line model-ID block present)
- Notes: lease-expiry requeue deliberately does NOT restart the grace
  window (real failures ≠ declines) — the analog question filed to
  Proposed. Next: R20 (prefer-by-name, last Ranked item).

## 2026-06-07 08:05 UTC — R20: prefer-by-name parity with target
- Verdict: shipped
- Branch/PR: loop/r20-prefer-by-name / https://github.com/currenttide/roost/pull/28
- What changed: two-layer fix. (1) placement_score: `preferred` (dict or
  bare-string form) matches worker id OR name — parity with target.
  (2) The deeper half the e2e test caught: the server's row lifts
  (_score_worker_row + the other_rows SELECT) never carried `name` at all,
  so the bonus couldn't fire server-side even with (1) — the routing test
  failed on the unit fix alone. README prefer line now documents <id|name>.
- Evidence:
  - `python -m pytest -q` → 535 passed in 16.93s (was 533; +2, none removed)
  - unit: by-name == by-id == bare-string == base+1000 exactly; non-match=0
  - e2e (real wire via TestClient): non-preferred poll inside the grace
    window → 204; the worker preferred BY NAME takes the job
- Judge: approve (round 1) — re-ran both gates; confirmed the /tmp repro
  still TypeErrors (wrong signature, as the slate judge flagged) and the
  re-anchored in-suite unit test pins the same property; probed NULL-name
  workers (no false bonus), prefer=None guard, name/ID ambiguity (explicit
  parity with target's same ambiguity, soft hint anyway), and confirmed
  exactly two _score_worker_row call sites both updated.
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (fenced
  first-line model-ID block present)
- Notes: A1 hunt #1 fully consumed — all 4 reproduced bugs fixed (R18-R20).
  RANKED DRY → replenishment cycle #3 next wake; A2 coverage items
  (worker.py 55%, mcp.py 59%, schema.py 60%) are first in line per the
  cycle-#2 deferral; A1 rotation continues at blobs/publish serving.

## 2026-06-07 02:52 UTC — Replenishment cycle #3 (blob/publish security hunt)
- Verdict: shipped
- Branch/PR: loop/replenish-3 / https://github.com/currenttide/roost/pull/29
- What changed: Ranked refilled with 3 judge-approved Tier A items from A1
  hunt #2 (blobs/publish), all backed by failing reproductions: R21 makes
  presigned PUT URLs single-use/race-safe; R22 rolls back failed direct blob
  uploads; R23 counts every archive entry against the publish extraction cap.
- Evidence:
  - `python -m pytest -q LOOP/repro-a1-hunt2.py` → 3 failed in 0.60s,
    exactly one per promoted defect and for the claimed reason
  - `python -m pytest -q` → 535 passed in 15.79s
  - `git diff --check` → clean
- Judge: approve (3 substantive approvals). Every round independently reran
  both gates (3 failed / 535 passed) and confirmed separate A1 classification,
  scope, and Done-when sufficiency. AUDIT DEVIATION: Claude Code omitted the
  mandatory first textual `MODEL-ID` line despite three explicit attempts;
  round 2's machine-readable `modelUsage` records `claude-sonnet-4-6`, and
  every invocation pinned `--model claude-sonnet-4-6`. The deviation is
  recorded rather than silently represented as format-compliant.
- Models: implementer gpt-5 (Codex) / judge claude-sonnet-4-6
- Notes: A4 had no new debt; A3 over R18-R20 found no undocumented drift.
  A2 remains worker.py 55%, mcp.py 59%, schema.py 60%, deferred again because
  confirmed security bugs take priority. A coverage run started during the
  survey did not terminate after several minutes and was killed; its report
  snapshot was not used as completion evidence. Hunt rotation advances next
  to worker executors. R21 is the top next iteration. The machine clock read
  02:52 UTC while the inherited preceding entry says 08:05 UTC, so append
  order is intentionally truthful rather than timestamp-sorted.

## 2026-06-06 20:50 UTC — Replenishment cycle #4 (A1 worker-executor hunt + A6 product gap survey)
- Verdict: shipped (slate promoted; implementation begins this iteration)
- Branch/PR: carried on R24 branch
- What changed: Ranked refilled with 3 judge-approved Tier A1 items from A1 hunt #3
  (area: worker executors — rotation continues). Also ran first A6 product gap
  survey (new source added to PROTOCOL.md this cycle per human direction).
  A1 hunt initially stalled as a background agent (226 lines, no result); relaunched
  as a foreground call, completed with 5 confirmed bugs + reproducing tests all
  failing on master. A6 survey found 4 promotable items + 2 newly-unblocked Proposed
  items; deferred (cap reached by A1 bugs — bugs outrank product gaps).
  PROTOCOL.md updated to add A6 source and revise Tier B boundary.
- Evidence (A1 survey):
  - `python -m pytest -q tests/test_judge_r4_bugs.py` → 3 failed in 30.73s (exactly
    the three promoted bugs: Bug5, Bug1, Bug4 — all FAIL on master as claimed)
  - `python -m pytest -q` → 537 passed in 15.65s (baseline clean)
- Evidence (A6 survey):
  - roost/mcp.py:129 — `roost_submit` enum confirmed `["claude","codex","docker"]`, `"auto"` absent
  - docs/INTEGRATIONS.md — tool table confirmed 9 of 16 tools; 6 absent by name
  - roost/cli.py:1916, :1029 — `history` and `prune-workers` confirmed absent from README
- Judge: A1 slate approved (round 1, claude-sonnet-4-6) — re-ran baseline (537),
  independently verified all 3 bug claims by reading source, confirmed 3 repro tests
  fail on master with precise error messages. A6 slate also judge-approved (round 1)
  but deferred per 3-item cap; noted in Proposed for cycle #5 promotion without re-judging.
- Models: surveyor claude-opus-4-8 / judge claude-sonnet-4-6 (fenced MODEL block present)
- Notes: A1 hunt rotation record: #1 matcher/placement (R18-R20), #2 blobs/publish
  (R21-R23), #3 worker executors (R24-R26 promoted; Bugs 2+3 deferred to Proposed).
  Next hunt area: captain/steward. A6 items in Proposed tagged "cycle #4 judge-approved"
  for fast-track promotion in cycle #5. Human direction this cycle: expanded loop to
  include A6 product gap source — first run surfaced headline gap (kind:auto missing
  from roost_submit schema and mobile API.md).

## 2026-06-06 21:30 UTC — R24: Auto job crash after decline marker misclassified as `declined`
- Verdict: shipped
- Branch/PR: loop/r24-auto-decline-misclassification / https://github.com/currenttide/roost/pull/31
- What changed: one-line guard in `run_job` — `elif declined:` → `elif declined and exit_code == 0:`.
  A triage subprocess that emits ROOST_DECLINE: then crashes (non-zero exit) now correctly
  reports `type="failed"` instead of `type="declined"`. The distinction matters: `declined`
  tells the CP to requeue on another node; without the fix, a crashing triage process causes
  an infinite retry loop across the fleet. A code comment explains the invariant.
- Evidence:
  - `python -m pytest -q` → 538 passed in 15.35s (was 537; +1 new test)
  - New test `test_auto_job_crash_after_decline_marker_reported_as_failed`: mocks kind:auto
    subprocess emitting the marker then exiting 1; asserts `type="failed"`
- Judge: approve (round 1, claude-sonnet-4-6) — re-ran pytest (538), independently verified
  the priority chain at worker.py:1882, confirmed fix correct and minimal; no existing
  test deletions; all 7 decline tests green.
- Models: implementer claude-opus-4-8 / judge claude-sonnet-4-6 (fenced MODEL block present)
- Notes: replenishment bookkeeping and PROTOCOL.md A6 addition ride this commit. Next: R25
  (_running/_active leak on cancel).

## 2026-06-07 03:01 UTC — R21: Make presigned blob PUT single-use and race-safe
- Verdict: shipped
- Branch/PR: loop/r21-presigned-put-single-use / https://github.com/currenttide/roost/pull/30
- What changed: presigned PUT now atomically claims `pending → uploading`,
  streams to a private temp file, atomically installs it, then conditionally
  finalizes metadata. Replay/concurrent losers get 409; failure removes partial
  content and releases the claim without touching already-ready blobs.
- Evidence:
  - `python -m pytest -q` → 537 passed in 15.78s
  - `python -m pytest -q tests/test_blobs.py` → 13 passed in 1.15s
  - scratch CP :8794 → first PUT 200, replay 409, GET returned
    `trusted-live-bytes`, downloaded SHA-256 matched first-PUT metadata
- Judge: approve (round 1) — independently ran 537 passed in 15.99s,
  13 focused tests in 1.22s, the original R21 repro (passed), and its own
  fresh scratch-CP smoke (200 then 409; bytes/size/hash unchanged). Reviewed
  SQLite claim atomicity, temp/final visibility, failure cleanup, and the
  forced-concurrency test. Claude Code again omitted the requested first
  textual model-ID line; machine-readable `modelUsage` records the pinned
  `claude-sonnet-4-6`.
- Models: implementer gpt-5 (Codex) / judge claude-sonnet-4-6
- Notes: R22 and R23 remain open and untouched; R22 is now top Ranked.
