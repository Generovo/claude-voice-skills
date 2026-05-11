# Latency tuning — three operating profiles for `gpt-realtime-whisper`

The transcription guide cites end-to-end latencies between **0.4 s and 3.0 s** depending on configuration. This file documents three operating points, the configuration knobs that produce them, their trade-offs, a measurement protocol, a decision tree for picking one, and the network-budget realities that bound the achievable floor.

All field names and event names trace back to `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md`. `[UNCONFIRMED]` items remain unconfirmed until probed on a live session.

---

## Profile 1 — Ultra-low (~0.4 s)

**Target end-to-end latency.** First `.delta` visible to the user within ~400 ms of the spoken word leaving their mouth.

**Use cases.** Live captions for streamed video / audio, IDE voice command, real-time accessibility overlays, voice search auto-complete.

**Configuration sketch.**

```json
{
  "type": "session.update",
  "session": {
    "type": "transcription",
    "audio": {
      "input": {
        "format": { "type": "audio/pcm", "rate": 24000 },
        "transcription": {
          "model": "gpt-realtime-whisper",
          "language": "en"
        },
        "turn_detection": {
          "type": "server_vad",
          "threshold": 0.4,
          "prefix_padding_ms": 200,
          "silence_duration_ms": 300
        }
      }
    }
  }
}
```

- **Small audio chunks.** Stream 20–40 ms PCM frames via `input_audio_buffer.append` — do not buffer.
- **Aggressive VAD.** Lower `threshold` and shorter `silence_duration_ms` mean the server commits speech segments sooner. `[UNCONFIRMED]` exact `server_vad` field names and default values for the transcription endpoint — the research file enumerates these for the conversational endpoint (`threshold`, `prefix_padding_ms`, `silence_duration_ms`); they almost certainly apply here too but were not explicitly re-enumerated in the transcription guide.
- **Explicit language hint.** Skipping auto-detect shaves the first-delta delay.

**Trade-offs.**

- Accuracy is the **worst** of the three profiles. The model commits with less context, so word-final consonants and homophones may be misheard initially. Revisions in subsequent deltas partially compensate.
- Robust to noise: bad. Lower VAD threshold catches more "speech-like" noise (typing, ambient HVAC) as utterance starts.
- Completeness: short utterances may be split mid-phrase if a speaker pauses for >300 ms.

**Best for.** UIs where appearing-fast is more important than being-right-the-first-time, and where the user can self-correct visually.

---

## Profile 2 — Balanced (0.8 – 1.2 s)

**Target end-to-end latency.** First `.delta` visible to the user within 0.8 – 1.2 s; `.completed` shortly after the speaker finishes.

**Use cases.** Meetings (Zoom-style captions), call-center agent screens, customer-support chat-with-voice, voice notes streaming. **Default for most production use cases.**

**Configuration sketch.**

```json
{
  "type": "session.update",
  "session": {
    "type": "transcription",
    "audio": {
      "input": {
        "format": { "type": "audio/pcm", "rate": 24000 },
        "transcription": {
          "model": "gpt-realtime-whisper",
          "language": "en"
        },
        "turn_detection": {
          "type": "server_vad",
          "threshold": 0.5,
          "prefix_padding_ms": 300,
          "silence_duration_ms": 500
        }
      }
    }
  }
}
```

- **Default chunk size.** 80–100 ms PCM frames.
- **Default VAD settings.** Mirroring the conversational endpoint's defaults — `threshold: 0.5`, `prefix_padding_ms: 300`, `silence_duration_ms: 500`. (Per the research file, these are the defaults shown for `server_vad` on the conversational endpoint; assumed identical for transcription, `[UNCONFIRMED]`.)
- **Language hint.** Still recommended.

**Trade-offs.**

- Accuracy: meaningfully better than ultra-low. The model has 500 ms more context per commit and can disambiguate most homophones on the first delta.
- Robust to noise: good. Default thresholds reject typical background noise.
- Completeness: utterance boundaries align with natural pauses; transcript segments are usually coherent sentences.

**Best for.** Almost any production transcript-display feature where users will read the text rather than glancing at it. Pick this unless you have a specific reason to deviate.

---

## Profile 3 — Quality-max (1.5 – 2 s)

**Target end-to-end latency.** First `.delta` may take 1.5 – 2 s; `.completed` arrives after long silence is confirmed.

**Use cases.** Medical / legal dictation (where transcript is reviewed before action), broadcast captioning (where typos go on the public record), regulated industries with audit requirements.

**Configuration sketch.**

```json
{
  "type": "session.update",
  "session": {
    "type": "transcription",
    "audio": {
      "input": {
        "format": { "type": "audio/pcm", "rate": 24000 },
        "transcription": {
          "model": "gpt-realtime-whisper",
          "language": "en"
        },
        "turn_detection": {
          "type": "server_vad",
          "threshold": 0.6,
          "prefix_padding_ms": 500,
          "silence_duration_ms": 800
        }
      },
      "include": ["item.input_audio_transcription.logprobs"]
    }
  }
}
```

- **Conservative VAD.** Higher `threshold` rejects more borderline noise; longer `silence_duration_ms` forces the model to wait for confident speech-end before committing.
- **Logprobs on.** Enable `include = ["item.input_audio_transcription.logprobs"]` so downstream UI can flag low-confidence tokens for human review.
- **Optional: manual commit mode.** Disable VAD entirely (`[UNCONFIRMED]` whether `turn_detection: null` or `turn_detection: { "type": "none" }` is the correct disable shape — neither is documented for transcription; the conversational endpoint also lacks an explicit "none" in the docs) and have the dictator press a "done" key, which triggers `input_audio_buffer.commit`.

**Trade-offs.**

- Accuracy: best. The model has the full utterance plus padding before committing.
- Robust to noise: best. High threshold ignores room tone, distant voices, paper rustle.
- Completeness: utterance segments may run long (full multi-sentence chunks), which simplifies downstream parsing but means the dictator waits longer before seeing the screen update.

**Best for.** Workflows where the transcript is reviewed and signed off before it has any effect. The dictator is operating in "write, then verify" mode, not "speak, see, correct."

---

## What `audio.input.transcription` actually controls

The research file documents `audio.input.transcription` as carrying:

- `model` — must be `gpt-realtime-whisper`.
- `language` — ISO-639-1 hint.

`[UNCONFIRMED]` whether additional fields (e.g. `prompt` for context biasing, `temperature` for sampling control) are accepted on the transcription sub-object. The research file did not enumerate them. The legacy `whisper-1` API accepted `prompt` for domain hinting (drug names, person names) — if this transfers, it would be a useful quality-max lever. Probe to confirm.

The latency-shaping levers documented and inferred:

| Lever | Field | Effect | Confidence |
|---|---|---|---|
| Audio chunk size | client-side, frame granularity of `input_audio_buffer.append` | smaller = lower buffer delay, more frames/sec | documented (research file) |
| VAD threshold | `audio.input.turn_detection.threshold` | lower = earlier speech detect | conversational endpoint default 0.5; `[UNCONFIRMED]` for transcription |
| VAD silence window | `audio.input.turn_detection.silence_duration_ms` | shorter = earlier commit, more splits | conversational default 500; `[UNCONFIRMED]` for transcription |
| VAD prefix padding | `audio.input.turn_detection.prefix_padding_ms` | longer = more context before commit | conversational default 300; `[UNCONFIRMED]` for transcription |
| Language hint | `audio.input.transcription.language` | skips auto-detect | documented |
| Logprobs | `session.include` includes `"item.input_audio_transcription.logprobs"` | no latency effect; enables downstream confidence UI | documented |
| Manual commit | client sends `input_audio_buffer.commit` | full control, no VAD-driven splits | documented |

---

## When to choose which — decision tree

```
Are you displaying the transcript live to a user who is the same person speaking?
├── Yes — is each word visible within ~500 ms important?
│        ├── Yes (live captions, accessibility) → Ultra-low
│        └── No (dictation review) → Quality-max
└── No — transcript is for a third party / downstream system
         ├── Listener is reading text in real time (meeting, call agent) → Balanced
         ├── Transcript feeds an LLM/agent that responds → Balanced
         └── Transcript is archival / compliance / will be reviewed → Quality-max
```

If unsure, start with **Balanced** and instrument latency / quality. Move to Ultra-low only if users complain about lag; move to Quality-max only if the error rate causes downstream harm.

---

## Measurement protocol

Before deploying, instrument the three profiles on a representative audio clip and compare. The harness referenced in this skill (`scripts/test-latency-vs-quality.py`, to be added) should:

1. Stream a known reference audio file through the transcription session at each profile's config.
2. Timestamp:
   - **TTF-delta** — time from sending the first audio frame containing speech to receiving the first `.delta` for the corresponding `item_id`.
   - **TTF-completed** — time from end-of-utterance in the source audio to receiving `.completed`.
3. Compute **WER** (word error rate) between the final `.completed` transcript and a ground-truth reference.
4. Report a 3×3 matrix: TTF-delta, TTF-completed, WER per profile.

Network RTT and audio clip choice both materially affect the numbers — measure from your production server's region, against audio that matches your real domain (call audio vs broadcast audio vs dictation).

→ See `scripts/test-latency-vs-quality.py` (to be added).

---

## Network considerations

End-to-end latency cannot drop below the network round-trip time between your client (or your server, if you proxy) and `api.openai.com`. RTT dominates over ~150 ms.

| Region pair | Typical RTT | Practical floor for Ultra-low |
|---|---|---|
| US-East ↔ OpenAI (us-east) | 10–30 ms | ~400 ms (model latency dominates) |
| EU ↔ OpenAI (us-east) | 80–120 ms | ~500 ms |
| MENA ↔ OpenAI (us-east) | 150–250 ms | ~600 ms (RTT-bound) |
| Asia-Pacific ↔ OpenAI (us-east) | 180–300 ms | ~700 ms |

Mitigations:

- **Place your proxy close to OpenAI.** If you run a server-side proxy (mint ephemeral keys, forward audio frames), host it in the same region as the OpenAI endpoint to minimize the OpenAI-side RTT. The user-to-proxy hop still costs RTT but that one is unavoidable.
- **WebSocket persistence.** Re-use a single WebSocket per session; do not tear down and reconnect between utterances. TCP+TLS handshake is 2–3 RTTs by itself.
- **No CDN for the realtime path.** Realtime is point-to-point; CDN edge caching does not apply. (Static assets, ephemeral-key UIs, etc. can still use a CDN.)
- **WebRTC vs WebSocket.** `[UNCONFIRMED]` whether a WebRTC variant exists for the transcription endpoint — the research file did not enumerate one for `gpt-realtime-whisper`. WebSocket is the documented transport.

For Ultra-low targets specifically, an RTT above 200 ms makes the 0.4 s number unachievable regardless of model speed — set realistic targets per region.

---

## Pitfalls when tuning

- **Tuning latency without measuring WER.** Cutting `silence_duration_ms` to 100 ms looks faster but degrades the transcript. Always measure both axes.
- **Confusing first-delta with first-completed.** Users perceive first-delta (text appears). Downstream consumers (CRM, LLM agents) consume first-completed. Optimizing the wrong one wastes effort.
- **Ignoring the audio capture pipeline.** If your `AudioWorkletNode` buffers 4096 samples before emitting (~170 ms at 24 kHz), no server-side tuning recovers that. Profile end-to-end including capture.
- **Setting `language` wrong.** If the speaker is multilingual or code-switches, an explicit language hint can hurt accuracy on the non-hinted segments. Omit the hint and accept the small auto-detect latency cost if input language is uncertain.
- **Assuming the conversational defaults apply.** The `server_vad` defaults documented in the research file are for the conversational endpoint. They are the best available reference for the transcription endpoint too, but `[UNCONFIRMED]` until probed.
