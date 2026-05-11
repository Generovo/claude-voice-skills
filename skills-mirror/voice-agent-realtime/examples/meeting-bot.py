"""
meeting-bot.py — Recall.ai meeting bot <-> OpenAI Realtime bridge.

Pattern using Recall.ai as the meeting-bot transport.

Why Recall.ai: native Zoom/Meet/Teams SDKs require platform-specific desktop
apps, OAuth flows, and per-vendor implementations. Recall.ai abstracts these
into a single API and unified audio stream.

This file demonstrates the BRIDGING pattern. You must create the bot through
Recall's API (see /create-bot below) and configure Recall to stream audio to
this service's `/recall-audio` WebSocket.

See https://docs.recall.ai/ for production setup details.

Flow:
    POST /create-bot { meeting_url, bot_name?, instructions? }
        -> calls Recall API, which dispatches a bot into the meeting
        -> Recall opens a WebSocket to this service's PUBLIC_WS_URL
            (configured in the bot creation payload)
        -> /recall-audio receives JSON metadata + PCM16 16 kHz audio frames
        -> service bridges audio to OpenAI Realtime (gpt-realtime-2)
        -> captures rolling transcript via response.output_audio_transcript.delta
        -> optionally sends synthesized speech back into the meeting via
           Recall's output audio channel (TODO — see send_audio_to_recall_meeting)
        -> writes transcript-<bot_id>-<timestamp>.txt on WS close

Dependencies:  pip install fastapi uvicorn websockets httpx
Python 3.12+. For 3.13+: pip install audioop-lts and swap the import below.

Run:
  uvicorn examples.meeting-bot:app --host 0.0.0.0 --port 8080
  ngrok http 8080

  # Then create a bot:
  curl -X POST http://localhost:8080/create-bot \\
    -H "Content-Type: application/json" \\
    -d '{"meeting_url": "https://zoom.us/j/...", "bot_name": "AI Notes"}'

Env:
  OPENAI_API_KEY
  OPENAI_MODEL                (optional, default gpt-realtime-2)
  OPENAI_VOICE                (optional, default marin)
  RECALL_API_KEY
  PUBLIC_WS_URL               (e.g., wss://<tunnel>/recall-audio — written into the create-bot call)
  SYSTEM_PROMPT               (optional, default in code)

Sources
-------
- OpenAI Realtime API (model, events, endpoint, voices):
  ../../docs/research/openai-realtime-api-2026-05-11.md (project-local)
  https://developers.openai.com/api/docs/guides/realtime-conversations
- Recall.ai bot API and real-time media:
  https://docs.recall.ai/ (top-level)
  (exact endpoint paths and field names confirmed at integration time —
  TODO comments below mark fields flagged [UNCONFIRMED] in research)

Notes on UNCONFIRMED items propagated from the research doc
-----------------------------------------------------------
- `OpenAI-Beta: realtime=v1` header sent defensively.
- Recall.ai's exact JSON field names for the bot creation payload and the
  inbound WS frame schema are NOT in the OpenAI research doc — those need
  cross-referencing with Recall's docs. TODO comments flag the spots.
"""

from __future__ import annotations

import asyncio
import audioop  # deprecated in 3.13 — install audioop-lts for 3.13+
import base64
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
import websockets
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from websockets.asyncio.client import ClientConnection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("meeting-bot")


# --------------------------------------------------------------------------- #
# Env / config                                                                #
# --------------------------------------------------------------------------- #


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-realtime-2")
OPENAI_VOICE = os.environ.get("OPENAI_VOICE", "marin")
OPENAI_WS_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}"

PUBLIC_WS_URL = os.environ.get("PUBLIC_WS_URL", "wss://example.invalid/recall-audio")

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    (
        "You are a meeting assistant. Listen quietly. Maintain a running transcript "
        "via your transcript output. Speak only when a participant says 'Assistant, "
        "...' or you are explicitly addressed. Be brief."
    ),
)

# Recall.ai's documented raw audio output for real-time streaming is PCM 16-bit
# mono at 16 kHz. OpenAI conversational sessions take PCM16 at 24 kHz. We
# resample on the way in and on the way out.
RECALL_RATE = 16000
OPENAI_RATE = 24000
PCM_WIDTH = 2

RECALL_API_BASE = "https://api.recall.ai"


# --------------------------------------------------------------------------- #
# FastAPI app                                                                 #
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = [
        k
        for k in ("OPENAI_API_KEY", "RECALL_API_KEY")
        if not os.environ.get(k)
    ]
    if missing:
        log.warning("Missing env vars (service will fail on use): %s", ", ".join(missing))
    log.info(
        "meeting-bot started | model=%s | voice=%s | public_ws=%s",
        OPENAI_MODEL,
        OPENAI_VOICE,
        PUBLIC_WS_URL,
    )
    yield
    log.info("meeting-bot stopped")


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "model": OPENAI_MODEL}


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


def recall_pcm_to_openai_pcm(pcm_16k: bytes, st: ResampleState) -> str:
    """PCM16 16k -> PCM16 24k base64 for OpenAI."""
    pcm_24k = st.convert(pcm_16k, RECALL_RATE, OPENAI_RATE)
    return base64.b64encode(pcm_24k).decode("ascii")


def openai_pcm_to_recall_pcm(pcm_24k_b64: str, st: ResampleState) -> bytes:
    """OpenAI PCM16 24k base64 -> PCM16 16k raw bytes for Recall."""
    pcm_24k = base64.b64decode(pcm_24k_b64)
    return st.convert(pcm_24k, OPENAI_RATE, RECALL_RATE)


# --------------------------------------------------------------------------- #
# Create-bot endpoint (convenience helper around Recall API)                  #
# --------------------------------------------------------------------------- #


class CreateBotRequest(BaseModel):
    meeting_url: str
    bot_name: str | None = "AI Notes"
    instructions: str | None = None


@app.post("/create-bot")
async def create_bot(req: CreateBotRequest) -> dict[str, Any]:
    """Dispatch a Recall bot into a meeting, streaming audio to PUBLIC_WS_URL.

    TODO: confirm with vendor docs (https://docs.recall.ai/) the exact field
    names. As of the research doc cutoff, the precise JSON shape for
    real_time_media / websocket_audio_destination_url was [UNCONFIRMED].
    The body shape below is the most commonly documented variant; adjust
    once you have a successful first run.
    """
    api_key = _require_env("RECALL_API_KEY")
    body: dict[str, Any] = {
        "meeting_url": req.meeting_url,
        "bot_name": req.bot_name or "AI Notes",
        # TODO: confirm exact Recall field names. Recent Recall docs have used
        # both "real_time_media" and "real_time_transcription" objects with
        # different sub-fields. The shape below targets raw-audio streaming.
        "real_time_media": {
            "websocket_audio_destination_url": PUBLIC_WS_URL,
        },
    }
    headers = {
        "Authorization": f"Token {api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.post(
            f"{RECALL_API_BASE}/api/v1/bot/",
            headers=headers,
            content=json.dumps(body),
        )
        if resp.status_code >= 400:
            log.error("[recall] create-bot failed %s: %s", resp.status_code, resp.text)
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        data = resp.json()
        log.info("[recall] bot created id=%s", data.get("id"))
        # Stash the per-meeting instructions override on the bot row if present,
        # so the WS handler can pick it up via the bot_id correlated through
        # the inbound metadata frame. (Recall echoes bot_id on the stream.)
        if req.instructions:
            _instructions_cache[str(data.get("id"))] = req.instructions
        return data


# In-memory cache: bot_id -> instructions override (set at /create-bot time,
# read by the WS handler when the metadata frame arrives). Replace with Redis
# or similar for multi-replica deployments.
_instructions_cache: dict[str, str] = {}


# --------------------------------------------------------------------------- #
# Recall webhook (bot lifecycle)                                              #
# --------------------------------------------------------------------------- #


@app.websocket("/recall-events")
async def recall_events(ws: WebSocket) -> None:
    """Optional Recall webhook receiver for bot lifecycle events.

    TODO: confirm with vendor docs whether Recall delivers lifecycle events
    via HTTP webhook or WebSocket. If HTTP, replace this endpoint with a
    POST handler. For now, accept both shapes.
    """
    await ws.accept()
    log.info("[recall] events WS connected")
    try:
        while True:
            raw = await ws.receive_text()
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("[recall] non-JSON event frame")
                continue
            log.info("[recall] event %s", evt.get("event") or evt.get("type") or evt)
    except WebSocketDisconnect:
        log.info("[recall] events WS disconnected")


# --------------------------------------------------------------------------- #
# OpenAI Realtime session bootstrap                                           #
# --------------------------------------------------------------------------- #


async def _open_openai_ws() -> ClientConnection:
    headers = [
        ("Authorization", f"Bearer {_require_env('OPENAI_API_KEY')}"),
        # [UNCONFIRMED] research flagged whether OpenAI-Beta is still required
        # for gpt-realtime-2. Sending defensively.
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


async def _send_session_update(ws: ClientConnection, instructions: str) -> None:
    """Configure the Realtime session in mostly-listen mode.

    Field shapes per research/openai-realtime-api-2026-05-11.md.
    """
    session = {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": OPENAI_MODEL,
            "output_modalities": ["audio"],
            "instructions": instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": OPENAI_RATE},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                        # Mostly-listen: let OpenAI detect speech but don't
                        # auto-respond. Speaking is gated on the system prompt
                        # ("Assistant, ...") and we let the model decide.
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
# Recall audio bridge (Recall <-> OpenAI)                                     #
# --------------------------------------------------------------------------- #


async def send_audio_to_recall_meeting(
    ws_recall: WebSocket, bot_id: str | None, pcm_16k: bytes
) -> None:
    """Send synthesized speech back into the meeting via Recall's output channel.

    TODO: confirm with vendor docs — Recall's API for sending audio FROM your
    bot INTO the meeting is [UNCONFIRMED] in the OpenAI research doc and has
    historically required a separate "output media" channel or REST endpoint
    (not just writing to the same WS). Common shapes Recall has used:

        - A separate WebSocket the bot opens on bot.real_time_media.audio_input
        - A REST endpoint: POST /api/v1/bot/<bot_id>/output_audio/
        - A WebRTC track if you opted into Recall's WebRTC output

    Until confirmed, this function logs what would be sent. Wire up to the
    correct Recall endpoint once you've checked your Recall plan's docs.
    """
    log.info(
        "[recall] would send %d bytes pcm16@16k to meeting (bot_id=%s) — wiring TODO",
        len(pcm_16k),
        bot_id,
    )
    # TODO: confirm with vendor docs and replace the line above with the
    # actual delivery (REST POST, separate WS, or WebRTC track).


@app.websocket("/recall-audio")
async def recall_audio(ws_recall: WebSocket) -> None:
    """Recall -> us -> OpenAI -> us -> Recall (optional output)."""
    await ws_recall.accept()
    log.info("[recall] audio WS accepted")

    bot_id: str | None = None
    instructions = SYSTEM_PROMPT
    in_state = ResampleState()   # Recall 16k -> OpenAI 24k
    out_state = ResampleState()  # OpenAI 24k -> Recall 16k

    ws_oai: ClientConnection | None = None
    transcript_buffer: list[str] = []
    finalized_utterances: list[tuple[str, str]] = []  # (iso_ts, text)
    current_utterance: list[str] = []

    async def pump_recall_to_openai() -> None:
        """Read Recall frames, forward audio + metadata to OpenAI.

        Recall's WS protocol [UNCONFIRMED]: based on common patterns, frames
        are either JSON text (metadata, lifecycle) or binary (raw PCM audio).
        Some Recall configurations wrap binary inside base64-encoded JSON. We
        handle both shapes defensively.
        """
        nonlocal bot_id, instructions, ws_oai
        try:
            while True:
                msg = await ws_recall.receive()
                # Starlette WebSocket.receive() returns {"type":"websocket.receive","text"/"bytes":...}
                if msg.get("type") == "websocket.disconnect":
                    break

                pcm_chunk: bytes | None = None

                text = msg.get("text")
                if text is not None:
                    # JSON frame — metadata or base64-wrapped audio.
                    try:
                        evt = json.loads(text)
                    except json.JSONDecodeError:
                        log.warning("[recall] non-JSON text frame ignored")
                        continue
                    # TODO: confirm with vendor docs the exact shape of
                    # Recall's metadata frames. Common fields observed:
                    # event/type, bot_id, data, audio (base64), participant.
                    etype = evt.get("event") or evt.get("type")
                    if etype:
                        log.info("[recall] frame type=%s", etype)
                    if evt.get("bot_id") and bot_id is None:
                        bot_id = str(evt["bot_id"])
                        override = _instructions_cache.pop(bot_id, None)
                        if override:
                            instructions = override
                        log.info("[recall] bound bot_id=%s", bot_id)
                    if "audio" in evt and isinstance(evt["audio"], str):
                        try:
                            pcm_chunk = base64.b64decode(evt["audio"])
                        except Exception:
                            log.warning("[recall] base64 audio decode failed")

                binary = msg.get("bytes")
                if binary is not None:
                    pcm_chunk = binary

                if pcm_chunk is None:
                    continue

                # Lazy-open OpenAI on first audio frame.
                if ws_oai is None:
                    ws_oai = await _open_openai_ws()
                    await _send_session_update(ws_oai, instructions)

                pcm_24k_b64 = recall_pcm_to_openai_pcm(pcm_chunk, in_state)
                await ws_oai.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": pcm_24k_b64,
                        }
                    )
                )
        except WebSocketDisconnect:
            log.info("[recall] audio WS disconnected")
        except Exception:
            log.exception("[recall->openai] pump error")

    async def pump_openai_to_recall() -> None:
        """Read OpenAI events, optionally forward audio back to Recall, build transcript."""
        nonlocal ws_oai
        while ws_oai is None:
            await asyncio.sleep(0.05)
        try:
            async for raw in ws_oai:
                evt = json.loads(raw)
                etype = evt.get("type")
                if etype == "response.output_audio.delta":
                    delta_b64 = evt.get("delta", "")
                    if not delta_b64:
                        continue
                    pcm_16k = openai_pcm_to_recall_pcm(delta_b64, out_state)
                    await send_audio_to_recall_meeting(ws_recall, bot_id, pcm_16k)
                elif etype == "response.output_audio.done":
                    log.info("[openai] response.output_audio.done")
                elif etype == "response.output_audio_transcript.delta":
                    delta = evt.get("delta", "")
                    if delta:
                        current_utterance.append(delta)
                        transcript_buffer.append(delta)
                elif etype == "response.output_audio_transcript.done":
                    text = "".join(current_utterance).strip()
                    if text:
                        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        finalized_utterances.append((ts, text))
                        log.info("[openai] utterance [%s] %s", ts, text)
                    current_utterance.clear()
                elif etype == "response.done":
                    log.debug("[openai] response.done")
                elif etype == "input_audio_buffer.speech_started":
                    log.debug("[openai] speech_started")
                elif etype == "input_audio_buffer.speech_stopped":
                    log.debug("[openai] speech_stopped")
                elif etype == "session.created":
                    log.info("[openai] session.created")
                elif etype == "session.updated":
                    log.info("[openai] session.updated")
                elif etype in ("error", "invalid_request_error"):
                    log.error("[openai] error event: %s", evt)
                else:
                    log.debug("[openai] %s", etype)
        except websockets.ConnectionClosed:
            log.info("[openai] closed")
        except Exception:
            log.exception("[openai->recall] pump error")

    try:
        t_in = asyncio.create_task(pump_recall_to_openai())
        t_out = asyncio.create_task(pump_openai_to_recall())
        await asyncio.gather(t_in, t_out)
    finally:
        if ws_oai is not None:
            try:
                await ws_oai.close()
            except Exception:
                pass

        # Persist the transcript on disconnect.
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            slug = bot_id or "unknown"
            path = f"transcript-{slug}-{stamp}.txt"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"# meeting-bot transcript bot_id={slug} written={stamp}\n\n")
                for ts, text in finalized_utterances:
                    fh.write(f"[{ts}] {text}\n")
                if not finalized_utterances and transcript_buffer:
                    fh.write("# raw deltas (no .done events received):\n")
                    fh.write("".join(transcript_buffer) + "\n")
            log.info("[meeting-bot] transcript written to %s", path)
        except Exception:
            log.exception("[meeting-bot] failed to write transcript")

        log.info("[bridge] cleanup done | bot_id=%s", bot_id)


# --------------------------------------------------------------------------- #
# Entrypoint hint                                                             #
# --------------------------------------------------------------------------- #


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "meeting-bot:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level="info",
    )
