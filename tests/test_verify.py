"""Trust-loop verifier parsing tests (the pure parts; the live agent is validated
on the fleet)."""
from __future__ import annotations

from roost import verify
from roost.verify import parse_verdict, render_user


def test_parse_pass():
    p, reason = parse_verdict("checked the file, it exists.\nROOST_VERIFY: PASS — file written and non-empty")
    assert p is True
    assert "file written" in reason


def test_parse_fail():
    p, reason = parse_verdict("the endpoint returned 500.\nROOST_VERIFY: FAIL — service not healthy")
    assert p is False
    assert "service not healthy" in reason


def test_parse_no_verdict():
    p, reason = parse_verdict("I looked at it and it seems fine probably")
    assert p is None


def test_parse_marker_inside_stream_json():
    line = '{"type":"result","result":"ran the test, 0 failures. ROOST_VERIFY: PASS — all tests green"}'
    p, reason = parse_verdict(line)
    assert p is True
    assert "tests green" in reason


def test_last_marker_wins():
    # An earlier mention (e.g. echoing the instructions) must not override the final verdict.
    text = ("I must end with ROOST_VERIFY: PASS or FAIL.\n"
            "... did the work ...\nROOST_VERIFY: FAIL — output is empty")
    p, reason = parse_verdict(text)
    assert p is False


def test_render_user_contains_goal_and_marker():
    u = render_user("count primes under 100", "the count is 25")
    assert "count primes under 100" in u
    assert "the count is 25" in u
    assert verify.VERIFY_MARKER in u


def test_render_user_handles_no_result():
    u = render_user("do a thing", None)
    assert "no textual result" in u.lower()
