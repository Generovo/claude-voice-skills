# Pricing & limits — voice-translate-live

All facts here trace to `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md`. Items the docs did not fully specify carry the `[UNCONFIRMED]` tag.

## Headline numbers

| Property | Value | Notes |
|---|---|---|
| Pricing | **$0.034 / minute** | Billed by audio duration, not tokens. `[UNCONFIRMED]` whether this counts input minutes only, output minutes only, or both directions summed. Treat as **wall-clock session minutes per session** for conservative estimates. |
| Context window | 16,000 tokens | Per session. |
| Max output | 2,000 tokens | Per response. |
| Knowledge cutoff | Sep 30, 2024 | Not directly relevant to translation. |
| Rate limits | `[UNCONFIRMED]` | Not documented on the model card or translation guide. See `https://platform.openai.com/docs/guides/rate-limits` for current per-org limits, or query `GET https://api.openai.com/v1/dashboard/rate_limits` against your project. |

The $0.034/min rate is roughly **half** the equivalent audio-input pricing of `gpt-realtime-2` (which works out to ~$0.06–0.07/min for typical conversational use), reflecting that translation is a narrower task with no tool calling and no reasoning levels.

## Billing-unit caveat

The research file flags one ambiguity: does $0.034/min apply per **input minute**, per **output minute**, or to the **sum** of both directions? The translation endpoint emits both translated audio and source/target transcripts, and a session runs as one continuous stream. The safe assumption for budgeting is: **one session-minute = one billable minute** (i.e., 60 seconds of WebSocket connection time during which audio is flowing). Multi-language fan-out (one session per output language) therefore multiplies cost linearly with the number of target languages.

`[UNCONFIRMED]` — confirm with a real billed session before sizing a large deployment.

---

## Worked example 1 — 5-minute international support call

**Scenario.** A French-speaking customer calls a Moroccan e-commerce support line. The agent speaks Arabic. You open **two** translation sessions on the Twilio bridge:

- Session A: input fr → output ar (customer → agent direction)
- Session B: input ar → output fr (agent → customer direction)

Call length: 5 minutes.

**Cost.**
```
2 sessions × 5 min × $0.034/min = $0.34 per call
```

For a support line at 1,000 calls/day:
```
1,000 calls × $0.34 = $340/day = ~$10,200/month
```

---

## Worked example 2 — 1-hour conference, 4 speakers, 3 target languages

**Scenario.** A 60-minute internal all-hands. Four presenters speak (typically only one at a time). The audience needs the talk translated to three languages simultaneously: English, Spanish, Arabic.

**Architecture.** One session per **target** language (not per speaker — the model translates whoever is speaking on the muxed input stream). Each session is fed the same mixed audio from all four presenters.

```
3 target languages × 1 session each = 3 concurrent sessions
3 sessions × 60 min × $0.034/min = $6.12 per conference
```

If the source language varies (some speakers in English, some in Spanish, some in French), the sessions still cost the same — only their **input transcript** changes. Each session covers all source speakers.

**Scaling note.** Adding a 4th target language costs another $2.04 for the hour (1 session × 60 min × $0.034). Adding a 5th speaker costs nothing (the existing sessions hear them too).

---

## Worked example 3 — 24/7 multilingual hotline, 100 min/day, 30 days

**Scenario.** A multilingual support hotline runs around the clock. Each call uses bilateral interpretation (2 sessions, 1 per direction). Total billed audio minutes per day: 100 min × 2 directions = 200 session-minutes/day.

**Monthly cost.**
```
200 session-min/day × 30 days × $0.034/min = $204/month
```

If the same hotline supports a **second pair** of languages on overflow (e.g., add Arabic↔English alongside the default French↔English), and 30% of calls use it:
```
30 min/day × 2 directions × 30 days × $0.034/min = $61.20/month additional
```

Total monthly: ~$265/month for a 100-min/day 24/7 hotline covering two language pairs.

---

## Context window budget (16K tokens)

The 16K context is far smaller than the conversational model's 128K but more than adequate for translation — the model only needs enough source-audio history to maintain coherent translation across utterances, not to hold a long conversation.

`[UNCONFIRMED]` how OpenAI accounts for context on this endpoint specifically. Token-to-audio conversion on the conversational endpoint is **1 token per 100 ms of user audio** (from the realtime-costs guide). If the same conversion applies here, 16K tokens covers roughly:

```
16,000 tokens × 100 ms/token = 1,600,000 ms = ~26.6 minutes of input audio
```

In practice the model only needs a short rolling window of source audio (recent utterances) for coherence — long sessions should run for hours without ever pressing the 16K limit. If you do see context-exhaustion errors, restart the session at a natural turn boundary; there is no `prompt` field documented for carrying state forward.

## Output limits

Max 2,000 output tokens per response. For translation this is generous — a single utterance never approaches the limit. The number matters mostly as a guarantee: even a long utterance will not be silently truncated mid-stream within a single response.

## Rate limits

Not documented on the model card or translation guide. Use the platform docs or live probe:

```
GET https://platform.openai.com/docs/guides/rate-limits
```

For production deployments expecting >10 concurrent sessions or >1,000 concurrent calls, contact OpenAI sales to confirm tier limits before launch. `[UNCONFIRMED]` whether translation sessions have a different per-org concurrency cap than conversational sessions.

## Sources

- `https://developers.openai.com/api/docs/models/gpt-realtime-translate` — pricing line ($0.034/min), context (16K), max output (2,000)
- `https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api/` — announcement, pricing restatement
- `https://developers.openai.com/api/docs/guides/realtime-costs` — token-to-audio conversion (100 ms/token user audio) — applied here as a heuristic for the 16K context budget
