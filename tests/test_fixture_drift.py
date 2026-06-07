"""Fixture drift guard for the mobile contract (R13, API.md §8).

Replays the canonical fixture scenario (mobile-app/record_fixtures.py
``capture()``) against an in-process control plane and structurally compares
every live response with the committed golden fixture. The contract is
additive-only: the server may ADD fields; a key that disappears or changes
JSON type fails here — on the default pytest run, not at the next manual
audit. Values (ids, timestamps) are not compared, only shape.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "mobile-app" / "fixtures"


def _load_recorder():
    """Import mobile-app/record_fixtures.py (dir name isn't importable)."""
    spec = importlib.util.spec_from_file_location(
        "record_fixtures", REPO / "mobile-app" / "record_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("record_fixtures", mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def live_shapes(tmp_path_factory) -> dict[str, Any]:
    db = tmp_path_factory.mktemp("drift") / "cp.db"
    return _load_recorder().capture(db)


def _type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):  # before int — bool is an int subclass
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def assert_shape_superset(live: Any, golden: Any, path: str = "$") -> list[str]:
    """Every key/shape in `golden` must exist in `live`; extras in live are OK.

    Returns a list of human-readable drift findings (empty = no drift).
    `golden` null pins only the key's existence; differing non-null JSON
    types are drift (int/float both count as number).
    """
    drifts: list[str] = []
    if golden is None:
        return drifts  # key existed — that's all a null golden can pin
    if live is None:
        drifts.append(f"{path}: was {_type_name(golden)}, now null/missing-value")
        return drifts
    gt, lt = _type_name(golden), _type_name(live)
    if gt != lt:
        drifts.append(f"{path}: type changed {gt} -> {lt}")
        return drifts
    if isinstance(golden, dict):
        for k, gv in golden.items():
            if k not in live:
                drifts.append(f"{path}.{k}: REMOVED (additive-only contract)")
                continue
            drifts.extend(assert_shape_superset(live[k], gv, f"{path}.{k}"))
    elif isinstance(golden, list):
        if golden and not live:
            drifts.append(f"{path}: golden has elements, live is empty "
                          "(scenario drift?)")
        elif golden and live:
            # The scenario is deterministic, but list ORDER may not be —
            # compare each golden element against its best-matching live
            # element (fewest drifts), so reordering isn't false drift.
            for i, gv in enumerate(golden):
                candidates = [assert_shape_superset(lv, gv, f"{path}[{i}]")
                              for lv in live]
                drifts.extend(min(candidates, key=len))
    return drifts


# ---------- the checker itself must catch drift (self-test) ----------


def test_checker_catches_removal_and_type_change():
    assert assert_shape_superset({"a": 1, "b": "x"}, {"a": 1}) == []  # additive OK
    assert any("REMOVED" in d
               for d in assert_shape_superset({}, {"a": 1}))
    assert any("type changed" in d
               for d in assert_shape_superset({"a": "1"}, {"a": 1}))
    assert any("now null" in d
               for d in assert_shape_superset({"a": None}, {"a": 1}))
    # null golden pins existence only.
    assert assert_shape_superset({"a": 123}, {"a": None}) == []
    # nested + list element shape.
    assert any("REMOVED" in d for d in assert_shape_superset(
        {"runs": [{"x": 1}]}, {"runs": [{"x": 1, "y": 2}]}))


# ---------- live vs golden, one test per fixture ----------


def _json_fixture_names() -> list[str]:
    return sorted(p.name for p in FIXTURES.glob("*.json"))


def test_every_fixture_is_captured(live_shapes):
    # A fixture file with no live counterpart means the scenario and the
    # goldens have drifted apart — fix capture() or delete the fixture.
    missing = set(_json_fixture_names()) - set(live_shapes)
    assert not missing, f"fixtures not produced by capture(): {sorted(missing)}"


@pytest.mark.parametrize("name", _json_fixture_names())
def test_live_shape_covers_golden(name: str, live_shapes):
    golden = json.loads((FIXTURES / name).read_text())
    drifts = assert_shape_superset(live_shapes[name], golden)
    assert not drifts, (
        f"{name}: server response shape drifted from the golden fixture "
        f"(additive-only, API.md §8):\n  " + "\n  ".join(drifts) +
        "\n  If the change is intentional+additive, regenerate: "
        "python mobile-app/record_fixtures.py"
    )


def test_sse_transcript_event_names_covered(live_shapes):
    """The SSE fixture's event vocabulary must still be emitted live."""
    def event_names(text: str) -> set[str]:
        return {line.split(":", 1)[1].strip()
                for line in text.splitlines() if line.startswith("event:")}

    golden = (FIXTURES / "stream_succeeded.sse.txt").read_text()
    live = live_shapes["stream_succeeded.sse.txt"]
    missing = event_names(golden) - event_names(live)
    assert not missing, f"SSE event types no longer emitted: {sorted(missing)}"
