---
name: voice-transcribe-stream
description: Use when building streaming speech-to-text with OpenAI's GPT-Realtime-Whisper model — live captions, meeting transcription, voice notes, low-latency transcripts. Covers progressive deltas, latency tuning profiles (0.4s / 0.8-1.2s / 1.5-2s), and the streaming protocol. Do NOT use for conversational agents (see voice-agent-realtime) or translation (see voice-translate-live).
---

# Voice Transcribe Stream (GPT-Realtime-Whisper)

This skill is the operational playbook for building real-time streaming speech-to-text on OpenAI's `gpt-realtime-whisper` model (announced 2026-05-07). Every event name, JSON field, endpoint URL, and pricing number traces back to `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md`, itself sourced from the official OpenAI docs and a live API probe. Items the research file flagged `[UNCONFIRMED]` carry the same tag here — do not silently treat them as proven.

> **READ THIS BEFORE WIRING THE UI.** The transcription endpoint emits **progressive deltas** that may be **revised** before being finalized. Render `conversation.item.input_audio_transcription.delta` as speculative (italic / grayed) and only commit on `.completed`. See `## Progressive deltas — the core UX pattern`.

## When to use this skill

- **Live captions** for streamed video / events / accessibility overlays where words must appear on screen within ~0.5s of being spoken.
- **Meeting transcription** with low-latency transcript panels (Zoom-style captions, real-time minutes).
- **Call-center / customer support transcription** — agent screen shows what the customer is saying as they say it.
- **Real-time UI feedback** for voice search, IDE voice commands, voice-to-text on mobile.
- **Voice notes** where users want streaming feedback rather than waiting for a batch transcript (WhatsApp-style live transcribe).
- **Healthcare / legal dictation** with progressive correction — dictator sees text appear, edits inline, and the model self-corrects via delta revisions.
- **Voice-agent front-end** — feeding a transcript stream into a downstream LLM or business logic that does not need the full `gpt-realtime-2` audio-in / audio-out pipeline.

## When NOT to use

- If you want the model to **respond / take actions** (agent with tool calling, voice replies) → use `voice-agent-realtime` (powered by `gpt-realtime-2`). Whisper streams text only — no audio output, no reasoning, no tool calls.
- If you want **translation** (audio in one language → audio in another) → use `voice-translate-live` (powered by `gpt-realtime-translate`, $0.034/min). Whisper transcribes in the source language only.
- If the audio is **batch / non-streaming** (recorded file, voice memo with no live UI) → use the standard `whisper-1` or `gpt-4o-transcribe` REST endpoint. The realtime API adds infrastructure cost for no benefit on async workloads.
- If you need **word-level timestamps or speaker diarization** → consider `gpt-4o-transcribe-diarize` (separate model in the live model list). `[UNCONFIRMED]` whether `gpt-realtime-whisper` supports diarization — the model card and transcription guide did not mention it; assume not, until probed.

---

## Model at a glance

| Property | Value |
|---|---|
| Model ID | `gpt-realtime-whisper` |
| Modalities in | Audio, text (text input for prompt / context hinting) |
| Modalities out | Text only (no audio output) |
| Tool calling | Not applicable |
| Reasoning levels | Not applicable |
| Pricing | **$0.017 / minute** of audio duration |
| Max context window | 16,000 tokens |
| Max output per response | 2,000 tokens |
| Audio format | `audio/pcm` PCM16 at 24 kHz (shown in docs example); `[UNCONFIRMED]` whether `audio/pcmu` (μ-law for telephony) is accepted |
| Sample rate | 24,000 Hz in the example; `[UNCONFIRMED]` whether 16/8 kHz are accepted |
| Knowledge cutoff | Sep 30, 2024 |
| Latency target (docs) | 0.4 – 3.0 seconds depending on use case |

Full pricing worked examples → `references/pricing-limits.md`.

---

## Endpoint and session config

Per the research file, the WebSocket URL for the transcription endpoint is `[UNCONFIRMED]` — two likely shapes need to be probed:

```
# Option A (likely; matches conversational base path)
wss://api.openai.com/v1/realtime?model=gpt-realtime-whisper

# Option B (model card lists this REST path; may also be the WS path)
wss://api.openai.com/v1/realtime/transcription_sessions?model=gpt-realtime-whisper

Header: Authorization: Bearer ${OPENAI_API_KEY}
```

The discriminator is `session.type = "transcription"` inside the first `session.update`. That is what tells the realtime backend you want transcription semantics rather than a conversational response loop.

Minimal session config (verbatim shape from the transcription guide):

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
        "turn_detection": { "type": "server_vad" }
      }
    },
    "include": ["item.input_audio_transcription.logprobs"]
  }
}
```

Key fields:
- `session.type = "transcription"` — switches the session into transcription mode (no response loop, no audio output).
- `audio.input.transcription.model = "gpt-realtime-whisper"` — selects the model.
- `audio.input.transcription.language` — ISO-639-1 hint. Omit to let the model auto-detect; supply when known to reduce latency and improve quality.
- `audio.input.turn_detection` — `server_vad` chunks audio at utterance boundaries automatically; manual mode requires sending `input_audio_buffer.commit` yourself.
- `include = ["item.input_audio_transcription.logprobs"]` — opt-in per-token log probabilities for confidence-aware UI rendering.

Audio chunks then arrive via the **unprefixed** `input_audio_buffer.append` event (matching the conversational endpoint vocabulary — NOT the `session.`-prefixed translation endpoint):

```json
{ "type": "input_audio_buffer.append", "audio": "<base64 PCM16 24kHz>" }
```

For manual mode (no VAD), follow up with `input_audio_buffer.commit` to flush.

---

## Progressive deltas — the core UX pattern

The transcription endpoint emits **two transcription events** flagged in the research file:

- `conversation.item.input_audio_transcription.delta` — `event.delta` carries newly available transcript text. Identifies the in-progress utterance via `event.item_id`.
- `conversation.item.input_audio_transcription.completed` — final transcript for that `item_id`. The model is committing the text; it will not revise this segment further.

**Rendering rule of thumb.**

1. **Speculative.** As `.delta` events stream in, append to a per-`item_id` buffer and render it as speculative — italic, lower opacity, no line wrap commit.
2. **Finalized.** On `.completed`, replace the speculative buffer for that `item_id` with the final text, render it in the regular style, and scroll-anchor it above the next live region.
3. **Revisions.** Deltas may revise earlier deltas within the same `item_id` — the model can correct a word it heard wrong now that it has more context. The safe pattern is: keep the speculative region as a single replaceable block per `item_id` (don't append-only) and re-render the full buffer each delta. On `.completed`, the segment is locked.

`[UNCONFIRMED]` — the research file does not explicitly document delta retraction / replacement semantics for `gpt-realtime-whisper`. Streaming Whisper variants typically allow mid-segment correction; assume revisions happen and code defensively. Probe on a live session to confirm.

Full deep-dive on event flow, payload shapes, UX patterns (append-only / replace-last / finalized-above + speculative-below) → `references/streaming-deltas.md`.

---

## Latency tuning profiles

The transcription guide cites 0.4 – 3.0 s end-to-end latency depending on configuration. Three operating points are useful in practice:

| Profile | Target end-to-end | Use cases |
|---|---|---|
| **Ultra-low** | ~0.4 s | Live captions for streamed video / audio, IDE voice command, real-time accessibility overlays |
| **Balanced** | 0.8 – 1.2 s | Meetings, call-center, customer support — default for most cases |
| **Quality-max** | 1.5 – 2 s | Medical / legal dictation, broadcast captioning, anything where accuracy outweighs speed |

What controls latency:

- **Chunk size for `input_audio_buffer.append`.** `[UNCONFIRMED]` whether the transcription endpoint uses the unprefixed `input_audio_buffer.append` (research shows yes — it matches the conversational endpoint) or the `session.`-prefixed translation variant. Smaller chunks (e.g. 20 ms) cut buffer latency at the cost of more frames per second on the wire. 100 ms is a balanced default.
- **Server VAD settings.** `audio.input.turn_detection` with `server_vad` chunks at utterance boundaries. Lower `silence_duration_ms` and `threshold` produce earlier commits (lower latency, more false splits); raising them improves coherence (higher latency).
- **Manual commit timing.** If you disable VAD and drive commits yourself, you control the exact latency / context trade-off. Useful for dictation where the speaker controls pauses, less useful for free conversation.
- **Language hint.** Setting `audio.input.transcription.language` explicitly skips the model's auto-detect step, shaving a small but measurable amount off first-delta latency.
- **Network RTT.** End-to-end cannot drop below your client's round-trip to `api.openai.com`. See `references/latency-tuning.md` for the network-budget breakdown.

Full per-profile config and decision tree → `references/latency-tuning.md`.

---

## Long-session strategy

The model has a **16K** context window and **2K** max output tokens. For multi-hour transcription (long meetings, all-day captioning, hotline shifts), a single session will eventually exhaust either limit.

**Rotation pattern.**

- Open a new transcription session every **N minutes** (e.g. every 10 minutes for safety, every 15 if conservative).
- **Buffer overlap.** Before closing the old session, save the last **2–3 seconds** of input audio. Send that overlap into the new session before resuming live capture. This prevents mid-word truncation across the session boundary — the new session re-hears the tail of the old utterance and emits a coherent transcript without a visible seam.
- **Item-ID continuity.** `item_id`s do not carry over across sessions. In your UI / store, prepend a session-level prefix so transcript items remain unique across rotations.
- **Cost note.** Rotation is **free** — billing is by audio duration, not by session count. See `references/pricing-limits.md` for a worked rotation example.

`[UNCONFIRMED]` exact hard-cap behavior. The research file does not say what happens when the 16K context is exhausted — silent drop, error event, or session close. Implement rotation prophylactically rather than waiting for the failure mode to manifest.

---

## Channel-specific transport

The transcription endpoint accepts WebSocket; clients vary by where the audio originates.

### Web (live captions overlay)

- Browser captures mic via `getUserMedia`, downsamples / encodes to PCM16 24 kHz in an `AudioWorkletNode`, base64-chunks, and sends via WebSocket to your server, which forwards to OpenAI.
- Direct browser-to-OpenAI is possible via ephemeral key mint (REST `client_secrets`) `[UNCONFIRMED]` — the research file lists the conversational and translation mint endpoints but did not enumerate one for `transcription_sessions`. Server-proxy is the safe default.
- UI renders speculative delta + finalized text as described above.

→ see `examples/live-captions.tsx` (to be added)

### Telephony (Twilio call transcription)

- Twilio Media Streams sends μ-law 8 kHz PCM frames over a WebSocket to your bridge.
- Your bridge resamples to PCM16 24 kHz (the docs example only shows this format) and base64-encodes per `input_audio_buffer.append`.
- `[UNCONFIRMED]` whether `audio/pcmu` is accepted on this endpoint — if so, you can skip the resample and feed Twilio's native frames through. Until proven, resample.
- Use VAD-driven commits so each utterance's `.completed` event corresponds to a usable transcript segment for downstream CRM logging.

→ see `examples/twilio-transcribe.py` (to be added)

### WhatsApp voice notes (streaming)

- WhatsApp Cloud API delivers a voice-note media URL after the user sends it; the file is OGG/Opus.
- For a streaming UX (live transcript appearing as the user listens to their own playback, or for an internal review tool), pipe the decoded PCM into the transcription session in real time instead of using batch `whisper-1`.
- Useful when paired with WhatsApp Business agents that need to react to the content of voice notes faster than a full file decode + batch transcribe cycle.

→ see `examples/whatsapp-voice-notes.py` (to be added)

### Meeting bots (live minutes)

- Recall.ai (or similar) joins as a virtual participant and exposes meeting audio as a WebSocket stream.
- Fan out: same audio → transcription session for live captions, and (separately) → `voice-translate-live` if multilingual translation is needed.
- For meetings exceeding ~10 minutes, apply the rotation pattern above. Stitch transcripts in your store by chronological ordering, not by session order.

→ see `examples/meeting-minutes.py` (to be added)

---

## Pricing & limits

| Property | Value |
|---|---|
| Pricing | $0.017 / minute of audio duration |
| Context window | 16,000 tokens |
| Max output | 2,000 tokens |
| Billing unit | Audio duration (not tokens) |
| Rate limits | `[UNCONFIRMED]` — see OpenAI platform docs |

Worked examples (60-min support call, 200-hour/day call center, 8-hour live event, rotated long session) → `references/pricing-limits.md`.

---

## Common pitfalls

- **Buffering too aggressively.** Holding 1-second chunks before sending kills the latency budget. Stream 20–100 ms PCM chunks as you capture; don't batch audio for "fewer round-trips."
- **Confusing this with the conversational endpoint.** No audio output, no `response.create`, no `response.output_*` events. If you wrote code that listens for `response.output_audio_transcript.delta`, you're on the wrong skill — that's `voice-agent-realtime`. The transcription events are `conversation.item.input_audio_transcription.delta` / `.completed`.
- **Ignoring delta corrections in the UI.** If you append-only render every `.delta` chunk, mid-segment corrections produce garbled doubled text. Render each `item_id` as a single replaceable block until `.completed`.
- **Hitting 2K output tokens on long sessions.** Without rotation, a multi-hour session eventually saturates the output budget. See `## Long-session strategy`.
- **Audio format mismatch.** Default is PCM16 at 24 kHz. `[UNCONFIRMED]` whether telephony's 8 kHz μ-law (`audio/pcmu`) is accepted directly; until probed, resample on ingress.
- **Diarization expected but `[UNCONFIRMED]`.** The model card and transcription guide did not mention speaker diarization for `gpt-realtime-whisper`. If you need speaker labels, evaluate `gpt-4o-transcribe-diarize` or layer a separate diarizer on the audio.
- **Mixing event vocabularies across skills.** The translation endpoint uses `session.input_audio_buffer.append` (prefixed); this skill uses the unprefixed `input_audio_buffer.append`. Don't copy-paste between adapters without checking.
- **Trusting unconfirmed lifecycle events.** `session.created`, `session.updated`, `input_audio_buffer.speech_started/stopped/committed`, `error` are `[UNCONFIRMED]` for this endpoint — likely present (shared session vocabulary with the conversational endpoint) but not contractually documented. Probe before depending on them.

---

## Reference index

Long-form references (this dispatch):
- `references/streaming-deltas.md` — full event flow, payload shapes, partial vs final, UX patterns for live captions, corrections / rollback, cross-reference to the conversational endpoint events catalogue.
- `references/latency-tuning.md` — per-profile config deep-dive (ultra-low / balanced / quality-max), trade-offs, measurement protocol, decision tree, network considerations.
- `references/pricing-limits.md` — full pricing table, 4 worked cost examples (60-min support call, 200h/day call center, 8h live event, rotated 1h session), context limits, rate limits.

Examples (to be added):
- `examples/live-captions.tsx` — browser WebSocket client with speculative-delta UI for live captions overlay.
- `examples/twilio-transcribe.py` — Twilio Media Streams ↔ OpenAI transcription bridge with VAD-driven CRM logging.
- `examples/whatsapp-voice-notes.py` — WhatsApp Cloud API voice-note streaming transcription.
- `examples/meeting-minutes.py` — Recall.ai meeting-bot transcription with 10-minute session rotation.

Scripts (to be added):
- `scripts/test-latency-vs-quality.py` — live measurement harness that compares the three latency profiles against a reference clip and reports first-delta / final-completed latencies plus WER.

Canonical source for every fact in this skill: `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md`.
