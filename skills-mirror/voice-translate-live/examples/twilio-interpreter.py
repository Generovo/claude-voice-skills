"""
twilio-interpreter.py — Twilio Voice <-> OpenAI Realtime Translate bridge.

Purpose
-------
A FastAPI service that mediates a 3-way phone call between two callers who do
not share a language. Caller A speaks LANG_A (default "fr"); Caller B speaks
LANG_B (default "ar"). The bridge maintains TWO translation sessions against
OpenAI's gpt-realtime-translate model:

  Session 1 (a→b): caller A's audio → LANG_B audio for caller B.
  Session 2 (b→a): caller B's audio → LANG_A audio for caller A.

Each session is a WebSocket to:
    wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate

Per the 2026-05-11 research snapshot, the translation endpoint uses a DIFFERENT
event vocabulary from the conversational endpoint — audio buffer events are
PREFIXED with `session.` (e.g. `session.input_audio_buffer.append`). The
conversational endpoint uses unprefixed names. Mixing them silently breaks:
the connection stays open, no error is raised, and no translated audio ever
comes back. Every event name in this file is the prefixed translation form.
See ../SKILL.md ## Event vocabulary — CRITICAL DIFFERENCE.

Twilio setup
------------
The simplest topology is a Twilio Conference with two participants (A and B)
plus this bridge service attached via <Connect><Stream> on each leg, so the
bridge receives the audio of one leg per WebSocket connection. In production
you would track which `streamSid` corresponds to which caller via a CallSid →
leg mapping in your call-control layer. For this example we route by URL path
parameter: /media/a for caller A's leg, /media/b for caller B's leg.

A realistic deployment will also dial-out from leg A to leg B via Twilio's
REST API and join them in a <Conference>, but that orchestration is outside
the scope of this single-file example.

Dependencies
------------
    pip install fastapi uvicorn websockets python-multipart httpx

Python 3.12+. NOTE: the stdlib `audioop` module is deprecated in 3.13 and
slated for removal. For 3.13+ install the drop-in replacement:

    pip install audioop-lts

…and replace `import audioop` with `import audioop_lts as audioop`.

Run
---
    uvicorn examples.twilio-interpreter:app --host 0.0.0.0 --port 8080
    ngrok http 8080
    Configure Twilio number A's voice webhook to: https://<tunnel>/twiml-leg-a
    Configure Twilio number B's voice webhook to: https://<tunnel>/twiml-leg-b
    (Or use a single number that dials out to the second leg via Twilio's
     REST API and joins both legs in a <Conference>.)

Environment
-----------
    OPENAI_API_KEY       Bearer token for the Realtime Translations API (required).
    OPENAI_MODEL         Model ID; defaults to "gpt-realtime-translate".
    PUBLIC_WS_URL        Public WSS base URL of THIS service, e.g.
                         wss://<tunnel>  — used to build the <Stream url=…>.
    LANG_A               ISO-639-1 code, leg A's spoken language. Default "fr".
    LANG_B               ISO-639-1 code, leg B's spoken language. Default "ar".
    SYSTEM_PROMPT_NOTE   Optional. The translate endpoint does NOT use an
                         `instructions` field (translation is direct; see
                         references/session-config.md). This var exists for
                         compatibility with the voice-agent-realtime sibling
                         skill's env contract but is currently ignored.
                         [UNCONFIRMED] whether a future version of the translate
                         model accepts steering text — when the docs add the
                         field, propagate this var into the session.update body.

Sources (verified 2026-05-11)
-----------------------------
  - /Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md
    (section: gpt-realtime-translate; [CONTRADICTION] flag on session. prefix)
  - https://developers.openai.com/api/docs/guides/realtime-translation
  - https://developers.openai.com/api/docs/models/gpt-realtime-translate
  - Twilio Media Streams frame schema:
    https://www.twilio.com/docs/voice/twiml/stream
    https://www.twilio.com/docs/voice/media-streams/websocket-messages

Notes on [UNCONFIRMED] items propagated from the research file
--------------------------------------------------------------
  - The translation endpoint may emit `session.created`, `session.updated`,
    `error`, and `*.done` / `*.completed` companion events alongside the
    documented `.delta` events. The research file flags these as likely but
    not contractual. This bridge logs whatever it sees on debug level and only
    branches on the documented `.delta` event names.
  - The translate model's `voice` configuration is [UNCONFIRMED]; only
    `audio.output.language` is shown in the docs example. We therefore only
    set `audio.output.language` in the session.update body.
  - `audio/pcmu` (μ-law) is documented for the conversational endpoint but
    [UNCONFIRMED] for the translation endpoint. We take the safe path: PCM16
    24 kHz both ways, with stdlib audioop doing the 8k<->24k resample + μ-law
    codec on the Twilio side.
  - The translation endpoint's full client→server event list is [UNCONFIRMED]
    beyond `session.update` and `session.input_audio_buffer.append`. This
    bridge does NOT send `commit`, `clear`, or `response.create` equivalents
    — VAD on the server side drives turn detection.
"""

from __future__ import annotations

import asyncio
import audioop  # deprecated in 3.13 — see header
import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Literal

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from websockets.asyncio.client import ClientConnection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
)
log = logging.getLogger("twilio-interpreter")


# --------------------------------------------------------------------------- #
# Env / config                                                                #
# --------------------------------------------------------------------------- #


OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-realtime-translate")
PUBLIC_WS_URL = os.environ.get("PUBLIC_WS_URL", "wss://example.invalid")
LANG_A = os.environ.get("LANG_A", "fr")
LANG_B = os.environ.get("LANG_B", "ar")
# Reserved for future use (translation endpoint currently has no instructions
# field per the 2026-05-11 docs snapshot).
SYSTEM_PROMPT_NOTE = os.environ.get("SYSTEM_PROMPT_NOTE", "")

# WebSocket URL of the translation endpoint. The base path is distinct from
# the conversational `/v1/realtime` endpoint — DO NOT swap them.
OPENAI_WS_URL = (
    f"wss://api.openai.com/v1/realtime/translations?model={OPENAI_MODEL}"
)

# Twilio Media Streams audio is base64-encoded μ-law at 8 kHz, mono.
# OpenAI Realtime Translate takes PCM16 at 24 kHz (only documented format).
TWILIO_RATE = 8000
OPENAI_RATE = 24000
PCM_WIDTH = 2  # bytes per sample for PCM16


# --------------------------------------------------------------------------- #
# FastAPI app                                                                 #
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not OPENAI_API_KEY:
        log.warning(
            "OPENAI_API_KEY not set — bridge will fail when a call comes in.",
        )
    log.info(
        "twilio-interpreter started | model=%s | public_ws=%s | lang_a=%s | lang_b=%s",
        OPENAI_MODEL,
        PUBLIC_WS_URL,
        LANG_A,
        LANG_B,
    )
    yield
    log.info("twilio-interpreter stopped")


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "model": OPENAI_MODEL,
        "lang_a": LANG_A,
        "lang_b": LANG_B,
    }


# --------------------------------------------------------------------------- #
# TwiML endpoints                                                             #
# --------------------------------------------------------------------------- #


def _twiml_for_leg(leg: Literal["a", "b"]) -> str:
    """Build the <Connect><Stream> TwiML for one leg."""
    stream_url = f"{PUBLIC_WS_URL}/media/{leg}"
    # <Connect><Stream> is the bidirectional variant. We need bidirectional so
    # the bridge can write translated audio back to the caller's leg.
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{stream_url}"/>
  </Connect>
</Response>"""


@app.post("/twiml-leg-a")
async def twiml_leg_a() -> Response:
    return Response(content=_twiml_for_leg("a"), media_type="text/xml")


@app.post("/twiml-leg-b")
async def twiml_leg_b() -> Response:
    return Response(content=_twiml_for_leg("b"), media_type="text/xml")


# --------------------------------------------------------------------------- #
# Audio helpers                                                               #
# --------------------------------------------------------------------------- #


class ResampleState:
    """Persists audioop.ratecv state across frames in one direction.

    `audioop.ratecv` is stateful — its third return value carries
    inter-frame interpolation state that MUST be threaded into the next
    call, otherwise you get clicks and pitch artefacts every chunk
    boundary. One instance per direction (twilio->openai, openai->twilio).
    """

    def __init__(self) -> None:
        self.state: Any = None

    def convert(self, pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
        out, self.state = audioop.ratecv(
            pcm, PCM_WIDTH, 1, src_rate, dst_rate, self.state
        )
        return out


def twilio_ulaw_to_openai_pcm(ulaw_b64: str, st: ResampleState) -> str:
    """Decode Twilio μ-law 8k -> PCM16 24k base64 for OpenAI."""
    ulaw = base64.b64decode(ulaw_b64)
    pcm_8k = audioop.ulaw2lin(ulaw, PCM_WIDTH)
    pcm_24k = st.convert(pcm_8k, TWILIO_RATE, OPENAI_RATE)
    return base64.b64encode(pcm_24k).decode("ascii")


def openai_pcm_to_twilio_ulaw(pcm_b64: str, st: ResampleState) -> str:
    """Decode OpenAI PCM16 24k -> μ-law 8k base64 for Twilio."""
    pcm_24k = base64.b64decode(pcm_b64)
    pcm_8k = st.convert(pcm_24k, OPENAI_RATE, TWILIO_RATE)
    ulaw = audioop.lin2ulaw(pcm_8k, PCM_WIDTH)
    return base64.b64encode(ulaw).decode("ascii")


# --------------------------------------------------------------------------- #
# OpenAI translation session bootstrap                                        #
# --------------------------------------------------------------------------- #


async def open_translate_ws() -> ClientConnection:
    """Open an authenticated WS to the OpenAI Realtime Translations endpoint."""
    headers = [
        ("Authorization", f"Bearer {OPENAI_API_KEY}"),
        # [UNCONFIRMED] whether OpenAI-Beta: realtime=v1 is still required on
        # the new endpoints; not shown in the 2026-05 doc snippets. Omitted
        # here — add only if a live probe shows the endpoint rejects without it.
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


async def send_translation_session_update(
    ws: ClientConnection,
    target_lang: str,
    tag: str,
) -> None:
    """Configure a translation session.

    The translate guide (verified 2026-05-11) shows only `audio.output.language`
    as a documented field. Other session fields (input format, voice, etc.)
    are [UNCONFIRMED] for this endpoint and are omitted to avoid the wire
    silently dropping the entire session.update due to an unrecognised field.
    See ../references/session-config.md.
    """
    payload: dict[str, Any] = {
        "type": "session.update",
        "session": {
            "audio": {
                "output": {
                    "language": target_lang,
                },
            },
        },
    }
    await ws.send(json.dumps(payload))
    log.info("%s session.update sent (target=%s)", tag, target_lang)


async def send_translation_audio(
    ws: ClientConnection,
    pcm_b64: str,
    tag: str,
) -> None:
    """Forward a base64 PCM16 24k chunk to the translation endpoint.

    CRITICAL: the event name is PREFIXED with `session.` on this endpoint,
    unlike the conversational endpoint which uses the unprefixed form.
    Conversational endpoint (CONTRAST, do NOT use here):
        {"type": "input_audio_buffer.append", "audio": "..."}
    Translation endpoint (used below):
        {"type": "session.input_audio_buffer.append", "audio": "..."}
    """
    payload = {
        "type": "session.input_audio_buffer.append",
        "audio": pcm_b64,
    }
    try:
        await ws.send(json.dumps(payload))
    except websockets.ConnectionClosed:
        log.warning("%s tried to send after WS close", tag)


# --------------------------------------------------------------------------- #
# Bridge state — shared across both /media WebSocket connections              #
# --------------------------------------------------------------------------- #


class LegConnection:
    """One Twilio leg (the FastAPI WebSocket + its streamSid).

    The bridge writes translated audio back to a leg by referencing this
    object — `streamSid` is required in the outbound `media` and `clear`
    frames Twilio expects.
    """

    def __init__(self, leg_id: str) -> None:
        self.leg_id = leg_id
        self.ws_twilio: WebSocket | None = None
        self.stream_sid: str | None = None
        self.call_sid: str | None = None

    async def send_audio(self, ulaw_b64: str) -> None:
        if self.ws_twilio is None or self.stream_sid is None:
            return
        try:
            await self.ws_twilio.send_text(
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": self.stream_sid,
                        "media": {"payload": ulaw_b64},
                    }
                )
            )
        except Exception:
            # Connection probably closed; surface as warning, not exception.
            log.warning("[%s] failed to send audio to Twilio", self.leg_id)

    async def send_clear(self) -> None:
        if self.ws_twilio is None or self.stream_sid is None:
            return
        try:
            await self.ws_twilio.send_text(
                json.dumps(
                    {
                        "event": "clear",
                        "streamSid": self.stream_sid,
                    }
                )
            )
        except Exception:
            log.warning("[%s] failed to send clear to Twilio", self.leg_id)


class BridgeState:
    """In-memory routing table for the two-leg interpreter.

    For a single concurrent call this is the entire backing store. For a
    production multi-tenant deployment you would index by callSid or by a
    conference SID, not by global singleton.
    """

    def __init__(self) -> None:
        self.leg_a = LegConnection("a")
        self.leg_b = LegConnection("b")
        # ready_events let each pump task wait until both legs have started
        # before initiating the openai sessions, so we don't open billing-
        # accruing sessions until there's actually a call to translate.
        self.leg_a_ready = asyncio.Event()
        self.leg_b_ready = asyncio.Event()

    def leg(self, leg_id: str) -> LegConnection:
        if leg_id == "a":
            return self.leg_a
        if leg_id == "b":
            return self.leg_b
        raise ValueError(f"unknown leg_id: {leg_id}")

    def other_leg(self, leg_id: str) -> LegConnection:
        return self.leg_b if leg_id == "a" else self.leg_a

    def ready_event(self, leg_id: str) -> asyncio.Event:
        return self.leg_a_ready if leg_id == "a" else self.leg_b_ready


# Module-level singleton. Replace with a per-conference map in production.
STATE = BridgeState()


# --------------------------------------------------------------------------- #
# Per-leg pumps                                                               #
# --------------------------------------------------------------------------- #


def _direction_tag(leg_id: str) -> str:
    """Pretty direction string for log lines (a->b or b->a)."""
    return "a->b" if leg_id == "a" else "b->a"


def _target_lang_for(leg_id: str) -> str:
    """Target language is the OTHER leg's spoken language.

    Leg A speaks LANG_A → we translate INTO LANG_B (what leg B understands).
    Leg B speaks LANG_B → we translate INTO LANG_A (what leg A understands).
    """
    return LANG_B if leg_id == "a" else LANG_A


async def pump_twilio_to_openai(
    leg_id: str,
    ws_openai: ClientConnection,
    in_state: ResampleState,
) -> None:
    """Read frames from the Twilio leg WS, forward audio to OpenAI translate.

    Logging tag: [twilio->openai a->b] when leg_id="a".
    """
    leg = STATE.leg(leg_id)
    direction = _direction_tag(leg_id)
    tag = f"[twilio->openai {direction}]"
    ws_twilio = leg.ws_twilio
    if ws_twilio is None:
        log.error("%s pump started without a Twilio WS", tag)
        return
    try:
        while True:
            raw = await ws_twilio.receive_text()
            msg = json.loads(raw)
            event = msg.get("event")
            if event == "connected":
                log.info("%s twilio connected proto=%s", tag, msg.get("protocol"))
            elif event == "start":
                start = msg.get("start", {})
                leg.stream_sid = start.get("streamSid")
                leg.call_sid = start.get("callSid")
                log.info(
                    "%s start streamSid=%s callSid=%s",
                    tag,
                    leg.stream_sid,
                    leg.call_sid,
                )
                STATE.ready_event(leg_id).set()
            elif event == "media":
                payload = msg.get("media", {}).get("payload")
                if not payload:
                    continue
                pcm_b64 = twilio_ulaw_to_openai_pcm(payload, in_state)
                await send_translation_audio(ws_openai, pcm_b64, tag)
            elif event == "mark":
                log.debug("%s mark: %s", tag, msg.get("mark", {}).get("name"))
            elif event == "stop":
                log.info("%s stop", tag)
                break
            else:
                log.debug("%s unknown twilio event: %s", tag, event)
    except WebSocketDisconnect:
        log.info("%s twilio disconnected", tag)
    except Exception:
        log.exception("%s pump error", tag)


async def pump_openai_to_twilio(
    leg_id: str,
    ws_openai: ClientConnection,
    out_state: ResampleState,
) -> None:
    """Read events from OpenAI translation WS, forward translated audio to the OTHER leg.

    Direction routing: this pump processes the session that takes leg `leg_id`
    as INPUT and emits translated audio for the OTHER leg. We therefore write
    to STATE.other_leg(leg_id), NOT to STATE.leg(leg_id). Getting this wrong
    is the #1 bug class for multi-direction bridges — the caller hears their
    own translated voice back instead of the counterpart's translation.

    Logging tag: [openai->twilio a->b] when leg_id="a" (input from A, output
    routed to B).
    """
    direction = _direction_tag(leg_id)
    tag = f"[openai->twilio {direction}]"
    dest = STATE.other_leg(leg_id)
    try:
        async for raw in ws_openai:
            evt = json.loads(raw)
            etype = evt.get("type")
            if etype == "session.output_audio.delta":
                delta_b64 = evt.get("delta", "")
                if not delta_b64:
                    continue
                ulaw_b64 = openai_pcm_to_twilio_ulaw(delta_b64, out_state)
                await dest.send_audio(ulaw_b64)
            elif etype == "session.output_transcript.delta":
                # Target-language transcript for the destination leg. Useful
                # for captions, logging, or pushing into a sidecar UI. We
                # only log here; a real app would forward over a separate
                # control channel.
                log.debug(
                    "%s target transcript: %s",
                    tag,
                    evt.get("delta"),
                )
            elif etype == "session.input_transcript.delta":
                # Source-language transcript of the SPEAKER (leg `leg_id`).
                # Same caveat as above.
                log.debug(
                    "%s source transcript: %s",
                    tag,
                    evt.get("delta"),
                )
            elif etype == "session.created":
                log.info("%s session.created", tag)
            elif etype == "session.updated":
                log.info("%s session.updated", tag)
            elif etype == "error" or etype == "invalid_request_error":
                # [UNCONFIRMED] whether the translation endpoint actually emits
                # these envelopes; the research file notes lifecycle events
                # are probable but not contractual. We branch defensively.
                log.error("%s openai error: %s", tag, evt)
            else:
                # The translation endpoint may emit *.done / *.completed
                # counterparts to the documented .delta events. Per the
                # research file these are [UNCONFIRMED] — we log on debug so
                # a live probe surfaces their actual names.
                log.debug("%s openai event: %s", tag, etype)
    except websockets.ConnectionClosed:
        log.info("%s openai closed", tag)
    except Exception:
        log.exception("%s pump error", tag)


# --------------------------------------------------------------------------- #
# Per-leg WebSocket endpoint                                                  #
# --------------------------------------------------------------------------- #


@app.websocket("/media/{leg_id}")
async def media_ws(ws_twilio: WebSocket, leg_id: str) -> None:
    """Accept a Twilio Media Stream for one leg and bridge it to a translation session.

    The session translates this leg's audio INTO the other leg's language and
    routes the translated audio back to the OTHER leg's Twilio WS.
    """
    if leg_id not in ("a", "b"):
        await ws_twilio.close(code=1008)
        return
    await ws_twilio.accept()
    log.info("[twilio %s] accepted media stream", leg_id)

    leg = STATE.leg(leg_id)
    leg.ws_twilio = ws_twilio
    leg.stream_sid = None
    leg.call_sid = None
    STATE.ready_event(leg_id).clear()

    in_state = ResampleState()   # Twilio -> OpenAI (8k -> 24k μ-law->PCM16)
    out_state = ResampleState()  # OpenAI -> Twilio (24k -> 8k PCM16->μ-law)

    target_lang = _target_lang_for(leg_id)
    ws_openai: ClientConnection | None = None

    try:
        ws_openai = await open_translate_ws()
        await send_translation_session_update(
            ws_openai,
            target_lang,
            tag=f"[openai {leg_id}]",
        )

        # Run both directions concurrently. Pump terminates when either side
        # closes; we then tear down the other.
        t_in = asyncio.create_task(
            pump_twilio_to_openai(leg_id, ws_openai, in_state),
            name=f"twilio->openai {leg_id}",
        )
        t_out = asyncio.create_task(
            pump_openai_to_twilio(leg_id, ws_openai, out_state),
            name=f"openai->twilio {leg_id}",
        )
        done, pending = await asyncio.wait(
            {t_in, t_out},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for p in pending:
            p.cancel()
        for d in done:
            # Surface any exception from the completed task.
            exc = d.exception()
            if exc is not None:
                log.warning(
                    "[bridge %s] pump task exited with: %r",
                    leg_id,
                    exc,
                )
    finally:
        if ws_openai is not None:
            try:
                await ws_openai.close()
            except Exception:
                pass
        leg.ws_twilio = None
        log.info(
            "[bridge %s] cleanup done | streamSid=%s callSid=%s",
            leg_id,
            leg.stream_sid,
            leg.call_sid,
        )


# --------------------------------------------------------------------------- #
# Self-check: grep guard for event-vocabulary regressions                     #
# --------------------------------------------------------------------------- #
#
# A regression test for this file: every occurrence of
# `input_audio_buffer.append` MUST be preceded by `session.` (or appear in a
# comment that explicitly contrasts with the conversational endpoint, marked
# with the word CONTRAST). If a future edit drops the prefix on an actual
# send call the wire silently accepts the JSON but the model never sees the
# audio — and the bug is invisible without close inspection.
#
# To run the guard locally:
#   grep -n 'input_audio_buffer.append' examples/twilio-interpreter.py \
#     | grep -v 'session.input_audio_buffer.append' \
#     | grep -v 'CONTRAST'
# Should produce no lines.


# --------------------------------------------------------------------------- #
# Entrypoint hint                                                             #
# --------------------------------------------------------------------------- #


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "twilio-interpreter:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level="info",
    )
