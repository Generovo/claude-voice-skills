# Session config — voice-translate-live

The exact `session.update` JSON shape for the `/v1/realtime/translations` endpoint, sourced from `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md` (which sources the OpenAI translation guide at `https://developers.openai.com/api/docs/guides/realtime-translation`). Items the docs did not show are tagged `[UNCONFIRMED]`.

## Minimal session.update — verbatim shape

The translation guide showed only this JSON shape for client→server `session.update`:

```json
{
  "type": "session.update",
  "session": {
    "audio": {
      "output": {
        "language": "es"
      }
    }
  }
}
```

That is the entire documented configuration surface — a single nested field, `audio.output.language`. Everything else below is either inferred from the conversational endpoint's session schema (which may not apply here) or `[UNCONFIRMED]`.

## Full session shape (with `[UNCONFIRMED]` annotations)

```json
{
  "type": "session.update",
  "session": {
    "type": "realtime",
    "model": "gpt-realtime-translate",
    "audio": {
      "input": {
        "format": { "type": "audio/pcm", "rate": 24000 }
      },
      "output": {
        "language": "es",
        "format": { "type": "audio/pcm" },
        "voice": "marin"
      }
    }
  }
}
```

### Field-by-field

| Field | Type | Status | Description |
|---|---|---|---|
| `type` (outer) | string | Documented | Always `"session.update"` for this event. |
| `session.type` | string | `[UNCONFIRMED]` | The conversational endpoint uses `"realtime"` here, and the transcription endpoint uses `"transcription"`. A translation-specific value like `"translation"` may be required — the translation guide did not show this field at all. Try without it first; add only if probing shows it is required. |
| `session.model` | string | `[UNCONFIRMED]` | Documented value: `"gpt-realtime-translate"`. The model is also pinned via the query string on the WebSocket URL (`?model=gpt-realtime-translate`), so this field may be redundant on this endpoint. The translation guide did not show it. |
| `session.audio.input.format.type` | string | `[UNCONFIRMED]` | The translation guide did not show any input format field — the format may be fixed by the endpoint to PCM16. If the conversational schema applies, valid values are `"audio/pcm"` and `"audio/pcmu"` (μ-law). |
| `session.audio.input.format.rate` | number | `[UNCONFIRMED]` | Likely `24000` (24 kHz) for PCM16. The research file states the translation endpoint operates at 24 kHz on both sides. |
| `session.audio.input.turn_detection` | object | `[UNCONFIRMED]` | The translation guide did not show this field. Server VAD is presumably enabled by default for continuous translation. Whether you can override it (`{ "type": "server_vad", ... }` or `{ "type": "semantic_vad", ... }`) is not documented for this endpoint. |
| `session.audio.output.language` | string | **Documented** | The only field shown in the translation guide example. ISO-639-1 two-letter code. Example shown: `"es"`. |
| `session.audio.output.format` | object | `[UNCONFIRMED]` | Not shown in the translation guide. Output is presumably PCM16 24 kHz, mirroring input. |
| `session.audio.output.voice` | string | `[UNCONFIRMED]` — flagged in research file item #6 | Not shown anywhere in the translation guide. The model card lists audio output, but no voice picker appears in any code sample. Voice may be model-fixed, language-fixed, or configurable via the same `voice` field as conversational sessions (`alloy`, `ash`, `ballad`, `coral`, `echo`, `sage`, `shimmer`, `verse`, `marin`, `cedar`). Probe before relying on a specific voice. |

## What the translation endpoint does NOT support

The conversational endpoint allows these session fields. None of them are valid (or even meaningful) on the translation endpoint:

- `instructions` — translation is direct; no system prompt.
- `tools`, `tool_choice` — **explicitly not supported** per the model card.
- `temperature` — `[UNCONFIRMED]`; no mention in the translation guide.
- `max_response_output_tokens` — `[UNCONFIRMED]`; the 2,000-token output cap may be applied automatically.
- `prompt` — `[UNCONFIRMED]`; no mention.
- `reasoning.effort` — not applicable; translation has no reasoning levels.
- `output_modalities` — implied to always be `["audio", "text"]` (translated audio + concurrent transcripts); no override documented.

## Sending audio after session.update

After the session is configured, audio chunks are appended with the **prefixed** event name:

```json
{
  "type": "session.input_audio_buffer.append",
  "audio": "<base64-encoded PCM16 24 kHz mono chunk>"
}
```

Note the `session.` prefix — this is the single most important difference from the conversational endpoint, which uses bare `input_audio_buffer.append`. See `../SKILL.md ## Event vocabulary — CRITICAL DIFFERENCE`.

`[UNCONFIRMED]` whether the translation endpoint also supports:
- `session.input_audio_buffer.commit` (manual turn end)
- `session.input_audio_buffer.clear` (drop the in-progress buffer)
- `session.output_audio_buffer.clear` (drop in-progress output)
- A `response.create`-equivalent

The translation guide showed only `session.update` and `session.input_audio_buffer.append` on the client→server direction. Translation is a continuous-stream model, so `response.create` likely does not apply — drive the session via VAD instead.

## Server→client events to expect

Per the research file, three streaming `.delta` events are documented:

```json
{ "type": "session.input_transcript.delta",  "delta": "Bonjour, je m'appelle…" }
{ "type": "session.output_transcript.delta", "delta": "Hola, me llamo…" }
{ "type": "session.output_audio.delta",      "delta": "<base64 PCM16 audio chunk>" }
```

`[UNCONFIRMED]` whether `.done` / `.completed` companion events exist. They almost certainly do (otherwise you cannot know when a translation chunk has finished) — verify with a live probe before depending on a specific `.done` event name.

`[UNCONFIRMED]` whether `session.created`, `session.updated`, and `error` events fire on this endpoint. They probably do (consistent with the conversational endpoint), but the translation guide showed no such events.

## Comparison with conversational session

For the full conversational session schema (`output_modalities`, `voice`, `tools`, `tool_choice`, `turn_detection`, `reasoning.effort`, etc.), see `~/.claude/skills/voice-agent-realtime/references/events-websocket.md`. **Do not** copy/paste fields from that schema into a translation session — most of them are not accepted, and the wire silently drops anything the endpoint does not understand.

The two endpoints share **only**:
- The `session.update` event name (but with different inner schemas).
- ISO-639-1 language codes.
- PCM16 24 kHz as the canonical audio format.

Everything else (tool calling, reasoning, multi-modality output, voice selection, manual turn control, response.create) is conversational-only.

## Sources

- `https://developers.openai.com/api/docs/guides/realtime-translation` — translation guide (the **only** documented JSON shape comes from here)
- `https://developers.openai.com/api/docs/models/gpt-realtime-translate` — model card
- Research file open items #4 (full event list) and #6 (voice config) explicitly note the gaps above
