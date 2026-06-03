"""Independent verification of a goal's result — the "trust loop" (ease-of-use-plan
Phase 0). After an agent reports a task done, a SEPARATE agent (fresh context,
adversarial framing) checks whether the goal was actually achieved and returns a
verdict + evidence. "succeeded" then means *verified*, not just "exit 0".

This module holds the prompt + verdict parsing (pure, unit-tested). The worker runs
the verifier subprocess and feeds its output to ``parse_verdict``.
"""

from __future__ import annotations

from typing import Optional

VERIFY_MARKER = "ROOST_VERIFY:"

VERIFIER_SYSTEM = """\
You are an INDEPENDENT verifier. You did NOT do the work — you are checking someone
else's claim, and your job is to catch it if the goal was NOT actually achieved.

Be adversarial and concrete. Do not trust the executor's summary; check the real state
yourself — run commands, read the files it claims to have written, re-run the test, hit
the endpoint, recompute the answer. A plausible-sounding report is not evidence.

Decide whether the GOAL is genuinely and completely achieved. Briefly state the specific
evidence you checked (commands run, what you observed). Then end your reply with EXACTLY
one final line:

    {marker} PASS — <one line: what you confirmed>
    {marker} FAIL — <one line: what is missing or wrong>

Rules:
- PASS only if you positively confirmed the goal is met. If you cannot confirm it, FAIL.
- If the goal is impossible or was misunderstood, FAIL and say so.
- Output the marker line exactly once, as the very last line.
"""


def render_user(goal: str, result_text: Optional[str]) -> str:
    """The verifier's task prompt: the original goal + what the executor reported."""
    reported = (result_text or "").strip() or "(the executor produced no textual result)"
    return (
        f"GOAL (what was asked):\n{goal}\n\n"
        f"WHAT THE EXECUTOR REPORTED IT DID:\n{reported}\n\n"
        "Independently verify whether the GOAL is actually achieved, then give your "
        f"final {VERIFY_MARKER} PASS/FAIL line."
    )


def system_prompt() -> str:
    return VERIFIER_SYSTEM.format(marker=VERIFY_MARKER)


def render_fix(goal: str, critique: str, prev_result: Optional[str]) -> str:
    """Prompt for a self-healing fix attempt: the previous result was rejected by an
    independent verifier; do the task correctly this time, on this same machine."""
    prev = (prev_result or "").strip() or "(no result captured)"
    return (
        f"Your previous attempt at this task was REJECTED by an independent verifier. "
        f"Fix it and actually complete the goal on THIS machine.\n\n"
        f"ORIGINAL GOAL:\n{goal}\n\n"
        f"WHY IT WAS REJECTED:\n{critique}\n\n"
        f"YOUR PREVIOUS (rejected) RESULT:\n{prev}\n\n"
        f"Do the work correctly now. Inspect the real state, make the change, and report "
        f"concisely what you did."
    )


def parse_verdict(output: str) -> tuple[Optional[bool], str]:
    """Parse verifier output for the verdict. Returns (passed, reason):
    passed True/False if a PASS/FAIL marker is found, else None (no verdict).
    Scans for the LAST marker occurrence (the agent's final line), tolerating the
    marker appearing inside stream-json text."""
    idx = output.rfind(VERIFY_MARKER)
    if idx == -1:
        return None, "verifier produced no verdict"
    after = output[idx + len(VERIFY_MARKER):]
    # Trim at the first JSON/newline boundary so we don't swallow trailing stream-json.
    for stop in ('"', "\\n", "\n"):
        cut = after.find(stop)
        if cut != -1:
            after = after[:cut]
    after = after.strip().lstrip("—-").strip()
    upper = after.upper()
    if upper.startswith("PASS"):
        return True, after[4:].lstrip("—-: ").strip() or "verified"
    if upper.startswith("FAIL"):
        return False, after[4:].lstrip("—-: ").strip() or "not verified"
    return None, f"unrecognized verdict: {after[:80]}"
