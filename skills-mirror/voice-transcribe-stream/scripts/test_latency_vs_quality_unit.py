"""Unit tests for test-latency-vs-quality.py.

These tests cover the pure helper functions (no API calls, no audio files
required): normalize_text, compute_wer, load_audio_pairs, aggregate_by_profile.

Run from this directory:

    python3 -m pytest test_latency_vs_quality_unit.py -v
"""

from __future__ import annotations

import importlib.util
import math
import pathlib

import pytest

# The sibling module's filename uses a hyphen (matches the established
# convention for runnable scripts in this skill — see twilio-transcribe.py).
# Python cannot `import` a hyphenated module directly, so we load it via the
# importlib machinery.
_HERE = pathlib.Path(__file__).parent
_SPEC = importlib.util.spec_from_file_location(
    "tlq", _HERE / "test-latency-vs-quality.py"
)
assert _SPEC is not None and _SPEC.loader is not None
tlq = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tlq)


# --------------------------------------------------------------------------- #
# compute_wer                                                                 #
# --------------------------------------------------------------------------- #


def test_compute_wer_perfect_match_zero():
    """Word Error Rate: 0 when reference == hypothesis."""
    assert tlq.compute_wer(
        "the cat sat on the mat", "the cat sat on the mat"
    ) == pytest.approx(0.0)


def test_compute_wer_one_substitution():
    """1 substitution in 6 words = 1/6 WER."""
    assert tlq.compute_wer(
        "the cat sat on the mat", "the dog sat on the mat"
    ) == pytest.approx(1 / 6, rel=1e-2)


def test_compute_wer_full_mismatch_is_one():
    """No overlap: WER = 1.0 (or close)."""
    wer = tlq.compute_wer("aaa bbb ccc", "xxx yyy zzz")
    # 3 substitutions in 3 reference words = 1.0; insertions could push it
    # higher but for same-length, it's exactly 1.0.
    assert wer >= 1.0


def test_compute_wer_empty_reference_returns_nan_or_high():
    """Empty reference is degenerate; document the behavior."""
    result = tlq.compute_wer("", "any text")
    # Either NaN, math.inf, or > 1.0 — any of these is acceptable; just don't
    # return 0 (which would silently hide the fact that we had nothing to
    # compare against).
    assert math.isnan(result) or result > 1.0


# --------------------------------------------------------------------------- #
# normalize_text                                                              #
# --------------------------------------------------------------------------- #


def test_normalize_text_lowercases_and_strips():
    assert tlq.normalize_text("The Quick, Brown Fox!") == "the quick brown fox"


# --------------------------------------------------------------------------- #
# load_audio_pairs                                                            #
# --------------------------------------------------------------------------- #


def test_load_audio_pairs_jsonl(tmp_path):
    f = tmp_path / "pairs.jsonl"
    f.write_text(
        '{"id": "p1", "audio": "samples/a.wav", "reference": "hello world"}\n'
        '{"id": "p2", "audio": "samples/b.wav", "reference": "lorem ipsum"}\n'
    )
    pairs = tlq.load_audio_pairs(f)
    assert len(pairs) == 2
    assert pairs[0]["reference"] == "hello world"


# --------------------------------------------------------------------------- #
# aggregate_by_profile                                                        #
# --------------------------------------------------------------------------- #


def test_aggregate_profile_results():
    """Per profile: count, mean_latency, mean_wer."""
    rows = [
        {"profile": "ultra-low", "ttft": 0.35, "wer": 0.10},
        {"profile": "ultra-low", "ttft": 0.42, "wer": 0.12},
        {"profile": "balanced", "ttft": 0.95, "wer": 0.04},
    ]
    agg = tlq.aggregate_by_profile(rows)
    assert agg["ultra-low"]["count"] == 2
    assert agg["ultra-low"]["mean_ttft"] == pytest.approx(0.385, rel=1e-2)
    assert agg["ultra-low"]["mean_wer"] == pytest.approx(0.11, rel=1e-2)
    assert agg["balanced"]["count"] == 1


def test_aggregate_skips_runs_missing_metrics():
    rows = [
        {"profile": "ultra-low", "ttft": None, "wer": 0.10},
        {"profile": "ultra-low", "ttft": 0.42, "wer": None},
        {"profile": "ultra-low", "ttft": 0.40, "wer": 0.15},
    ]
    agg = tlq.aggregate_by_profile(rows)
    # ttft: 2 valid (0.42, 0.40) -> mean 0.41
    # wer:  2 valid (0.10, 0.15) -> mean 0.125
    assert agg["ultra-low"]["mean_ttft"] == pytest.approx(0.41, rel=1e-2)
    assert agg["ultra-low"]["mean_wer"] == pytest.approx(0.125, rel=1e-2)
