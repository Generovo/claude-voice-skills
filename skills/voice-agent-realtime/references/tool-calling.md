# Tool calling — `gpt-realtime-2`

Voice agents only become useful when they can **do things**, not just talk. `gpt-realtime-2` supports two flavours of tool calls:

- **Function calls** — your code defines a schema, the model emits arguments, you execute and return results.
- **MCP** — the server itself talks to a remote Model Context Protocol server on your behalf; you don't see the round-trip.

Parallel tool calls are explicitly supported on `gpt-realtime-2` (per the 2026-05-07 announcement).

All event names and JSON shapes are sourced from `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md`.

---

## Tool definition shape

Tools live inside `session.tools[]`. The shape from the docs:

```json
{
  "type": "function",
  "name": "get_weather",
  "description": "Get the current weather for a city. Use when the user asks about temperature, conditions, or forecasts.",
  "parameters": {
    "type": "object",
    "properties": {
      "city":    { "type": "string", "description": "City name, e.g. 'Casablanca'" },
      "units":   { "type": "string", "enum": ["metric", "imperial"], "default": "metric" }
    },
    "required": ["city"]
  }
}
```

Keys:
- `type` — `"function"` for your own code; `"mcp"` for MCP servers (see below).
- `name` — must match the function name you'll dispatch on when results come back. `[a-zA-Z0-9_]+`.
- `description` — the **only** hint the model has for when to call this tool. Be specific about when to use it AND when not to.
- `parameters` — JSON Schema (object). Use `required` aggressively; the model invents missing optional fields.

### `tool_choice`

Sent on `session.update` or per-turn on `response.create`. Values seen in docs:

- `"auto"` — model decides (default).
- `"required"` — model must call at least one tool this turn.
- `[UNCONFIRMED]` `"none"` and `{ "type": "function", "name": "..." }` (force-a-specific-tool) — supported on prior Realtime generations, very likely still accepted, not explicitly listed on 2026 doc.

---

## Parallel tool calls

New in `gpt-realtime-2`: a single user turn can produce **multiple concurrent function calls**.

### Event timeline

When the user asks "What's the weather in Casablanca and what's on my calendar today?", the model can emit both calls in the same response. The server-side event stream looks like:

```
< response.created
< response.output_item.added     { item.type: "function_call", item.call_id: "call_aaa", name: "get_weather" }
< response.output_item.added     { item.type: "function_call", item.call_id: "call_bbb", name: "get_calendar" }
< response.function_call_arguments.delta  { call_id: "call_aaa", delta: '{"ci' }
< response.function_call_arguments.delta  { call_id: "call_bbb", delta: '{"da' }
< response.function_call_arguments.delta  { call_id: "call_aaa", delta: 'ty":"Casablanca"}' }
< response.function_call_arguments.delta  { call_id: "call_bbb", delta: 'te":"2026-05-11"}' }
< response.done                  { output: [...both call items with their final arguments...] }
```

Two things to notice:
1. Deltas for distinct `call_id`s **interleave**. Your client must buffer per-`call_id`.
2. Final, definitive arguments are guaranteed on `response.done.response.output[]` even if you missed a delta. The hypothetical `response.function_call_arguments.done` (`[UNCONFIRMED]` per research item #1) cannot be relied on as the sole completion signal.

### Pseudo-code dispatch

```ts
const pendingArgs = new Map<string, string>(); // call_id -> accumulating JSON string

ws.on("message", (evt) => {
  const e = JSON.parse(evt);
  switch (e.type) {
    case "response.function_call_arguments.delta":
      pendingArgs.set(e.call_id, (pendingArgs.get(e.call_id) ?? "") + e.delta);
      break;
    case "response.done":
      for (const item of e.response.output) {
        if (item.type !== "function_call") continue;
        const args = JSON.parse(item.arguments);
        // Run in parallel — do NOT await sequentially:
        runTool(item.name, args).then((result) => {
          sendFunctionOutput(item.call_id, result);
        });
      }
      // After ALL parallel results have been submitted, request the next turn:
      Promise.all(/* ... */).then(() => ws.send(JSON.stringify({ type: "response.create" })));
      break;
  }
});
```

---

## Submitting tool outputs

For each `call_id` you ran, send:

```json
{
  "type": "conversation.item.create",
  "item": {
    "type": "function_call_output",
    "call_id": "call_aaa",
    "output": "{\"temp_c\":21,\"conditions\":\"sunny\"}"
  }
}
```

Then **once**, after submitting all parallel outputs, send:

```json
{ "type": "response.create" }
```

Notes:
- `output` must be a **string**, even if the data is structured. JSON-encode it.
- Don't send `response.create` after each individual output in a parallel batch — that creates one response per output instead of one consolidated response.
- If a tool errored, return a JSON string like `{"error":"timeout","retry_after_s":5}`. The model handles it gracefully and will often surface a recovery utterance ("I'm having trouble with that right now" is documented as built-in recovery behavior).

---

## MCP (Model Context Protocol)

`gpt-realtime-2` can call MCP servers **directly server-side** — the OpenAI service authenticates to your MCP server and dispatches tool calls. You see lifecycle events but not the raw round-trip.

MCP tool variant inside `session.tools[]`:

```json
{
  "type": "mcp",
  "server_label": "my-crm",
  "server_url": "https://mcp.example.com",
  "authorization": "Bearer eyJ...",
  "headers": { "X-Tenant-Id": "abc" },
  "allowed_tools": ["search_customer", "create_ticket"],
  "require_approval": "never"
}
```

Alternative: replace `server_url` with `connector_id` for OpenAI-managed connectors.

### MCP server events

- `mcp_list_tools.in_progress` — server is fetching the tool list.
- `mcp_list_tools.completed` — tool list cached for the session.
- `mcp_list_tools.failed` — auth or network failure.
- `response.mcp_call.in_progress` — model is mid-MCP-call.
- `response.mcp_call.failed` — MCP call failed.
- `response.mcp_call_arguments.done` — final arguments captured.

OpenAI's MCP guide: `https://developers.openai.com/api/docs/guides/realtime-mcp` (see research file Sources section).

---

## End-to-end mini-example: weather + calendar in parallel

User asks: *"What's the weather in Casablanca and what's on my calendar today?"*

```
> session.update                 (declare get_weather + get_calendar tools, tool_choice: "auto")
> input_audio_buffer.append      (user speech chunks, base64 PCM16)
< input_audio_buffer.speech_started
< input_audio_buffer.speech_stopped
< input_audio_buffer.committed   (server VAD committed the turn)
< response.created               (the model picked up the turn)
< response.output_item.added     (item.call_id: "call_aaa", name: "get_weather")
< response.output_item.added     (item.call_id: "call_bbb", name: "get_calendar")
< response.function_call_arguments.delta   (call_aaa, partial JSON)
< response.function_call_arguments.delta   (call_bbb, partial JSON)
...
< response.done                  (full args for both calls in response.output[])

# Your code runs both in parallel:
> conversation.item.create       (function_call_output for call_aaa)
> conversation.item.create       (function_call_output for call_bbb)
> response.create                (one consolidated request for the spoken summary)

< response.created               (model starts the spoken answer)
< response.output_audio.delta    ... (streamed PCM16/μ-law to the user)
< response.output_audio_transcript.delta ... (captions)
< response.output_audio.done
< response.done                  (usage + final state)
```

Total round-trips: **1 user turn → 1 parallel tool burst → 1 consolidated spoken answer**. Without parallel calls this would have been 2 sequential turns with a "let me check the weather first, then your calendar" preamble in between.

---

## See also

- `/Users/youssef/.claude/skills/voice-agent-realtime/references/preambles-interruptions.md` — preambles are emitted **between** `response.created` and `response.function_call_arguments.delta`, often as a short spoken filler ("let me check that"). Tool transparency is in the announcement as a feature.
- `/Users/youssef/.claude/skills/voice-agent-realtime/references/events-websocket.md` — full event catalog with payload field names.
- Research file `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md` — canonical source.
