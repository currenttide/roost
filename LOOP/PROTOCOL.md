# Loop protocol

Rules for the autonomous improvement loop running over this repo. The loop is hands;
the human (via `BACKLOG.md`) is direction. Every iteration follows this protocol
exactly — deviations get logged in the journal, not silently taken.

## Iteration shape

1. **Read** `LOOP/BACKLOG.md` and the tail of `LOOP/JOURNAL.md` (resume any item
   left `in-progress`).
2. **Pick** the top unblocked item from the Ranked section. One item per iteration.
   Never pick from Proposed — items reach Ranked only via the human or the
   Replenishment protocol (below). If Ranked has no unblocked items, run
   Replenishment instead of an implementation iteration.
3. **Branch** from `master`: `loop/<item-slug>`.
4. **Implement** the item, scoped to its Done-when. No drive-by refactors outside
   the item's scope.
5. **Verify** per the evidence table below. Run the full gate, not a subset.
6. **Judge (autoreview)**: spawn an independent judge subagent on a model that
   **differs from the implementer's** — pass it explicitly on the Agent call
   (`model: sonnet` while the loop runs on Opus; `model: opus` if the session is
   ever on Sonnet; never rely on inheritance, which silently yields the same
   model). The judge gets the backlog item (including its Done-when), the diff,
   and the draft journal entry, and **must state its exact model ID** (as its
   system prompt reports it) at the top of every verdict — the journal and PR
   record both model IDs, so a same-model judging is visible in the audit trail,
   not silent. The judge:
   - **re-runs the evidence gate itself** (at minimum `python -m pytest -q`,
     plus the surface's gate from the table below) — it never trusts pasted output;
   - checks scope: the diff serves the item, no drive-by changes;
   - checks honesty: no tests weakened/skipped/deleted, claims stay within the
     evidence-table caps, Done-when actually met;
   - returns a structured verdict: `approve` | `revise: <findings>` | `reject: <why>`.
   Address `revise` findings and re-judge. Three rounds without `approve` →
   mark the item `blocked: judge` and journal the disagreement honestly
   (optionally pull in the Codex rescue reviewer as a tiebreak). The implementer
   never overrules the judge silently.
7. **Land**: commit, push, open a PR to `master` titled with the backlog item ID.
   PR body includes the evidence (commands + output tails, artifacts) and the
   judge's verdict.
8. **Journal**: append an entry (format below) with verdict
   `shipped` / `failed` / `blocked` / `in-progress`.
9. **Update backlog**: mark the item's status. New ideas discovered while working
   go to Proposed — never self-promote them.

## Evidence table — claims are capped at what was actually run

| Surface | Required gate | Claim allowed |
|---|---|---|
| Backend (`roost/`) | `python -m pytest -q` fully green + new tests for new behavior | "works" |
| Backend behavior change | additionally: live smoke against a scratch control plane | "works" |
| RoostKit / mobile pure logic | Linux Swift/Kotlin test harness green (see module READMEs) | "works" |
| mac-app UI / iOS simulator | Roost job on the Mac node: build + test + `simctl` screenshot returned as artifact | "works" (artifact linked in PR) |
| mac-app, Mac unreachable | Linux type-check of RoostKit only | "compiles, needs-mac-verify" — item stays open |

## Honesty rules

- **Evidence or it didn't happen.** Every claim in the journal/PR cites the exact
  command and an output tail. No "tests pass" without the run.
- A feature with no test is not shipped. Write the test in the same PR.
- Never weaken, skip, or delete a test to get green. If a test is genuinely wrong,
  fixing it is its own justified change, flagged loudly in the journal.
- Log failures and blocks as faithfully as wins. A `blocked` entry is the protocol
  working, not a problem to hide.
- Do not mark a backlog item done unless its Done-when is met end to end.

## Anti-churn rules

- One backlog item per iteration; small diffs.
- No refactoring for its own sake — refactors must be a ranked backlog item.
- When the Ranked section has no unblocked items: run the Replenishment
  protocol below — never invent work outside it.

## Replenishment — the continuous-improvement engine

When Ranked runs dry, the loop does not stop: it runs a replenishment cycle to
refill Ranked from renewable, objectively-checkable sources. This is what makes
the loop perpetual — and the tier system is what keeps perpetual from becoming
churn.

### Tier A sources (self-promotable — each is verifiable, and renewable)

- **A1 — Bug hunt**: adversarial finder agents sweep an area for suspected bugs.
  A finding only becomes a backlog item once a **reproducing test is written and
  fails** on current code. The fix makes it pass. No failing test → not a bug →
  at most a Proposed note. This source never exhausts: each hunt picks a
  different area/lens (correctness, concurrency, error paths, input validation).
- **A2 — Coverage gaps**: untested modules/functions, measured (not estimated).
  New tests must assert real behavior — the judge rejects assertion-free or
  tautological tests.
- **A3 — Drift sweep**: re-run the map→verify docs-truth pass over areas changed
  since the last sweep (`git log` since the journal's last sweep entry). Human
  commits and merged PRs continuously create new drift; sweep cost scales with
  what actually changed.
- **A4 — Journaled debts**: anything the loop's own journal recorded as
  deferred, skipped, or needs-follow-up.
- **A5 — Ratchets**: the human-owned **Ratchets** table in `BACKLOG.md` — monotone,
  measurable quality metrics (e.g. branch coverage of `roost/`). The loop may
  always take an iteration that strictly improves a listed ratchet without
  regressing any other. First iteration on a new ratchet just measures and
  records the baseline. The loop may *propose* new ratchets (Tier B); only the
  human adds them to the table.
- **A6 — Product gap survey**: the loop reads the user-facing product surface —
  README, CLI help, MCP tool list, INTEGRATIONS.md, API.md, mobile clients —
  and asks, from a user's perspective: *what is incomplete, awkward, or missing
  that someone using this product day-to-day would notice?* The loop should also
  scan the **Proposed** list for items that recent merges have made clearly
  unblocked and ready to implement. A finding self-promotes to Ranked when ALL
  four gates pass:
  1. **Additive** — no existing API, contract, or CLI interface is removed,
     renamed, or made incompatible. The fix adds or completes, never breaks.
  2. **Gap is real and code-verifiable** — the judge can confirm the gap exists
     by reading the current codebase (a server capability with no CLI command;
     a feature half-landed with the UI wiring still missing; a Proposed item
     whose blocker was resolved by a merged PR). No speculation.
  3. **Done-when is concrete** — the judge can re-run a specific test, command,
     or smoke to confirm completion. "Users will find it easier" is Tier B;
     "`roost schedule list` returns a 200 with the correct schema" is Tier A.
  4. **No design decision required** — the right approach is obvious from the
     existing architecture. If the loop finds itself choosing between two
     meaningfully different designs, or if the work touches external systems,
     adds dependencies, or changes how the product is positioned, it drafts a
     Proposed note instead and flags it for the human.
  This source is renewable: product surfaces grow over time and every merge
  can create new gaps. Unlike A1–A5, A6 may promote items directly from
  Proposed to Ranked (bypassing the usual human-only rule) when the four gates
  are met and the judge approves.

### Tier B (human-gated — drafted, never self-promoted)

Anything that requires a product direction or design decision: new external
dependencies, API/contract changes (removals, renames, breaking changes), UX
redesigns, new product surfaces with unclear scope, refactors unjustified by a
reproducing defect, or features whose value depends on positioning judgment the
loop cannot make alone. Goes to Proposed with evidence and a clear "what the
human needs to decide" note; surfaced in replenishment notifications.

The boundary with A6: if a reasonable engineer, seeing the current codebase,
would agree that the gap is obviously real and the fix is obviously correct —
it's A6. If a reasonable engineer might say "good question, but I'm not sure
that's the right direction" — it's Tier B.

### The cycle

1. **Survey** the cheapest sufficient sources: journal debts (A4) + ratchet table
   (A5) first; then a drift sweep scoped to changes since last sweep (A3); then a
   coverage measure (A2); then a product gap survey (A6 — scan Proposed for
   newly-unblocked items, then scan the live product surface); then a bug hunt in
   the least-recently-hunted area (A1 — rotate areas, record the rotation in the
   journal).
2. **Draft a slate**: every candidate with evidence (file:line, failing test,
   metric delta) and a tier.
3. **Judge the slate**: the Sonnet judge verifies tier assignments — it must be
   able to *re-check the evidence itself* (run the failing test, re-measure the
   metric). Unverifiable or borderline → Tier B.
4. **Promote** at most **3** judge-approved Tier A items into Ranked, tagged
   `self-promoted`. Tier B → Proposed.
5. **Notify the human**: one-paragraph slate summary — what was self-promoted,
   what awaits their call.
6. Implement as normal iterations (same judge gate per PR).

### Pacing and the idle state

- Idle is a *pause*, not an end state. When a full cycle (all six sources)
  yields zero judge-approved Tier A work, the loop idles and re-checks on wake:
  - **Repo changed** (new commits/merges since last survey) → run a drift sweep
    + targeted hunt over the changed areas.
  - **Repo unchanged** → deepen one notch instead of repeating: next bug-hunt
    area in the rotation, or the next uncovered module. Two consecutive
    deepening cycles with zero confirmed findings → long-idle (max wake
    interval) until the repo changes. That is honest patience, not failure.
- Hard limits regardless of source: one item per iteration, small diffs, every
  PR through the judge, never weaken a metric to improve another (coverage must
  not drop to make lint pass; a ratchet gain that regresses another ratchet is
  a rejection).
- Anti-gaming: deleting code to raise coverage %, trivial tests to inflate
  counts, or reclassifying debts to mint A4 work are all judge-rejectable on
  sight — the judge's standing instruction is to ask "is the codebase actually
  better?"

## Security rules (from CLAUDE.md — restated because the loop runs unattended)

- Never commit credentials, tokens, or runtime DBs.
- Any flow touching real Claude credentials stays an explicit, consented human
  choice — the loop never builds or triggers silent credential copying.
- Outward-facing actions beyond pushing branches/PRs to this repo (publishing
  packages, posting anywhere external) are out of scope; propose instead.
- **Security-surface items are out of scope for this loop.** Any backlog item
  whose Surface tag contains "security" (e.g. `backend/security`, `publish/security`)
  must be marked `blocked: security-session` and skipped — those items are handled
  in a dedicated security-review session. Replenishment must not self-promote
  security findings into Ranked; they go to Proposed only.

## Journal entry format

```
## <UTC timestamp> — <item-id>: <title>
- Verdict: shipped | failed | blocked | in-progress
- Branch/PR: <branch> / <PR url or "-">
- What changed: <2-4 lines>
- Evidence:
  - `<command>` → <one-line result, e.g. "341 passed in 12.3s">
  - <artifact path/link if any>
- Judge: <verdict (+ rounds), addressed/dismissed findings w/ reasons>
- Models: implementer <model-id> / judge <self-reported model-id — must differ>
- Notes: <surprises, debts created, proposals filed>
```
