"""
whatsapp-translate.py — WhatsApp Cloud API voice notes <-> OpenAI Realtime Translate bridge.

Purpose
-------
A FastAPI service that receives WhatsApp voice notes in one language,
translates them via OpenAI's gpt-realtime-translate model, and replies with a
voice note in a chosen target language. Per-conversation target language is
configurable by sending a short slash-command text (e.g. `/ar`, `/fr`, `/en`).

User-visible flow
-----------------
1. User sends `/fr` (or any supported ISO-639-1 code) in WhatsApp.
2. Bot replies "Got it — I'll translate your next voice notes to French."
3. User records a voice note in any of the 70+ source languages the model
   supports (Arabic, Spanish, Mandarin, etc.).
4. Bot replies with a translated voice note in French.

Default target language (when the user has never set one) is `DEFAULT_TARGET_LANG`
(default `en`), so a fresh user can start sending voice notes immediately and
receive English back.

Webhook flow (per voice note)
-----------------------------
    POST /webhook -> signature check -> dispatch by message type
        - text starting with "/"  -> language command, persist mapping, ack
        - text (other)            -> polite usage hint
        - audio                   -> background task:
            1. GET media URL via Graph API
            2. Download OGG/OPUS bytes
            3. ffmpeg: OGG/OPUS -> PCM16 mono 24 kHz
            4. Open OpenAI Realtime Translate WS to
               wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate
            5. session.update with audio.output.language = <target>
            6. Stream audio via session.input_audio_buffer.append  (NOTE PREFIX)
            7. Collect session.output_audio.delta chunks
            8. ffmpeg: PCM16 24 kHz -> OGG/OPUS
            9. Upload reply via Graph media endpoint
           10. Send audio message back to the user

CRITICAL — event vocabulary
---------------------------
The translation endpoint uses event names PREFIXED with `session.`, not the
bare names used by the conversational `gpt-realtime-2` endpoint. Mixing them
silently breaks: the wire stays open, no error, no translated audio comes back.
See ../SKILL.md ## Event vocabulary — CRITICAL DIFFERENCE.

This file uses exclusively the prefixed forms:
    session.input_audio_buffer.append    (client -> server)
    session.output_audio.delta           (server -> client, base64 PCM16 audio)
    session.output_transcript.delta      (server -> client, target-language text)
    session.input_transcript.delta       (server -> client, source-language text)

Dependencies
------------
    pip install fastapi uvicorn websockets httpx python-multipart
External:  ffmpeg in PATH (used for OGG/OPUS <-> PCM16 transcoding)

Run
---
    uvicorn examples.whatsapp-translate:app --host 0.0.0.0 --port 8080
    ngrok http 8080
    Configure Meta webhook to: https://<tunnel>/webhook
    Subscribe to the "messages" field in your WhatsApp Business Account.

Env
---
    OPENAI_API_KEY                  Bearer token for Realtime API (required).
    OPENAI_MODEL                    Defaults to `gpt-realtime-translate`.
    WHATSAPP_ACCESS_TOKEN           Graph API access token.
    WHATSAPP_PHONE_NUMBER_ID        Sender phone number id for messaging.
    WHATSAPP_VERIFY_TOKEN           Token for the GET /webhook handshake.
    WHATSAPP_APP_SECRET             Used to verify x-hub-signature-256.
    DEFAULT_TARGET_LANG             ISO-639-1 code, default `en`.

Sources (verified 2026-05-11)
-----------------------------
- /Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md
- https://developers.openai.com/api/docs/guides/realtime-translation
- https://developers.openai.com/api/docs/models/gpt-realtime-translate
- https://developers.facebook.com/docs/whatsapp/cloud-api/webhooks
- https://developers.facebook.com/docs/whatsapp/cloud-api/reference/media
- https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages

[UNCONFIRMED] items propagated from the research file
-----------------------------------------------------
- `audio.output.language` is the only documented session field for translation.
  Other session fields (input format, voice, turn_detection, etc.) are
  `[UNCONFIRMED]`. We include a conservative `audio.input.format` hint but the
  endpoint may silently ignore it.
- The translation endpoint's full server -> client event list is not published.
  We branch on the documented `session.output_audio.delta` and treat any
  `.done` / `.completed` / `error` envelope as a session-end signal.
- `OpenAI-Beta: realtime=v1` header — research flagged whether still required
  on the new endpoints. We omit it on the translation WS; add only if a live
  probe shows the endpoint rejects without it.
- Voice configuration is `[UNCONFIRMED]` for translation; we set only the
  output language.
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
log = logging.getLogger("whatsapp-translate")


# --------------------------------------------------------------------------- #
# Env / config                                                                #
# --------------------------------------------------------------------------- #


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-realtime-translate")
DEFAULT_TARGET_LANG = os.environ.get("DEFAULT_TARGET_LANG", "en").lower()

# WebSocket URL of the translation endpoint. Note `/translations` — distinct
# from the conversational `/v1/realtime` endpoint. Do NOT swap them.
OPENAI_WS_URL = (
    f"wss://api.openai.com/v1/realtime/translations?model={OPENAI_MODEL}"
)

# Meta Graph API version. v22.0 is current as of 2026-05-11.
GRAPH_API_VERSION = "v22.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# The translation endpoint operates at PCM16 24 kHz on both sides (only
# documented format). WhatsApp voice notes are OGG/OPUS — ffmpeg handles both
# the codec and resample in one shot.
OPENAI_RATE = 24000
PCM_WIDTH = 2

# Chunk audio into ~1s slices when streaming to OpenAI (24000 samples * 2
# bytes/sample). Keeps each WS frame comfortably under any per-message ceiling.
INPUT_CHUNK_SAMPLES = 24000

# Pretty names for the slash-command ack. Add codes as needed — the model
# itself supports 13 output languages per the OpenAI announcement; the full
# canonical list was `[UNCONFIRMED]` at the research cutoff, so this map is
# pragmatic, not exhaustive.
LANG_NAMES: dict[str, str] = {
    "en": "English",
    "fr": "French",
    "ar": "Arabic",
    "es": "Spanish",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "ja": "Japanese",
    "zh": "Chinese",
    "ko": "Korean",
    "hi": "Hindi",
    "ru": "Russian",
}


# --------------------------------------------------------------------------- #
# Per-conversation state — sender phone -> target language                    #
# --------------------------------------------------------------------------- #


# Simple in-memory map: sender E.164 -> ISO-639-1 target code. Replace with
# Redis (or a Postgres row) for multi-replica deployments — every replica MUST
# share this state or users will see their language preference drift between
# webhook deliveries hitting different pods.
conversation_target_lang: dict[str, str] = {}


def _get_target_for(sender: str) -> str:
    return conversation_target_lang.get(sender, DEFAULT_TARGET_LANG)


def _set_target_for(sender: str, lang: str) -> None:
    conversation_target_lang[sender] = lang.lower()


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
        log.warning(
            "Missing env vars (service will fail on use): %s", ", ".join(missing)
        )
    log.info(
        "whatsapp-translate started | model=%s | default_target=%s",
        OPENAI_MODEL,
        DEFAULT_TARGET_LANG,
    )
    yield
    log.info("whatsapp-translate stopped")


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {
        "status": "ok",
        "model": OPENAI_MODEL,
        "default_target": DEFAULT_TARGET_LANG,
    }


# --------------------------------------------------------------------------- #
# Webhook — verification (GET) and event ingest (POST)                        #
# --------------------------------------------------------------------------- #


@app.get("/webhook")
async def verify_webhook(request: Request) -> Response:
    """Meta verification handshake.

    Meta sends:
        GET /webhook?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=<challenge>
    We echo the challenge back when the token matches our configured value.
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
    """Verify x-hub-signature-256: sha256=<hex>.

    Constant-time comparison; rejects on missing/short/malformed headers.
    """
    if not header_value or not header_value.startswith("sha256="):
        return False
    secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    provided = header_value.split("=", 1)[1].strip()
    return hmac.compare_digest(expected, provided)


@app.post("/webhook")
async def receive_webhook(request: Request) -> Response:
    """Receive WhatsApp Cloud API webhook events.

    Verifies HMAC, dispatches by message type, and returns 200 immediately —
    actual translation runs as a background task to respect Meta's 20s SLA.
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

    # Meta nests events deep:
    #   entry[0].changes[0].value.messages[*]
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
            text_obj = msg.get("text") or {}
            body_text = text_obj.get("body", "").strip()
            asyncio.create_task(_handle_text(sender, body_text))
        else:
            log.info(
                "[whatsapp] ignoring message type=%s from=%s", mtype, sender
            )

    return Response(status_code=200)


# --------------------------------------------------------------------------- #
# Text message handlers                                                       #
# --------------------------------------------------------------------------- #


async def _handle_text(sender: str, body: str) -> None:
    """Either a `/lang` slash command or generic text — reply by text."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            if body.startswith("/"):
                await _handle_lang_command(http, sender, body)
            else:
                await _send_text_message(
                    http,
                    sender,
                    "Hi! Send me a voice note and I'll translate it to your "
                    "target language. Set the language by sending a slash "
                    "command like /fr, /en, /ar, /es, /de. "
                    f"Current target: {LANG_NAMES.get(_get_target_for(sender), _get_target_for(sender))}.",
                )
    except Exception:
        log.exception("[whatsapp] text reply failed to=%s", sender)


async def _handle_lang_command(
    http: httpx.AsyncClient, sender: str, body: str
) -> None:
    """Parse `/<code>` or `/lang <code>` and persist the target language.

    Examples:
        /fr           -> set target to French
        /lang fr      -> same
        /ar           -> set target to Arabic
        /xx           -> reply with the list of known shortcuts
    """
    token = body.lstrip("/").strip().lower()
    if token.startswith("lang"):
        # `/lang fr` shape — second word is the code.
        parts = token.split()
        token = parts[1] if len(parts) >= 2 else ""

    code = token.split()[0] if token else ""

    if code in LANG_NAMES:
        _set_target_for(sender, code)
        pretty = LANG_NAMES[code]
        await _send_text_message(
            http,
            sender,
            f"Got it — I'll translate your next voice notes to {pretty}.",
        )
        log.info("[whatsapp] %s target=%s", sender, code)
        return

    # Unrecognised — reply with the supported list.
    listing = ", ".join(f"/{c} ({n})" for c, n in LANG_NAMES.items())
    await _send_text_message(
        http,
        sender,
        f"I didn't recognise that language code. Try one of: {listing}.",
    )


# --------------------------------------------------------------------------- #
# Voice note handler                                                          #
# --------------------------------------------------------------------------- #


async def _handle_voice_note(media_id: str, sender: str) -> None:
    """Download -> transcode -> translate via OpenAI -> transcode -> upload -> send."""
    t0 = time.monotonic()
    target = _get_target_for(sender)
    log.info(
        "[whatsapp] voice note media_id=%s from=%s target=%s",
        media_id,
        sender,
        target,
    )
    try:
        async with httpx.AsyncClient(timeout=60.0) as http:
            ogg_bytes = await _download_media(http, media_id)
            log.info("[whatsapp] downloaded %d bytes ogg/opus", len(ogg_bytes))

            pcm24k = await _ogg_to_pcm16_24k(ogg_bytes)
            log.info(
                "[whatsapp] transcoded source to pcm16@24k, %d bytes",
                len(pcm24k),
            )

            reply_pcm = await _run_openai_translate(pcm24k, target)
            log.info(
                "[openai] translated audio pcm16@24k, %d bytes (target=%s)",
                len(reply_pcm),
                target,
            )

            if not reply_pcm:
                log.warning(
                    "[whatsapp] empty translation - sending text fallback to %s",
                    sender,
                )
                await _send_text_message(
                    http,
                    sender,
                    "Sorry - I couldn't translate that voice note. "
                    "Please try again with a clearer recording.",
                )
                return

            reply_ogg = await _pcm16_24k_to_ogg(reply_pcm)
            log.info(
                "[whatsapp] transcoded reply to ogg/opus, %d bytes",
                len(reply_ogg),
            )

            uploaded_id = await _upload_audio_media(http, reply_ogg)
            log.info("[whatsapp] uploaded media id=%s", uploaded_id)

            await _send_audio_message(http, sender, uploaded_id)
            log.info(
                "[whatsapp] reply delivered to=%s elapsed=%.2fs target=%s",
                sender,
                time.monotonic() - t0,
                target,
            )
    except Exception:
        log.exception(
            "[whatsapp] voice note handling failed media_id=%s", media_id
        )


# --------------------------------------------------------------------------- #
# Graph API helpers                                                           #
# --------------------------------------------------------------------------- #


def _wa_headers() -> dict[str, str]:
    token = _require_env("WHATSAPP_ACCESS_TOKEN")
    return {"Authorization": f"Bearer {token}"}


async def _download_media(http: httpx.AsyncClient, media_id: str) -> bytes:
    """Two-step: look up the CDN URL, then GET it with the same bearer auth."""
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


async def _upload_audio_media(
    http: httpx.AsyncClient, ogg_bytes: bytes
) -> str:
    """POST multipart to /<phone_number_id>/media, returns media id."""
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
    """Spawn ffmpeg, pipe stdin -> stdout, return stdout bytes. Raises on error.

    Uses asyncio.create_subprocess_exec (no shell) — arguments are passed as a
    list, never interpolated into a command string.
    """
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
# OpenAI Realtime Translate driver                                            #
# --------------------------------------------------------------------------- #


async def _open_openai_ws() -> ClientConnection:
    """Open an authenticated WebSocket to the Realtime Translations endpoint.

    [UNCONFIRMED] whether `OpenAI-Beta: realtime=v1` is still required on the
    new translate endpoint - not shown in the 2026-05 docs snippets. We omit
    it here; add if a live probe shows the endpoint rejects without it.
    """
    headers = [
        ("Authorization", f"Bearer {_require_env('OPENAI_API_KEY')}"),
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


async def _send_translation_session_update(
    ws: ClientConnection, target_lang: str
) -> None:
    """Configure a translation session for `target_lang` output.

    Per references/session-config.md the only DOCUMENTED field on this endpoint
    is `audio.output.language`. We additionally pass `audio.input.format` as a
    conservative hint - the endpoint may silently ignore unknown fields per the
    research notes.

    Notably we do NOT set `voice` ([UNCONFIRMED] for translation) nor
    `instructions` (translation is direct - no system prompt).
    """
    payload: dict[str, Any] = {
        "type": "session.update",
        "session": {
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": OPENAI_RATE},
                },
                "output": {
                    "language": target_lang,
                },
            },
        },
    }
    await ws.send(json.dumps(payload))
    log.info("[openai] session.update sent (target=%s)", target_lang)


async def _stream_audio_chunks(ws: ClientConnection, pcm: bytes) -> None:
    """Slice PCM16 into ~1s chunks and append each via session.input_audio_buffer.append.

    CRITICAL: the event name is PREFIXED with `session.` here.
        CONTRAST — Conversational endpoint (DO NOT USE HERE):
            {"type": "input_audio_buffer.append", "audio": "..."}  # CONTRAST
        Translation endpoint (used below):
            {"type": "session.input_audio_buffer.append", "audio": "..."}
    """
    chunk_bytes = INPUT_CHUNK_SAMPLES * PCM_WIDTH
    total = 0
    for i in range(0, len(pcm), chunk_bytes):
        chunk = pcm[i : i + chunk_bytes]
        if not chunk:
            break
        await ws.send(
            json.dumps(
                {
                    "type": "session.input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                }
            )
        )
        total += len(chunk)
    log.info("[openai] streamed %d bytes of input audio in chunks", total)


async def _run_openai_translate(pcm_in: bytes, target_lang: str) -> bytes:
    """Open a translate session, push the audio, return concatenated PCM16 24 kHz output.

    Translation is continuous-stream - there is no documented `response.create`
    on this endpoint. We rely on server VAD to finalise the utterance once we
    stop appending audio. The receive loop terminates on the first `.done` /
    `.completed` / `error` envelope.
    """
    ws = await _open_openai_ws()
    try:
        await _send_translation_session_update(ws, target_lang)
        await _stream_audio_chunks(ws, pcm_in)
        # We deliberately do NOT send `session.input_audio_buffer.commit` -
        # its support on this endpoint is [UNCONFIRMED] and the translation
        # guide does not include it in the client->server set.

        out_chunks: list[bytes] = []
        async for raw in ws:
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = evt.get("type", "")

            if etype == "session.output_audio.delta":
                delta_b64 = evt.get("delta", "")
                if delta_b64:
                    out_chunks.append(base64.b64decode(delta_b64))
            elif etype == "session.output_transcript.delta":
                # Target-language transcript for logging / future captions.
                log.debug(
                    "[openai] target transcript: %s", evt.get("delta")
                )
            elif etype == "session.input_transcript.delta":
                log.debug(
                    "[openai] source transcript: %s", evt.get("delta")
                )
            elif etype == "session.created":
                log.info("[openai] session.created")
            elif etype == "session.updated":
                log.info("[openai] session.updated")
            elif etype in ("error", "invalid_request_error"):
                # [UNCONFIRMED] whether these envelopes are actually emitted.
                log.error("[openai] error event: %s", evt)
                break
            elif etype.endswith(".done") or etype.endswith(".completed"):
                # [UNCONFIRMED] exact .done name(s) on the translation endpoint.
                # Treat first .done/.completed as end-of-utterance for this
                # single-shot voice note flow.
                log.info("[openai] terminal event: %s", etype)
                break
            else:
                log.debug("[openai] %s", etype)
        return b"".join(out_chunks)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Self-check guard for event-vocabulary regressions                           #
# --------------------------------------------------------------------------- #
#
# Every occurrence of `input_audio_buffer.append` in this file MUST be either
# (a) prefixed with `session.` on an actual ws.send call, or (b) appear inside
# a comment marked CONTRAST that explicitly contrasts with the conversational
# endpoint. Run:
#
#   grep -n 'input_audio_buffer.append' examples/whatsapp-translate.py \
#       | grep -v 'session.input_audio_buffer.append' \
#       | grep -v CONTRAST
#
# Should produce no lines.


# --------------------------------------------------------------------------- #
# Entrypoint hint                                                             #
# --------------------------------------------------------------------------- #


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "whatsapp-translate:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level="info",
    )
