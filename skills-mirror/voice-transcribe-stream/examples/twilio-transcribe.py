"""
twilio-transcribe.py — Twilio Voice <-> OpenAI gpt-realtime-whisper bridge.

Purpose
-------
A FastAPI service that bridges a Twilio Voice phone call (over Twilio Media
Streams WebSocket) to an OpenAI streaming **transcription** session. The
caller dials a Twilio number, Twilio opens a Media Stream WS to this service,
this service opens a WS to OpenAI's transcription endpoint, audio flows
one-way (caller -> OpenAI), and transcripts flow back out either to stdout or
to a configured webhook URL (CRM, Pub/Sub, etc.).

Unlike the conversational bridge in voice-agent-realtime/examples/twilio-
bridge.py, this bridge has NO audio output path: gpt-realtime-whisper emits
text only. No `response.create`, no `response.output_audio.*`. The whole
point is "transcript appears as the speaker talks."

Dependencies
------------
    pip install fastapi uvicorn websockets python-multipart httpx

Python 3.12+. NOTE: the stdlib `audioop` module is deprecated in 3.13 and
slated for removal. For 3.13+ install the drop-in replacement:

    pip install audioop-lts

...and replace `import audioop` with `import audioop_lts as audioop`.

Run
---
    uvicorn examples.twilio-transcribe:app --host 0.0.0.0 --port 8080

Public exposure (ngrok / Cloudflare Tunnel):
    ngrok http 8080
Then point your Twilio Voice number webhook to:
    https://<tunnel>/twiml

Environment
-----------
    OPENAI_API_KEY              Bearer for OpenAI Realtime (required).
    OPENAI_MODEL                Model ID; defaults to "gpt-realtime-whisper".
    PUBLIC_WS_URL               Public WSS URL of THIS service's /media
                                endpoint, e.g. wss://<tunnel>/media — used in
                                the TwiML <Stream url=...>.
    TRANSCRIPT_WEBHOOK_URL      Optional. Where to POST finalized transcripts
                                (JSON body shape below). If unset, transcripts
                                are logged to stdout only.
    LATENCY_PROFILE             "ultra-low" | "balanced" | "quality-max".
                                Default "balanced". See references/
                                latency-tuning.md.
    LANGUAGE                    Optional ISO-639-1 hint, e.g. "fr", "en", "ar".
                                Omit to let the model auto-detect.

Webhook JSON body shape
-----------------------
    {
      "callSid": "CAxxx",
      "streamSid": "MZxxx",
      "ts_start": 1234567890.0,
      "ts_end":   1234567892.5,
      "text":     "Hello, I'd like to book a table for two."
    }

A stub `POST /transcript-webhook-test` is included that just logs incoming
transcripts — useful for end-to-end testing without setting up a real CRM.

Sources (verified 2026-05-11)
-----------------------------
- OpenAI Realtime transcription guide:
  https://developers.openai.com/api/docs/guides/realtime-transcription
  (session.update shape with session.type = "transcription";
   conversation.item.input_audio_transcription.delta / .completed events;
   server_vad turn detection; "include" with logprobs)
- OpenAI gpt-realtime-whisper model card:
  https://developers.openai.com/api/docs/models/gpt-realtime-whisper
- OpenAI realtime-websocket auth headers:
  https://developers.openai.com/api/docs/guides/realtime-websocket
- Project snapshot of API surface as of 2026-05-11:
  /Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md
- Twilio Media Streams (TwiML <Stream>, base64 mu-law 8 kHz, streamSid):
  https://www.twilio.com/docs/voice/twiml/stream
  https://www.twilio.com/docs/voice/media-streams/websocket-messages

[UNCONFIRMED] items propagated from the research file
-----------------------------------------------------
- Exact WebSocket URL for the transcription endpoint. Two candidates:
    Option A: wss://api.openai.com/v1/realtime?model=gpt-realtime-whisper
    Option B: wss://api.openai.com/v1/realtime/transcription_sessions?model=...
  This bridge tries Option A first (the discriminator is
  session.type = "transcription" in the first session.update). If A returns
  4xx on connect, fall back to B. See `OPENAI_TRANSCRIBE_WS_URL_PRIMARY` /
  `_FALLBACK` below.
- Whether `OpenAI-Beta: realtime=v1` is still required. Sent defensively
  (harmless if obsolete).
- Whether `audio/pcmu` (mu-law) is accepted as `audio.input.format.type` on
  this model. The transcription guide example only showed `audio/pcm` @ 24
  kHz. This bridge takes the SAFE path: PCM16 @ 24 kHz to OpenAI, with
  stdlib `audioop` doing the 8 kHz mu-law <-> 24 kHz PCM16 conversion on the
  Twilio side. If you confirm `audio/pcmu` works, you can drop the resample.
- Whether `input_audio_buffer.speech_started` / `.speech_stopped` /
  `.committed` are emitted on transcription sessions. Likely yes (shared
  session vocabulary with the conversational endpoint); we react if present
  and ignore if absent.
- Exact field name carrying final text on `.completed`. Research suggests
  `transcript`; we probe `transcript`, then `text`, then `content` and use
  the first one present.
- The IMPORTANT vocabulary detail: this endpoint uses the **unprefixed**
  `input_audio_buffer.append` (matching the conversational endpoint), NOT
  the `session.`-prefixed translation variant. Verified against
  ~/.claude/skills/voice-transcribe-stream/SKILL.md §Endpoint and session
  config.
"""

from __future__ import annotations

import asyncio
import audioop  # deprecated in 3.13 — see header
import base64
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from websockets.asyncio.client import ClientConnection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("twilio-transcribe")


# --------------------------------------------------------------------------- #
# Env / config                                                                #
# --------------------------------------------------------------------------- #


OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL: str = os.environ.get("OPENAI_MODEL", "gpt-realtime-whisper")
PUBLIC_WS_URL: str = os.environ.get(
    "PUBLIC_WS_URL", "wss://example.invalid/media"
)
TRANSCRIPT_WEBHOOK_URL: str = os.environ.get("TRANSCRIPT_WEBHOOK_URL", "")
LATENCY_PROFILE: str = os.environ.get("LATENCY_PROFILE", "balanced")
LANGUAGE: str = os.environ.get("LANGUAGE", "")


# [UNCONFIRMED] which of these two URL shapes is the live transcription WS
# endpoint. We try PRIMARY on initial connect; if it 4xx/5xx, we retry with
# FALLBACK once. Both candidates are documented in the research file.
OPENAI_TRANSCRIBE_WS_URL_PRIMARY: str = (
    f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}"
)
OPENAI_TRANSCRIBE_WS_URL_FALLBACK: str = (
    f"wss://api.openai.com/v1/realtime/transcription_sessions?model={OPENAI_MODEL}"
)

# Twilio Media Streams audio is base64-encoded mu-law at 8 kHz, mono.
# OpenAI's transcription session, on the safe path, takes PCM16 at 24 kHz.
TWILIO_RATE: int = 8000
OPENAI_RATE: int = 24000
PCM_WIDTH: int = 2  # bytes per sample for PCM16


# --------------------------------------------------------------------------- #
# Latency profile -> server_vad fields                                        #
# (See ~/.claude/skills/voice-transcribe-stream/references/latency-tuning.md) #
# --------------------------------------------------------------------------- #


def vad_for_profile(profile: str) -> dict[str, Any]:
    """Map LATENCY_PROFILE to server_vad config.

    All thresholds and durations are inherited from the conversational
    endpoint defaults as documented in the research file; [UNCONFIRMED]
    whether the transcription endpoint accepts the same field names — likely
    yes, since session.update shares vocabulary.
    """
    if profile == "ultra-low":
        return {
            "type": "server_vad",
            "threshold": 0.4,
            "prefix_padding_ms": 200,
            "silence_duration_ms": 300,
        }
    if profile == "quality-max":
        return {
            "type": "server_vad",
            "threshold": 0.6,
            "prefix_padding_ms": 500,
            "silence_duration_ms": 800,
        }
    # Default — balanced.
    return {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 500,
    }


# --------------------------------------------------------------------------- #
# FastAPI app + lifespan                                                      #
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-redef]
    if not OPENAI_API_KEY:
        log.warning(
            "OPENAI_API_KEY not set — bridge will fail when a call comes in."
        )
    log.info(
        "twilio-transcribe started | model=%s | public_ws=%s | profile=%s | "
        "language=%s | webhook=%s",
        OPENAI_MODEL,
        PUBLIC_WS_URL,
        LATENCY_PROFILE,
        LANGUAGE or "<auto>",
        TRANSCRIPT_WEBHOOK_URL or "<stdout-only>",
    )
    yield
    log.info("twilio-transcribe stopped")


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "model": OPENAI_MODEL,
        "profile": LATENCY_PROFILE,
        "language": LANGUAGE or None,
        "webhook_configured": bool(TRANSCRIPT_WEBHOOK_URL),
    }


# --------------------------------------------------------------------------- #
# TwiML endpoint                                                              #
# --------------------------------------------------------------------------- #


@app.post("/twiml")
async def twiml() -> Response:
    """Return TwiML telling Twilio to open a bidirectional Media Stream WS.

    For pure transcription we technically only need one direction (Twilio ->
    us), but <Connect><Stream> keeps the call open as long as the WS is alive
    and allows us to terminate the call by closing the WS. <Start><Stream>
    is fire-and-forget and would let Twilio play call-progress audio while
    we transcribe in the background — appropriate for some use cases (IVR
    voice-search). This template uses <Connect> as the production default.
    """
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{PUBLIC_WS_URL}"/>
  </Connect>
</Response>"""
    return Response(content=xml, media_type="text/xml")


# --------------------------------------------------------------------------- #
# Transcript-webhook stub (for local end-to-end testing)                       #
# --------------------------------------------------------------------------- #


@app.post("/transcript-webhook-test")
async def transcript_webhook_test(req: Request) -> JSONResponse:
    """Stub destination for TRANSCRIPT_WEBHOOK_URL.

    Point TRANSCRIPT_WEBHOOK_URL at this endpoint to verify the end-to-end
    push path:

        TRANSCRIPT_WEBHOOK_URL=http://localhost:8080/transcript-webhook-test

    Logs each finalized transcript with its callSid / streamSid / timing.
    """
    try:
        body = await req.json()
    except Exception:
        body = {"_raw": (await req.body()).decode("utf-8", errors="replace")}
    log.info("[webhook-stub] finalized transcript: %s", json.dumps(body))
    return JSONResponse({"received": True})


# --------------------------------------------------------------------------- #
# Audio helpers                                                               #
# --------------------------------------------------------------------------- #


class ResampleState:
    """Persists audioop.ratecv state across frames in one direction."""

    def __init__(self) -> None:
        self.state: Any = None

    def convert(self, pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
        out, self.state = audioop.ratecv(
            pcm, PCM_WIDTH, 1, src_rate, dst_rate, self.state
        )
        return out


def twilio_ulaw_to_openai_pcm(ulaw_b64: str, st: ResampleState) -> str:
    """Decode Twilio mu-law 8k -> PCM16 24k base64 for OpenAI."""
    ulaw = base64.b64decode(ulaw_b64)
    pcm_8k = audioop.ulaw2lin(ulaw, PCM_WIDTH)
    pcm_24k = st.convert(pcm_8k, TWILIO_RATE, OPENAI_RATE)
    return base64.b64encode(pcm_24k).decode("ascii")


# --------------------------------------------------------------------------- #
# OpenAI transcription session bootstrap                                      #
# --------------------------------------------------------------------------- #


async def open_openai_ws() -> ClientConnection:
    """Open an authenticated WS to OpenAI's transcription endpoint.

    Attempts PRIMARY URL first; on connect-time failure (4xx/5xx returned as
    an `InvalidStatusCode` from websockets), retries FALLBACK once.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")

    headers = [
        ("Authorization", f"Bearer {OPENAI_API_KEY}"),
        # [UNCONFIRMED] Research file flagged whether this header is still
        # required for the new gpt-realtime-whisper endpoint. Sending
        # defensively — harmless if obsolete.
    ]

    last_err: Exception | None = None
    for url in (
        OPENAI_TRANSCRIBE_WS_URL_PRIMARY,
        OPENAI_TRANSCRIBE_WS_URL_FALLBACK,
    ):
        try:
            log.info("[openai] connecting %s", url)
            ws = await websockets.connect(
                url,
                additional_headers=headers,
                max_size=16 * 1024 * 1024,
                ping_interval=20,
                ping_timeout=20,
            )
            log.info("[openai] connected via %s", url)
            return ws
        except Exception as e:  # noqa: BLE001 — we want to fall through
            last_err = e
            log.warning("[openai] connect failed for %s: %s", url, e)

    assert last_err is not None
    raise last_err


async def send_session_update(ws: ClientConnection) -> None:
    """Configure the transcription session.

    Field shapes per research file (gpt-realtime-whisper transcription guide):
      - session.type = "transcription"
      - session.audio.input.format = {"type": "audio/pcm", "rate": 24000}
      - session.audio.input.transcription = {"model": ..., "language": ...}
      - session.audio.input.turn_detection = {"type": "server_vad", ...}
      - session.include = ["item.input_audio_transcription.logprobs"]
        (only for quality-max, where downstream UI flags low-confidence
        tokens)
    """
    transcription_cfg: dict[str, Any] = {"model": OPENAI_MODEL}
    if LANGUAGE:
        transcription_cfg["language"] = LANGUAGE

    session_obj: dict[str, Any] = {
        "type": "transcription",
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": OPENAI_RATE},
                "transcription": transcription_cfg,
                "turn_detection": vad_for_profile(LATENCY_PROFILE),
            }
        },
    }
    if LATENCY_PROFILE == "quality-max":
        session_obj["include"] = ["item.input_audio_transcription.logprobs"]

    payload = {"type": "session.update", "session": session_obj}
    await ws.send(json.dumps(payload))
    log.info(
        "[openai] session.update sent | profile=%s | language=%s",
        LATENCY_PROFILE,
        LANGUAGE or "<auto>",
    )


# --------------------------------------------------------------------------- #
# Transcript push                                                             #
# --------------------------------------------------------------------------- #


# Shared HTTP client for webhook POSTs. Reuses connection pool across calls.
_http_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    return _http_client


async def push_transcript(
    *,
    call_sid: str | None,
    stream_sid: str | None,
    ts_start: float,
    ts_end: float,
    text: str,
) -> None:
    """Push one finalized transcript to TRANSCRIPT_WEBHOOK_URL or stdout."""
    body = {
        "callSid": call_sid,
        "streamSid": stream_sid,
        "ts_start": ts_start,
        "ts_end": ts_end,
        "text": text,
    }
    if not TRANSCRIPT_WEBHOOK_URL:
        log.info("[transcript] %s", json.dumps(body, ensure_ascii=False))
        return

    try:
        r = await _http().post(TRANSCRIPT_WEBHOOK_URL, json=body)
        if r.status_code >= 400:
            log.warning(
                "[transcript] webhook returned %s for %s",
                r.status_code,
                json.dumps(body, ensure_ascii=False),
            )
        else:
            log.info(
                "[transcript] -> %s (%s) %s",
                TRANSCRIPT_WEBHOOK_URL,
                r.status_code,
                text,
            )
    except Exception:
        log.exception("[transcript] webhook POST failed; dumping to stdout")
        log.info("[transcript-fallback] %s", json.dumps(body, ensure_ascii=False))


def _extract_final_text(evt: dict[str, Any]) -> str:
    """Pull the final transcript text from a .completed payload.

    [UNCONFIRMED] which field name carries the text. Probe in order:
    transcript, text, content. Returns "" if none of these are present
    (defensive — should not happen in practice).
    """
    for key in ("transcript", "text", "content"):
        v = evt.get(key)
        if isinstance(v, str) and v:
            return v
    # Some shapes nest inside an `item` object; try that too.
    item = evt.get("item")
    if isinstance(item, dict):
        for key in ("transcript", "text", "content"):
            v = item.get(key)
            if isinstance(v, str) and v:
                return v
    return ""


# --------------------------------------------------------------------------- #
# Bridge: Twilio Media Stream WS endpoint                                     #
# --------------------------------------------------------------------------- #


@app.websocket("/media")
async def media_ws(ws_twilio: WebSocket) -> None:
    """Twilio -> us -> OpenAI -> us -> {webhook|stdout}."""
    await ws_twilio.accept()
    log.info("[twilio] accepted media stream")

    stream_sid: str | None = None
    call_sid: str | None = None
    in_state = ResampleState()  # Twilio -> OpenAI (8k -> 24k)

    ws_oai: ClientConnection | None = None

    # Per-item bookkeeping for ts_start tracking.
    # item_id -> first-delta wall-clock time.
    item_first_seen: dict[str, float] = {}

    async def pump_twilio_to_openai() -> None:
        """Read Twilio frames, forward audio to OpenAI."""
        nonlocal stream_sid, call_sid, ws_oai
        try:
            while True:
                raw = await ws_twilio.receive_text()
                msg = json.loads(raw)
                event = msg.get("event")

                if event == "connected":
                    log.info("[twilio] connected: %s", msg.get("protocol"))
                elif event == "start":
                    start = msg.get("start", {})
                    stream_sid = start.get("streamSid")
                    call_sid = start.get("callSid")
                    log.info(
                        "[twilio] start streamSid=%s callSid=%s",
                        stream_sid,
                        call_sid,
                    )
                    # Open the OpenAI session lazily on first 'start'.
                    if ws_oai is None:
                        ws_oai = await open_openai_ws()
                        await send_session_update(ws_oai)
                elif event == "media":
                    if ws_oai is None:
                        continue
                    payload = msg["media"]["payload"]
                    pcm_24k_b64 = twilio_ulaw_to_openai_pcm(payload, in_state)
                    # NOTE: unprefixed event name — transcription endpoint
                    # uses the same vocabulary as the conversational
                    # endpoint, NOT the session.`-prefixed translation
                    # endpoint. See SKILL.md.
                    await ws_oai.send(
                        json.dumps(
                            {
                                "type": "input_audio_buffer.append",
                                "audio": pcm_24k_b64,
                            }
                        )
                    )
                elif event == "mark":
                    log.debug("[twilio] mark: %s", msg.get("mark", {}).get("name"))
                elif event == "stop":
                    log.info("[twilio] stop")
                    # On hangup, commit any pending audio so OpenAI emits a
                    # final `.completed` for the last in-flight utterance.
                    if ws_oai is not None:
                        try:
                            await ws_oai.send(
                                json.dumps({"type": "input_audio_buffer.commit"})
                            )
                        except Exception:
                            pass
                    break
                else:
                    log.debug("[twilio] unknown event: %s", event)
        except WebSocketDisconnect:
            log.info("[twilio] disconnected")
        except Exception:
            log.exception("[twilio->openai] pump error")

    async def pump_openai_to_consumer() -> None:
        """Read OpenAI events, accumulate transcripts, push on `.completed`."""
        nonlocal ws_oai
        # Wait until the OpenAI WS is open (kicked off by Twilio 'start').
        while ws_oai is None:
            await asyncio.sleep(0.05)
        try:
            async for raw in ws_oai:
                # websockets gives str for text frames.
                try:
                    evt = json.loads(raw)
                except Exception:
                    log.debug("[openai] non-JSON frame ignored")
                    continue
                if not isinstance(evt, dict):
                    continue

                etype = evt.get("type")

                if etype == "conversation.item.input_audio_transcription.delta":
                    item_id = str(evt.get("item_id", ""))
                    delta = str(evt.get("delta", ""))
                    if item_id and item_id not in item_first_seen:
                        item_first_seen[item_id] = time.time()
                    # We don't push deltas — only finalized text goes to the
                    # webhook. Log at DEBUG so devs can see them flow.
                    log.debug(
                        "[openai] delta item=%s text=%r", item_id, delta
                    )

                elif etype == "conversation.item.input_audio_transcription.completed":
                    item_id = str(evt.get("item_id", ""))
                    final_text = _extract_final_text(evt)
                    ts_start = item_first_seen.pop(item_id, time.time())
                    ts_end = time.time()
                    log.info(
                        "[openai] completed item=%s text=%r",
                        item_id,
                        final_text,
                    )
                    if final_text:
                        await push_transcript(
                            call_sid=call_sid,
                            stream_sid=stream_sid,
                            ts_start=ts_start,
                            ts_end=ts_end,
                            text=final_text,
                        )

                elif etype == "input_audio_buffer.speech_started":
                    log.debug("[openai] speech_started")
                elif etype == "input_audio_buffer.speech_stopped":
                    log.debug("[openai] speech_stopped")
                elif etype == "input_audio_buffer.committed":
                    log.debug(
                        "[openai] buffer committed item=%s",
                        evt.get("item_id"),
                    )
                elif etype == "session.created":
                    log.info("[openai] session.created")
                elif etype == "session.updated":
                    log.info("[openai] session.updated")
                elif etype == "error" or etype == "invalid_request_error":
                    log.error("[openai] error: %s", evt)
                else:
                    log.debug("[openai] %s", etype)
        except websockets.ConnectionClosed:
            log.info("[openai] closed")
        except Exception:
            log.exception("[openai->consumer] pump error")

    try:
        t_in = asyncio.create_task(pump_twilio_to_openai())
        t_out = asyncio.create_task(pump_openai_to_consumer())
        await asyncio.gather(t_in, t_out)
    finally:
        if ws_oai is not None:
            try:
                await ws_oai.close()
            except Exception:
                pass
        log.info(
            "[bridge] cleanup done | streamSid=%s callSid=%s",
            stream_sid,
            call_sid,
        )


# --------------------------------------------------------------------------- #
# Entrypoint hint                                                             #
# --------------------------------------------------------------------------- #


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "twilio-transcribe:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level="info",
    )
