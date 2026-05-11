# Preambles & interruption handling — `gpt-realtime-2`

Two of the most important UX details in a real voicebot:

1. **Preambles** — short fillers ("let me check that…", "looking that up now…") that the model speaks **before** running a tool. They make the agent feel responsive instead of mysteriously silent during a 2-second API call.
2. **Interruption handling (barge-in)** — when the user starts speaking while the model is still speaking, the model must shut up immediately, drop its queued audio, and listen.

Both are features of `gpt-realtime-2` (per the 2026-05-07 announcement), but you have to wire them up correctly. All event names below are from `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md`.

---

## Preambles

### What they are

Per the announcement, `gpt-realtime-2` will spontaneously emit short utterances **before** invoking a tool, e.g.:

- "Sure, let me check that for you."
- "One moment, pulling that up now."
- "Got it — searching the customer record."

These count as **tool transparency**: the user hears that something is happening, instead of dead silence.

### Where preambles appear in the event stream

The audio of the preamble is just normal `response.output_audio.delta` traffic. Timeline:

```
< response.created
< response.output_audio.delta      ← preamble audio: "let me check that"
< response.output_audio.delta
< response.output_audio.done       ← preamble finished playing
< response.output_item.added       ← function_call item added
< response.function_call_arguments.delta   ← arguments streaming
...
< response.done
> conversation.item.create         ← your function_call_output
> response.create                  ← request the post-tool spoken answer
< response.created
< response.output_audio.delta      ← actual answer: "the temperature in Casablanca is..."
```

The preamble is one self-contained audio "content part" in the response, then the function call is the next content part.

### Prompt patterns that produce them naturally

You don't toggle preambles on or off with a flag — you nudge them via `instructions`. Phrases that work well:

```
You are a phone support agent. Before calling any tool that may take
more than 200ms, briefly tell the user what you are about to do
("let me check that", "one moment, looking that up"). Keep these
acknowledgements under 5 words. Never say "please hold" — say what
you are doing.
```

Counter-patterns to avoid: instructions like "respond as quickly as possible" or "be concise" can suppress preambles. If you observe silence-during-tool-calls, soften the brevity instruction.

### When to suppress preambles

For ultra-fast tools (<150 ms) preambles add latency without helping. Either omit those tools from the "use a preamble" rule in `instructions`, or pass them as MCP tools (server-side; user never knows they ran).

---

## Interruption handling (barge-in)

### Turn detection modes

Configured under `session.audio.input.turn_detection` (the research file documents both shapes):

**Server VAD** — energy-based, fast, suitable for clean audio:

```json
{
  "type": "server_vad",
  "threshold": 0.5,
  "prefix_padding_ms": 300,
  "silence_duration_ms": 500,
  "create_response": true,
  "interrupt_response": true
}
```

Field meanings:
- `threshold` — energy threshold (0–1). Higher = needs louder speech to trigger. Default 0.5.
- `prefix_padding_ms` — audio buffered before VAD trigger (so you don't clip the user's first phoneme). Default 300 ms.
- `silence_duration_ms` — silence required after speech to consider the turn over. Default 500 ms. Telephony: bump to 700–900 ms because PSTN gaps are noisy.
- `create_response` — when `true`, the server auto-fires `response.create` on speech_stopped. Set `false` if you want manual control.
- `interrupt_response` — when `true`, the server auto-cancels an in-flight response when the user starts speaking again. Recommended on.

**Semantic VAD** — model-based, smarter (knows pause vs end-of-turn), slightly more latency:

```json
{
  "type": "semantic_vad",
  "eagerness": "medium",
  "create_response": true,
  "interrupt_response": true
}
```

`eagerness`: `"low"` | `"medium"` | `"high"` | `"auto"`. Higher = commits the turn faster, risks cutting the user off.

`[UNCONFIRMED]` Whether `type: "none"` (fully manual VAD) is accepted — research file flag, the VAD guide didn't list it. If you need fully manual control, send `input_audio_buffer.commit` yourself and skip `turn_detection`.

### Barge-in flow

Even with `interrupt_response: true` doing most of the work, you should react on the client too — the server's cancel takes a network round-trip and you don't want stale audio bleeding through your speaker.

```
User starts speaking mid-bot-response:
< input_audio_buffer.speech_started   ← server VAD detected it

Client immediately:
  1. STOP playing buffered output_audio.delta locally (clear your audio sink).
  2. Send  > response.cancel
  3. Send  > conversation.item.truncate
            { item_id: "<the assistant item being interrupted>",
              content_index: 0,
              audio_end_ms: <ms of audio actually heard by user> }
  4. (output_audio_buffer.clear is also documented but `response.cancel` covers it)

Server:
< response.cancelled

User audio continues streaming in:
> input_audio_buffer.append ... (their interruption)
< input_audio_buffer.speech_stopped
< input_audio_buffer.committed
< response.created                    ← new response to the interruption
```

The `conversation.item.truncate` step is what tells the **model** "you never actually finished that sentence; do not assume the user heard the rest." Without it, the model thinks it said the whole thing and may not repeat important info.

### Computing `audio_end_ms` for truncate

You need to track how much of the response's audio has actually played out of the user's speaker, not how much you received from the server. The difference is your audio buffer.

```
audio_end_ms ≈ (current_playback_position_in_assistant_audio_ms)
```

If you have, say, 800 ms of unplayed audio in your output buffer when the user interrupts, and you've played 2400 ms, send `audio_end_ms: 2400`, not 3200.

---

## Common bugs

1. **Late cancels → audio bleed.** You handle `response.cancel` only after fully processing 5 more `output_audio.delta` events. Always cancel **synchronously** on `input_audio_buffer.speech_started`. Drain your buffer first.

2. **VAD threshold too aggressive (`threshold: 0.3` in a noisy call center).** Background hum or keyboard clicks trigger `speech_started`, which cancels your bot mid-sentence. Either raise `threshold` to 0.6–0.7 for telephony, switch to `semantic_vad` with `eagerness: "low"`, or pre-process audio to suppress noise client-side.

3. **VAD too conservative (`silence_duration_ms: 1500`).** Bot waits 1.5 s after the user finishes before responding — feels broken. Tune to 400–600 ms for chat-like flow, 700–900 ms only when telephony jitter requires it.

4. **Audio format mismatch.** Server is configured for `audio/pcm` at 24 kHz, but Twilio is sending μ-law at 8 kHz. You'll get either silence or garbled noise. Three fixes: (a) configure `audio.input.format.type: "audio/pcmu"` if μ-law is acceptable end-to-end; (b) resample 8 kHz → 24 kHz PCM16 on the bridge; (c) use `audio/pcma` `[UNCONFIRMED]` for A-law (not seen in 2026 docs but plausible). The research file flags audio format strings beyond `audio/pcm` and `audio/pcmu` as `[UNCONFIRMED]` (item #3).

5. **Base64 encoding errors.** Audio is sent as base64-encoded PCM16 in `input_audio_buffer.append`. Subtle bugs: not zero-padding on chunk boundaries, sending raw bytes instead of base64, double-encoding. Symptom: server emits `input_audio_buffer.speech_started` followed by garbage transcripts ("zzzz brrrr").

6. **`conversation.item.truncate` with wrong `audio_end_ms`.** Setting `audio_end_ms` to the server-received audio length instead of client-played length causes the model to "remember" speech the user never heard. Track playback position, not receive position.

7. **Cancelling before `response.created`.** If the user barges in during the gap between `input_audio_buffer.committed` and `response.created`, your `response.cancel` may fail with "no active response." Guard the cancel with a check that you actually have a response in flight, or just swallow the error.

8. **Silence in noisy environments.** Server VAD threshold-based detection fails on persistent low-level noise (HVAC, fan, mic gain too high). Run a 1-second mic baseline at session start and adapt your `threshold` accordingly. Or switch to `semantic_vad`.

---

## See also

- `/Users/youssef/.claude/skills/voice-agent-realtime/references/events-websocket.md` — full server event catalog (look up the exact `input_audio_buffer.*` events).
- Research file `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md` — canonical source.
- OpenAI VAD guide: `https://developers.openai.com/api/docs/guides/realtime-vad`.
