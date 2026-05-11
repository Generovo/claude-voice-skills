# Pricing & limits — `gpt-realtime-whisper`

All numbers here trace back to `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md`, sourced from the OpenAI model card, announcement, and live API probe on 2026-05-11.

---

## Pricing summary

| Property | Value |
|---|---|
| Audio input | **$0.017 / minute** of audio duration |
| Billing unit | Audio duration (wall-clock minutes of audio streamed in), not tokens |
| Text output | Included — not billed separately (model card lists no per-token output cost) |
| Cached input | `[UNCONFIRMED]` — not documented on the model card for `gpt-realtime-whisper` |

There is no per-token billing dimension here, unlike `gpt-realtime-2`. The transcription endpoint bills purely on the duration of audio you stream in. Whether you receive a few words or pages of transcript text out of the same minute of audio does not change the cost.

---

## Limits

| Property | Value |
|---|---|
| Context window | 16,000 tokens |
| Max output per response | 2,000 tokens |
| Knowledge cutoff | Sep 30, 2024 |
| Rate limits | `[UNCONFIRMED]` — see [OpenAI platform limits docs](https://platform.openai.com/docs/guides/rate-limits) for current per-org values |
| Concurrent sessions | `[UNCONFIRMED]` — not specified in research sources; platform-wide org limits likely apply |

**Why 16K context matters.** The session retains recent audio context for coherent transcription. Multi-hour sessions exhaust the window. See the SKILL.md `## Long-session strategy` section for the rotation pattern.

**Why 2K max output matters.** A single utterance's transcript fits comfortably (≪ 2K tokens). Long sessions eventually accumulate enough total transcript output that this becomes the binding constraint before context does. Rotate sessions to reset both counters.

---

## Worked examples

### Example 1 — 60-minute customer-support call

- Audio duration: 60 minutes.
- Cost: 60 × $0.017 = **$1.02**.

A typical inbound support call transcribed end-to-end (both sides on the same session) runs ~$1 per hour of call. Compare to `gpt-realtime-2` audio-in at $32/M tokens (≈ $0.36/min input at 100 ms-per-token user-side conversion), making transcription roughly 21× cheaper than the conversational model for the input-audio dimension alone.

### Example 2 — Call center, 200 agent-hours per day

- 200 hours × 60 min × $0.017 = **$204 / day** total transcription cost for the call-center floor.
- Per agent per hour: $1.02. Per agent over an 8-hour shift: $8.16.
- For N agents simultaneously: $1.02 × N per hour. (Clarification: the $3.40 figure in some early estimates assumed a 200-hour-per-month-per-agent baseline, not per-day; the per-day cost scales linearly with active calls.)

This is the dominant cost line item for transcript-driven QA, CRM auto-logging, or live coaching features. Plan capacity by aggregate call-minutes per month, not by agent count.

### Example 3 — 8-hour live-streamed event

- Audio duration: 8 × 60 = 480 minutes.
- Cost: 480 × $0.017 = **$8.16** for the full event captioning.

Streaming-event captions over a multi-camera day-long event have a marginal cost so small that the constraint is rarely budget — it's latency, language coverage, and operational reliability.

### Example 4 — Long session rotated every 10 minutes (1 hour total)

- Audio duration: 60 minutes (split across 6 sessions of 10 minutes each).
- Cost: 60 × $0.017 = **$1.02** total.

**Rotation is free.** Billing is by audio duration, not by session count. Opening and closing six sessions over an hour costs the same as one continuous hour-long session would have, but spares you the 16K context / 2K output exhaustion risk. The 2–3 s overlap audio sent into each new session for continuity counts towards the duration meter; it adds at most a few extra seconds per rotation, which is negligible (~$0.001 per rotation).

---

## Cost comparison vs. neighbors

| Model | Use case | Pricing | Notes |
|---|---|---|---|
| `gpt-realtime-whisper` | Streaming STT | **$0.017 / min** | Audio in, text out only |
| `gpt-realtime-translate` | Live translation | $0.034 / min | 2× this skill; also returns text transcripts as a side-effect |
| `gpt-realtime-2` (audio in) | Conversational agent | ~$0.36 / min input audio (at $32/M tokens, 1 token / 100 ms user audio) | Apples-to-oranges — also includes reasoning, audio output, tool calls |
| `whisper-1` (REST) | Batch STT | $0.006 / min | Cheapest but non-streaming; use for recorded audio |
| `gpt-4o-transcribe` (REST) | Batch STT, higher quality | `[UNCONFIRMED]` — see model card | Use for offline quality-max workflows |

**Decision rules:**

- Streaming UX needed → `gpt-realtime-whisper` ($0.017/min).
- Batch / async file → `whisper-1` or `gpt-4o-transcribe` (cheaper, no streaming infra).
- Translation needed → `gpt-realtime-translate` (built-in source-language transcript as bonus).
- Agent reasoning / tool calls → `gpt-realtime-2` (the transcription is then a side-effect of the conversation, not the goal).

---

## Capacity planning quick-math

- **$1 buys ~59 minutes** of transcription audio.
- **$10 buys ~9.8 hours.**
- **$100 buys ~98 hours.**
- **$1,000 buys ~980 hours / ~41 days continuous.**

For monthly budgeting: estimate total expected audio minutes across all sessions, multiply by $0.017, add a buffer for retries / overlap audio on rotations (~3% is generous).

---

## Rate limits

`[UNCONFIRMED]` — the research file does not enumerate request-per-minute or concurrent-session limits for `gpt-realtime-whisper`. Default OpenAI platform limits apply per organization tier and may differ from `gpt-realtime-2`. Before launching, check:

- [OpenAI rate-limits dashboard](https://platform.openai.com/account/limits)
- Your org's tier-specific concurrency caps
- Per-region availability if you depend on a non-US OpenAI region

Plan a graceful-degrade path: if rate-limited, queue audio, fall back to batch `whisper-1` for the affected segments, or display "transcript paused — reconnecting" to users rather than dropping audio silently.
