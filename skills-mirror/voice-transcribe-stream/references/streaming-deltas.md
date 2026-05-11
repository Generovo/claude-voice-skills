# Streaming deltas — event flow and UX patterns

Deep-dive on the event vocabulary emitted by `gpt-realtime-whisper` during streaming transcription, with payload shapes and rendering patterns for live-caption UIs.

All event names, JSON keys, and shapes here trace back to `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md`. Items not explicitly enumerated in that file carry an `[UNCONFIRMED]` tag.

---

## Event flow

The transcription guide explicitly enumerates **two transcription server-events**:

1. `conversation.item.input_audio_transcription.delta` — partial transcript chunk for an in-progress utterance.
2. `conversation.item.input_audio_transcription.completed` — final transcript for that utterance, locked.

Both identify the utterance by `event.item_id`. A single utterance produces one or more `.delta` events followed by exactly one `.completed`. The full timeline for a typical short utterance:

```
client → server   session.update                                 (configure once)
client → server   input_audio_buffer.append   (chunk 1)
client → server   input_audio_buffer.append   (chunk 2)
...
[server VAD detects speech start]                                 [UNCONFIRMED] input_audio_buffer.speech_started
client → server   input_audio_buffer.append   (chunk N)
[server VAD detects silence — utterance boundary]                 [UNCONFIRMED] input_audio_buffer.speech_stopped
                                                                  [UNCONFIRMED] input_audio_buffer.committed
server → client   conversation.item.input_audio_transcription.delta      (item_id=A, delta="hello")
server → client   conversation.item.input_audio_transcription.delta      (item_id=A, delta="hello, how")
server → client   conversation.item.input_audio_transcription.delta      (item_id=A, delta="hello, how are")
server → client   conversation.item.input_audio_transcription.completed  (item_id=A, transcript="hello, how are you?")
```

`[UNCONFIRMED]` — the bracketed events (`speech_started`, `speech_stopped`, `committed`) are not enumerated in the transcription guide. They exist on the conversational endpoint with the same session vocabulary, so are likely emitted here too, but the research file flags them as unconfirmed until probed.

`[UNCONFIRMED]` — the **exact granularity** of `.delta` chunks (per-word, per-token, per-N-ms) is not documented. In practice, expect word-level or sub-phrase chunking; do not depend on token-level boundaries.

---

## Partial vs final

### `.delta` payload (partial)

The research file shows `event.delta` carries newly available transcript text. Inferred shape:

```json
{
  "type": "conversation.item.input_audio_transcription.delta",
  "item_id": "item_abc123",
  "delta": "<newly available text — likely a substring or word added to the in-progress transcript>"
}
```

`[UNCONFIRMED]` whether `delta` is an **incremental suffix** (just the new characters) or a **replacement** of the in-progress transcript. The research file did not specify. Conservative implementation: treat each `delta` as the **full current transcript** for that `item_id` and replace your in-memory buffer on each event. That handles both semantics safely.

`[UNCONFIRMED]` other fields in the delta payload (`content_index`, `event_id`, timestamps, logprobs when `include` is set). Likely present; probe to enumerate.

### `.completed` payload (final)

The research file confirms `item_id` is included. Inferred shape:

```json
{
  "type": "conversation.item.input_audio_transcription.completed",
  "item_id": "item_abc123",
  "transcript": "<final committed transcript text for this utterance>"
}
```

`[UNCONFIRMED]` exact field name for the final text (`transcript` vs `content` vs `text`). Probe to confirm — and code defensively until then by checking common shapes.

When `include` contains `"item.input_audio_transcription.logprobs"`, the completed event `[UNCONFIRMED]` carries per-token log probabilities for confidence-aware downstream consumers (e.g. flagging low-confidence segments for review in legal/medical use).

---

## UX patterns for live captions

Three rendering strategies, each suited to a different use case.

### Pattern A — Replace-last (recommended default)

Maintain a per-`item_id` buffer. On every `.delta`, **replace** the buffer's value with the latest text and re-render the speculative region. On `.completed`, lock that buffer and start a new one for the next utterance.

```
state = { items: Map<item_id, { text: string, finalized: boolean }> }

on('.delta', e => state.items.set(e.item_id, { text: e.delta, finalized: false }))
on('.completed', e => state.items.set(e.item_id, { text: e.transcript, finalized: true }))
```

Render: iterate items in order, draw `finalized: true` items in regular style, draw the trailing `finalized: false` item in italic / lower opacity.

**Pros.** Clean visual: previous utterances stay locked, current utterance updates in place. Robust to revisions.
**Cons.** Requires re-rendering the live region on each delta — fine for short live regions, may flicker on very high delta rates.

### Pattern B — Append-only (only if deltas are guaranteed suffixes)

If you can confirm via live probe that `event.delta` is always a **strict suffix** added to the in-progress transcript, append-only is simpler:

```
on('.delta', e => buffers[e.item_id] += e.delta)
on('.completed', e => { buffers[e.item_id] = e.transcript; lock(e.item_id); })
```

**Pros.** Cheapest render path; no replacement logic.
**Cons.** Breaks loudly if the model emits a revision mid-utterance — you get doubled / garbled text. Do not use until the suffix invariant is confirmed.

### Pattern C — Finalized-above + speculative-below

Two distinct UI regions:

- **Top region (finalized).** Renders all `.completed` items in chronological order. Append-only. Locked text.
- **Bottom region (speculative).** A single line showing the most recent `.delta`'s text for the active `item_id`, in italic / grayed.

On `.completed`, the speculative line empties, and the finalized transcript scrolls one item up.

**Pros.** Visually unambiguous distinction between "we heard this and we're sure" vs "we're still listening." Matches the affordance pattern of Google Live Caption and iOS Live Captions.
**Cons.** Two-region layout requires more screen real estate; not ideal for compact overlays.

---

## Corrections & rollback

`[UNCONFIRMED]` — the research file does not explicitly cover delta retraction / replacement semantics for `gpt-realtime-whisper`. However, **most streaming ASR models** allow the model to revise an earlier delta in the same utterance as it gathers more context (the classic example: an early "I scream" delta corrected to "ice cream" once the model hears the rest of the sentence).

Code defensively assuming revisions happen:

- Treat `.delta.delta` as the **current best estimate** for the in-progress utterance, not as immutable history.
- Use Pattern A or C above so a revision does not visibly corrupt the rendered transcript.
- Do not persist `.delta` text to any downstream store (CRM, search index, audit log) — only persist `.completed` transcripts. Speculative deltas are throw-away UI state.

On `.completed`, the model has committed. **No further events for that `item_id` are expected.** If you do observe a post-completion event in practice, file a research-file update; that would be a meaningful protocol detail.

---

## Cross-references

- **Conversational endpoint events** — for the full event catalogue used by `gpt-realtime-2` (response lifecycle, audio output, tool calls), see `voice-agent-realtime/references/events-websocket.md`. The two transcription events documented here are **the same names** used inside a conversational session when `audio.input.transcription` is configured, which is why the conversational endpoint also enumerates them.
- **Translation endpoint events** — entirely different prefix scheme (`session.input_audio_buffer.append`, `session.input_transcript.delta`). See `voice-translate-live/SKILL.md § Event vocabulary — CRITICAL DIFFERENCE`. Do not import patterns from this file into translation adapters without translating the event names.
- **Source of truth** — `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md` § `gpt-realtime-whisper` and § `Exhaustive event lexicon (conversational endpoint)` (the two transcription events are listed at the bottom of that lexicon, marked `(transcription)`).

---

## Probing checklist

Before relying on any `[UNCONFIRMED]` semantics, open a live session and observe:

1. The exact JSON shape of `.delta` and `.completed` (field names, presence of `content_index`, `event_id`).
2. Whether `.delta.delta` is a suffix or a full replacement.
3. Presence and shape of `input_audio_buffer.speech_started` / `.speech_stopped` / `.committed`.
4. Presence and shape of `session.created` / `session.updated` / `error`.
5. Whether logprobs appear inside `.delta`, only inside `.completed`, or as a separate event.
6. Whether revisions across deltas actually occur on real audio (record a deliberately ambiguous utterance and watch the stream).

Roll any confirmed details back into the research file with a date-stamped note, then drop the `[UNCONFIRMED]` tags here.
