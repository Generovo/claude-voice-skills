# OpenAI Realtime API — Notes captured 2026-05-11

> **Scope:** Single source of truth for the 3 new OpenAI voice models announced 2026-05-07 (`gpt-realtime-2`, `gpt-realtime-translate`, `gpt-realtime-whisper`). All event names, endpoint URLs, JSON field names, and pricing numbers in downstream skills must be sourced from this file — never from memory.
>
> **Conventions used in this file:**
> - Exact strings (event names, JSON keys, URLs) are in `code formatting`.
> - `[UNCONFIRMED]` marks anything the source pages didn't explicitly say but that I inferred or that is implied.
> - `[CONTRADICTION]` marks places where two source pages disagreed.
> - Each section ends with the URL(s) it was sourced from.
>
> **Live API probe (2026-05-11):** All three model IDs are present on `GET https://api.openai.com/v1/models`. Confirmed model IDs returned by the API: `gpt-realtime-2`, `gpt-realtime-translate`, `gpt-realtime-whisper`. Adjacent live IDs that also exist on the account (for context, not used by this project): `gpt-realtime`, `gpt-realtime-1.5`, `gpt-realtime-2025-08-28`, `gpt-realtime-mini`, `gpt-realtime-mini-2025-10-06`, `gpt-realtime-mini-2025-12-15`, `gpt-4o-realtime-preview*`, `gpt-4o-mini-realtime-preview*`, `gpt-4o-transcribe`, `gpt-4o-mini-transcribe*`, `gpt-4o-transcribe-diarize`, `whisper-1`.

---

## gpt-realtime-2

- **Model ID:** `gpt-realtime-2`
- **Aliases:** none listed on the model card (single version row); `[UNCONFIRMED]` whether a dated snapshot alias like `gpt-realtime-2-YYYY-MM-DD` will appear later. The 2025 generation precedent is `gpt-realtime-2025-08-28` snapshot under the `gpt-realtime` family.
- **Announcement date:** 2026-05-07 (announcement page title block).
- **Modalities:**
  - Input: `Text, audio, image`
  - Output: `Text, audio`
- **Endpoint:**
  - WebSocket: `wss://api.openai.com/v1/realtime?model=gpt-realtime-2`
  - WebRTC SDP exchange: `POST https://api.openai.com/v1/realtime/calls`
  - Ephemeral token mint: `POST https://api.openai.com/v1/realtime/client_secrets`
  - Chat Completions (non-realtime): `v1/chat/completions` (listed on model card; `[UNCONFIRMED]` whether this is a useful path for audio I/O — model card lists it but Realtime API is the canonical route).
  - SIP inbound: `sip:$PROJECT_ID@sip.api.openai.com;transport=tls`
- **Headers (server-side WS / SDP POST):**
  - `Authorization: Bearer ${OPENAI_API_KEY}`
  - `OpenAI-Safety-Identifier: hashed-user-id` (optional, recommended)
  - `Content-Type: application/sdp` (client-side SDP POST with ephemeral key)
  - `[UNCONFIRMED]` whether `OpenAI-Beta: realtime=v1` is still required — older docs required it; the 2026-05-11 WebSocket guide snippet does NOT show it. Treat as not required for the new models unless probing proves otherwise.
- **Events client→server** (from realtime-conversations guide; payload shapes quoted exactly where shown):
  - `session.update` — payload `{ "type": "session.update", "session": { ... } }` where `session` contains `type`, `model`, `output_modalities`, `audio`, `instructions`, `voice`, `tools`, `tool_choice`, `temperature`, `max_response_output_tokens`, `prompt`, `turn_detection`.
  - `conversation.item.create` — payload `{ "type": "conversation.item.create", "item": { "type": "message"|"function_call_output", "role": "user"|"assistant", "content": [...], "call_id": "...", "output": "..." } }`. Inner content items use `type`: `"input_text"` (+ `text`), `"input_audio"` (+ `audio` base64), `"input_image"` (+ `image_url`).
  - `response.create` — payload `{ "type": "response.create", "response": { "output_modalities": ["text"|"audio"], "conversation": "none", "metadata": {}, "input": [], "instructions": "", "tools": [], "audio": {"output": {"format": {"type": ""}}} } }`. The inner `response` object overrides session config for one turn.
  - `response.cancel`
  - `input_audio_buffer.append` — payload `{ "type": "input_audio_buffer.append", "audio": "<base64>" }`
  - `input_audio_buffer.commit`
  - `input_audio_buffer.clear`
  - `output_audio_buffer.clear`
  - `conversation.item.truncate` — fields `item_id`, `content_index`, `audio_end_ms`
- **Events server→client** (from realtime-conversations guide):
  - `session.created`
  - `session.updated`
  - `conversation.item.added`
  - `conversation.item.done`
  - `response.created`
  - `response.output_item.added`
  - `response.content_part.added`
  - `response.output_text.delta`
  - `response.output_text.done`
  - `response.content_part.done`
  - `response.output_item.done`
  - `response.done`
  - `response.output_audio.delta`
  - `response.output_audio.done`
  - `response.output_audio_transcript.delta`
  - `response.output_audio_transcript.done`
  - `input_audio_buffer.speech_started`
  - `input_audio_buffer.speech_stopped`
  - `input_audio_buffer.committed`
  - `response.function_call_arguments.delta`
  - `response.cancelled`
  - `rate_limits.updated`
  - `error`
  - `invalid_request_error` (error envelope: `{ "type": "invalid_request_error", "code": "invalid_value", "message": "", "param": "", "event_id": "" }`)
  - MCP / tool lifecycle (server-side tool calls): `mcp_list_tools.in_progress`, `mcp_list_tools.completed`, `mcp_list_tools.failed`, `response.mcp_call.in_progress`, `response.mcp_call.failed`, `response.mcp_call_arguments.done`
  - `[UNCONFIRMED]` whether a `response.function_call_arguments.done` event exists as a counterpart to `.delta`. The conversations guide only enumerated `.delta`. The docs say final args are available on `response.done`. Tests on a live session may reveal `.done` exists but wasn't documented — flag for verification before relying on it.
- **Pricing** (per 1M tokens, from model card + announcement):
  - Text input: `$4.00`
  - Text cached input: `$0.40`
  - Text output: `$24.00`
  - Audio input: `$32.00`
  - Audio cached input: `$0.40`
  - Audio output: `$64.00`
  - Image input: `$5.00`
  - Image cached input: `$0.50`
  - Token-to-audio conversion (from `realtime-costs` guide): user-message audio tokens at `1 token per 100 ms of audio`; assistant-message audio tokens at `1 token per 50 ms of audio`.
- **Context window:** `128,000` tokens (up from 32K in the prior `gpt-realtime` generation per announcement page).
- **Max output tokens:** `32,000`.
- **Knowledge cutoff:** `Sep 30, 2024`.
- **Reasoning levels:** `minimal`, `low`, `medium`, `high`, `xhigh`. Default is `low`. Configured via `reasoning.effort` on the session per the prompting guide. `[UNCONFIRMED]` exact JSON path — is it `session.reasoning.effort` inside `session.update`, or top-level? Prompting guide says the parameter field is `reasoning.effort` but did not show a full JSON example. Need to verify shape before sending.
- **Audio formats** (from realtime-conversations guide):
  - Input/output type strings used in `audio.input.format.type` and `audio.output.format.type`: `audio/pcm`, `audio/pcmu`.
  - `[UNCONFIRMED]` other variants — the docs sample only showed `audio/pcm` (PCM16) and `audio/pcmu` (μ-law). The older Realtime API had `pcm16`, `g711_ulaw`, `g711_alaw` as the values; the new schema uses MIME-like `audio/*` strings. Possibly `audio/pcma` exists for A-law but not seen on a page yet.
- **Sample rates:** `24000` Hz for PCM input (shown in transcription session example). `[UNCONFIRMED]` whether 16000/8000 Hz inputs are accepted for telephony scenarios — SIP integration exists, implying 8 kHz μ-law is likely on the wire, but the page didn't confirm an explicit `rate` field value.
- **Tool calling format:**
  - Tools defined inside `session.tools` array. Each tool: `{ "type": "function"|"mcp", "name": "string", "description": "string", "parameters": { "type": "object", "properties": {}, "required": [] } }`.
  - For MCP tools: `{ "type": "mcp", "server_label": "...", "server_url": "..."|"connector_id": "...", "authorization": "...", "headers": {...}, "allowed_tools": [...], "require_approval": "never" }`.
  - `tool_choice` values seen in docs: `"auto"`, `"required"`. `[UNCONFIRMED]` whether `"none"` or a forced `{ "type": "function", "name": "..." }` shape is also accepted (it was in prior Realtime — likely still).
  - Returning a function result: send `conversation.item.create` with `item.type = "function_call_output"`, `item.call_id`, `item.output` (string-encoded JSON), then send `response.create` to trigger next turn.
  - **Parallel tool calls** are explicitly supported (announcement).
- **Voices supported** (from realtime-conversations guide list — applies to gpt-realtime-2 audio output): `alloy`, `ash`, `ballad`, `coral`, `echo`, `sage`, `shimmer`, `verse`, `marin`, `cedar`. Configured via `audio.output.voice`.
- **Voice activity detection (`turn_detection`):**
  - `type: "server_vad"` with `threshold` (0–1), `prefix_padding_ms`, `silence_duration_ms`, `create_response` (bool, conversation mode only), `interrupt_response` (bool, conversation mode only). Example default: `threshold: 0.5`, `prefix_padding_ms: 300`, `silence_duration_ms: 500`.
  - `type: "semantic_vad"` with `eagerness: "low"|"medium"|"high"|"auto"`, `create_response`, `interrupt_response`.
  - `[UNCONFIRMED]` whether `type: "none"` is supported for fully manual turn control — the VAD guide did not list it.
- **Other notable features (announcement):**
  - Preambles (short "let me check that…" fillers before tool calls).
  - Parallel tool calls + tool transparency (audible "looking that up now").
  - Stronger recovery behavior ("I'm having trouble with that right now").
  - Domain understanding for healthcare/specialized terms.
  - Controllable tone/delivery.
- **WebRTC data channel name:** `oai-events`.
- **Sources:**
  - `https://developers.openai.com/api/docs/models/gpt-realtime-2` (model card, pricing, modalities, context, cutoff)
  - `https://developers.openai.com/api/docs/guides/realtime` (overview, model list)
  - `https://developers.openai.com/api/docs/guides/realtime-websocket` (WS URL, headers, session.update shape)
  - `https://developers.openai.com/api/docs/guides/realtime-webrtc` (SDP POST, client_secrets, oai-events channel)
  - `https://developers.openai.com/api/docs/guides/realtime-conversations` (exhaustive event list, voices, modalities)
  - `https://developers.openai.com/api/docs/guides/realtime-vad` (turn_detection schema)
  - `https://developers.openai.com/api/docs/guides/realtime-models-prompting` (reasoning levels)
  - `https://developers.openai.com/api/docs/guides/realtime-mcp` (tool format, MCP)
  - `https://developers.openai.com/api/docs/guides/realtime-costs` (token-to-audio conversion)
  - `https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api/` (announcement; pricing repetition)

---

## gpt-realtime-translate

- **Model ID:** `gpt-realtime-translate`
- **Aliases:** none listed on the model card.
- **Announcement date:** 2026-05-07.
- **Modalities:**
  - Input: `Audio`
  - Output: `Audio, text` (translated audio + concurrent source-language and target-language transcripts)
- **Endpoint:**
  - WebSocket: `wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate`
  - WebRTC SDP exchange: `POST https://api.openai.com/v1/realtime/translations/calls`
  - Ephemeral token mint: `POST https://api.openai.com/v1/realtime/translations/client_secrets`
  - Note: distinct base path `/v1/realtime/translations` — NOT the same as the conversational `/v1/realtime` path.
- **Headers:**
  - `Authorization: Bearer ${OPENAI_API_KEY}`
  - `OpenAI-Safety-Identifier: hashed-user-id` (optional)
- **Events client→server** (from translation guide code blocks, verbatim):
  - `session.update` — payload `{ "type": "session.update", "session": { "audio": { "output": { "language": "<ISO-639-1>" } } } }`
  - `session.input_audio_buffer.append` — payload `{ "type": "session.input_audio_buffer.append", "audio": "<base64-pcm16-24khz>" }`
  - **[CONTRADICTION with `gpt-realtime-2`]** This endpoint prefixes audio buffer events with `session.` (e.g. `session.input_audio_buffer.append`), while the conversational endpoint uses unprefixed `input_audio_buffer.append`. The translation endpoint appears to be a **simplified, distinct protocol**, not the same event vocabulary as the conversation API. Downstream skills MUST keep these two protocols separate.
  - `[UNCONFIRMED]` whether other events (`commit`, `clear`, `response.create`, etc.) exist on the translation endpoint. The translation guide showed only `session.update` and `session.input_audio_buffer.append` for the client→server direction. Translation is a continuous-stream model, so `response.create` may not apply.
- **Events server→client** (from translation guide code blocks):
  - `session.output_audio.delta` — `event.delta` carries base64 PCM16 of translated speech
  - `session.output_transcript.delta` — `event.delta` carries target-language transcript text
  - `session.input_transcript.delta` — `event.delta` carries source-language transcript text
  - `[UNCONFIRMED]` corresponding `*.done` / `*.completed` events. Not shown in the example. Likely exist; verify via live probe.
  - `[UNCONFIRMED]` whether `error`, `session.created`, `session.updated` events are also emitted (almost certainly yes — but not in any code sample on the page).
- **Pricing:** `$0.034 per minute` of realtime audio duration. Billed by audio duration, not tokens.
- **Context window:** `16,000` tokens.
- **Max output tokens:** `2,000`.
- **Knowledge cutoff:** `Sep 30, 2024`.
- **Tool calling:** **Not supported** (explicitly listed as unsupported on the model card features list).
- **Languages:**
  - 70+ input languages (announcement; full list `[UNCONFIRMED]` — not enumerated on either source page).
  - 13 output languages (announcement; full list `[UNCONFIRMED]`).
  - Example language code shown: `"es"` (Spanish). Implied ISO-639-1 codes.
- **Audio formats:**
  - Input/output: PCM16 (16-bit signed little-endian PCM), base64-encoded on the wire.
  - `[UNCONFIRMED]` whether the MIME-style `audio/pcm` and `audio/pcmu` strings apply here — the translation guide showed no `format.type` field at all, just `audio.output.language`. The format may be fixed-by-endpoint (PCM16 only) rather than configurable.
- **Sample rate:** `24 kHz` (24000 Hz) — base64 PCM16 at 24 kHz on both input and output.
- **Voice:** `[UNCONFIRMED]` — the translation guide did not show a `voice` field. The model card says output modality includes audio, but the example only sets `audio.output.language`. The voice may be model-fixed or configurable via the same `audio.output.voice` field as conversational sessions.
- **Reasoning levels:** Not applicable to this model (no mention; translation is direct, not reasoning-heavy).
- **Quoted benchmark (announcement):** "GPT-Realtime-Translate delivered 12.5% lower Word Error Rates than any other model we tested" — BolnaAI for Hindi/Tamil/Telugu.
- **Sources:**
  - `https://developers.openai.com/api/docs/models/gpt-realtime-translate` (model card)
  - `https://developers.openai.com/api/docs/guides/realtime-translation` (event names, code examples)
  - `https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api/` (announcement; 70+ input / 13 output langs, pricing)

---

## gpt-realtime-whisper

- **Model ID:** `gpt-realtime-whisper`
- **Aliases:** none listed on the model card.
- **Announcement date:** 2026-05-07.
- **Description (announcement):** "streaming speech-to-text that transcribes speech live as the speaker talks." Marketed for live captions, meeting notes, voice agents needing continuous understanding.
- **Modalities:**
  - Input: `Audio, text` (text input for prompt/context hinting)
  - Output: `Text`
- **Endpoint:**
  - WebSocket: typically `wss://api.openai.com/v1/realtime?model=gpt-realtime-whisper` `[UNCONFIRMED]` — the transcription guide showed the model name used inside session config (`audio.input.transcription.model = "gpt-realtime-whisper"`) and referenced the path `v1/realtime/transcription_sessions` from the model card. The guide did not show a direct WebSocket URL for transcription. Two likely shapes — verify on first probe:
    - `wss://api.openai.com/v1/realtime/transcription_sessions?model=gpt-realtime-whisper`, OR
    - `wss://api.openai.com/v1/realtime?intent=transcription` with the model set via `session.update`.
  - REST session create: `v1/realtime/transcription_sessions` (model card; full URL `[UNCONFIRMED]` — assume `https://api.openai.com/v1/realtime/transcription_sessions`).
- **Headers:**
  - `Authorization: Bearer ${OPENAI_API_KEY}`
- **Events client→server** (from realtime-transcription guide):
  - `session.update` — payload shape shown:
    ```
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
  - `input_audio_buffer.append` — sends audio chunks (note: unprefixed, matches the conversational endpoint, NOT the `session.`-prefixed translation endpoint).
  - `input_audio_buffer.commit` — manually triggers transcription when not relying on VAD.
- **Events server→client** (from realtime-transcription guide):
  - `conversation.item.input_audio_transcription.delta` — `event.delta` carries newly available transcript text. Identifies item via `item_id`.
  - `conversation.item.input_audio_transcription.completed` — final transcript for an item; payload includes `item_id`.
  - `[UNCONFIRMED]` whether `error`, `input_audio_buffer.speech_started`, `input_audio_buffer.speech_stopped`, `input_audio_buffer.committed`, `session.created`, `session.updated` are also emitted. Likely yes (consistent with conversation endpoint and the shared `session.update`/`input_audio_buffer.append` request vocabulary). Verify on live probe before depending on them.
- **Pricing:** `$0.017 per minute` of audio duration. Billed by audio duration.
- **Context window:** `16,000` tokens.
- **Max output tokens:** `2,000`.
- **Knowledge cutoff:** `Sep 30, 2024`.
- **Languages:** `[UNCONFIRMED]` — example shows `"en"`. The model card and announcement did not enumerate supported language codes. Whisper-1 supported 50+ languages historically; assume similar coverage but verify before claiming.
- **Audio formats:**
  - Input format type string seen in session config: `audio/pcm`. `[UNCONFIRMED]` whether `audio/pcmu` (μ-law for telephony) and others are accepted on this model — only `audio/pcm` shown in the docs example.
- **Sample rate:** `24000` Hz (24 kHz) in the shown example. `[UNCONFIRMED]` whether 16 kHz / 8 kHz are accepted.
- **Latency targets (transcription guide):** "0.4–3.0 seconds depending on use case."
- **Advanced features:**
  - VAD: `server_vad` shown in example.
  - Optional log probabilities: include via `session.include = ["item.input_audio_transcription.logprobs"]`.
  - `[UNCONFIRMED]` Diarization: NOT mentioned for `gpt-realtime-whisper`. A separate model `gpt-4o-transcribe-diarize` exists in the live model list for that use case.
  - `[UNCONFIRMED]` Word-level timestamps: not mentioned on the page. Older Whisper API had `timestamp_granularities`; whether it applies here is unknown.
- **Tool calling:** Not applicable (transcription-only).
- **Sources:**
  - `https://developers.openai.com/api/docs/models/gpt-realtime-whisper` (model card)
  - `https://developers.openai.com/api/docs/guides/realtime-transcription` (session config, event names)
  - `https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api/` (announcement)

---

## Common protocol (events, session config)

This section captures protocol details that apply across `gpt-realtime-2` and (where indicated) `gpt-realtime-whisper`. The `/v1/realtime/translations` endpoint used by `gpt-realtime-translate` is a separate, simpler protocol — DO NOT assume these conventions apply there. See the [CONTRADICTION] note in the translate section.

### Endpoints (canonical bases)
- WebSocket conversational: `wss://api.openai.com/v1/realtime?model=<model_id>`
- WebSocket translation: `wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate`
- WebSocket transcription: `[UNCONFIRMED]` — likely `wss://api.openai.com/v1/realtime?model=gpt-realtime-whisper` with `session.type = "transcription"`, or a dedicated `/v1/realtime/transcription_sessions` path. Need to probe.
- WebRTC SDP POST: `POST https://api.openai.com/v1/realtime/calls` (conversational), `POST https://api.openai.com/v1/realtime/translations/calls` (translation).
- Ephemeral session/client_secret mint: `POST https://api.openai.com/v1/realtime/client_secrets` (conversational), `POST https://api.openai.com/v1/realtime/translations/client_secrets` (translation).
- SIP inbound: `sip:$PROJECT_ID@sip.api.openai.com;transport=tls`.

### SIP call control (REST)
- `POST https://api.openai.com/v1/realtime/calls/$CALL_ID/accept` — body: `model`, `instructions`, `voice`, `tools`.
- `POST https://api.openai.com/v1/realtime/calls/$CALL_ID/reject` — body: optional `status_code` (default `603 Decline`).
- `POST https://api.openai.com/v1/realtime/calls/$CALL_ID/refer` — body: `target_uri` (e.g. `tel:+14155550123`).
- `POST https://api.openai.com/v1/realtime/calls/$CALL_ID/hangup`.

### Webhooks
- `realtime.call.incoming` — fires on incoming SIP. Payload includes `call_id` and `sip_headers`.
- Webhook verification headers: `webhook-id`, `webhook-timestamp`, `webhook-signature` (format `v1,<signature>=`).

### Session config top-level fields (conversational endpoint)
From the realtime-conversations guide:

```
type, model, output_modalities, audio, instructions, voice,
tools, tool_choice, temperature, max_response_output_tokens,
prompt, turn_detection
```

Nested `audio` fields:
```
audio.input
audio.input.format         // { "type": "audio/pcm"|"audio/pcmu", "rate": 24000 }
audio.input.format.type
audio.input.format.rate
audio.input.turn_detection // { "type": "server_vad"|"semantic_vad", ... }
audio.input.turn_detection.type
audio.input.transcription  // { "model": "gpt-realtime-whisper", "language": "en" } (used in transcription sessions)
audio.output
audio.output.format
audio.output.format.type
audio.output.voice         // one of: alloy, ash, ballad, coral, echo, sage, shimmer, verse, marin, cedar
audio.output.language      // only seen on translation sessions
```

`include` (session-level): list of optional extra fields, e.g. `"item.input_audio_transcription.logprobs"`.

`[UNCONFIRMED]` exact JSON path for reasoning effort. Prompting guide says parameter is `reasoning.effort`. Likely shape: `session.reasoning = { "effort": "low" }`. Probe before relying.

### Modality value strings
- `"audio"`
- `"text"`

### Voice option strings (`audio.output.voice`)
`alloy`, `ash`, `ballad`, `coral`, `echo`, `sage`, `shimmer`, `verse`, `marin`, `cedar`. `marin` is shown in the WebRTC default example.

### Turn detection (`turn_detection`)
- `{ "type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 300, "silence_duration_ms": 500, "create_response": true, "interrupt_response": true }`
- `{ "type": "semantic_vad", "eagerness": "low"|"medium"|"high"|"auto", "create_response": true, "interrupt_response": true }`

### Tool definition shape (in `session.tools[]`)
```
{
  "type": "function"|"mcp",
  "name": "string",
  "description": "string",
  "parameters": { "type": "object", "properties": {...}, "required": [...] }
}
```

MCP tool variant:
```
{
  "type": "mcp",
  "server_label": "stable-handle",
  "server_url": "https://...",        // or
  "connector_id": "...",
  "authorization": "OAuth token",
  "headers": { ... },
  "allowed_tools": ["..."],
  "require_approval": "never"
}
```

`tool_choice` values seen: `"auto"`, `"required"`. `[UNCONFIRMED]` whether `"none"` and `{ "type": "function", "name": "..." }` shape are accepted on the new models (they were on prior generations).

### Returning a function call result
Send:
```
{
  "type": "conversation.item.create",
  "item": {
    "type": "function_call_output",
    "call_id": "<call_id from response.function_call_arguments.delta / response.done>",
    "output": "<JSON string with the result>"
  }
}
```
…then send `{ "type": "response.create" }` to trigger the next turn.

### Exhaustive event lexicon (conversational endpoint)

**Client → server (8 + truncate = 9):**
- `session.update`
- `conversation.item.create`
- `conversation.item.truncate`
- `response.create`
- `response.cancel`
- `input_audio_buffer.append`
- `input_audio_buffer.commit`
- `input_audio_buffer.clear`
- `output_audio_buffer.clear`

**Server → client (24+ enumerated):**
- `session.created`
- `session.updated`
- `conversation.item.added`
- `conversation.item.done`
- `response.created`
- `response.output_item.added`
- `response.content_part.added`
- `response.output_text.delta`
- `response.output_text.done`
- `response.content_part.done`
- `response.output_item.done`
- `response.done`
- `response.output_audio.delta`
- `response.output_audio.done`
- `response.output_audio_transcript.delta`
- `response.output_audio_transcript.done`
- `input_audio_buffer.speech_started`
- `input_audio_buffer.speech_stopped`
- `input_audio_buffer.committed`
- `response.function_call_arguments.delta`
- `response.cancelled`
- `rate_limits.updated`
- `error`
- `invalid_request_error`
- `mcp_list_tools.in_progress`
- `mcp_list_tools.completed`
- `mcp_list_tools.failed`
- `response.mcp_call.in_progress`
- `response.mcp_call.failed`
- `response.mcp_call_arguments.done`
- (transcription) `conversation.item.input_audio_transcription.delta`
- (transcription) `conversation.item.input_audio_transcription.completed`

### Exhaustive event lexicon (translation endpoint — `/v1/realtime/translations`)

**Client → server:**
- `session.update` (note: same name as conversational, but content is different — only `audio.output.language` and a few other fields are accepted)
- `session.input_audio_buffer.append` (note: `session.` prefix — DIFFERENT from conversational endpoint)

**Server → client (from code samples; partial — full list `[UNCONFIRMED]`):**
- `session.output_audio.delta`
- `session.output_transcript.delta`
- `session.input_transcript.delta`

### WebRTC data channel name
`oai-events`

### Authentication summary
- Server-side WebSocket / SDP / mint: `Authorization: Bearer ${OPENAI_API_KEY}`.
- Client-side SDP POST (after server mints a client_secret): `Authorization: Bearer ${EPHEMERAL_KEY}` + `Content-Type: application/sdp`.
- `OpenAI-Beta: realtime=v1` header: `[UNCONFIRMED]` whether still required. The current 2026 doc snippets don't include it.
- Safety identifier (optional, recommended): `OpenAI-Safety-Identifier: hashed-user-id`.

---

## Sources fetched

| URL | Time (UTC, approx) | Status | What it gave us |
|---|---|---|---|
| `https://developers.openai.com/api/docs/models/gpt-realtime-2` | 2026-05-11 | 200 | Model card: pricing, modalities, context, cutoff, endpoint paths |
| `https://developers.openai.com/api/docs/models/gpt-realtime-translate` | 2026-05-11 | 200 | Model card: pricing, modalities, context, "tool calling: not supported" |
| `https://developers.openai.com/api/docs/models/gpt-realtime-whisper` | 2026-05-11 | 200 | Model card: pricing, modalities, context |
| `https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api/` | 2026-05-11 | 403 via WebFetch — fetched via `r.jina.ai` reader proxy successfully | Announcement: 70+ input / 13 output langs, $0.034/min + $0.017/min, 32K→128K context bump, reasoning levels minimal/low/medium/high/xhigh, default low |
| `https://developers.openai.com/api/docs/guides/realtime` | 2026-05-11 | 200 | Overview, list of sub-guide URLs |
| `https://developers.openai.com/api/docs/guides/realtime-websocket` | 2026-05-11 | 200 | `wss://api.openai.com/v1/realtime?model=gpt-realtime-2`, auth headers |
| `https://developers.openai.com/api/docs/guides/realtime-webrtc` | 2026-05-11 | 200 | `POST /v1/realtime/calls`, `POST /v1/realtime/client_secrets`, `oai-events` data channel |
| `https://developers.openai.com/api/docs/guides/realtime-conversations` | 2026-05-11 | 200 | Exhaustive event lists, voices, modalities, session fields, response.create + conversation.item.create payload shapes |
| `https://developers.openai.com/api/docs/guides/realtime-translation` | 2026-05-11 | 200 | Translation endpoint URL, `session.input_audio_buffer.append` (prefixed), `session.output_audio.delta`, `session.output_transcript.delta`, `session.input_transcript.delta` |
| `https://developers.openai.com/api/docs/guides/realtime-transcription` | 2026-05-11 | 200 | Transcription session.update example, `conversation.item.input_audio_transcription.delta` / `.completed`, `include` logprobs |
| `https://developers.openai.com/api/docs/guides/realtime-vad` | 2026-05-11 | 200 | server_vad and semantic_vad schemas, default thresholds |
| `https://developers.openai.com/api/docs/guides/realtime-models-prompting` | 2026-05-11 | 200 | Reasoning levels (minimal/low/medium/high/xhigh), `reasoning.effort` field name |
| `https://developers.openai.com/api/docs/guides/realtime-mcp` | 2026-05-11 | 200 | Tool definition shape, MCP fields, function_call_output return pattern, tool_choice values |
| `https://developers.openai.com/api/docs/guides/realtime-costs` | 2026-05-11 | 200 | Token-to-audio conversion (100ms user / 50ms assistant) |
| `https://developers.openai.com/api/docs/guides/realtime-sip` | 2026-05-11 | 200 | SIP URI format, accept/reject/refer/hangup REST endpoints, `realtime.call.incoming` webhook |
| `https://developers.openai.com/api/docs/guides/voice-agents` | 2026-05-11 | 200 | Agents SDK code patterns; `RealtimeAgent`/`RealtimeSession` JS, `VoicePipeline` Python |
| `https://developers.openai.com/api/docs/guides/realtime-server-controls` | 2026-05-11 | 200 | Webhook verification headers |
| `https://platform.openai.com/docs/api-reference/realtime` | 2026-05-11 | 403 | Blocked; content available via developers.openai.com instead |
| `https://platform.openai.com/docs/guides/realtime` | 2026-05-11 | 403 | Blocked; same content as developers.openai.com guide |
| `https://developers.openai.com/api/docs/realtime` | 2026-05-11 | 404 | Wrong path; canonical is `/api/docs/guides/realtime` |
| `https://developers.openai.com/api/docs` | 2026-05-11 | 200 | Discovery: enumerated all sub-guide URLs |
| `GET https://api.openai.com/v1/models` (with $OPENAI_API_KEY) | 2026-05-11 | 200 | Live confirmation all 3 model IDs exist on the account |
| `GET https://api.openai.com/v1/models/gpt-realtime-2` | 2026-05-11 | 200 | Confirmed: `{"id":"gpt-realtime-2","object":"model","owned_by":"system"}` |
| `GET https://api.openai.com/v1/models/gpt-realtime-translate` | 2026-05-11 | 200 | Confirmed live |
| `GET https://api.openai.com/v1/models/gpt-realtime-whisper` | 2026-05-11 | 200 | Confirmed live |

### URLs that 404'd or 403'd (worth recording so we don't retry)
- `https://platform.openai.com/docs/api-reference/realtime` — 403 (Cloudflare / auth wall on WebFetch)
- `https://platform.openai.com/docs/guides/realtime` — 403
- `https://developers.openai.com/api/docs/realtime` — 404
- `https://developers.openai.com/api/docs/realtime-api` — 404
- `https://developers.openai.com/api/docs/realtime/realtime-events` — 404
- `https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api/` — 403 via WebFetch (worked via `r.jina.ai` reader)
- `https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api` (no trailing slash) — 403

### Open `[UNCONFIRMED]` items to resolve before downstream code depends on them

1. **`response.function_call_arguments.done` event** — only `.delta` was enumerated. Likely exists; verify via a live probe sending a function tool and observing the event stream.
2. **`reasoning.effort` JSON path** — guide names the field but doesn't show full JSON. Probe with `{ "type": "session.update", "session": { "reasoning": { "effort": "low" } } }` and see if it sticks.
3. **Audio format strings beyond `audio/pcm` and `audio/pcmu`** — μ-law confirmed via `audio/pcmu`; A-law (`audio/pcma`?), Opus, etc. not seen in docs. Probe.
4. **Translation endpoint full event list** — only `session.update`, `session.input_audio_buffer.append`, and three server `delta` events shown. There must be `.done`/`.completed` variants, error events, and lifecycle events. Probe.
5. **`gpt-realtime-whisper` WebSocket URL** — model card lists `/v1/realtime/transcription_sessions` as an endpoint but the guide uses `session.type = "transcription"` on a regular realtime WS connection. Test both shapes.
6. **`gpt-realtime-translate` voice config** — model card says audio output, but example only sets `audio.output.language`. Whether/how to pick a voice is unclear.
7. **`OpenAI-Beta: realtime=v1` header** — was required on the old API, no 2026 doc page mentions it. Probably no longer needed but worth verifying with and without.
8. **Telephony sample rates (8 kHz μ-law)** — SIP path exists, so 8 kHz must work on inbound calls, but the explicit `rate` value to use for telephony has not been documented in pages fetched.
9. **Translation supported language code lists** — 70 input / 13 output mentioned but never enumerated. Need a separate doc page or live probe.
10. **`response.create` from server / autonomous server-side responses** — server-controls guide didn't enumerate any event for the server to trigger a response programmatically. May exist via webhook callback flow.
