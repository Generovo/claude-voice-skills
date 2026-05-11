"""Unit tests for test-latency.py — verifies metric parsing without API calls."""
import importlib.util
import pathlib
import pytest

# Load test-latency.py as a module (it has a hyphen in name)
_spec = importlib.util.spec_from_file_location(
    "test_latency",
    pathlib.Path(__file__).parent / "test-latency.py",
)
test_latency = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(test_latency)


def test_compute_metrics_with_full_event_stream():
    """Given a complete stream, returns TTFA and total duration."""
    events = [
        ("session.created", 0.0),
        ("session.updated", 0.05),
        ("response.created", 0.10),
        ("response.output_audio.delta", 0.412),  # first audio
        ("response.output_audio.delta", 0.520),
        ("response.output_audio_transcript.delta", 0.520),
        ("response.output_audio.done", 1.700),
        ("response.done", 1.834),
    ]
    metrics = test_latency.compute_metrics(events)
    assert metrics["ttfa_seconds"] == pytest.approx(0.412)
    assert metrics["done_seconds"] == pytest.approx(1.834)
    assert metrics["first_transcript_seconds"] == pytest.approx(0.520)


def test_compute_metrics_missing_audio_delta_returns_none():
    """If no audio delta event seen, ttfa is None (not zero)."""
    events = [("session.created", 0.0), ("response.done", 1.0)]
    metrics = test_latency.compute_metrics(events)
    assert metrics["ttfa_seconds"] is None
    assert metrics["done_seconds"] == pytest.approx(1.0)


def test_aggregate_stats_handles_empty_runs():
    """Empty list of metrics returns sentinel None fields, not a crash."""
    stats = test_latency.aggregate_stats([])
    assert stats["count"] == 0
    assert stats["ttfa_median"] is None


def test_aggregate_stats_skips_none_values():
    """Mixed runs (some without audio) aggregate only the valid ones."""
    runs = [
        {"ttfa_seconds": 0.4, "done_seconds": 1.5, "first_transcript_seconds": 0.5},
        {"ttfa_seconds": None, "done_seconds": 1.6, "first_transcript_seconds": None},
        {"ttfa_seconds": 0.6, "done_seconds": 1.7, "first_transcript_seconds": 0.7},
    ]
    stats = test_latency.aggregate_stats(runs)
    assert stats["count"] == 3
    assert stats["ttfa_median"] == pytest.approx(0.5)  # median of [0.4, 0.6]
    assert stats["done_median"] == pytest.approx(1.6)


def test_default_sample_returns_pcm16_bytes():
    """The embedded sample produces non-empty PCM16 bytes."""
    sample = test_latency._default_sample_pcm16()
    assert isinstance(sample, bytes)
    assert len(sample) > 0
    assert len(sample) % 2 == 0  # 16-bit samples
