"""Tests for the `roost do` router's verdict parsing (pure part)."""
from __future__ import annotations

from roost.cli import _parse_classification

GOAL = "do the thing"


def test_parse_single_clean():
    d = _parse_classification('{"mode":"single","ambiguous":false,"destructive":false,"restated":"X"}', GOAL)
    assert d["mode"] == "single" and d["restated"] == "X"
    assert d["ambiguous"] is False and d["destructive"] is False


def test_parse_multi():
    d = _parse_classification('{"mode":"multi"}', GOAL)
    assert d["mode"] == "multi"


def test_parse_ambiguous_with_question():
    d = _parse_classification(
        '{"mode":"single","ambiguous":true,"clarifying_question":"which repo?"}', GOAL)
    assert d["ambiguous"] is True
    assert d["clarifying_question"] == "which repo?"


def test_parse_destructive():
    d = _parse_classification('{"mode":"single","destructive":true,"restated":"rm -rf X"}', GOAL)
    assert d["destructive"] is True


def test_parse_json_embedded_in_prose():
    d = _parse_classification('Here is my verdict:\n{"mode":"multi","destructive":false}\nDone.', GOAL)
    assert d["mode"] == "multi"


def test_parse_garbage_falls_back_to_safe_default():
    d = _parse_classification("I think you should probably do it, sounds fine", GOAL)
    assert d == {"mode": "single", "ambiguous": False, "clarifying_question": None,
                 "destructive": False, "simple": False, "restated": GOAL}


def test_parse_simple_flag():
    d = _parse_classification('{"mode":"single","simple":true}', GOAL)
    assert d["simple"] is True
    # default is conservative: not simple unless explicitly true
    assert _parse_classification('{"mode":"single"}', GOAL)["simple"] is False


def test_parse_empty():
    d = _parse_classification("", GOAL)
    assert d["mode"] == "single" and d["restated"] == GOAL
