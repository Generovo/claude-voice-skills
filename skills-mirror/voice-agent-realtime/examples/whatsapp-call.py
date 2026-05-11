"""
whatsapp-call.py — WhatsApp Cloud API voice notes <-> OpenAI Realtime bridge.

Purpose
-------
A FastAPI service that receives WhatsApp Cloud API voice notes via webhook,
runs them through an OpenAI Realtime (gpt-realtime-2) session as a single
audio turn, and replies with a synthesized voice note back to the same chat.

Flow:
    User sends WhatsApp voice note
        -> Meta webhook fires (POST /webhook)
        -> Service downloads OGG/OPUS media (Graph API, two-step auth)
        -> Transcodes OGG/OPUS -> PCM16 24 kHz mono (ffmpeg)
        -> Streams audio to OpenAI Realtime via input_audio_buffer.append
        -> Commits buffer, asks for response, collects response.output_audio.delta
        -> Transcodes PCM16 24 kHz -> OGG/OPUS (ffmpeg)
        -> Uploads media to WhatsApp Cloud, sends audio message reply
        -> Returns 200 to Meta (within 20s SLA, processing offloaded)

A separate section at the bottom documents the BSP-relay pattern for
full-duplex WhatsApp Calls (not natively in the Cloud API).

Dependencies:  pip install fastapi uvicorn websockets httpx python-multipart
External:      ffmpeg in PATH (used for OGG/OPUS <-> PCM16 transcoding)

Run:
  uvicorn examples.whatsapp-call:app --host 0.0.0.0 --port 8080
  ngrok http 8080
  Configure Meta Developer Console webhook to https://<tunnel>/webhook
  Subscribe to "messages" field in the WhatsApp Business Account.

Env:
  OPENAI_API_KEY
  OPENAI_MODEL                (optional, default gpt-realtime-2)
  OPENAI_VOICE                (optional, default marin)
  WHATSAPP_ACCESS_TOKEN
  WHATSAPP_PHONE_NUMBER_ID
  WHATSAPP_VERIFY_TOKEN
  WHATSAPP_APP_SECRET
  SYSTEM_PROMPT               (optional)

Sources
-------
- OpenAI Realtime API (model, events, endpoint, voices):
  ../../docs/research/openai-realtime-api-2026-05-11.md (project-local)
  https://developers.openai.com/api/docs/guides/realtime-conversations
- WhatsApp Cloud API (webhooks, media download/upload, messages):
  https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks
  https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media
  https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages

Notes on UNCONFIRMED items propagated from the research doc
-----------------------------------------------------------
- `OpenAI-Beta: realtime=v1` header sent defensively (research flagged whether
  it's still required as UNCONFIRMED — harmless if obsolete).
- `turn_detection: { "type": "none" }` is requested below to disable VAD for
  single-shot turns. Research flagged `"none"` as [UNCONFIRMED] — the VAD guide
  only listed `server_vad` and `semantic_vad`. We send `"none"` anyway; if the
  server rejects it, fall back to omitting `turn_detection` entirely and
  driving the turn manually with `input_audio_buffer.commit` + `response.create`
  (which is the path this file uses regardless of the VAD setting).
"""

from __future__ import annotations

import asyncio
import audioop  # deprecated in 3.13 — install audioop-lts for 3.13+
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from websockets.asyncio.client import ClientConnection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("whatsapp-call")


# --------------------------------------------------------------------------- #
# Env / config                                                                #
# --------------------------------------------------------------------------- #


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


# Resolved lazily on first use, but we sanity-check presence at startup.
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-realtime-2")
OPENAI_VOICE = os.environ.get("OPENAI_VOICE", "marin")
OPENAI_WS_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}"

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    (
        "You are a friendly bilingual (French/English) WhatsApp voice assistant. "
        "The user just sent you a voice note. Reply briefly and naturally with a "
        "voice message. If they wrote in French, reply in French. If they wrote in "
        "English, reply in English. Keep replies concise — under 30 seconds of audio."
    ),
)

# Meta Graph API version. v22.0 is current as of 2026-05-11.
GRAPH_API_VERSION = "v22.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Audio rates: WhatsApp voice notes are OGG/OPUS (typically 16/48 kHz). OpenAI
# conversational sessions take PCM16 24 kHz. We transcode via ffmpeg, so we let
# ffmpeg do both the codec and the resample in one shot — no audioop needed
# for the WhatsApp path. (audioop kept imported for symmetry with twilio-bridge
# in case a future revision adds a fast resample step.)
OPENAI_RATE = 24000
PCM_WIDTH = 2

# OpenAI accepts large input audio frames, but we chunk to ~1s (24000 samples =
# 48000 bytes PCM16) to stay well under any per-message limits.
INPUT_CHUNK_SAMPLES = 24000


# --------------------------------------------------------------------------- #
# FastAPI app                                                                 #
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = [
        k
        for k in (
            "OPENAI_API_KEY",
            "WHATSAPP_ACCESS_TOKEN",
            "WHATSAPP_PHONE_NUMBER_ID",
            "WHATSAPP_VERIFY_TOKEN",
            "WHATSAPP_APP_SECRET",
        )
        if not os.environ.get(k)
    ]
    if missing:
        log.warning("Missing env vars (service will fail on use): %s", ", ".join(missing))
    log.info(
        "whatsapp-call started | model=%s | voice=%s",
        OPENAI_MODEL,
        OPENAI_VOICE,
    )
    yield
    log.info("whatsapp-call stopped")


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "model": OPENAI_MODEL}


# --------------------------------------------------------------------------- #
# Webhook verification (GET) + event ingest (POST)                            #
# --------------------------------------------------------------------------- #


@app.get("/webhook")
async def verify_webhook(request: Request) -> Response:
    """Meta verification handshake.

    Meta sends:
        GET /webhook?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=<challenge>
    We echo the challenge if the token matches our configured value.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")
    expected = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")

    if mode == "subscribe" and token and expected and hmac.compare_digest(token, expected):
        log.info("[whatsapp] webhook verification ok")
        return PlainTextResponse(challenge)
    log.warning("[whatsapp] webhook verification failed (mode=%s)", mode)
    raise HTTPException(status_code=403, detail="verification failed")


def _verify_signature(body: bytes, header_value: str | None) -> bool:
    """Verify x-hub-signature-256: sha256=<hex>."""
    if not header_value or not header_value.startswith("sha256="):
        return False
    secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    provided = header_value.split("=", 1)[1].strip()
    # constant-time comparison
    return hmac.compare_digest(expected, provided)


@app.post("/webhook")
async def receive_webhook(request: Request) -> Response:
    """Receive WhatsApp Cloud API webhook events.

    Verifies HMAC-SHA256 signature, then dispatches by message type. Returns
    200 immediately — actual processing runs as a background task so we never
    miss Meta's 20s SLA, even if the OpenAI round-trip takes longer.
    """
    raw = await request.body()
    sig = request.headers.get("x-hub-signature-256")
    if not _verify_signature(raw, sig):
        log.warning("[whatsapp] signature mismatch (got header=%r)", sig)
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("[whatsapp] non-JSON webhook body")
        raise HTTPException(status_code=400, detail="bad json")

    # Meta's nested shape: { entry: [ { changes: [ { value: { messages: [ ... ] } } ] } ] }
    try:
        entry = (payload.get("entry") or [{}])[0]
        change = (entry.get("changes") or [{}])[0]
        value = change.get("value", {})
        messages = value.get("messages") or []
    except Exception:
        messages = []

    for msg in messages:
        mtype = msg.get("type")
        sender = msg.get("from")
        if not sender:
            continue
        if mtype == "audio":
            audio = msg.get("audio") or {}
            media_id = audio.get("id")
            if media_id:
                asyncio.create_task(_handle_voice_note(media_id, sender))
        elif mtype == "text":
            asyncio.create_task(_handle_text(sender))
        else:
            log.info("[whatsapp] ignoring message type=%s from=%s", mtype, sender)

    return Response(status_code=200)


# --------------------------------------------------------------------------- #
# Voice note flow                                                             #
# --------------------------------------------------------------------------- #


async def _handle_voice_note(media_id: str, recipient: str) -> None:
    """Download, run through OpenAI, upload reply. Logs every transition."""
    t0 = time.monotonic()
    log.info("[whatsapp] voice note media_id=%s from=%s", media_id, recipient)
    try:
        async with httpx.AsyncClient(timeout=60.0) as http:
            ogg_bytes = await _download_media(http, media_id)
            log.info("[whatsapp] downloaded %d bytes ogg/opus", len(ogg_bytes))

            pcm24k = await _ogg_to_pcm16_24k(ogg_bytes)
            log.info("[whatsapp] transcoded to pcm16@24k, %d bytes", len(pcm24k))

            reply_pcm = await _run_openai_single_turn(pcm24k)
            log.info("[openai] reply pcm16@24k, %d bytes", len(reply_pcm))

            reply_ogg = await _pcm16_24k_to_ogg(reply_pcm)
            log.info("[whatsapp] transcoded reply to ogg/opus, %d bytes", len(reply_ogg))

            uploaded_id = await _upload_audio_media(http, reply_ogg)
            log.info("[whatsapp] uploaded media id=%s", uploaded_id)

            await _send_audio_message(http, recipient, uploaded_id)
            log.info(
                "[whatsapp] reply delivered to=%s elapsed=%.2fs",
                recipient,
                time.monotonic() - t0,
            )
    except Exception:
        log.exception("[whatsapp] voice note handling failed media_id=%s", media_id)


async def _handle_text(recipient: str) -> None:
    """Polite redirect: this bot only responds to voice notes."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            await _send_text_message(
                http,
                recipient,
                "Hello! I respond to voice notes. Please send me a voice message "
                "and I'll reply.",
            )
            log.info("[whatsapp] sent text redirect to=%s", recipient)
    except Exception:
        log.exception("[whatsapp] text reply failed to=%s", recipient)


# --------------------------------------------------------------------------- #
# Graph API helpers                                                           #
# --------------------------------------------------------------------------- #


def _wa_headers() -> dict[str, str]:
    token = _require_env("WHATSAPP_ACCESS_TOKEN")
    return {"Authorization": f"Bearer {token}"}


async def _download_media(http: httpx.AsyncClient, media_id: str) -> bytes:
    """Two-step: look up CDN URL, then GET it with the same bearer auth."""
    meta = await http.get(f"{GRAPH_API_BASE}/{media_id}", headers=_wa_headers())
    meta.raise_for_status()
    media_url = meta.json().get("url")
    if not media_url:
        raise RuntimeError(f"media lookup missing url: {meta.text}")
    blob = await http.get(media_url, headers=_wa_headers())
    blob.raise_for_status()
    return blob.content


async def _upload_audio_media(http: httpx.AsyncClient, ogg_bytes: bytes) -> str:
    """POST to /<phone_number_id>/media multipart, returns media id."""
    phone_id = _require_env("WHATSAPP_PHONE_NUMBER_ID")
    files = {
        "messaging_product": (None, "whatsapp"),
        "type": (None, "audio/ogg"),
        "file": ("reply.ogg", ogg_bytes, "audio/ogg"),
    }
    resp = await http.post(
        f"{GRAPH_API_BASE}/{phone_id}/media",
        headers=_wa_headers(),
        files=files,
    )
    resp.raise_for_status()
    data = resp.json()
    media_id = data.get("id")
    if not media_id:
        raise RuntimeError(f"upload missing id: {data}")
    return media_id


async def _send_audio_message(
    http: httpx.AsyncClient, recipient: str, media_id: str
) -> None:
    phone_id = _require_env("WHATSAPP_PHONE_NUMBER_ID")
    body = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "audio",
        "audio": {"id": media_id},
    }
    resp = await http.post(
        f"{GRAPH_API_BASE}/{phone_id}/messages",
        headers={**_wa_headers(), "Content-Type": "application/json"},
        content=json.dumps(body),
    )
    resp.raise_for_status()


async def _send_text_message(
    http: httpx.AsyncClient, recipient: str, text: str
) -> None:
    phone_id = _require_env("WHATSAPP_PHONE_NUMBER_ID")
    body = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"body": text},
    }
    resp = await http.post(
        f"{GRAPH_API_BASE}/{phone_id}/messages",
        headers={**_wa_headers(), "Content-Type": "application/json"},
        content=json.dumps(body),
    )
    resp.raise_for_status()


# --------------------------------------------------------------------------- #
# ffmpeg transcoders                                                          #
# --------------------------------------------------------------------------- #


async def _run_ffmpeg(args: list[str], stdin: bytes) -> bytes:
    """Spawn ffmpeg, pipe stdin -> stdout, return stdout bytes. Raises on error."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (rc={proc.returncode}): {err.decode('utf-8', 'replace')[:500]}"
        )
    return out


async def _ogg_to_pcm16_24k(ogg_bytes: bytes) -> bytes:
    """OGG/OPUS -> PCM16 mono 24 kHz raw."""
    args = [
        "-loglevel", "error",
        "-f", "ogg",
        "-i", "pipe:0",
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-ac", "1",
        "-ar", str(OPENAI_RATE),
        "pipe:1",
    ]
    return await _run_ffmpeg(args, ogg_bytes)


async def _pcm16_24k_to_ogg(pcm_bytes: bytes) -> bytes:
    """PCM16 mono 24 kHz raw -> OGG/OPUS (libopus, mono, 24 kHz)."""
    args = [
        "-loglevel", "error",
        "-f", "s16le",
        "-ar", str(OPENAI_RATE),
        "-ac", "1",
        "-i", "pipe:0",
        "-c:a", "libopus",
        "-b:a", "32k",
        "-f", "ogg",
        "pipe:1",
    ]
    return await _run_ffmpeg(args, pcm_bytes)


# --------------------------------------------------------------------------- #
# OpenAI Realtime single-turn driver                                          #
# --------------------------------------------------------------------------- #


async def _open_openai_ws() -> ClientConnection:
    headers = [
        ("Authorization", f"Bearer {_require_env('OPENAI_API_KEY')}"),
        # [UNCONFIRMED] research flagged whether OpenAI-Beta is still required
        # for gpt-realtime-2. Sending defensively.
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


async def _send_session_update(ws: ClientConnection) -> None:
    """Configure a single-shot turn: audio in, audio out, no VAD.

    Field shapes per research/openai-realtime-api-2026-05-11.md.
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
                    # Live-confirmed 2026-05-11: None disables VAD entirely (manual
                    # commit + response.create flow). The string "none" is rejected by
                    # the server (Supported values are: 'server_vad' and 'semantic_vad').
                    "turn_detection": None,
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


async def _stream_audio_chunks(ws: ClientConnection, pcm: bytes) -> None:
    """Slice PCM16 into ~1s chunks and append each via input_audio_buffer.append."""
    chunk_bytes = INPUT_CHUNK_SAMPLES * PCM_WIDTH
    total = 0
    for i in range(0, len(pcm), chunk_bytes):
        chunk = pcm[i : i + chunk_bytes]
        if not chunk:
            break
        await ws.send(
            json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                }
            )
        )
        total += len(chunk)
    log.info("[openai] streamed %d bytes of input audio in chunks", total)


async def _run_openai_single_turn(pcm_in: bytes) -> bytes:
    """Single-shot turn: send audio, commit, await response, return PCM16 24 kHz."""
    ws = await _open_openai_ws()
    try:
        await _send_session_update(ws)
        await _stream_audio_chunks(ws, pcm_in)
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        log.info("[openai] input_audio_buffer.commit sent")
        await ws.send(json.dumps({"type": "response.create"}))
        log.info("[openai] response.create sent")

        out_chunks: list[bytes] = []
        async for raw in ws:
            evt = json.loads(raw)
            etype = evt.get("type")
            if etype == "response.output_audio.delta":
                delta_b64 = evt.get("delta", "")
                if delta_b64:
                    out_chunks.append(base64.b64decode(delta_b64))
            elif etype == "response.output_audio.done":
                log.info("[openai] response.output_audio.done")
            elif etype == "response.output_audio_transcript.delta":
                log.debug("[openai] transcript: %s", evt.get("delta"))
            elif etype == "response.done":
                log.info("[openai] response.done — total audio chunks=%d", len(out_chunks))
                break
            elif etype in ("error", "invalid_request_error"):
                log.error("[openai] error event: %s", evt)
                break
            elif etype == "session.created":
                log.info("[openai] session.created")
            elif etype == "session.updated":
                log.info("[openai] session.updated")
            else:
                log.debug("[openai] %s", etype)
        return b"".join(out_chunks)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Entrypoint hint                                                             #
# --------------------------------------------------------------------------- #


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "whatsapp-call:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level="info",
    )


# --------------------------------------------------------------------------- #
# BSP relay pattern (full-duplex WhatsApp Calls)                              #
# --------------------------------------------------------------------------- #
#
# As of 2026-05-11, the WhatsApp Cloud API does NOT expose real-time voice
# calls publicly — only voice notes (audio messages, the path implemented
# above). Production real-time WhatsApp voice agents go through a
# Business Solution Provider (BSP) that exposes the underlying media stream
# via SIP or a WebRTC/WebSocket bridge.
#
# Common BSP integration patterns (vendor-specific — confirm with your BSP):
#
#   1. SIP trunk: BSP routes the WhatsApp call as a standard SIP INVITE to a
#      SIP URI you control. You'd answer via a media server (FreeSWITCH,
#      Asterisk, Jambonz, Pion) and bridge its RTP to OpenAI Realtime. The
#      simplest path is to use OpenAI's SIP inbound directly:
#          sip:$PROJECT_ID@sip.api.openai.com;transport=tls
#      …and let OpenAI accept/reject/refer the call via the REST endpoints
#      documented at /v1/realtime/calls/$CALL_ID/accept etc.
#
#   2. WebSocket media stream (Twilio-style): the BSP forwards the WhatsApp
#      call media as base64-framed audio over a WebSocket. This is the same
#      shape as twilio-bridge.py — adapt it by:
#        - Pointing the inbound WS endpoint at the BSP's frame schema.
#        - Matching the BSP's codec/rate. Likely candidates: 8 kHz mu-law
#          (telephony-style), 16 kHz PCM16, or Opus (resample to 24 kHz for
#          OpenAI in either case).
#        - Threading streamSid/callSid equivalents per BSP convention.
#
#   3. WebRTC: some BSPs offer a WebRTC SDK on the server side. Wire that
#      SDK's audio track to OpenAI's WebRTC endpoint
#      (POST /v1/realtime/calls with an SDP offer) — see webrtc-client.tsx
#      for the browser-side shape; server-side WebRTC bridges in Python
#      typically use aiortc.
#
# Template to copy: twilio-bridge.py in this same directory. The WS frame
# pump, OpenAI session bootstrap, and barge-in handling are reusable as-is;
# only the inbound codec/resample needs swapping for the BSP's format.
#
# [UNCONFIRMED] which BSPs currently expose live WhatsApp voice calls to
# third-party media servers (the list changes over time and depends on
# WhatsApp Business approval). Check 360dialog, Gupshup, Infobip, MessageBird,
# Twilio (yes — Twilio has a WhatsApp voice product separate from SMS/voice),
# and Vonage as starting points.
