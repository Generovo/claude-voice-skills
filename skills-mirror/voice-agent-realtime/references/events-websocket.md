# WebSocket event catalog — `gpt-realtime-2` conversational endpoint

All events on this page apply to the **conversational** WebSocket / WebRTC channel at:

- WebSocket: `wss://api.openai.com/v1/realtime?model=gpt-realtime-2`
- WebRTC data channel: `oai-events`

The translation endpoint (`/v1/realtime/translations`) uses a **different, simpler event vocabulary** — see the research file's translate section. Do not mix them.

All event names below are copy-pasted from the OpenAI realtime guides (sourced via the research file at `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md`). Where the research file flagged an item as `[UNCONFIRMED]`, the same tag is preserved here.

---

## Client → Server events (9)

These are the messages **your code sends** to the OpenAI Realtime endpoint.

| Event (`type` value) | Purpose | Key payload fields |
|---|---|---|
| `session.update` | Configure or reconfigure the session (model, voice, tools, turn detection, reasoning, instructions, modalities). Can be sent any time mid-session. | `session.type`, `session.model`, `session.output_modalities`, `session.audio` (with nested `input`/`output`), `session.instructions`, `session.voice`, `session.tools`, `session.tool_choice`, `session.temperature`, `session.max_response_output_tokens`, `session.prompt`, `session.turn_detection`, `session.include`, `session.reasoning` (`[UNCONFIRMED]` exact path — research item #2) |
| `conversation.item.create` | Append a new item to the conversation history. Used for user messages (typed text, injected audio, image), system context items, and **function call outputs**. | `item.type` (`"message"` \| `"function_call_output"`), `item.role` (`"user"` \| `"assistant"`), `item.content[]` (with `type: "input_text"` + `text`, or `type: "input_audio"` + `audio` (base64), or `type: "input_image"` + `image_url`), `item.call_id`, `item.output` (string-JSON, for `function_call_output`) |
| `conversation.item.truncate` | Cut an item's audio playback short — used after a user barge-in so the model "forgets" the part it never actually played to the user. | `item_id`, `content_index`, `audio_end_ms` |
| `response.create` | Explicitly request the model to produce a response. With `server_vad` and `create_response: true`, the server emits this implicitly when the user stops speaking. The inner `response` object can override session config for that turn only. | `response.output_modalities` (`["text"]` or `["audio"]`), `response.conversation` (`"none"` to skip history append), `response.metadata`, `response.input`, `response.instructions`, `response.tools`, `response.audio.output.format.type` |
| `response.cancel` | Stop an in-flight response immediately. Critical for barge-in: when the user starts speaking, cancel the model's current audio output. | (no required payload beyond `type`) |
| `input_audio_buffer.append` | Stream user audio in. PCM16 or μ-law, base64-encoded, sent in chunks (typically 20–100 ms each). | `audio` (base64 string) |
| `input_audio_buffer.commit` | Manually mark the end of a user turn. Only needed when **not** using server VAD; with `server_vad` the server commits on silence. | (no required payload beyond `type`) |
| `input_audio_buffer.clear` | Discard buffered user audio that has not yet been committed. | (no required payload beyond `type`) |
| `output_audio_buffer.clear` | Stop the server from emitting any remaining queued output audio for the current response. | (no required payload beyond `type`) |

---

## Server → Client events (24+ on conversational; ~6 more on MCP/transcription paths)

These are the messages **OpenAI sends to your code**.

### Session lifecycle

| Event | Purpose | Key fields |
|---|---|---|
| `session.created` | Fires once when the WebSocket connection completes auth and the server allocates a session. Contains the initial server-side session config. | `session.id`, `session.model`, full `session` object |
| `session.updated` | Acknowledgement that a `session.update` you sent was applied. | resolved `session` object |
| `rate_limits.updated` | Periodic rate-limit telemetry from the server (tokens / requests remaining). | `rate_limits[]` array with name, remaining, reset_seconds |

### Conversation items

| Event | Purpose | Key fields |
|---|---|---|
| `conversation.item.added` | A new item (user message, assistant message, function call, function output) was appended to the conversation history. | `item.id`, `item.type`, `item.role`, `item.content` |
| `conversation.item.done` | The item is fully assembled (all deltas received). | `item.id`, `item.type`, complete `item` |

### Response lifecycle

| Event | Purpose | Key fields |
|---|---|---|
| `response.created` | Model has started generating a response. Use this to start your audio playback pipeline. | `response.id`, `response.status` |
| `response.output_item.added` | A new top-level output item (message, function_call) was added to the response. | `response.id`, `item.id`, `item.type` |
| `response.content_part.added` | A new content part inside a message was added (e.g. a text part vs an audio part). | `response.id`, `item.id`, `part.type` |
| `response.output_text.delta` | Streaming text token from the model. | `delta` (text fragment) |
| `response.output_text.done` | Text part finalized. | full `text` string |
| `response.output_audio.delta` | Streaming output audio chunk (base64 PCM16 or μ-law per the configured `audio.output.format.type`). | `delta` (base64) |
| `response.output_audio.done` | Output audio for this part finished. | (final marker) |
| `response.output_audio_transcript.delta` | Streaming transcript of the assistant's spoken audio (useful for captions and logging). | `delta` (text) |
| `response.output_audio_transcript.done` | Final assistant audio transcript. | full transcript text |
| `response.content_part.done` | A content part finished. | `part` object |
| `response.output_item.done` | Output item finished. | `item` object |
| `response.done` | Entire response is complete. Final usage data + final function-call arguments are available here. | `response.usage`, `response.output[]` (with final `arguments` strings for any function calls) |
| `response.cancelled` | Acknowledgement that a `response.cancel` you sent was honored. | `response.id` |

### Voice activity detection (user-side)

| Event | Purpose | Key fields |
|---|---|---|
| `input_audio_buffer.speech_started` | Server VAD detected the user started speaking. Use this as your barge-in signal — fire `response.cancel` + `conversation.item.truncate` immediately. | `audio_start_ms`, `item_id` |
| `input_audio_buffer.speech_stopped` | Server VAD detected the user stopped speaking. | `audio_end_ms`, `item_id` |
| `input_audio_buffer.committed` | Buffered user audio was committed into a conversation item — either by server VAD or by your explicit `input_audio_buffer.commit`. | `item_id`, `previous_item_id` |

### Function calling

| Event | Purpose | Key fields |
|---|---|---|
| `response.function_call_arguments.delta` | Streaming JSON argument fragments for a function call the model is emitting. Multiple in-flight `call_id`s indicate parallel tool calls. | `response.id`, `call_id`, `delta` (JSON fragment) |
| `response.function_call_arguments.done` | `[UNCONFIRMED]` — research file (item #1) notes only `.delta` was enumerated in the docs; counterpart `.done` is likely but not officially listed. Final arguments **are guaranteed** to appear on `response.done` regardless. | (likely `call_id`, `arguments`) |

### MCP (server-side tool calls)

| Event | Purpose | Key fields |
|---|---|---|
| `mcp_list_tools.in_progress` | Server is fetching the MCP server's tool list. | `server_label` |
| `mcp_list_tools.completed` | MCP tool list fetched. | `server_label`, `tools[]` |
| `mcp_list_tools.failed` | MCP tool list fetch failed. | `server_label`, `error` |
| `response.mcp_call.in_progress` | Model is invoking an MCP tool. | `call_id`, `server_label`, `name` |
| `response.mcp_call.failed` | MCP tool call failed. | `call_id`, `error` |
| `response.mcp_call_arguments.done` | MCP tool call arguments finalized. | `call_id`, `arguments` |

### Errors

| Event | Purpose | Key fields |
|---|---|---|
| `error` | Generic error. Inspect the wrapped error object for code + message. | `error.code`, `error.message`, `error.param`, `error.event_id` |
| `invalid_request_error` | Schema/validation error. Envelope: `{ "type": "invalid_request_error", "code": "invalid_value", "message": "", "param": "", "event_id": "" }` | `code`, `message`, `param`, `event_id` |

### Transcription (when session is in `type: "transcription"` mode)

| Event | Purpose | Key fields |
|---|---|---|
| `conversation.item.input_audio_transcription.delta` | Streaming user-audio transcript fragment. | `item_id`, `delta` |
| `conversation.item.input_audio_transcription.completed` | Final user-audio transcript. | `item_id`, `transcript` |

---

## Cross-references

- Research file (canonical source for everything on this page): `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md`
- OpenAI conversations guide (event names, payload shapes): `https://developers.openai.com/api/docs/guides/realtime-conversations`
- OpenAI WebSocket guide (URL, headers): `https://developers.openai.com/api/docs/guides/realtime-websocket`
- OpenAI WebRTC guide (`oai-events`, SDP POST, client_secrets): `https://developers.openai.com/api/docs/guides/realtime-webrtc`
- OpenAI VAD guide (server_vad / semantic_vad config): `https://developers.openai.com/api/docs/guides/realtime-vad`
- OpenAI MCP guide (tool format + MCP fields): `https://developers.openai.com/api/docs/guides/realtime-mcp`
- OpenAI SIP guide (call control REST + webhook): `https://developers.openai.com/api/docs/guides/realtime-sip`

## Open items propagated from the research file

1. `response.function_call_arguments.done` — exists in code but not enumerated in docs.
2. Exact JSON path for `reasoning.effort` — assume `session.reasoning.effort` until probed.
3. `OpenAI-Beta: realtime=v1` header — was required on old API, not in 2026 docs; treat as no longer required.
