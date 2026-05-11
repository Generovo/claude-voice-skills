"""
whatsapp-voice-notes.py — WhatsApp Cloud API voice notes -> streaming Whisper
                          transcription -> WhatsApp text reply.

Purpose
-------
A FastAPI service that:

  1. Receives WhatsApp voice notes via Meta's webhook (POST /webhook).
  2. Downloads the OGG/OPUS media via the Graph API.
  3. Transcodes to PCM16 mono 24 kHz with ffmpeg.
  4. Opens an OpenAI Realtime *transcription* session (gpt-realtime-whisper)
     and streams the audio in. Streaming matters here because some voice
     notes are 1-3 minutes long — we want the transcript ready as soon as
     the audio finishes, not after a single blocking REST call.
  5. Collects `conversation.item.input_audio_transcription.delta` chunks +
     `.completed` events, assembles the final transcript.
  6. Sends a WhatsApp **text** reply containing the transcript.

Why use this instead of the simpler `whisper-1` REST endpoint?
  - The same skill SKILL.md applies (low latency, server-side VAD, streamed
    deltas).
  - You get the first words of the transcript ~0.4-1.5s after the user stops
    speaking (depending on LATENCY_PROFILE), so the bot can reply almost
    immediately for short notes. Even if we wait for `.completed` before
    sending the WhatsApp reply (as this template does — text messages aren't
    progressive), the streaming session is faster overall than a REST call
    on long audio because audio upload and transcription are interleaved.
  - The same session config is reused across `twilio-transcribe.py`,
    `meeting-minutes.py`, and live-captions front-ends, which keeps mental
    model load low.

Sister files in this skill:
  - examples/twilio-transcribe.py   — same session bootstrap, phone-call shape.
  - examples/meeting-minutes.py     — same shape, Recall.ai meeting bot.
  - examples/live-captions.tsx      — browser side, same delta vocabulary.

Dependencies
------------
    pip install fastapi uvicorn websockets httpx python-multipart

External: ffmpeg in PATH (OGG/OPUS -> PCM16 24 kHz mono).

Run
---
    uvicorn examples.whatsapp-voice-notes:app --host 0.0.0.0 --port 8080
    ngrok http 8080
    # In Meta Developer Console, set Webhook URL to https://<tunnel>/webhook
    # and subscribe to the "messages" field on the WhatsApp Business Account.

Environment
-----------
    OPENAI_API_KEY                Bearer for OpenAI Realtime (required).
    OPENAI_MODEL                  Model id; default "gpt-realtime-whisper".
    WHATSAPP_ACCESS_TOKEN         Graph API bearer (required).
    WHATSAPP_PHONE_NUMBER_ID      Numeric phone-number id (required).
    WHATSAPP_VERIFY_TOKEN         Token Meta sends back for handshake (required).
    WHATSAPP_APP_SECRET           For HMAC SHA256 webhook signature (required).
    LATENCY_PROFILE               "ultra-low" | "balanced" | "quality-max".
                                  Default "balanced".
    LANGUAGE                      Optional ISO-639-1 hint, e.g. "fr", "en", "ar".

Sources (verified 2026-05-11)
-----------------------------
- OpenAI Realtime transcription guide:
  https://developers.openai.com/api/docs/guides/realtime-transcription
- OpenAI gpt-realtime-whisper model card:
  https://developers.openai.com/api/docs/models/gpt-realtime-whisper
- Project snapshot:
  /Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md
- WhatsApp Cloud API webhooks/messages/media:
  https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks
  https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media
  https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages

[UNCONFIRMED] items propagated from research
--------------------------------------------
- Exact WS URL for the transcription endpoint. Two candidates (PRIMARY +
  FALLBACK below), matching twilio-transcribe.py.
- Whether `OpenAI-Beta: realtime=v1` is still required (sent defensively).
- Whether `input_audio_buffer.speech_started` / `.speech_stopped` /
  `.committed` are emitted on the transcription endpoint. Likely yes;
  handled if present, ignored if absent.
- Exact `.completed` final-text field name: probing `transcript`, `text`,
  `content` in order.
- IMPORTANT vocabulary detail: this endpoint uses the **unprefixed**
  `input_audio_buffer.append`, NOT `session.input_audio_buffer.append`
  which is unique to the /v1/realtime/translations endpoint. See SKILL.md.
"""

from __future__ import annotations

import asyncio
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
log = logging.getLogger("whatsapp-voice-notes")


# --------------------------------------------------------------------------- #
# Env / config                                                                #
# --------------------------------------------------------------------------- #


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


OPENAI_MODEL: str = os.environ.get("OPENAI_MODEL", "gpt-realtime-whisper")
LATENCY_PROFILE: str = os.environ.get("LATENCY_PROFILE", "balanced")
LANGUAGE: str = os.environ.get("LANGUAGE", "")

# [UNCONFIRMED] which of these two URL shapes is the live transcription WS
# endpoint. We try PRIMARY first; if it fails on connect, fall back. See
# twilio-transcribe.py for the same dual-URL pattern.
OPENAI_TRANSCRIBE_WS_URL_PRIMARY: str = (
    f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL}"
)
OPENAI_TRANSCRIBE_WS_URL_FALLBACK: str = (
    f"wss://api.openai.com/v1/realtime/transcription_sessions?model={OPENAI_MODEL}"
)

# Meta Graph API version (current as of 2026-05-11).
GRAPH_API_VERSION = "v22.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Audio rates. WhatsApp voice notes are OGG/OPUS (usually 16 or 48 kHz).
# OpenAI transcription takes PCM16 mono @ 24 kHz on the safe path.
OPENAI_RATE = 24000
PCM_WIDTH = 2

# Chunk audio at ~200 ms when sending to OpenAI — gives the server room to
# start producing deltas before the whole voice note is uploaded.
INPUT_CHUNK_MS_DEFAULT = 200

# Hard upper bound on time we wait for `.completed` before assembling
# whatever deltas we've collected and replying. Voice notes longer than
# ~5 minutes are uncommon; bump if you need to.
COMPLETED_TIMEOUT_S = 180.0


# --------------------------------------------------------------------------- #
# Latency profile -> server_vad fields                                        #
# (Mirrors twilio-transcribe.py so the two examples are interchangeable.)    #
# --------------------------------------------------------------------------- #


def vad_for_profile(profile: str) -> dict[str, Any]:
    """Map LATENCY_PROFILE to server_vad config.

    [UNCONFIRMED] whether the transcription endpoint accepts the same VAD
    field names as the conversational endpoint — likely yes per research.
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


def chunk_bytes_for_profile(profile: str) -> int:
    """Per-profile send-chunk size in bytes.

    Smaller chunks -> faster first delta but more WS overhead.
    Bigger chunks  -> fewer round-trips, slightly slower first delta.
    """
    if profile == "ultra-low":
        ms = 80
    elif profile == "quality-max":
        ms = 500
    else:
        ms = INPUT_CHUNK_MS_DEFAULT
    return int(OPENAI_RATE * PCM_WIDTH * ms / 1000)


# --------------------------------------------------------------------------- #
# FastAPI app                                                                 #
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-redef]
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
        log.warning(
            "Missing env vars (service will fail on use): %s", ", ".join(missing)
        )
    log.info(
        "whatsapp-voice-notes started | model=%s | profile=%s | language=%s",
        OPENAI_MODEL,
        LATENCY_PROFILE,
        LANGUAGE or "<auto>",
    )
    yield
    log.info("whatsapp-voice-notes stopped")


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "model": OPENAI_MODEL,
        "profile": LATENCY_PROFILE,
        "language": LANGUAGE or None,
    }


# --------------------------------------------------------------------------- #
# Webhook verification (GET) + event ingest (POST)                            #
# --------------------------------------------------------------------------- #


@app.get("/webhook")
async def verify_webhook(request: Request) -> Response:
    """Meta verification handshake.

    Meta sends:
        GET /webhook?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=<challenge>
    We echo `hub.challenge` if the token matches our configured value.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge", "")
    expected = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
    if (
        mode == "subscribe"
        and token
        and expected
        and hmac.compare_digest(token, expected)
    ):
        log.info("[whatsapp] webhook verification ok")
        return PlainTextResponse(challenge)
    log.warning("[whatsapp] webhook verification failed (mode=%s)", mode)
    raise HTTPException(status_code=403, detail="verification failed")


def _verify_signature(body: bytes, header_value: str | None) -> bool:
    """Verify Meta's x-hub-signature-256: sha256=<hex>."""
    if not header_value or not header_value.startswith("sha256="):
        return False
    secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    provided = header_value.split("=", 1)[1].strip()
    return hmac.compare_digest(expected, provided)


@app.post("/webhook")
async def receive_webhook(request: Request) -> Response:
    """Receive WhatsApp Cloud API webhook events.

    HMAC-verifies, then dispatches by message type. Returns 200 immediately
    after queuing the work — Meta enforces a ~20s SLA, and a real voice note
    transcription can take longer than that (audio upload + streaming).
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

    # Meta's nested shape:
    #   { entry: [ { changes: [ { value: { messages: [ ... ] } } ] } ] }
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
            log.info(
                "[whatsapp] ignoring message type=%s from=%s", mtype, sender
            )

    return Response(status_code=200)


# --------------------------------------------------------------------------- #
# Voice note flow                                                             #
# --------------------------------------------------------------------------- #


async def _handle_voice_note(media_id: str, recipient: str) -> None:
    """Download -> ffmpeg -> OpenAI stream -> WhatsApp text reply."""
    t0 = time.monotonic()
    log.info("[whatsapp] voice note media_id=%s from=%s", media_id, recipient)
    try:
        async with httpx.AsyncClient(timeout=60.0) as http:
            ogg_bytes = await _download_media(http, media_id)
            log.info("[whatsapp] downloaded %d bytes ogg/opus", len(ogg_bytes))

            pcm24k = await _ogg_to_pcm16_24k(ogg_bytes)
            log.info(
                "[whatsapp] transcoded to pcm16@24k, %d bytes", len(pcm24k)
            )

            transcript = await _stream_transcribe(pcm24k)
            log.info(
                "[openai] final transcript len=%d chars | elapsed=%.2fs",
                len(transcript),
                time.monotonic() - t0,
            )

            if not transcript.strip():
                reply = (
                    "Sorry — I couldn't make out any speech in your voice "
                    "note. Could you try again, ideally somewhere quieter?"
                )
            else:
                reply = f"Transcript:\n\n{transcript}"

            await _send_text_message(http, recipient, reply)
            log.info(
                "[whatsapp] reply delivered to=%s elapsed=%.2fs",
                recipient,
                time.monotonic() - t0,
            )
    except Exception:
        log.exception(
            "[whatsapp] voice note handling failed media_id=%s", media_id
        )


async def _handle_text(recipient: str) -> None:
    """Polite message explaining the service when text arrives instead of audio."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            await _send_text_message(
                http,
                recipient,
                (
                    "Hello! Send me a voice note and I'll reply with the "
                    "transcribed text."
                ),
            )
            log.info("[whatsapp] sent text redirect to=%s", recipient)
    except Exception:
        log.exception("[whatsapp] text reply failed to=%s", recipient)


# --------------------------------------------------------------------------- #
# Graph API helpers                                                           #
# --------------------------------------------------------------------------- #


def _wa_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_require_env('WHATSAPP_ACCESS_TOKEN')}"}


async def _download_media(http: httpx.AsyncClient, media_id: str) -> bytes:
    """Two-step Graph media download: meta lookup -> CDN GET."""
    meta = await http.get(
        f"{GRAPH_API_BASE}/{media_id}", headers=_wa_headers()
    )
    meta.raise_for_status()
    media_url = meta.json().get("url")
    if not media_url:
        raise RuntimeError(f"media lookup missing url: {meta.text}")
    blob = await http.get(media_url, headers=_wa_headers())
    blob.raise_for_status()
    return blob.content


async def _send_text_message(
    http: httpx.AsyncClient, recipient: str, text: str
) -> None:
    """POST /<phone_number_id>/messages with a text body."""
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
        content=json.dumps(body, ensure_ascii=False),
    )
    resp.raise_for_status()


# --------------------------------------------------------------------------- #
# ffmpeg transcoder                                                           #
# --------------------------------------------------------------------------- #


async def _run_ffmpeg(args: list[str], stdin: bytes) -> bytes:
    """Spawn ffmpeg via asyncio's safe subprocess API, pipe stdin -> stdout."""
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
            f"ffmpeg failed (rc={proc.returncode}): "
            f"{err.decode('utf-8', 'replace')[:500]}"
        )
    return out


async def _ogg_to_pcm16_24k(ogg_bytes: bytes) -> bytes:
    """OGG/OPUS (any rate) -> PCM16 mono 24 kHz raw."""
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


# --------------------------------------------------------------------------- #
# OpenAI Realtime transcription session                                       #
# --------------------------------------------------------------------------- #


async def _open_openai_ws() -> ClientConnection:
    """Connect to OpenAI's transcription endpoint, retrying with FALLBACK URL."""
    headers = [
        ("Authorization", f"Bearer {_require_env('OPENAI_API_KEY')}"),
        # [UNCONFIRMED] header still required? Sent defensively.
        ("OpenAI-Beta", "realtime=v1"),
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


async def _send_session_update(ws: ClientConnection) -> None:
    """Configure the transcription session.

    Field shapes per the research file:
      - session.type = "transcription"
      - session.audio.input.format = {"type": "audio/pcm", "rate": 24000}
      - session.audio.input.transcription = {"model": ..., "language"?: ...}
      - session.audio.input.turn_detection = {"type": "server_vad", ...}
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
        session_obj["include"] = [
            "item.input_audio_transcription.logprobs"
        ]

    await ws.send(
        json.dumps({"type": "session.update", "session": session_obj})
    )
    log.info(
        "[openai] session.update sent | profile=%s | language=%s",
        LATENCY_PROFILE,
        LANGUAGE or "<auto>",
    )


def _extract_final_text(evt: dict[str, Any]) -> str:
    """Pull finalized text from a .completed event.

    [UNCONFIRMED] which field name carries the text. Probe in order:
    transcript, text, content (matches twilio-transcribe.py).
    """
    for key in ("transcript", "text", "content"):
        v = evt.get(key)
        if isinstance(v, str) and v:
            return v
    item = evt.get("item")
    if isinstance(item, dict):
        for key in ("transcript", "text", "content"):
            v = item.get(key)
            if isinstance(v, str) and v:
                return v
    return ""


async def _stream_transcribe(pcm: bytes) -> str:
    """Open a transcription session, push PCM, return assembled transcript.

    The streaming gain on long voice notes comes from interleaving audio
    upload with server-side transcription — by the time `commit` is sent,
    the server is already partway through emitting deltas. We collect ALL
    deltas; when we see a matching `.completed`, we prefer its `transcript`
    field (final, locked text) over the deltas.

    Returns the empty string if no transcript came back. The caller is
    responsible for choosing a fallback reply.
    """
    ws = await _open_openai_ws()
    try:
        await _send_session_update(ws)

        chunk_bytes = chunk_bytes_for_profile(LATENCY_PROFILE)

        # Per-item state. The transcription endpoint can emit multiple
        # utterance items if the user paused — we concat them in order.
        # item_id -> running delta text
        delta_by_item: dict[str, list[str]] = {}
        # item_id -> finalized transcript (from .completed)
        final_by_item: dict[str, str] = {}
        # ordered list of item ids as we see them
        item_order: list[str] = []
        # marker for "we got at least one .completed"
        got_any_completed = asyncio.Event()

        async def send_audio() -> None:
            """Slice and send the PCM buffer, then commit."""
            total = 0
            for i in range(0, len(pcm), chunk_bytes):
                piece = pcm[i : i + chunk_bytes]
                if not piece:
                    break
                await ws.send(
                    json.dumps(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(piece).decode("ascii"),
                        }
                    )
                )
                total += len(piece)
            log.info(
                "[openai] streamed %d bytes of input audio in chunks", total
            )
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
            log.info("[openai] input_audio_buffer.commit sent")

        async def read_events() -> None:
            """Drain the server stream, populate delta/final dicts.

            We stop reading when either:
              - The server has emitted a `.completed` AND we've signaled
                that we sent commit (handled by the gather() that runs
                send_audio first).
              - The server emits an error.
              - The WS closes.
            """
            try:
                async for raw in ws:
                    try:
                        evt = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(evt, dict):
                        continue
                    etype = str(evt.get("type", ""))

                    if etype.endswith(".transcription.delta") or (
                        etype.endswith(".delta") and "transcript" in etype
                    ):
                        item_id = str(evt.get("item_id", ""))
                        if not item_id:
                            continue
                        delta = evt.get("delta") or evt.get("text") or ""
                        if not isinstance(delta, str) or not delta:
                            continue
                        if item_id not in delta_by_item:
                            delta_by_item[item_id] = []
                            item_order.append(item_id)
                        delta_by_item[item_id].append(delta)
                        log.debug(
                            "[openai] delta item=%s len=%d", item_id, len(delta)
                        )
                    elif etype.endswith(".completed") and (
                        "transcription" in etype or "transcript" in etype
                    ):
                        item_id = str(evt.get("item_id", ""))
                        final_text = _extract_final_text(evt)
                        if item_id and final_text:
                            final_by_item[item_id] = final_text
                            if item_id not in item_order:
                                item_order.append(item_id)
                        log.info(
                            "[openai] completed item=%s len=%d",
                            item_id,
                            len(final_text),
                        )
                        got_any_completed.set()
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
                    elif etype in ("error", "invalid_request_error") or (
                        etype.endswith(".error")
                    ):
                        log.error("[openai] error: %s", evt)
                        # Signal the waiter so we don't hang.
                        got_any_completed.set()
                        return
                    else:
                        log.debug("[openai] %s", etype)
            except websockets.ConnectionClosed:
                log.info("[openai] closed")
                got_any_completed.set()
            except Exception:
                log.exception("[openai] read loop error")
                got_any_completed.set()

        # Kick off the sender + reader concurrently. The reader keeps draining
        # past send_audio's completion until we've seen at least one
        # `.completed` event (or hit the timeout).
        reader_task = asyncio.create_task(read_events())
        await send_audio()

        try:
            await asyncio.wait_for(
                got_any_completed.wait(), timeout=COMPLETED_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            log.warning(
                "[openai] timeout after %.0fs without .completed; "
                "using accumulated deltas",
                COMPLETED_TIMEOUT_S,
            )

        # Give the reader a brief grace period to flush any trailing events
        # that arrived after the first .completed (e.g. additional items
        # from a multi-utterance voice note).
        try:
            await asyncio.wait_for(asyncio.shield(reader_task), timeout=2.0)
        except asyncio.TimeoutError:
            reader_task.cancel()
        except Exception:
            pass

        # Stitch transcript by item order: prefer finalized text over deltas.
        out_parts: list[str] = []
        for item_id in item_order:
            if item_id in final_by_item:
                out_parts.append(final_by_item[item_id])
            elif item_id in delta_by_item:
                out_parts.append("".join(delta_by_item[item_id]))
        return " ".join(part.strip() for part in out_parts if part.strip())
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
        "whatsapp-voice-notes:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level="info",
    )
