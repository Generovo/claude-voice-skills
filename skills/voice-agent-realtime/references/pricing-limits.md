# Pricing, limits, and worked examples — `gpt-realtime-2`

All numbers are from `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md`, sourced from the OpenAI model card and the 2026-05-07 announcement.

---

## Pricing table

All figures are **USD per 1 million tokens** (or per image for image input).

| Token kind | Price per 1M tokens |
|---|---|
| Text input | $4.00 |
| Text cached input | $0.40 |
| Text output | $24.00 |
| Audio input | $32.00 |
| Audio cached input | $0.40 |
| Audio output | $64.00 |
| Image input | $5.00 |
| Image cached input | $0.50 |

### Token-to-audio conversion (from the realtime-costs guide)

- **User-side audio** (input): `1 token per 100 ms` of audio → **600 tokens / minute** of user speech.
- **Assistant-side audio** (output): `1 token per 50 ms` of audio → **1,200 tokens / minute** of bot speech.

The asymmetry reflects that the model emits audio at higher token density than it consumes.

---

## Worked examples

> **All assumptions are explicit so you can substitute your own.** Numbers are rough — actual cost depends on transcript length, tool calls, reasoning level, and how much context gets cached.

### Example 1 — 10-minute support call

Assumptions:
- 10 min total: ~5 min user speech, ~5 min bot speech.
- Light system prompt (~500 text tokens).
- 1 tool call mid-conversation, ~200 text tokens of arguments + result.
- No image input, no caching benefit (single-shot session).

Token math:

| Component | Tokens | Rate ($/1M) | Cost |
|---|---:|---:|---:|
| Audio input (5 min × 600) | 3,000 | $32.00 | $0.096 |
| Audio output (5 min × 1,200) | 6,000 | $64.00 | $0.384 |
| Text input (system prompt + tool result) | 700 | $4.00 | $0.003 |
| Text output (function call args) | 100 | $24.00 | $0.002 |
| **Total** | | | **~$0.49** |

**Rough rule of thumb:** $0.05 per minute of conversation. A 10-min call ≈ $0.50.

### Example 2 — Callbot, 30 calls/hour, 3-min avg

Assumptions:
- 30 calls/hour × 3 min/call = 90 minutes of conversation per hour.
- 50/50 user/bot split (45 min user, 45 min bot).
- Constant system prompt (~800 text tokens), reused across calls but **NOT cached across sessions** (each new WS session has a fresh state). Inline `prompt` field MAY cache `[UNCONFIRMED]`.
- 0.5 tool calls per call on average.

Per hour:

| Component | Tokens | Rate ($/1M) | Cost / hour |
|---|---:|---:|---:|
| Audio input (45 × 600) | 27,000 | $32.00 | $0.864 |
| Audio output (45 × 1,200) | 54,000 | $64.00 | $3.456 |
| Text input (30 calls × 800 prompt) | 24,000 | $4.00 | $0.096 |
| Text output (~50 tokens × 30 calls × 0.5 tools) | 750 | $24.00 | $0.018 |
| **Total / hour** | | | **~$4.43** |

**Per call:** ~$0.15. **Per day at this rate (8h):** ~$35. **Per month (22 business days):** ~$780.

If you can warm-cache the system prompt (cached text input at $0.40/M), the $0.096 drops to ~$0.0096 — negligible savings here. Caching matters far more in long single sessions than across many short sessions.

### Example 3 — Single complex turn, 4 tool calls, 60 s reasoning at `high`

Assumptions:
- User speaks for 8 s.
- Model reasons internally for ~60 s at `reasoning.effort: "high"` (no audio output during this time — reasoning is text-only on the assistant side).
- 4 parallel tool calls, each with ~150 text tokens of arguments + ~500 text tokens of results.
- Final spoken answer is 20 s of bot audio.

Token math:

| Component | Tokens | Rate ($/1M) | Cost |
|---|---:|---:|---:|
| Audio input (8 s × 10 = 80) | 80 | $32.00 | $0.003 |
| Audio output (20 s × 20 = 400) | 400 | $64.00 | $0.026 |
| Text output (reasoning tokens at high; rough estimate ~3,000) | 3,000 | $24.00 | $0.072 |
| Text output (4 × 150 function call args) | 600 | $24.00 | $0.014 |
| Text input (4 × 500 function results) | 2,000 | $4.00 | $0.008 |
| **Total** | | | **~$0.12** |

`high` reasoning is the lion's share. Drop to `low` (default) and the reasoning bill collapses by ~5–10x, but the model is less likely to make all 4 tool decisions correctly in one shot. Bump to `xhigh` only for genuinely adversarial / multi-constraint requests.

---

## Context limits

| Limit | Value |
|---|---|
| Total context window | **128,000 tokens** |
| Max output tokens per response | **32,000 tokens** |
| Knowledge cutoff | Sep 30, 2024 |

128K sounds enormous but **audio is expensive in tokens**:

- 600 tokens / min of user audio + 1,200 tokens / min of bot audio = **1,800 tokens / minute of conversation**.
- 128,000 / 1,800 ≈ **71 minutes** of audio-only context before truncation.

In a typical mixed conversation with system prompt + tool calls + reasoning + audio, expect to hit context pressure around **45–60 minutes** of continuous conversation.

### Pruning pattern for long sessions

After every ~50 turns or when usage from `rate_limits.updated` indicates high context pressure:

1. Snapshot the last 6–10 turns (the recent context the model genuinely needs).
2. Send `conversation.item.truncate` (`[UNCONFIRMED]` for full-item removal — `truncate` is documented for clipping audio playback length; for removing items entirely, the canonical mechanism is to start a new session with a compressed history. Verify against current docs before relying on it.)
3. Alternative: at session-pressure time, gracefully end the session and start a new WebSocket connection with a `session.update` whose `prompt` includes a summary of the prior conversation.

### Worked context example: 100 turns of mixed audio + text

Assumptions per turn: ~7 s user audio, ~9 s bot audio, 1 short tool call.

| Component / turn | Tokens / turn |
|---|---:|
| User audio (7 s × 10) | 70 |
| Bot audio (9 s × 20) | 180 |
| Bot text output (function call args + spoken transcript) | ~80 |
| Tool result text input | ~150 |
| **Total / turn** | **~480** |

100 turns × 480 = **48,000 tokens** — well within 128K. A pure-text agent at this rate could go ~250 turns; a heavily-audio agent (longer utterances both sides) tops out around ~120 turns.

When pruning:
- Always keep the system prompt (`instructions`).
- Keep the last 8–10 turns verbatim (model needs immediate context).
- Replace the middle with a ~500-token summary: `"Earlier in this call: user reported error E42, agent ran get_diagnostics, escalated to billing, …"`.

---

## Rate limits

`[UNCONFIRMED — check https://platform.openai.com/docs/guides/rate-limits]`

The research file did not capture explicit Realtime API rate limits (tier-dependent on OpenAI's side). What we can say:

- The `rate_limits.updated` server event broadcasts current remaining quota during a live session. Inspect `rate_limits[]` for `requests`, `tokens`, etc. with `remaining` and `reset_seconds`.
- For production capacity planning, request a tier upgrade via the OpenAI dashboard before launch.

---

## See also

- Research file: `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md` (canonical pricing + token-conversion source).
- OpenAI costs guide: `https://developers.openai.com/api/docs/guides/realtime-costs`.
- OpenAI model card: `https://developers.openai.com/api/docs/models/gpt-realtime-2`.
- OpenAI announcement: `https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api/`.
