"""Unit tests for eval-prompt.py — verifies scoring/aggregation without API calls."""
import importlib.util
import pathlib
import pytest

_spec = importlib.util.spec_from_file_location(
    "eval_prompt", pathlib.Path(__file__).parent / "eval-prompt.py"
)
ep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ep)


def test_parse_judge_verdict_pass():
    judge = '{"verdict": "pass", "reason": "agent correctly..."}'
    v, r = ep.parse_judge_verdict(judge)
    assert v == "pass"
    assert r.startswith("agent correctly")


def test_parse_judge_verdict_fail_with_extra_text():
    """Judge sometimes wraps JSON in ``` fences or prose. Be tolerant."""
    judge = 'Looking at it... ```json\n{"verdict": "fail", "reason": "wrong tool"}\n```'
    v, r = ep.parse_judge_verdict(judge)
    assert v == "fail"
    assert "wrong tool" in r


def test_parse_judge_verdict_unparseable_returns_unknown():
    v, r = ep.parse_judge_verdict("totally not json")
    assert v == "unknown"


def test_pass_rate_basic():
    rows = [("c1", "pass", ""), ("c2", "fail", ""), ("c3", "pass", ""), ("c4", "pass", "")]
    assert ep.pass_rate(rows) == pytest.approx(0.75)


def test_pass_rate_excludes_unknown():
    """Unknown verdicts are excluded from the denominator (they're not failures, they're parse errors)."""
    rows = [("c1", "pass", ""), ("c2", "unknown", ""), ("c3", "fail", "")]
    assert ep.pass_rate(rows) == pytest.approx(0.5)


def test_load_evals_reads_jsonl(tmp_path):
    f = tmp_path / "evals.jsonl"
    f.write_text(
        '{"id": "c1", "input": "what time?", "expected_behavior": "responds with time"}\n'
        '{"id": "c2", "input": "book table", "expected_behavior": "calls reservation tool"}\n'
    )
    cases = ep.load_evals(f)
    assert len(cases) == 2
    assert cases[0]["id"] == "c1"
    assert cases[1]["expected_behavior"].startswith("calls")
