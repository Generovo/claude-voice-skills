"""
test-translation-quality.py — measure translation quality vs reference on a JSONL eval set.

Purpose
-------
For each pair (source audio file + reference translation) in a JSONL eval set,
this script opens an OpenAI Realtime Translate session, streams the source
audio, collects the translated text deltas, and computes a BLEU score against
the reference. It aggregates results by language-pair and writes a detailed
JSONL results file for later inspection.

Run
---
    python3 test-translation-quality.py \\
        --pairs samples/pairs.jsonl \\
        --model gpt-realtime-translate

Env: OPENAI_API_KEY

Output
------
    fr-en-1  fr→en  BLEU=0.847
    en-fr-1  en→fr  BLEU=0.621
    ar-fr-1  ar→fr  BLEU=0.733

      fr→en: 1 pairs, mean BLEU 0.847
      en→fr: 1 pairs, mean BLEU 0.621
      ar→fr: 1 pairs, mean BLEU 0.733
      Total: 3 pairs, mean BLEU 0.734
      Written: results-20260511-104213.jsonl

Sample input (samples/pairs.jsonl)
----------------------------------
    {"id": "fr-en-1", "source_audio": "samples/audio/fr-greeting.wav",
     "source_lang": "fr", "target_lang": "en",
     "reference": "hello and welcome how can I help you today"}

Audio format
------------
Each `source_audio` MUST be a WAV file containing 16-bit PCM, mono, at 24 kHz —
that is, the only documented input format on the translation endpoint (see
research/openai-realtime-api-2026-05-11.md). The script reads `getframerate()`,
`getnchannels()`, and `getsampwidth()` and refuses to send mismatched audio so
that BLEU scores reflect translation quality, not codec mismatch artifacts.

Source of truth
---------------
Every event name, JSON field, and endpoint URL in this script traces to
/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md.
Items the research file flagged `[UNCONFIRMED]` are propagated as inline
comments below.

The translation endpoint uses a DIFFERENT event vocabulary from the
conversational endpoint. Audio buffer events are PREFIXED with `session.`:

    session.input_audio_buffer.append     (client -> server)
    session.output_audio.delta            (server -> client, base64 audio)
    session.output_transcript.delta       (server -> client, target-lang text)
    session.input_transcript.delta        (server -> client, source-lang text)

This script only consumes the transcript deltas (it's a quality eval, not an
audio playback tool). See ../SKILL.md ## Event vocabulary — CRITICAL DIFFERENCE.

[UNCONFIRMED] items propagated here
-----------------------------------
- The translation endpoint's full set of `.done` / `.completed` companion events
  is not enumerated in the docs. We treat any event whose `type` ends in
  `.done` or is exactly `"error"` as a session-end signal.
- `session.created` and `session.updated` lifecycle events are probable but not
  contractual. We log them at debug level if they arrive.
- Whether `session.input_audio_buffer.commit` is accepted on this endpoint is
  [UNCONFIRMED]. We do NOT send it — VAD on the server side is expected to drive
  finalization, and forcing a commit risks rejection. If you observe your eval
  hanging after audio upload, probe whether a commit is required.
- The `voice` configuration field is `[UNCONFIRMED]` for translation — we
  configure only `audio.output.language`.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import wave
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import websockets

TRANSLATE_WS = "wss://api.openai.com/v1/realtime/translations?model={model}"


# --------------------------------------------------------------------------- #
# Pure functions (covered by test_translation_quality_unit.py)                #
# --------------------------------------------------------------------------- #


_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_text(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Used as a pre-step for the fallback BLEU implementation when sacrebleu is
    not installed. sacrebleu has its own tokenisation, so the helper is only
    called by `compute_bleu_simple` in the fallback branch.
    """
    s = _PUNCT.sub(" ", s)
    s = " ".join(s.split())
    return s.lower().strip()


def compute_bleu_simple(ref: str, hyp: str) -> float:
    """Compute a BLEU-style similarity in [0.0, 1.0].

    Strategy:
      1. Try `sacrebleu` if installed (canonical BLEU implementation).
      2. Fall back to unigram F1 on normalised tokens.

    The fallback is intentionally simple so the eval script remains usable
    in environments where sacrebleu can't be installed (e.g. air-gapped CI).
    For headline numbers in a paper or PR, install sacrebleu.
    """
    if not ref or not hyp:
        return 0.0
    try:
        import sacrebleu  # type: ignore

        score = sacrebleu.corpus_bleu([hyp], [[ref]]).score / 100.0
        return float(min(max(score, 0.0), 1.0))
    except ImportError:
        ref_tokens = normalize_text(ref).split()
        hyp_tokens = normalize_text(hyp).split()
        if not ref_tokens or not hyp_tokens:
            return 0.0
        ref_set, hyp_set = set(ref_tokens), set(hyp_tokens)
        overlap = len(ref_set & hyp_set)
        precision = overlap / len(hyp_set) if hyp_set else 0.0
        recall = overlap / len(ref_set) if ref_set else 0.0
        if precision + recall == 0:
            return 0.0
        return 2 * precision * recall / (precision + recall)


def load_pairs(path: Path) -> List[Dict[str, Any]]:
    """Parse JSONL of {id, source_audio, source_lang, target_lang, reference}.

    Blank lines are skipped. Each remaining line MUST be a valid JSON object.
    """
    pairs: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            pairs.append(json.loads(line))
    return pairs


def aggregate_by_pair(
    rows: List[Tuple[str, str, float, str]],
) -> Dict[str, Dict[str, Any]]:
    """Aggregate rows by lang_pair.

    `rows` is a list of `(pair_id, lang_pair, bleu, hypothesis)`. The hypothesis
    is unused in aggregation but kept on the row for later JSONL dumping.
    """
    by_pair: Dict[str, List[float]] = defaultdict(list)
    for _, lang_pair, bleu, _ in rows:
        by_pair[lang_pair].append(bleu)
    return {
        lp: {"count": len(scores), "mean_bleu": sum(scores) / len(scores)}
        for lp, scores in by_pair.items()
    }


# --------------------------------------------------------------------------- #
# API integration                                                             #
# --------------------------------------------------------------------------- #


def _load_wav_24k_mono_pcm16(audio_path: Path) -> bytes:
    """Read a WAV file, verify it matches the translation endpoint's input
    format (PCM16 mono 24 kHz), and return the raw PCM frames as bytes.
    """
    with wave.open(str(audio_path), "rb") as w:
        nchannels = w.getnchannels()
        sampwidth = w.getsampwidth()
        framerate = w.getframerate()
        if sampwidth != 2 or nchannels != 1 or framerate != 24000:
            raise ValueError(
                f"Expected WAV PCM16 mono 24kHz, got "
                f"width={sampwidth*8}bit, channels={nchannels}, rate={framerate}: "
                f"{audio_path}"
            )
        return w.readframes(w.getnframes())


def _is_terminal_event(etype: str) -> bool:
    """Return True for any event that should end the receive loop.

    [UNCONFIRMED] which exact event signals the end of a translated utterance
    on this endpoint. We branch on the documented `response.done`-style names
    as well as a generic `*.done` / `*.completed` / `error` suffix.
    """
    if etype == "error" or etype == "invalid_request_error":
        return True
    if etype in ("response.done", "session.response.done"):
        return True
    if etype.endswith(".done") or etype.endswith(".completed"):
        # We do NOT terminate on `session.output_audio.done` or
        # `session.output_transcript.done` alone, because a session may emit
        # several utterances before the WS closes. But for an eval script
        # that pushes a single audio file, the first `.done` is a reasonable
        # finalisation hint. Refine here if your eval audio contains multiple
        # utterances per file.
        return True
    return False


async def translate_one(pair: Dict[str, Any], model: str) -> str:
    """Open a translate session, stream `pair.source_audio`, return target text.

    Returns the concatenation of all `session.output_transcript.delta` payloads
    received during the session — the target-language translation as text.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required")

    audio_path = Path(pair["source_audio"])
    if not audio_path.exists():
        raise FileNotFoundError(f"Sample audio not found: {audio_path}")
    audio = _load_wav_24k_mono_pcm16(audio_path)

    headers = [("Authorization", f"Bearer {api_key}")]
    url = TRANSLATE_WS.format(model=model)

    output_text: List[str] = []
    async with websockets.connect(
        url,
        additional_headers=headers,
        max_size=16 * 1024 * 1024,
    ) as ws:
        # session.update — translation endpoint uses a minimal config surface.
        # Per references/session-config.md the only documented field is
        # audio.output.language. [UNCONFIRMED] input format field shape — we
        # send the conservative PCM16 24 kHz hint that the conversational
        # schema uses; if the translation endpoint rejects unknown fields
        # silently, the wire will still accept the audio frames.
        await ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcm", "rate": 24000},
                            },
                            "output": {
                                "language": pair["target_lang"],
                            },
                        },
                    },
                }
            )
        )

        # Stream the audio in ~1s chunks (24000 samples * 2 bytes/sample).
        # CRITICAL: event name is PREFIXED with `session.` on this endpoint.
        # CONTRAST: the conversational endpoint uses unprefixed
        # `input_audio_buffer.append` (CONTRAST). See ../SKILL.md.
        chunk = 24000 * 2
        for i in range(0, len(audio), chunk):
            piece = audio[i : i + chunk]
            if not piece:
                break
            await ws.send(
                json.dumps(
                    {
                        "type": "session.input_audio_buffer.append",
                        "audio": base64.b64encode(piece).decode("ascii"),
                    }
                )
            )

        # We do NOT send `session.input_audio_buffer.commit` — its existence
        # is [UNCONFIRMED] on this endpoint. Server VAD should finalise the
        # utterance once we stop appending. If your eval hangs here, probe
        # whether a commit is needed.

        # Collect transcript deltas until a terminal event arrives.
        try:
            async for raw in ws:
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                etype = ev.get("type", "")
                if etype == "session.output_transcript.delta":
                    output_text.append(ev.get("delta", ""))
                elif etype == "session.input_transcript.delta":
                    # Source-language transcript — useful for spot-checking
                    # that ASR understood the input. Not used for BLEU.
                    pass
                elif etype == "session.created" or etype == "session.updated":
                    pass
                elif _is_terminal_event(etype):
                    break
        except websockets.ConnectionClosed:
            # The server closed the WS — typical at end of stream. We've
            # already collected whatever transcript deltas arrived.
            pass

    return "".join(output_text).strip()


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #


async def _amain(args: argparse.Namespace) -> None:
    pairs = load_pairs(Path(args.pairs))
    rows: List[Tuple[str, str, float, str]] = []
    for p in pairs:
        try:
            hyp = await translate_one(p, args.model)
        except (FileNotFoundError, ValueError) as e:
            print(f"  SKIP {p.get('id', '?')}: {e}")
            continue
        except Exception as e:  # network / WS / auth
            print(f"  FAIL {p.get('id', '?')}: {e!r}")
            continue
        bleu = compute_bleu_simple(p.get("reference", ""), hyp)
        lang_pair = f"{p.get('source_lang', '?')}→{p.get('target_lang', '?')}"
        rows.append((p.get("id", "?"), lang_pair, bleu, hyp))
        print(f"  {p.get('id', '?')}  {lang_pair}  BLEU={bleu:.3f}")

    summary = aggregate_by_pair(rows)
    print()
    for lp, stats in sorted(summary.items()):
        print(f"  {lp}: {stats['count']} pairs, mean BLEU {stats['mean_bleu']:.3f}")
    if rows:
        total_mean = sum(b for _, _, b, _ in rows) / len(rows)
        print(f"  Total: {len(rows)} pairs, mean BLEU {total_mean:.3f}")

    if rows:
        out = Path(f"results-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl")
        with open(out, "w", encoding="utf-8") as f:
            for pid, lp, bleu, hyp in rows:
                f.write(
                    json.dumps(
                        {
                            "id": pid,
                            "lang_pair": lp,
                            "bleu": bleu,
                            "hypothesis": hyp,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        print(f"  Written: {out}")


def main() -> None:
    p = argparse.ArgumentParser(
        description="Measure translation quality vs reference on a JSONL eval set.",
    )
    p.add_argument(
        "--pairs",
        default="samples/pairs.jsonl",
        help="JSONL of test pairs (default: samples/pairs.jsonl)",
    )
    p.add_argument(
        "--model",
        default="gpt-realtime-translate",
        help="OpenAI translate model id (default: gpt-realtime-translate)",
    )
    args = p.parse_args()
    asyncio.run(_amain(args))


if __name__ == "__main__":  # pragma: no cover
    main()
