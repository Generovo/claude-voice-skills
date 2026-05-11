"""
test-latency-vs-quality.py — compare latency profiles vs transcription quality.

For each audio sample in samples/audio-pairs.jsonl, for each profile (ultra-low /
balanced / quality-max), open an OpenAI Realtime transcription session, push
the audio through, and measure:

  - TTFT: time to first text delta (proxy for "when does the user see
    something on screen").
  - Final transcript.
  - WER: Word Error Rate vs the reference transcript.

Output:
  Profile         Count   Mean TTFT   Mean WER
  ultra-low       3       0.418s      0.087
  balanced        3       0.962s      0.041
  quality-max     3       1.732s      0.018

Plus a `results-<timestamp>.jsonl` with per-run rows.

Run:
  python3 test-latency-vs-quality.py --pairs samples/audio-pairs.jsonl
  python3 test-latency-vs-quality.py --profiles ultra-low balanced
  python3 test-latency-vs-quality.py --model gpt-realtime-whisper

Env:
  OPENAI_API_KEY   Required.
  OPENAI_MODEL     Optional, default gpt-realtime-whisper (also overridable via --model).

Audio files
-----------
The samples referenced in `samples/audio-pairs.jsonl` are expected to be
WAV PCM16 mono 24 kHz (matches OpenAI's transcription input format directly).
You can supply your own pairs by editing the JSONL — each line:

    {"id": "...", "audio": "path/to/file.wav", "reference": "ground truth text"}

Sources / endpoint shape (verified 2026-05-11)
----------------------------------------------
- /Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md
- ~/.claude/skills/voice-transcribe-stream/SKILL.md
- ~/.claude/skills/voice-transcribe-stream/references/streaming-deltas.md
- ~/.claude/skills/voice-transcribe-stream/examples/twilio-transcribe.py
  (same session.update + unprefixed input_audio_buffer.append pattern)

[UNCONFIRMED] items propagated from research
--------------------------------------------
- Exact WS URL: trying ?model=... shape first; if that 4xx-s, the script will
  raise and you'd swap to `/v1/realtime/transcription_sessions?model=...`.
  Kept simple here because this is a benchmark tool — production code is in
  twilio-transcribe.py with both URLs wired up.
- VAD field names on the transcription endpoint. Per-profile VAD shapes are
  the same fields the conversational endpoint accepts.
- Whether the server emits `.transcription.delta` for THIS endpoint variant
  vs the previous `input_audio_transcription.delta` shape — we accept either
  defensively (substring match).
- `.completed` final-text field: probe `transcript`, `text`, `content`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import os
import re
import time
import wave
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import websockets


# --------------------------------------------------------------------------- #
# Endpoint                                                                    #
# --------------------------------------------------------------------------- #


WS_URL_PRIMARY = "wss://api.openai.com/v1/realtime?model={model}"
WS_URL_FALLBACK = (
    "wss://api.openai.com/v1/realtime/transcription_sessions?model={model}"
)


# --------------------------------------------------------------------------- #
# Pure functions (covered by unit tests)                                      #
# --------------------------------------------------------------------------- #


_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_text(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Used to make WER comparisons robust to capitalization and trailing
    punctuation differences. Not Unicode-folding (NFC/NFD) — keep accented
    characters as-is so French/Arabic don't get stripped of their meaning.
    """
    s = _PUNCT.sub(" ", s)
    return " ".join(s.split()).lower().strip()


def compute_wer(ref: str, hyp: str) -> float:
    """Word Error Rate = Levenshtein word edit distance / reference word count.

    Both strings are normalize_text-ed before comparison so WER reflects
    content differences, not formatting noise.

    Degenerate inputs:
      - empty ref + empty hyp -> 0.0
      - empty ref + non-empty hyp -> +inf (can't divide by zero — flag loudly)
      - empty hyp -> 1.0 (every reference word is a deletion)
    """
    r = normalize_text(ref).split()
    h = normalize_text(hyp).split()
    if not r:
        return math.inf if h else 0.0
    # Standard 2-D DP for Levenshtein over tokens.
    dp = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        dp[i][0] = i
    for j in range(len(h) + 1):
        dp[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            if r[i - 1] == h[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])
    return dp[len(r)][len(h)] / len(r)


def load_audio_pairs(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file: one {id, audio, reference} record per line.

    Blank lines and comments (lines starting with `#`) are skipped — useful
    when curating reference pairs by hand.
    """
    pairs: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            pairs.append(json.loads(line))
    return pairs


def aggregate_by_profile(
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Compute per-profile mean TTFT and mean WER, ignoring None entries.

    `count` is the max of the two lengths (so it reflects "I tried N runs"
    not just "I got N latency numbers").
    """
    by_profile: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: {"ttfts": [], "wers": []}
    )
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        p = r["profile"]
        counts[p] += 1
        ttft = r.get("ttft")
        wer = r.get("wer")
        if ttft is not None:
            by_profile[p]["ttfts"].append(float(ttft))
        if wer is not None and not (
            isinstance(wer, float) and math.isnan(wer)
        ):
            by_profile[p]["wers"].append(float(wer))

    out: dict[str, dict[str, Any]] = {}
    for p, vals in by_profile.items():
        out[p] = {
            "count": max(len(vals["ttfts"]), len(vals["wers"]), counts[p]),
            "mean_ttft": (
                sum(vals["ttfts"]) / len(vals["ttfts"]) if vals["ttfts"] else None
            ),
            "mean_wer": (
                sum(vals["wers"]) / len(vals["wers"]) if vals["wers"] else None
            ),
        }
    return out


# --------------------------------------------------------------------------- #
# Profile definitions                                                         #
# --------------------------------------------------------------------------- #


PROFILES: dict[str, dict[str, str]] = {
    "ultra-low": {
        "description": (
            "Aim ~0.4s TTFT. Tune via short audio chunks + aggressive VAD."
        ),
    },
    "balanced": {
        "description": "Aim ~1s TTFT. Default.",
    },
    "quality-max": {
        "description": "Aim ~1.5-2s TTFT. Larger windows for accuracy.",
    },
}

# Per-profile field overrides for audio.input.transcription.
# [UNCONFIRMED] — exact VAD field names on the transcription endpoint mirror
# the conversational endpoint per research. Document and ship the most likely
# shape (see references/latency-tuning.md).
PROFILE_CONFIG: dict[str, dict[str, int | float]] = {
    "ultra-low": {"chunk_ms": 80, "vad_threshold": 0.4, "silence_ms": 200},
    "balanced": {"chunk_ms": 200, "vad_threshold": 0.5, "silence_ms": 500},
    "quality-max": {"chunk_ms": 500, "vad_threshold": 0.6, "silence_ms": 1000},
}


# --------------------------------------------------------------------------- #
# API integration                                                             #
# --------------------------------------------------------------------------- #


def _load_wav_pcm16_24k(audio_path: Path) -> bytes:
    """Read a WAV file and validate it's PCM16 mono 24 kHz.

    The transcription endpoint takes audio/pcm @ 24 kHz directly; mismatched
    inputs would skew the latency/WER measurements, so we fail loudly.
    """
    with wave.open(str(audio_path), "rb") as w:
        if (
            w.getsampwidth() != 2
            or w.getnchannels() != 1
            or w.getframerate() != 24000
        ):
            raise ValueError(
                f"Expected WAV 16-bit mono 24kHz: {audio_path} "
                f"(got width={w.getsampwidth()} ch={w.getnchannels()} "
                f"rate={w.getframerate()})"
            )
        return w.readframes(w.getnframes())


async def transcribe_one(
    audio_path: Path, profile: str, model: str
) -> tuple[float | None, str]:
    """Open a session, push audio, return (ttft_seconds, final_transcript).

    Cancels the read loop as soon as we have both a TTFT marker and a
    finalized transcript (or a server error). Caller is responsible for
    handling FileNotFoundError / ValueError for bad audio inputs.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required")

    audio = _load_wav_pcm16_24k(audio_path)

    headers = [
        ("Authorization", f"Bearer {api_key}"),
    ]
    url = WS_URL_PRIMARY.format(model=model)

    cfg = PROFILE_CONFIG[profile]
    chunk_ms = int(cfg["chunk_ms"])
    # PCM16 mono @ 24 kHz: 2 bytes/sample * 24000 samples/s * (chunk_ms/1000).
    chunk_bytes = int(24000 * 2 * chunk_ms / 1000)

    ttft: float | None = None
    transcript_buf: list[str] = []
    started: float | None = None

    async with websockets.connect(
        url,
        additional_headers=headers,
        max_size=16 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
    ) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "transcription",
                        "model": model,
                        "audio": {
                            "input": {
                                "format": {
                                    "type": "audio/pcm",
                                    "rate": 24000,
                                },
                                "transcription": {"model": model},
                                # [UNCONFIRMED] VAD field names on the
                                # transcription endpoint mirror the
                                # conversational endpoint.
                                "turn_detection": {
                                    "type": "server_vad",
                                    "threshold": cfg["vad_threshold"],
                                    "silence_duration_ms": cfg["silence_ms"],
                                },
                            }
                        },
                    },
                }
            )
        )

        started = time.monotonic()

        # Send audio in chunks. Unprefixed event name — transcription
        # endpoint uses the conversational vocabulary (NOT the
        # session.-prefixed translate variant).
        for i in range(0, len(audio), chunk_bytes):
            piece = audio[i : i + chunk_bytes]
            if not piece:
                break
            await ws.send(
                json.dumps(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(piece).decode("ascii"),
                    }
                )
            )
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

        async for raw in ws:
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            t = str(ev.get("type", ""))

            # First text delta = TTFT marker. Match defensively on either
            # `*.transcription.delta` (conversational vocabulary) or any
            # `*.delta` event whose type string contains "transcript".
            if t.endswith(".transcription.delta") or (
                t.endswith(".delta") and "transcript" in t
            ):
                if ttft is None and started is not None:
                    ttft = time.monotonic() - started
                delta = ev.get("delta") or ev.get("text") or ""
                if isinstance(delta, str) and delta:
                    transcript_buf.append(delta)
            elif t.endswith(".completed") and (
                "transcription" in t or "transcript" in t
            ):
                final = (
                    ev.get("transcript") or ev.get("text") or ev.get("content")
                )
                if isinstance(final, str) and final:
                    transcript_buf = [final]
                break
            elif t == "error" or t.endswith(".error"):
                # Server-side error — stop and let the caller see what we got.
                break

    return (ttft, "".join(transcript_buf).strip())


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #


async def _amain(args: argparse.Namespace) -> None:
    pairs = load_audio_pairs(Path(args.pairs))
    rows: list[dict[str, Any]] = []
    for p in pairs:
        for profile in args.profiles:
            audio_path = Path(p["audio"])
            try:
                ttft, hyp = await transcribe_one(audio_path, profile, args.model)
            except (FileNotFoundError, ValueError, RuntimeError) as e:
                print(f"  SKIP {p['id']}/{profile}: {e}")
                rows.append(
                    {
                        "id": p["id"],
                        "profile": profile,
                        "ttft": None,
                        "wer": None,
                        "hypothesis": "",
                        "error": str(e),
                    }
                )
                continue
            wer = compute_wer(p["reference"], hyp)
            rows.append(
                {
                    "id": p["id"],
                    "profile": profile,
                    "ttft": ttft,
                    "wer": wer,
                    "hypothesis": hyp,
                }
            )
            ttft_s = f"{ttft:.3f}s" if ttft is not None else "n/a"
            print(f"  {p['id']}/{profile}: TTFT={ttft_s}  WER={wer:.3f}")

    agg = aggregate_by_profile(rows)
    print()
    print(
        f"  {'Profile':14s} {'Count':>5s}  {'Mean TTFT':>10s}  {'Mean WER':>9s}"
    )
    for p_name in ["ultra-low", "balanced", "quality-max"]:
        if p_name not in agg:
            continue
        a = agg[p_name]
        ttft_s = (
            f"{a['mean_ttft']:.3f}s" if a["mean_ttft"] is not None else "n/a"
        )
        wer_s = (
            f"{a['mean_wer']:.3f}" if a["mean_wer"] is not None else "n/a"
        )
        print(
            f"  {p_name:14s} {a['count']:>5d}  {ttft_s:>10s}  {wer_s:>9s}"
        )

    out = Path(f"results-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl")
    with open(out, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"  Written: {out}")


def main() -> None:
    p = argparse.ArgumentParser(
        description=(
            "Benchmark OpenAI Realtime transcription latency profiles vs "
            "WER on a fixed set of (audio, reference) pairs."
        )
    )
    p.add_argument(
        "--pairs",
        default="samples/audio-pairs.jsonl",
        help="Path to JSONL with {id, audio, reference} records.",
    )
    p.add_argument(
        "--model",
        default=os.environ.get("OPENAI_MODEL", "gpt-realtime-whisper"),
        help="OpenAI model id (default gpt-realtime-whisper).",
    )
    p.add_argument(
        "--profiles",
        nargs="+",
        default=["ultra-low", "balanced", "quality-max"],
        choices=list(PROFILES.keys()),
        help="Which latency profiles to benchmark.",
    )
    args = p.parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
