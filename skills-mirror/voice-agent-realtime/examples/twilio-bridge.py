"""
twilio-bridge.py — Twilio Voice <-> OpenAI Realtime (gpt-realtime-2) bridge.

Purpose
-------
A FastAPI service that bridges a Twilio Voice phone call (over Twilio Media
Streams WebSocket) to an OpenAI Realtime conversational session. The caller
dials a Twilio number, Twilio opens a Media Stream WS to this service, this
service opens a WS to OpenAI Realtime, and audio flows in both directions with
sample-rate/codec conversion (8 kHz mu-law <-> 24 kHz PCM16).

Dependencies
------------
    pip install fastapi uvicorn websockets python-multipart httpx

Python 3.12+. NOTE: the stdlib `audioop` module is deprecated in 3.13 and
slated for removal. For 3.13+ install the drop-in replacement:

    pip install audioop-lts

...and replace `import audioop` with `import audioop_lts as audioop`.

Run
---
    uvicorn examples.twilio-bridge:app --host 0.0.0.0 --port 8080

Public exposure (ngrok / Cloudflare Tunnel):
    ngrok http 8080
Then point your Twilio Voice number webhook to:
    https://<tunnel>/twiml

Environment
-----------
    OPENAI_API_KEY      Bearer token for OpenAI Realtime API (required).
    OPENAI_MODEL        Model ID; defaults to "gpt-realtime-2".
    PUBLIC_WS_URL       Public WSS URL of THIS service's /media endpoint, e.g.
                        wss://<tunnel>/media — embedded into the TwiML <Stream>.
    SYSTEM_PROMPT       Optional override for the assistant system prompt.
    OPENAI_VOICE        Optional voice name; defaults to "marin".

Sources
-------
- OpenAI Realtime API (model, events, endpoint, voices):
  see ../../docs/research/openai-realtime-api-2026-05-11.md (project-local)
  and https://developers.openai.com/api/docs/guides/realtime-conversations
- Twilio Media Streams (TwiML <Stream>, frame schema "start"/"media"/"stop",
  base64 mu-law 8 kHz mono payload, streamSid round-trip):
  https://www.twilio.com/docs/voice/twiml/stream
  https://www.twilio.com/docs/voice/media-streams/websocket-messages

Notes on UNCONFIRMED items propagated from the research doc
-----------------------------------------------------------
- The OpenAI Realtime API exposes `audio/pcmu` (mu-law) as an input/output
  audio format on conversational sessions, but the docs only showed it
  alongside `audio/pcm` at 24 kHz; whether 8 kHz mu-law is accepted natively
  on the wire (which would let us skip manual resampling) is [UNCONFIRMED]
  in the research file. This bridge takes the SAFE path: PCM16 at 24 kHz to
  OpenAI in both directions, with stdlib audioop doing the 8k<->24k resample
  and mu-law<->PCM16 codec on the Twilio side. If you confirm native
  8 kHz mu-law support, you can drop the resample entirely.
- The `OpenAI-Beta: realtime=v1` header is sent defensively (research flagged
  whether it's still required as UNCONFIRMED — harmless if obsolete).
"""

from __future__ import annotations

import asyncio
import audioop  # deprecated in 3.13 — see header
import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from websockets.asyncio.client import ClientConnection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("twilio-bridge")


# --------------------------------------------------------------------------- #
# Env / config                                                                #
# --------------------------------------------------------------------------- #


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


OPENAI_API_KEY = _require_env("OPENAI_API_KEY") if os.environ.get("OPENAI_API_KEY") else ""
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-realtime-2")
PUBLIC_WS_URL = os.environ.get("PUBLIC_WS_URL", "wss://example.invalid/media")
OPENAI_VOICE = os.environ.get("OPENAI_VOICE", "marin")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    (
        "You are a friendly bilingual (French/English) voice assistant answering a phone call. "
        "Keep replies short, natural, and conversational. If the caller speaks French, reply in "
        "French. If they speak English, reply in English. Acknowledge briefly before doing any "
        "lookups."
    ),
)

OPENAI_WS_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}"

# Twilio Media Streams audio is base64-encoded mu-law at 8 kHz, mono.
# OpenAI Realtime conversational sessions, in the safe path, take PCM16 at 24 kHz.
TWILIO_RATE = 8000
OPENAI_RATE = 24000
PCM_WIDTH = 2  # bytes per sample for PCM16


# --------------------------------------------------------------------------- #
# FastAPI app                                                                 #
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.environ.get("OPENAI_API_KEY"):
        log.warning("OPENAI_API_KEY not set — bridge will fail when a call comes in.")
    log.info(
        "twilio-bridge started | model=%s | public_ws=%s | voice=%s",
        OPENAI_MODEL,
        PUBLIC_WS_URL,
        OPENAI_VOICE,
    )
    yield
    log.info("twilio-bridge stopped")


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "model": OPENAI_MODEL}


# --------------------------------------------------------------------------- #
# TwiML endpoint                                                              #
# --------------------------------------------------------------------------- #


@app.post("/twiml")
async def twiml() -> Response:
    """Return TwiML telling Twilio to open a bidirectional Media Stream WS."""
    # <Connect><Stream> is the bidirectional variant (vs <Start><Stream> which
    # is one-way to your endpoint). See Twilio docs:
    # https://www.twilio.com/docs/voice/twiml/stream
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{PUBLIC_WS_URL}"/>
  </Connect>
</Response>"""
    return Response(content=xml, media_type="text/xml")


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


def openai_pcm_to_twilio_ulaw(pcm_b64: str, st: ResampleState) -> str:
    """Decode OpenAI PCM16 24k -> mu-law 8k base64 for Twilio."""
    pcm_24k = base64.b64decode(pcm_b64)
    pcm_8k = st.convert(pcm_24k, OPENAI_RATE, TWILIO_RATE)
    ulaw = audioop.lin2ulaw(pcm_8k, PCM_WIDTH)
    return base64.b64encode(ulaw).decode("ascii")


# --------------------------------------------------------------------------- #
# OpenAI Realtime session bootstrap                                           #
# --------------------------------------------------------------------------- #


async def open_openai_ws() -> ClientConnection:
    """Open an authenticated WS to the OpenAI Realtime API."""
    headers = [
        ("Authorization", f"Bearer {os.environ['OPENAI_API_KEY']}"),
        # [UNCONFIRMED] Research file flagged whether this header is still
        # required for the new gpt-realtime-2 endpoint. Sending defensively.
        ("OpenAI-Beta", "realtime=v1"),
    ]
    log.info("[openai] connecting %s", OPENAI_WS_URL)
    ws = await websockets.connect(
        OPENAI_WS_URL,
        additional_headers=headers,
        max_size=16 * 1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
    )
    log.info("[openai] connected")
    return ws


async def send_session_update(ws: ClientConnection) -> None:
    """Configure the Realtime session for telephony.

    Field shapes per research/openai-realtime-api-2026-05-11.md:
      - session.type, session.model, session.output_modalities (list),
      - session.audio.{input,output}.format.{type,rate},
      - session.audio.output.voice,
      - session.audio.input.turn_detection,
      - session.instructions
    """
    session = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": OPENAI_MODEL,
            "output_modalities": ["audio"],
            "instructions": SYSTEM_PROMPT,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": OPENAI_RATE},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                        "create_response": True,
                        "interrupt_response": True,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": OPENAI_RATE},
                    "voice": OPENAI_VOICE,
                },
            },
        },
    }
    await ws.send(json.dumps(session))
    log.info("[openai] session.update sent")


# --------------------------------------------------------------------------- #
# Bridge: Twilio Media Stream WS endpoint                                     #
# --------------------------------------------------------------------------- #


@app.websocket("/media")
async def media_ws(ws_twilio: WebSocket) -> None:
    """Twilio -> us -> OpenAI -> us -> Twilio."""
    await ws_twilio.accept()
    log.info("[twilio] accepted media stream")

    stream_sid: str | None = None
    call_sid: str | None = None
    in_state = ResampleState()  # Twilio -> OpenAI (8k -> 24k)
    out_state = ResampleState()  # OpenAI -> Twilio (24k -> 8k)

    ws_oai: ClientConnection | None = None

    async def pump_twilio_to_openai() -> None:
        """Read Twilio frames, forward audio + lifecycle to OpenAI."""
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
                    break
                else:
                    log.debug("[twilio] unknown event: %s", event)
        except WebSocketDisconnect:
            log.info("[twilio] disconnected")
        except Exception:
            log.exception("[twilio->openai] pump error")

    async def pump_openai_to_twilio() -> None:
        """Read OpenAI events, forward audio + handle barge-in to Twilio."""
        nonlocal ws_oai
        # Wait until the OpenAI WS is open (kicked off by Twilio 'start').
        while ws_oai is None:
            await asyncio.sleep(0.05)
        try:
            async for raw in ws_oai:
                evt = json.loads(raw)
                etype = evt.get("type")
                if etype == "response.output_audio.delta":
                    if stream_sid is None:
                        continue
                    delta_b64 = evt.get("delta", "")
                    if not delta_b64:
                        continue
                    ulaw_b64 = openai_pcm_to_twilio_ulaw(delta_b64, out_state)
                    await ws_twilio.send_text(
                        json.dumps(
                            {
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": ulaw_b64},
                            }
                        )
                    )
                elif etype == "response.output_audio.done":
                    # Mark the end of one response for client-side bookkeeping.
                    if stream_sid is not None:
                        await ws_twilio.send_text(
                            json.dumps(
                                {
                                    "event": "mark",
                                    "streamSid": stream_sid,
                                    "mark": {"name": "response-end"},
                                }
                            )
                        )
                elif etype == "input_audio_buffer.speech_started":
                    # Barge-in: the user interrupted. Tell Twilio to flush any
                    # buffered playback so we don't talk over the user.
                    # https://www.twilio.com/docs/voice/media-streams/websocket-messages#clear-message
                    log.info("[openai] speech_started -> sending Twilio clear")
                    if stream_sid is not None:
                        await ws_twilio.send_text(
                            json.dumps(
                                {"event": "clear", "streamSid": stream_sid}
                            )
                        )
                elif etype == "response.output_audio_transcript.delta":
                    log.debug("[openai] transcript: %s", evt.get("delta"))
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
            log.exception("[openai->twilio] pump error")

    try:
        t_in = asyncio.create_task(pump_twilio_to_openai())
        t_out = asyncio.create_task(pump_openai_to_twilio())
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
        "twilio-bridge:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level="info",
    )
