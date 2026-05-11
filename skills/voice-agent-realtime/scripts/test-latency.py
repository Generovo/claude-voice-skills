"""
test-latency.py — measure OpenAI Realtime API latency per reasoning level.

Run:
  python3 test-latency.py [--level low|medium|high|xhigh|minimal] [--runs 5]
                          [--audio path/to/sample.wav]
                          [--model gpt-realtime-2]

Env:  OPENAI_API_KEY (required)

Outputs a table:
  Level: high  Runs: 5
    TTFA median: 0.412s   p95: 0.580s
    Done median: 1.834s   p95: 2.110s

Source events: tracked from research file
  /Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md

Note: `reasoning.effort` JSON path is [UNCONFIRMED in research] — placed
inside `session.update.session.reasoning.effort` per prompting-guide hint,
verify on first live probe.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import statistics
import struct
import time
import wave
from pathlib import Path
from typing import Any

import websockets

REALTIME_WS = "wss://api.openai.com/v1/realtime?model={model}"


def compute_metrics(events: list[tuple[str, float]]) -> dict[str, float | None]:
    """Parse a list of (event_type, t_seconds) tuples → dict of latency metrics.

    - ttfa_seconds: time of first `response.output_audio.delta` (None if absent).
    - first_transcript_seconds: time of first `response.output_audio_transcript.delta`.
    - done_seconds: time of `response.done`.
    """
    ttfa = next((t for ev, t in events if ev == "response.output_audio.delta"), None)
    first_tx = next(
        (t for ev, t in events if ev == "response.output_audio_transcript.delta"), None
    )
    done = next((t for ev, t in events if ev == "response.done"), None)
    return {
        "ttfa_seconds": ttfa,
        "first_transcript_seconds": first_tx,
        "done_seconds": done,
    }


def aggregate_stats(runs: list[dict[str, float | None]]) -> dict[str, Any]:
    """List[dict] -> {count, ttfa_median, ttfa_p95, done_median, done_p95}.

    Skips None values so partial runs don't crash aggregation.
    """

    def percentile(values: list[float], p: float) -> float | None:
        if not values:
            return None
        s = sorted(values)
        k = max(0, int(len(s) * p) - 1)
        return s[k]

    def med(values: list[float]) -> float | None:
        return statistics.median(values) if values else None

    ttfas = [r["ttfa_seconds"] for r in runs if r.get("ttfa_seconds") is not None]
    dones = [r["done_seconds"] for r in runs if r.get("done_seconds") is not None]
    return {
        "count": len(runs),
        "ttfa_median": med(ttfas),
        "ttfa_p95": percentile(ttfas, 0.95),
        "done_median": med(dones),
        "done_p95": percentile(dones, 0.95),
    }


def _default_sample_pcm16(seconds: float = 1.0, freq: int = 440, rate: int = 24000) -> bytes:
    """Generate a short sine-wave PCM16 mono sample (used when no --audio is given)."""
    n = int(seconds * rate)
    samples = (
        int(0.3 * 32767 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)
    )
    return b"".join(struct.pack("<h", s) for s in samples)


def _load_audio_pcm16(path: Path) -> bytes:
    """Load a WAV file as PCM16 24 kHz mono bytes. Raises if format mismatch."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1 or w.getframerate() != 24000:
            raise ValueError(
                f"Expected 16-bit mono 24kHz WAV, got "
                f"{w.getsampwidth() * 8}-bit {w.getnchannels()}ch {w.getframerate()}Hz. "
                "Convert with: ffmpeg -i input -ac 1 -ar 24000 -sample_fmt s16 output.wav"
            )
        return w.readframes(w.getnframes())


async def run_one(model: str, level: str, audio_pcm: bytes, system_prompt: str) -> dict:
    """Run a single round-trip, capturing event timestamps."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY env var is required")
    url = REALTIME_WS.format(model=model)
    events: list[tuple[str, float]] = []
    headers = [
        ("Authorization", f"Bearer {api_key}"),
        # [UNCONFIRMED in research] whether this header is still required for
        # gpt-realtime-2 — sending defensively.
    ]
    async with websockets.connect(url, additional_headers=headers, max_size=None) as ws:
        # 1) Configure session
        await ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "model": model,
                        "output_modalities": ["audio"],
                        "instructions": system_prompt,
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": 24000},
                                # Live-confirmed 2026-05-11: turn_detection=None disables VAD
                                # for manual commit+response.create. {"type":"none"} is rejected.
                                "turn_detection": None,
                            },
                            "output": {
                                # rate is REQUIRED on output format (live-confirmed).
                                "format": {"type": "audio/pcm", "rate": 24000},
                                "voice": "marin",
                            },
                        },
                        # [UNCONFIRMED in research] exact JSON path for
                        # reasoning.effort — prompting guide names the param
                        # but didn't show a full JSON example.
                        "reasoning": {"effort": level},
                    },
                }
            )
        )
        # 2) Push audio in chunks (1 second of PCM16 24 kHz = 48000 bytes)
        chunk = 24000 * 2
        for i in range(0, len(audio_pcm), chunk):
            piece = audio_pcm[i : i + chunk]
            await ws.send(
                json.dumps(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(piece).decode(),
                    }
                )
            )
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        # 3) Request response & start timer
        started = time.monotonic()
        await ws.send(json.dumps({"type": "response.create"}))
        # 4) Consume events
        while True:
            raw = await ws.recv()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            event_type = payload.get("type", "")
            events.append((event_type, time.monotonic() - started))
            if event_type == "response.done" or event_type == "error":
                break
    return compute_metrics(events)


async def _amain(args: argparse.Namespace) -> None:
    if args.audio:
        audio = _load_audio_pcm16(Path(args.audio))
    else:
        audio = _default_sample_pcm16()
    system_prompt = (
        "You are a brief, helpful voice assistant. Reply in one short sentence."
    )
    runs: list[dict] = []
    for i in range(args.runs):
        m = await run_one(args.model, args.level, audio, system_prompt)
        runs.append(m)
        print(f"  run {i+1}: ttfa={m['ttfa_seconds']}  done={m['done_seconds']}")
    stats = aggregate_stats(runs)
    print()
    print(f"Level: {args.level}  Runs: {args.runs}")
    if stats["ttfa_median"] is not None:
        print(
            f"  TTFA median: {stats['ttfa_median']:.3f}s   "
            f"p95: {stats['ttfa_p95']:.3f}s"
        )
    else:
        print("  TTFA: no audio events captured")
    if stats["done_median"] is not None:
        print(
            f"  Done median: {stats['done_median']:.3f}s   "
            f"p95: {stats['done_p95']:.3f}s"
        )
    else:
        print("  Done: no response.done captured")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--level",
        choices=["minimal", "low", "medium", "high", "xhigh"],
        default="low",
    )
    p.add_argument("--runs", type=int, default=3)
    p.add_argument(
        "--audio", help="WAV file 16-bit mono 24kHz. If omitted, uses an embedded 1s sine."
    )
    p.add_argument("--model", default="gpt-realtime-2")
    args = p.parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
