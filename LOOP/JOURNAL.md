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
- Branch/PR: loop/r1-docker-argv-hardening / (PR pending judge)
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
