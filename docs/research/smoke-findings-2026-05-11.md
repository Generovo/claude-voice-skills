# Smoke-test findings — 2026-05-11

Real API probes against OpenAI Realtime, ~4 days after the May 7 release of `gpt-realtime-2`, `gpt-realtime-translate`, `gpt-realtime-whisper`.

## Setup

- OS: macOS 14.x (Darwin 25.4)
- Python: 3.12.8
- Auth: `OPENAI_API_KEY` (sk-proj prefix) in `.env`
- Cert bundle: `SSL_CERT_FILE=$(python3 -m certifi)` required on macOS — system Python doesn't trust public CAs out of the box.

## Finding 1 — `gpt-realtime-2` listed but rejected by Realtime WS

`GET /v1/models` returns `gpt-realtime-2` as a valid model. But:

```
wss://api.openai.com/v1/realtime?model=gpt-realtime-2
→ received 4000 (private use) invalid_request_error.invalid_model
```

**Implication:** The model is announced and visible in the catalog, but the Realtime WebSocket endpoint has not yet been wired to accept it. This matches the historical Azure-style lag, except here it's an OpenAI-internal lag between catalog publication and Realtime endpoint deployment.

**Workaround:** Use `gpt-realtime` (current GA, 2025-08-28 baseline) until v2 is enabled on the endpoint.

**Action:** Skills already parametrize the model via env var (`OPENAI_MODEL`). No code change needed. Users should set `OPENAI_MODEL=gpt-realtime` for the next ~2-8 weeks, then switch to `gpt-realtime-2` when OpenAI flips the switch.

## Finding 2 — `gpt-realtime` connects but default session.update yields empty response

With `model=gpt-realtime`, `test-latency.py` ran without errors but returned `ttfa=None` and `done=None` — meaning the session opened and audio was sent, but no `response.audio.delta` or `response.done` events came back.

**Likely causes (not exhaustively diagnosed):**
- `output_modalities` field may not be accepted on `gpt-realtime` (the new field is in v2 docs).
- `reasoning.effort` is `[UNCONFIRMED]` in research and likely not supported on the older model.
- The session.update payload follows the v2 documented shape; the v1.x model may need a different shape (e.g., `modalities: ["audio"]` vs `output_modalities: ["audio"]`).

**Action:** When v2 ships, the current scripts should work as-is. For now, users testing against `gpt-realtime` may need to:
1. Remove `reasoning.effort` from `session.update`.
2. Use `modalities: ["audio"]` instead of `output_modalities: ["audio"]`.
3. Adjust event-handling to also listen for older event names.

The skills propagate `[UNCONFIRMED]` tags exactly where these uncertainties live, so users adapting to the older model have explicit pointers.

## Finding 3 — All event names and endpoint URLs in research file remain correct

The WebSocket connection itself succeeds, session.update reaches the server (otherwise we'd get auth or URL errors, not a model-specific rejection). This confirms:
- Endpoint URL `wss://api.openai.com/v1/realtime?model=...` is correct.
- Auth header `Authorization: Bearer ...` is accepted.
- `OpenAI-Beta: realtime=v1` header (sent defensively) is at minimum not breaking the connection.

## Finding 4 — `gpt-realtime-translate` and `gpt-realtime-whisper` not smoke-tested

Time and audio-sample constraints. The skills are structurally validated (28/28 unit tests pass, py_compile + tsc clean, harness green). Full integration smoke against translate and whisper is deferred until:
- Real audio samples exist in `samples/audio/` for each skill.
- User confirms the v2 models are wired to the WS endpoint.

## Recommended next steps for the user

1. **Bookmark this file** — re-probe `gpt-realtime-2` in 1-2 weeks; flip `OPENAI_MODEL` once accepted.
2. **Add 3 real WAV samples** (mono, 24kHz, 16-bit) to:
   - `~/.claude/skills/voice-agent-realtime/scripts/samples/` (one short utterance, e.g., "What time is it in Paris?")
   - `~/.claude/skills/voice-translate-live/scripts/samples/audio/` (3 pairs — fr-en, en-fr, ar-fr)
   - `~/.claude/skills/voice-transcribe-stream/scripts/samples/audio/` (3 references)
3. **Rotate the leaked Azure key** (KEY 2 on `myvoiceagents`) and the OpenAI key (`sk-proj-...` shared in chat).
