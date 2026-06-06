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
