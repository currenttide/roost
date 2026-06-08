"""Golden-fixture + unit tests for the distilled live-stream transform (R107).

`roost.cli.distill_log_line` is the reference implementation of the
cross-platform distilled-stream contract (mobile-app/fixtures/distilled/SPEC.md).
The golden fixtures (cases.json) are the SHARED contract that iOS (R108) and
Android (R109) mirror exactly: this test asserts the CLI reference impl produces
the committed `distilled` for every committed `raw`. If the transform changes,
this test fails until the fixtures are regenerated — keeping all three clients
honest to one source of truth.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from roost.cli import distill_log_line

FIXTURES = Path(__file__).resolve().parent.parent / "mobile-app" / "fixtures" / "distilled"
CASES = json.loads((FIXTURES / "cases.json").read_text())["cases"]


# ---------- golden fixtures: the cross-platform contract ----------


@pytest.mark.parametrize("case", CASES, ids=[c["note"] for c in CASES])
def test_golden_fixture_distills_to_expected(case):
    """Every committed raw line distils to its committed expected output.

    `distilled: null` in the fixture means the line is suppressed → None.
    """
    assert distill_log_line(case["raw"]) == case["distilled"]


def test_fixtures_cover_both_capture_sources():
    sources = {c["source"] for c in CASES}
    assert "captured" in sources, "must ground the transform in real captured lines"
    # synthesized cases fill shapes absent from the small live capture
    assert sources <= {"captured", "synthesized"}


def test_fixtures_are_well_formed():
    for c in CASES:
        assert isinstance(c["raw"], str) and c["raw"]
        assert c["distilled"] is None or isinstance(c["distilled"], str)
        assert c["source"] in ("captured", "synthesized")


# ---------- unit tests: the transform rules directly ----------


def test_plain_text_passes_through_verbatim():
    # a `command` job's stdout is not stream-json — never mangled.
    assert distill_log_line("total 48\ndrwxr-xr-x  3 me") == "total 48\ndrwxr-xr-x  3 me"
    assert distill_log_line("plain line") == "plain line"


def test_empty_and_none_handled():
    assert distill_log_line("") == ""
    assert distill_log_line(None) is None


def test_malformed_json_passes_through():
    assert distill_log_line('{"broken') == '{"broken'


def test_roost_event_envelope_passes_through():
    raw = '{"type": "started", "attempt": 1, "exit_code": null}'
    assert distill_log_line(raw) == raw


def test_system_init_is_phase_divider():
    assert distill_log_line('{"type": "system", "subtype": "init"}') == "🔎 starting…"


def test_system_other_subtype_suppressed():
    assert distill_log_line('{"type": "system", "subtype": "thinking_tokens"}') is None


def test_rate_limit_event_suppressed():
    assert distill_log_line('{"type": "rate_limit_event", "rate_limit_info": {}}') is None


def test_result_success_and_error():
    assert distill_log_line('{"type": "result", "subtype": "success"}') == "✓ done"
    assert distill_log_line('{"type": "result", "is_error": true}') == "✗ failed"


def test_assistant_text_shown():
    raw = json.dumps({"type": "assistant",
                      "message": {"content": [{"type": "text", "text": "Hi there"}]}})
    assert distill_log_line(raw) == "Hi there"


def test_assistant_thinking_suppressed_with_signature():
    raw = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "thinking", "thinking": "deep thoughts", "signature": "Er0CCmMI" * 50}]}})
    out = distill_log_line(raw)
    assert out is None  # both reasoning AND the base64 signature suppressed


def test_tool_use_with_hint():
    raw = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}]}})
    assert distill_log_line(raw) == "→ Bash: ls -la"


def test_tool_use_hint_priority_command_over_description():
    raw = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "uptime", "description": "show uptime"}}]}})
    assert distill_log_line(raw) == "→ Bash: uptime"


def test_tool_use_without_hint_is_bare_arrow():
    raw = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "TodoWrite", "input": {"todos": []}}]}})
    assert distill_log_line(raw) == "→ TodoWrite"


def test_tool_use_hint_capped_and_collapsed():
    long = "x" * 200
    raw = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": long}}]}})
    out = distill_log_line(raw)
    assert out.startswith("→ Bash: ")
    assert out.endswith("…")
    assert len(out) == len("→ Bash: ") + 80 + 1  # hint cap 80 + ellipsis


def test_tool_result_str_truncated():
    raw = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": False, "content": "file contents\nmore"}]}})
    assert distill_log_line(raw) == "  ⎿ file contents more"


def test_tool_result_list_content():
    raw = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": [{"type": "text", "text": "the output"}]}]}})
    assert distill_log_line(raw) == "  ⎿ the output"


def test_tool_result_error_marked():
    raw = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "is_error": True, "content": "denied"}]}})
    assert distill_log_line(raw) == "  ⎿ ✗ denied"


def test_tool_result_empty_placeholder():
    raw = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": ""}]}})
    assert distill_log_line(raw) == "  ⎿ (result)"


def test_assistant_multiple_blocks_joined():
    raw = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Let me check"},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/etc/hostname"}}]}})
    assert distill_log_line(raw) == "Let me check\n→ Read: /etc/hostname"


def test_assistant_string_content():
    raw = json.dumps({"type": "assistant", "message": {"content": "direct string"}})
    assert distill_log_line(raw) == "direct string"


def test_assistant_empty_content_suppressed():
    raw = json.dumps({"type": "assistant", "message": {"content": []}})
    assert distill_log_line(raw) is None


def test_never_raises_on_odd_shapes():
    # pure + total: odd shapes must not raise.
    for bad in ['[]', '123', 'null', 'true', '{"type": 5}',
                '{"type": "assistant", "message": null}',
                '{"type": "assistant", "message": "hello"}',   # R111: truthy non-dict
                '{"type": "user", "message": [1, 2]}',          # R111: truthy non-dict
                '{"type": "assistant", "message": {"content": [null, 7]}}']:
        distill_log_line(bad)  # no exception


# ---------- R111: truthy non-dict `message` must suppress, not crash ----------
# Promoted from the A1 bug-hunt repros (/tmp/hunt-distill-repros.py). On master
# `distill_log_line` did `msg = obj.get("message") or {}` — the `or {}` only
# rescues FALSY messages, so a TRUTHY non-dict `message` (a JSON string / list /
# number in an assistant/user envelope) reached `msg.get("content")` and raised
# AttributeError, crashing the now-DEFAULT distilled `roost logs` / `--follow` /
# phone session view for that job. The fix uses `isinstance(msg, dict)` (else
# suppress → None), matching the mobile clients (iOS `as? [String:Any]`→nil,
# Android `optJSONObject`→null). The cross-platform contract is pinned by the
# new non-dict-message golden case in cases.json.


@pytest.mark.parametrize("message", ["hello there", ["a", "b"], 5, 3.14])
def test_truthy_non_dict_message_suppressed_not_raised(message):
    """A truthy non-dict `message` must suppress (return None), never raise."""
    raw = json.dumps({"type": "assistant", "message": message})
    assert distill_log_line(raw) is None  # suppressed, no AttributeError
    raw_user = json.dumps({"type": "user", "message": message})
    assert distill_log_line(raw_user) is None


def test_falsy_message_still_suppressed():
    """Boundary check: FALSY messages were already (and remain) suppressed."""
    for message in ("", [], {}, False, None):
        raw = json.dumps({"type": "assistant", "message": message})
        assert distill_log_line(raw) is None


def test_non_dict_message_reachable_end_to_end_via_server_logs():
    """The crashing `data` value round-trips through real server storage
    untouched (it is a valid str), so a worker POSTing this one line would have
    wedged the default logs view for any client that fetches it. Proves the bug
    is reachable end-to-end, not just a unit-level curiosity."""
    import pathlib
    import tempfile

    from fastapi.testclient import TestClient

    from roost import server

    token = "test-admin-token"
    db = pathlib.Path(tempfile.mkdtemp()) / "r.db"
    app = server.create_app(db_path=db, token=token, run_sweeper=False)
    c = TestClient(app)
    c.headers.update({"Authorization": f"Bearer {token}"})

    tok = c.post("/enroll-tokens", json={"label": "t"}).json()["token"]
    b = c.post("/enroll", json={"token": tok, "name": "w1", "capabilities": {}},
               headers={"Authorization": ""}).json()
    wid, cred = b["worker_id"], b["credential"]
    wh = {"Authorization": f"Bearer {cred}"}
    job = c.post("/jobs", json={"kind": "claude", "intent": "hi"}).json()
    jid = job["id"]

    adv = json.dumps({"type": "assistant", "message": "hello there"})
    r = c.post(f"/workers/{wid}/jobs/{jid}/logs", headers=wh,
               json={"stream": "stdout", "data": adv})
    assert r.status_code == 200, r.text

    payload = c.get(f"/jobs/{jid}/logs").json()
    stored = [lg for lg in payload["logs"] if lg.get("stream") != "event"]
    assert stored, "expected the stdout log line to be stored"
    for lg in stored:
        # Mirrors roost/cli.py logs()/_stream(): the default (non-verbose) path.
        assert distill_log_line(lg.get("data", "")) is None  # must NOT raise
