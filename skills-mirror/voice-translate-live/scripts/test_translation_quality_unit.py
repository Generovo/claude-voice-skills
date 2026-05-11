"""Unit tests for test-translation-quality.py.

These tests cover only the pure helper functions — no network calls, no audio
playback, no WebSocket sessions. Run with:

    python3 -m pytest test_translation_quality_unit.py -v

Phase RED expectation: before the sibling module exists, the import below fails
with ImportError / ModuleNotFoundError. After GREEN (`test-translation-quality.py`
written), all 9 tests pass.
"""

import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "tq", pathlib.Path(__file__).parent / "test-translation-quality.py"
)
tq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tq)


def test_normalize_text_strips_punctuation_and_lowercases():
    assert tq.normalize_text("Hello, World!") == "hello world"
    assert tq.normalize_text("Café — délicieux ?") == "café délicieux"
    assert tq.normalize_text("  multiple   spaces  ") == "multiple spaces"


def test_compute_bleu_simple_perfect_match_is_one():
    score = tq.compute_bleu_simple("the cat sat on the mat", "the cat sat on the mat")
    assert score >= 0.99


def test_compute_bleu_simple_no_overlap_is_low():
    score = tq.compute_bleu_simple("the cat sat on the mat", "ein hund läuft schnell")
    assert score <= 0.10


def test_compute_bleu_simple_partial_overlap_in_between():
    score = tq.compute_bleu_simple("the cat sat on the mat", "the dog sat on the rug")
    assert 0.20 < score < 0.85  # partial overlap


def test_compute_bleu_simple_empty_inputs_return_zero():
    assert tq.compute_bleu_simple("", "anything") == 0.0
    assert tq.compute_bleu_simple("anything", "") == 0.0


def test_load_pairs_jsonl_reads_records(tmp_path):
    f = tmp_path / "pairs.jsonl"
    f.write_text(
        '{"id": "p1", "source_audio": "fr.wav", "source_lang": "fr", "target_lang": "en", "reference": "hello"}\n'
        '{"id": "p2", "source_audio": "en.wav", "source_lang": "en", "target_lang": "fr", "reference": "bonjour"}\n'
    )
    pairs = tq.load_pairs(f)
    assert len(pairs) == 2
    assert pairs[0]["target_lang"] == "en"
    assert pairs[1]["reference"] == "bonjour"


def test_load_pairs_ignores_blank_lines(tmp_path):
    f = tmp_path / "pairs.jsonl"
    f.write_text('\n{"id": "p1", "reference": "hi"}\n\n')
    pairs = tq.load_pairs(f)
    assert len(pairs) == 1


def test_aggregate_by_pair_groups_correctly():
    rows = [
        ("p1", "fr→en", 0.8, ""),
        ("p2", "en→fr", 0.6, ""),
        ("p3", "fr→en", 0.9, ""),
        ("p4", "ar→fr", 0.7, ""),
    ]
    summary = tq.aggregate_by_pair(rows)
    assert summary["fr→en"]["count"] == 2
    assert summary["fr→en"]["mean_bleu"] == pytest.approx(0.85)
    assert summary["en→fr"]["count"] == 1
    assert summary["ar→fr"]["count"] == 1


def test_aggregate_by_pair_handles_empty():
    summary = tq.aggregate_by_pair([])
    assert summary == {}
