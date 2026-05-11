---
name: voice-translate-live
description: Use when building live voice translation features with OpenAI's GPT-Realtime-Translate model — interpreters, multilingual calls, conferences, live video translation. Covers the dedicated `/v1/realtime/translations` endpoint, multi-speaker session architecture, language support (70+ in / 13 out). Do NOT use for conversational agents (see voice-agent-realtime).
---

# Voice Translate Live (GPT-Realtime-Translate)

This skill is the operational playbook for building real-time voice translation on OpenAI's `gpt-realtime-translate` model (announced 2026-05-07). Every event name, JSON field, endpoint URL, and pricing number traces back to `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md`, itself sourced from the official OpenAI docs and a live API probe. Items the research file flagged `[UNCONFIRMED]` carry the same tag here — do not silently treat them as proven.

> **READ THIS BEFORE WRITING ANY EVENT NAME.** The translation endpoint uses a **different event vocabulary** from the conversational endpoint. Audio buffer events are prefixed `session.` (e.g. `session.input_audio_buffer.append`). The conversational endpoint uses unprefixed names. Mixing them silently breaks — the wire accepts the JSON but the model never sees your audio. See `## Event vocabulary — CRITICAL DIFFERENCE`.

## When to use this skill

- **Live interpreters** — bilateral conversations between two speakers who do not share a language (business meetings, diplomatic, medical consults).
- **Multilingual phone or video calls** — telephony or WebRTC where one or more participants need a real-time spoken translation track.
- **Conference simultaneous interpretation** — one speaker, N target-language listeners on separate audio buses.
- **Live-stream / broadcast translation** — translating a presenter to multiple language streams for an international audience.
- **Education sessions / training** — instructor speaks in one language, learners receive translated audio in their own.
- **E-commerce, travel, healthcare multilingual support** — frontline staff and customers speak different languages; the bot is an interpreter, not an agent.

## When NOT to use

- If you need **reasoning, tool calling, or function execution** during the call → use `voice-agent-realtime` (powered by `gpt-realtime-2`). Translate has **no tool calling** and no reasoning levels — it is a direct audio-in / audio-out translator.
- If you only need **transcription** (no spoken output, just text captions) → use `voice-transcribe-stream` (powered by `gpt-realtime-whisper`, $0.017/min). You will already get a source-language transcript for free as a side-effect of translation, but if transcription is your sole goal, the whisper model is half the price.
- If the audio is **batch / non-streaming** (recorded file translation, voice notes) → use `whisper-1` for transcription plus a regular Chat Completions call for translation. The Realtime API is overkill for non-interactive workloads.

---

## Model at a glance

| Property | Value |
|---|---|
| Model ID | `gpt-realtime-translate` |
| Modalities in | Audio |
| Modalities out | Audio + text (target-language audio + concurrent source/target transcripts) |
| Tool calling | **Not supported** |
| Reasoning levels | Not applicable |
| Pricing | **$0.034 / minute** of realtime audio duration |
| Max context window | 16,000 tokens |
| Max output per response | 2,000 tokens |
| Input languages | 70+ |
| Output languages | 13 |
| Audio format | PCM16, 24 kHz, base64-encoded on the wire |
| Voice config | `[UNCONFIRMED]` — translation guide did not show a `voice` field; model card lists audio output but only `audio.output.language` is shown in examples |
| Knowledge cutoff | Sep 30, 2024 |
| Benchmark cited | 12.5% lower WER than competing models on Hindi/Tamil/Telugu (BolnaAI eval, OpenAI announcement) |

Full pricing worked examples → `references/pricing-limits.md`.

---

## Endpoint

```
WebSocket: wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate
WebRTC SDP exchange: POST https://api.openai.com/v1/realtime/translations/calls
Ephemeral token mint: POST https://api.openai.com/v1/realtime/translations/client_secrets
Header: Authorization: Bearer ${OPENAI_API_KEY}
Header: OpenAI-Safety-Identifier: <hashed-user-id>  (optional, recommended)
```

The base path `/v1/realtime/translations` is **distinct** from the conversational base path `/v1/realtime`. Anything that worked on the conversational endpoint is not guaranteed to work here.

---

## Event vocabulary — CRITICAL DIFFERENCE

The translation endpoint uses a **simplified, prefixed** event vocabulary. The research file marks this as a `[CONTRADICTION]` against the conversational endpoint, and it is the single most common source of silent failure when porting code between the two.

| Action | Conversational endpoint | Translation endpoint |
|---|---|---|
| Configure session | `session.update` | `session.update` (same name; different fields) |
| Append audio chunk | `input_audio_buffer.append` | `session.input_audio_buffer.append` |
| Streamed translated audio (server→client) | `response.output_audio.delta` | `session.output_audio.delta` |
| Streamed target-language transcript | `response.output_audio_transcript.delta` | `session.output_transcript.delta` |
| Streamed source-language transcript | (n/a — model only sees user audio) | `session.input_transcript.delta` |
| Manual commit, response.create, cancel, truncate | Available | `[UNCONFIRMED]` — translation guide did not enumerate them; translation is a continuous-stream model so `response.create` likely does not apply |
| `.done` / `.completed` server events | Enumerated for each `.delta` | `[UNCONFIRMED]` — only `.delta` shown in docs; likely exist, probe before relying |
| Tool calling, MCP, function_call_output | Supported | **Not supported** |

> **WARNING.** Do NOT mix vocabularies. The translation endpoint silently drops unprefixed events — the connection stays open, you receive no error, and no translated audio ever comes back. Before sending audio, grep your own code for the literal `input_audio_buffer.append` and confirm every occurrence is preceded by `session.`.

`[UNCONFIRMED]` lifecycle events. `session.created`, `session.updated`, and `error` almost certainly exist (the WebSocket needs to acknowledge config and report errors), but the translation guide did not explicitly enumerate them. Treat their presence as probable but not contractual.

---

## Multi-speaker architecture

**The rule:** one session per **output language**, with **separate audio tracks per speaker** muxed into the same input direction. Mixing multiple target languages into one session is unsupported — each session targets a single output language via `audio.output.language`. Mixing speakers' audio into one input stream is supported (the model translates whoever is speaking), but you typically want per-speaker tracks so you can label and route the output cleanly.

```
                 ┌───── session A: out = "en" ─────► Listener A hears English
Speaker 1 (fr) ──┤
                 └───── session B: out = "es" ─────► Listener B hears Spanish
                 ┌─── (same two sessions reused) ────►
Speaker 2 (ar) ──┤
                 └─── (same two sessions reused) ────►
```

Why one session per output language:
- The session's `audio.output.language` is set once and applies to every translated chunk that session emits. To get two output languages from the same source audio, you need two parallel sessions, each receiving the same input audio frames.
- Each session maintains its own context (16K tokens) of recent source audio for coherent translation. A single session asked to alternate output languages would have to switch translation context every utterance — not a supported mode.
- Output transcripts (`session.output_transcript.delta`) are emitted in the session's configured target language; a single session cannot produce multilingual transcripts.

**Scaling math.** For N speakers and M target languages, you may need up to N × M sessions in the worst case (each listener pair) — but in practice you collapse along the speaker axis: M sessions total, each fed the mixed input audio from all speakers, each producing one language output. Add per-speaker source-language transcript routing if you want to label who said what in the captions UI.

**Audio fan-out.** Your bridge owns the fan-out: read user audio once from the transport (Twilio, WebRTC, meeting bot), base64-encode it once, and `session.input_audio_buffer.append` it to **each** target-language WebSocket in parallel. Don't open one session and pray.

---

## Languages

- **Input:** 70+ languages (OpenAI announcement). Full enumeration `[UNCONFIRMED]` — not on either source page fetched.
- **Output:** 13 languages (OpenAI announcement). Full enumeration `[UNCONFIRMED]` — not on either source page fetched. The translation guide example uses ISO-639-1 code `"es"` (Spanish); other codes follow the same standard.

Always use ISO-639-1 two-letter codes (`en`, `fr`, `es`, `ar`, `zh`, …) for `audio.output.language` until docs publish the canonical list.

→ see `references/languages.md` for known input/output language details and pair-selection tips.

---

## Channel-specific transport

The translation endpoint accepts WebSocket or WebRTC. Your choice depends on **who owns the audio path**.

### Telephony (Twilio interpreter)

- Twilio Media Streams sends μ-law 8 kHz PCM frames over a WebSocket to your bridge.
- Your bridge resamples to **PCM16 24 kHz** (the translation endpoint shows only this format) and base64-encodes per `session.input_audio_buffer.append`.
- For a bidirectional interpreter, open **two** translation sessions: caller-language→callee-language and callee-language→caller-language. Route each session's `session.output_audio.delta` back to the appropriate Twilio leg.

→ see `examples/twilio-interpreter.py` (to be added)

### Web (multi-participant conference)

- WebRTC client captures speaker mic; your server mints an ephemeral key via `POST /v1/realtime/translations/client_secrets` and the browser SDP-exchanges with `POST /v1/realtime/translations/calls`.
- Each listener subscribes to a separate translated audio stream by opening their own session (or by your server demultiplexing M parallel server-side sessions and re-encoding for WebRTC SFU delivery).

→ see `examples/web-conference.tsx` (to be added)

### WhatsApp (voice notes)

- WhatsApp **voice notes** are async (record, send, receive). Strictly speaking this skill is for realtime — for voice notes, transcribe with `whisper-1` and translate via Chat Completions, then synthesize with the TTS API.
- WhatsApp **Calls** (via Business Solution Provider bridging) are realtime and can use the WebSocket pattern above.

→ see `examples/whatsapp-translate.py` (to be added)

### Meeting bots (Recall / similar)

- A virtual participant (Recall.ai) joins the meeting and exposes the meeting audio as a WebSocket.
- Your bridge fans the audio out to one translation session per attendee target language and injects the translated audio back into the meeting via the bot's TTS-injection channel (or delivers separate language tracks to each listener).

→ see `examples/meeting-translator.py` (to be added)

---

## Latency considerations

Translation is **inherently higher-latency** than a conversational response: the model must hear enough of an utterance to translate coherently, then synthesize target-language speech. Expect **~1–2 seconds** end-to-end for short utterances, more for long ones. Do not target the <500ms time-to-first-audio numbers that apply to conversational `gpt-realtime-2`.

Practical implications:

- **Buffering.** Server VAD on the translation endpoint chunks naturally at utterance boundaries. If you commit too aggressively (e.g., flush every 200ms manually), the model lacks context and the translation quality drops. Let VAD drive commits.
- **Echo cancellation.** When playing translated audio back into the same room as the source speaker, you MUST echo-cancel — otherwise the translation gets fed back as input and the session goes into a translation loop.
- **Buffering UI.** Show listeners a "translating…" caption or a small playback delay buffer so they don't perceive the gap as a dropped connection.
- **Low-resource languages.** Source languages with thin training coverage may produce visibly slower translations and lower WER; pick the closest supported source-language code when the speaker is bilingual.

---

## Pricing & limits

| Property | Value |
|---|---|
| Pricing | $0.034 / minute of audio duration |
| Context window | 16,000 tokens |
| Max output | 2,000 tokens |
| Billing unit | Audio duration (not tokens) — `[UNCONFIRMED]` whether this counts input minutes only, output minutes only, or both. Treat as wall-clock session minutes per session for conservative estimates. |
| Rate limits | `[UNCONFIRMED]` — see OpenAI platform docs for current per-org limits |

Full worked examples (5-min support call, 1-hour 4-speaker / 3-language conference, 24/7 hotline) → `references/pricing-limits.md`.

---

## Common pitfalls

- **Mixing event vocabularies.** Sending `input_audio_buffer.append` (no prefix) to `/v1/realtime/translations` results in silent drops. Always `session.input_audio_buffer.append` here.
- **One session for multiple target languages.** Doesn't work. Open one session per output language. See `## Multi-speaker architecture`.
- **Audio format mismatch.** The translation endpoint shows PCM16 24 kHz on both sides. Telephony's native μ-law 8 kHz must be resampled before send. `[UNCONFIRMED]` whether `audio/pcmu` is even accepted on this endpoint — the docs only showed PCM16.
- **Expecting tool calling, MCP, or reasoning levels.** None of these apply. Translation is a direct audio-to-audio model with no agentic surface area.
- **Expecting `response.create`-driven turn control.** Translation is continuous-stream. There is no per-turn `response.create` in the documented vocabulary. Drive the session via VAD.
- **Echo loops on shared-room playback.** Translated audio played in the same physical space as the source mic will be picked up and re-translated infinitely. Use directional audio (headphones for listener) or hardware echo cancellation.
- **Trusting unconfirmed lifecycle events.** Don't write code that requires `session.created`, `session.updated`, or `error` until you have probed and observed them on a live session.

---

## Reference index

Long-form references (this dispatch):
- `references/languages.md` — input / output language details, pair-selection tips, code-switching notes.
- `references/session-config.md` — exact `session.update` JSON shape for the translation endpoint, field-by-field reference, comparison with conversational sessions.
- `references/pricing-limits.md` — full pricing table, 3 worked cost examples (5-min support call, 1-hour multi-language conference, 24/7 multilingual hotline), context limits.

Examples (to be added):
- `examples/twilio-interpreter.py` — Twilio Media Streams ↔ OpenAI translation bridge with two parallel sessions for bidirectional interpretation.
- `examples/web-conference.tsx` — browser WebRTC client for one-speaker, multiple-listener-language conference.
- `examples/whatsapp-translate.py` — WhatsApp Calls translator via Business Solution Provider bridge.
- `examples/meeting-translator.py` — Recall.ai meeting-bot integration with per-attendee target language.

Scripts (to be added):
- `scripts/probe-events.py` — live event-vocabulary probe to resolve `[UNCONFIRMED]` items (session lifecycle, `.done` events, voice config).

Sample files (to be added):
- A reference 24 kHz PCM16 audio clip and base64 chunk encoder.

Canonical source for every fact in this skill: `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md`.
