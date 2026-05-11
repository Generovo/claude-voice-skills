"""
meeting-translator.py — Recall.ai meeting bot <-> OpenAI Realtime Translate, N target languages.

Purpose
-------
A FastAPI service that dispatches a Recall.ai bot into a meeting (Zoom / Meet /
Teams), receives the meeting audio over a WebSocket, fans the audio out to N
parallel OpenAI Realtime Translate sessions (one per target language), and
writes live translated subtitles to a per-language transcript file.

By default the service produces subtitles only — translated audio is NOT
re-injected into the meeting, to avoid the speakers hearing their own
translated voice feeding back into the input mic (a hard-to-debug audio loop
described in `../SKILL.md ## Latency considerations`). The optional
`--push-tts` flag is documented at the bottom for deployments that have a
separate playback channel (e.g. individual listener earbuds).

Multi-language fan-out
----------------------
Per the SKILL.md `## Multi-speaker architecture` section, the translation
endpoint supports exactly ONE output language per session. To cover N target
languages we open N parallel WebSockets and `session.input_audio_buffer.append`
the same source audio frames to each. This is the documented and supported
multi-language pattern.

Endpoints
---------
    POST /create-bot
        body: { meeting_url, target_languages?: ["fr","en","ar"], bot_name? }
        action: calls Recall API to dispatch a bot with audio destination
                wss://<PUBLIC_WS_URL>; stashes the per-bot language list so the
                /recall-audio handler picks it up when the WS opens.

    WS /recall-audio
        Recall posts metadata + PCM audio frames here. For each configured
        target language we lazily open a translate session and stream the
        audio in parallel. Transcript deltas are appended to per-language
        in-memory buffers and flushed to disk on close / on each .done event.

    WS /recall-events  (optional bot-lifecycle webhook receiver)

Flow per audio frame
--------------------
    Recall PCM16 16 kHz mono frame
        -> resample to 24 kHz (translation endpoint's only documented rate)
        -> for each target lang:
            -> session.input_audio_buffer.append (PREFIXED — see CRITICAL note)
        -> server emits per-language session.output_transcript.delta
        -> we append to that language's transcript buffer
        -> on response.done / *.done, we finalise the utterance to disk

CRITICAL — event vocabulary
---------------------------
The translation endpoint uses event names PREFIXED with `session.`. Mixing
them with the conversational endpoint's unprefixed names silently breaks: the
WS stays open, no error, no translated text comes back. See
../SKILL.md ## Event vocabulary — CRITICAL DIFFERENCE.

Events used in this file:
    session.input_audio_buffer.append   (client -> server, audio chunk)
    session.output_transcript.delta     (server -> client, target-lang text)
    session.input_transcript.delta      (server -> client, source-lang text)
    session.output_audio.delta          (server -> client, base64 audio)
                                          — IGNORED by default; enable with
                                            --push-tts for separate playback.

Dependencies
------------
    pip install fastapi uvicorn websockets httpx pydantic

Python 3.12+. NOTE: the stdlib `audioop` module is deprecated in 3.13. For
3.13+ install `audioop-lts` and swap the import below.

Run
---
    uvicorn examples.meeting-translator:app --host 0.0.0.0 --port 8080
    ngrok http 8080

    # Then create a bot for an in-progress meeting:
    curl -X POST http://localhost:8080/create-bot \\
        -H "Content-Type: application/json" \\
        -d '{
              "meeting_url": "https://zoom.us/j/...",
              "target_languages": ["fr", "en", "ar"]
            }'

Env
---
    OPENAI_API_KEY                  Bearer token for Realtime API (required).
    OPENAI_MODEL                    Defaults to `gpt-realtime-translate`.
    RECALL_API_KEY                  Recall.ai API key.
    PUBLIC_WS_URL                   Public WSS URL of THIS service's
                                    /recall-audio endpoint, e.g.
                                    wss://<tunnel>/recall-audio
    DEFAULT_TARGET_LANGUAGES        Comma-separated default list (default
                                    "fr,en,ar").

Sources (verified 2026-05-11)
-----------------------------
- /Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md
- https://developers.openai.com/api/docs/guides/realtime-translation
- https://developers.openai.com/api/docs/models/gpt-realtime-translate
- https://docs.recall.ai/ (top-level — exact field names confirmed at
  integration time; TODO comments below mark `[UNCONFIRMED]` spots)

[UNCONFIRMED] items propagated from research / vendor docs
----------------------------------------------------------
- Recall.ai's exact JSON shape for the bot-creation payload — `real_time_media`
  vs `real_time_transcription`, `websocket_audio_destination_url` vs
  `audio_destination` — has shifted between Recall API revisions. The body
  shape below targets raw-audio streaming; adjust once you have a successful
  first run against your Recall API tier.
- Recall.ai inbound frame schema (JSON metadata + binary audio vs base64-in-JSON)
  varies by configuration. We handle both shapes defensively.
- Recall.ai audio sample rate — `[UNCONFIRMED]` definitively but historically
  16 kHz PCM16 mono. We resample on the way in to OpenAI's 24 kHz.
- The translate endpoint's full `.done` / `.completed` event names are
  `[UNCONFIRMED]`. We finalise an utterance on whichever .done arrives first
  for a given language session.
- Voice config and TTS-injection-back-to-meeting are `[UNCONFIRMED]` — see the
  `send_audio_to_recall_meeting` TODO at the bottom.
"""

from __future__ import annotations

import asyncio
import audioop  # deprecated in 3.13 — install audioop-lts for 3.13+
import base64
import json
import logging
import os
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
log = logging.getLogger("meeting-translator")


# --------------------------------------------------------------------------- #
# Env / config                                                                #
# --------------------------------------------------------------------------- #


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-realtime-translate")
OPENAI_WS_URL = (
    f"wss://api.openai.com/v1/realtime/translations?model={OPENAI_MODEL}"
)

PUBLIC_WS_URL = os.environ.get(
    "PUBLIC_WS_URL", "wss://example.invalid/recall-audio"
)

# Default fan-out languages. Override per-bot via /create-bot body.
DEFAULT_TARGET_LANGUAGES = [
    s.strip().lower()
    for s in os.environ.get("DEFAULT_TARGET_LANGUAGES", "fr,en,ar").split(",")
    if s.strip()
]

# Recall.ai's documented raw audio output for real-time streaming is PCM 16-bit
# mono at 16 kHz. OpenAI Realtime Translate takes PCM16 24 kHz. We resample
# on the way in.
RECALL_RATE = 16000
OPENAI_RATE = 24000
PCM_WIDTH = 2

RECALL_API_BASE = "https://api.recall.ai"

# Output of translated TTS is ignored by default. Set PUSH_TTS=1 in env (or
# wire your own deployment switch) to re-enable injection back into the
# meeting — typically only safe when listeners have separate earbuds and the
# meeting microphone cannot pick up the translated playback.
PUSH_TTS = os.environ.get("PUSH_TTS", "").lower() in ("1", "true", "yes")


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
        log.warning(
            "Missing env vars (service will fail on use): %s",
            ", ".join(missing),
        )
    log.info(
        "meeting-translator started | model=%s | public_ws=%s | default_langs=%s | push_tts=%s",
        OPENAI_MODEL,
        PUBLIC_WS_URL,
        DEFAULT_TARGET_LANGUAGES,
        PUSH_TTS,
    )
    yield
    log.info("meeting-translator stopped")


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "model": OPENAI_MODEL,
        "default_langs": DEFAULT_TARGET_LANGUAGES,
    }


# --------------------------------------------------------------------------- #
# Audio helpers                                                               #
# --------------------------------------------------------------------------- #


class ResampleState:
    """Persists audioop.ratecv state across frames in one direction.

    `audioop.ratecv` is stateful — its third return value carries inter-frame
    interpolation state that MUST be threaded into the next call, otherwise
    you get clicks and pitch artefacts every chunk boundary. One instance per
    direction (here we only need 16k -> 24k).
    """

    def __init__(self) -> None:
        self.state: Any = None

    def convert(self, pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
        out, self.state = audioop.ratecv(
            pcm, PCM_WIDTH, 1, src_rate, dst_rate, self.state
        )
        return out


def recall_pcm_to_openai_pcm_b64(
    pcm_16k: bytes, st: ResampleState
) -> str:
    """PCM16 16k -> PCM16 24k base64 ascii (the wire format for OpenAI append)."""
    pcm_24k = st.convert(pcm_16k, RECALL_RATE, OPENAI_RATE)
    return base64.b64encode(pcm_24k).decode("ascii")


# --------------------------------------------------------------------------- #
# /create-bot                                                                 #
# --------------------------------------------------------------------------- #


class CreateBotRequest(BaseModel):
    meeting_url: str
    target_languages: list[str] | None = None
    bot_name: str | None = "AI Interpreter"


# In-memory cache: bot_id -> list of target languages. Populated at
# /create-bot time, consumed by /recall-audio when the metadata frame
# announces the bot_id. Replace with Redis for multi-replica deployments.
_bot_langs_cache: dict[str, list[str]] = {}


@app.post("/create-bot")
async def create_bot(req: CreateBotRequest) -> dict[str, Any]:
    """Dispatch a Recall bot, asking it to stream audio to PUBLIC_WS_URL.

    TODO: confirm with vendor docs (https://docs.recall.ai/) the exact field
    names. As of the 2026-05-11 research cutoff the precise JSON shape for
    real_time_media / websocket_audio_destination_url was `[UNCONFIRMED]`.
    The body shape below targets raw-audio streaming; adjust once you have
    a successful first run.
    """
    api_key = _require_env("RECALL_API_KEY")
    target_languages = [
        c.lower() for c in (req.target_languages or DEFAULT_TARGET_LANGUAGES)
    ]
    if not target_languages:
        raise HTTPException(
            status_code=400, detail="target_languages cannot be empty"
        )

    body: dict[str, Any] = {
        "meeting_url": req.meeting_url,
        "bot_name": req.bot_name or "AI Interpreter",
        # TODO: confirm exact Recall field names. Recent Recall API versions
        # have used both `real_time_media` and `real_time_transcription` with
        # different sub-fields. We target raw-audio streaming.
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
            log.error(
                "[recall] create-bot failed %s: %s",
                resp.status_code,
                resp.text,
            )
            raise HTTPException(
                status_code=resp.status_code, detail=resp.text
            )
        data = resp.json()
        bot_id = str(data.get("id") or data.get("bot_id") or "")
        log.info(
            "[recall] bot created id=%s langs=%s", bot_id, target_languages
        )
        if bot_id:
            _bot_langs_cache[bot_id] = target_languages
        return {**data, "target_languages": target_languages}


# --------------------------------------------------------------------------- #
# Recall lifecycle WS (optional)                                              #
# --------------------------------------------------------------------------- #


@app.websocket("/recall-events")
async def recall_events(ws: WebSocket) -> None:
    """Optional Recall lifecycle event receiver.

    TODO: confirm with vendor docs whether Recall delivers lifecycle events
    via HTTP webhook or WebSocket. We accept WS here; if your plan uses
    HTTP webhooks, replace this with a POST handler.
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
            log.info(
                "[recall] event %s",
                evt.get("event") or evt.get("type") or evt,
            )
    except WebSocketDisconnect:
        log.info("[recall] events WS disconnected")


# --------------------------------------------------------------------------- #
# Per-language translate session                                              #
# --------------------------------------------------------------------------- #


class LangSession:
    """One translate session targeting a single output language.

    Each instance owns:
      - the OpenAI WebSocket (one session per output language)
      - a transcript buffer (current utterance fragments + finalised lines)
      - a background receive task that consumes server -> client events

    The fan-out in `MeetingBridge` writes to `self.ws` once per incoming
    audio frame; the receive task drains `self.ws` in parallel.
    """

    def __init__(self, lang: str, bot_id: str) -> None:
        self.lang = lang
        self.bot_id = bot_id
        self.ws: ClientConnection | None = None
        self.current_utterance: list[str] = []
        self.finalised: list[tuple[str, str]] = []  # (iso_ts, text)
        self._closed = False

    async def open(self) -> None:
        """Open the WebSocket and configure for `self.lang` output."""
        headers = [
            ("Authorization", f"Bearer {_require_env('OPENAI_API_KEY')}"),
        ]
        log.info(
            "[openai %s] connecting %s", self.lang, OPENAI_WS_URL
        )
        self.ws = await websockets.connect(
            OPENAI_WS_URL,
            additional_headers=headers,
            max_size=16 * 1024 * 1024,
            ping_interval=20,
            ping_timeout=20,
        )
        log.info("[openai %s] connected", self.lang)
        await self._send_session_update()

    async def _send_session_update(self) -> None:
        """Send session.update with audio.output.language = self.lang.

        Per references/session-config.md only `audio.output.language` is
        documented; we include a conservative `audio.input.format` hint that
        the endpoint may silently ignore.
        """
        assert self.ws is not None
        payload: dict[str, Any] = {
            "type": "session.update",
            "session": {
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": OPENAI_RATE},
                    },
                    "output": {
                        "language": self.lang,
                    },
                },
            },
        }
        await self.ws.send(json.dumps(payload))
        log.info(
            "[openai %s] session.update sent (target=%s)",
            self.lang,
            self.lang,
        )

    async def send_audio_b64(self, pcm_24k_b64: str) -> None:
        """Append a base64 PCM16 24 kHz chunk via the PREFIXED event name.

        CRITICAL: `session.input_audio_buffer.append` (with `session.` prefix).
        CONTRAST — the conversational endpoint uses unprefixed
        `input_audio_buffer.append`  # CONTRAST. Mixing them silently breaks
        — see ../SKILL.md ## Event vocabulary — CRITICAL DIFFERENCE.
        """
        if self.ws is None or self._closed:
            return
        try:
            await self.ws.send(
                json.dumps(
                    {
                        "type": "session.input_audio_buffer.append",
                        "audio": pcm_24k_b64,
                    }
                )
            )
        except websockets.ConnectionClosed:
            log.warning(
                "[openai %s] tried to send after WS close", self.lang
            )
            self._closed = True

    async def receive_loop(self) -> None:
        """Drain server -> client events; build per-utterance transcripts."""
        assert self.ws is not None
        try:
            async for raw in self.ws:
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type", "")

                if etype == "session.output_transcript.delta":
                    delta = evt.get("delta", "")
                    if delta:
                        self.current_utterance.append(delta)
                elif etype == "session.input_transcript.delta":
                    # Source-language transcript — useful for spot-checking
                    # which speaker said what. Not written by default; add
                    # your own sidecar if you want both tracks.
                    log.debug(
                        "[openai %s] source: %s", self.lang, evt.get("delta")
                    )
                elif etype == "session.output_audio.delta":
                    # Translated audio. Default: drop (subtitles-only mode).
                    if PUSH_TTS:
                        # TODO: wire to Recall's audio-injection channel.
                        # See send_audio_to_recall_meeting() at the bottom.
                        pass
                elif etype == "session.created":
                    log.info("[openai %s] session.created", self.lang)
                elif etype == "session.updated":
                    log.info("[openai %s] session.updated", self.lang)
                elif etype in ("error", "invalid_request_error"):
                    log.error(
                        "[openai %s] error event: %s", self.lang, evt
                    )
                elif etype.endswith(".done") or etype.endswith(".completed"):
                    # Finalise the current utterance to the in-memory buffer
                    # and to disk. [UNCONFIRMED] which exact .done event name
                    # the translation endpoint emits; we treat any .done /
                    # .completed as a finalisation signal for the in-flight
                    # utterance.
                    self._finalise_utterance()
                else:
                    log.debug("[openai %s] %s", self.lang, etype)
        except websockets.ConnectionClosed:
            log.info("[openai %s] closed", self.lang)
        except Exception:
            log.exception("[openai %s] receive loop error", self.lang)
        finally:
            # On any exit, finalise whatever is in the buffer.
            self._finalise_utterance()

    def _finalise_utterance(self) -> None:
        """Write the current utterance fragments out, then clear."""
        if not self.current_utterance:
            return
        text = "".join(self.current_utterance).strip()
        if text:
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            self.finalised.append((ts, text))
            log.info(
                "[transcript %s] [%s] %s", self.lang, ts, text
            )
            self._append_line_to_disk(ts, text)
        self.current_utterance.clear()

    def _transcript_path(self) -> str:
        slug = self.bot_id or "unknown"
        return f"transcript-{slug}-{self.lang}.txt"

    def _append_line_to_disk(self, ts: str, text: str) -> None:
        """Best-effort append to the per-language transcript file."""
        try:
            with open(self._transcript_path(), "a", encoding="utf-8") as fh:
                fh.write(f"[{ts}] {text}\n")
        except Exception:
            log.exception(
                "[transcript %s] failed to append to disk", self.lang
            )

    async def close(self) -> None:
        self._closed = True
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        # Persist a final header + any remaining finalised lines (defensive —
        # the per-line append above should already have written everything).
        try:
            with open(self._transcript_path(), "a", encoding="utf-8") as fh:
                fh.write(
                    f"# closed bot_id={self.bot_id} lang={self.lang} "
                    f"at={datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
                )
        except Exception:
            log.exception(
                "[transcript %s] failed to write footer", self.lang
            )


# --------------------------------------------------------------------------- #
# MeetingBridge — owns N LangSessions and the inbound Recall fan-out          #
# --------------------------------------------------------------------------- #


class MeetingBridge:
    """The per-WS state for one /recall-audio connection.

    Owns:
      - one resample state (Recall 16k -> OpenAI 24k) — the SAME resampled
        audio is fan-routed to every language session, so we resample ONCE
        and broadcast.
      - one LangSession per target language.
    """

    def __init__(self, bot_id: str, target_languages: list[str]) -> None:
        self.bot_id = bot_id
        self.target_languages = target_languages
        self.in_state = ResampleState()
        self.sessions: dict[str, LangSession] = {}
        self._receive_tasks: list[asyncio.Task[None]] = []

    async def open_all(self) -> None:
        """Open one OpenAI translate session per target language."""
        for lang in self.target_languages:
            sess = LangSession(lang, self.bot_id)
            await sess.open()
            self.sessions[lang] = sess
            self._receive_tasks.append(
                asyncio.create_task(
                    sess.receive_loop(), name=f"recv-{self.bot_id}-{lang}"
                )
            )
        log.info(
            "[bridge %s] opened %d sessions: %s",
            self.bot_id,
            len(self.sessions),
            list(self.sessions.keys()),
        )

    async def fan_out(self, pcm_16k: bytes) -> None:
        """Resample once, then send to every per-language session in parallel."""
        if not self.sessions:
            return
        pcm_24k_b64 = recall_pcm_to_openai_pcm_b64(pcm_16k, self.in_state)
        await asyncio.gather(
            *(s.send_audio_b64(pcm_24k_b64) for s in self.sessions.values()),
            return_exceptions=True,
        )

    async def close_all(self) -> None:
        """Close every per-language session and cancel its receive task."""
        for sess in self.sessions.values():
            await sess.close()
        for t in self._receive_tasks:
            t.cancel()
        # Drain cancellations so we don't leak coroutine warnings.
        for t in self._receive_tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        log.info(
            "[bridge %s] closed %d sessions", self.bot_id, len(self.sessions)
        )


# --------------------------------------------------------------------------- #
# /recall-audio — the main bridge                                             #
# --------------------------------------------------------------------------- #


@app.websocket("/recall-audio")
async def recall_audio(ws_recall: WebSocket) -> None:
    """Recall -> us -> N OpenAI translate sessions -> per-language transcripts."""
    await ws_recall.accept()
    log.info("[recall] audio WS accepted")

    bot_id: str = ""
    bridge: MeetingBridge | None = None
    target_languages: list[str] = []

    try:
        while True:
            msg = await ws_recall.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            pcm_chunk: bytes | None = None

            text = msg.get("text")
            if text is not None:
                # JSON metadata or base64-wrapped audio.
                try:
                    evt = json.loads(text)
                except json.JSONDecodeError:
                    log.warning("[recall] non-JSON text frame ignored")
                    continue
                # TODO: confirm with vendor docs the exact shape of Recall's
                # metadata frames. Common fields observed: event/type,
                # bot_id, data, audio (base64), participant.
                etype = evt.get("event") or evt.get("type")
                if etype:
                    log.info("[recall] frame type=%s", etype)
                if evt.get("bot_id") and not bot_id:
                    bot_id = str(evt["bot_id"])
                    target_languages = _bot_langs_cache.pop(
                        bot_id, DEFAULT_TARGET_LANGUAGES
                    )
                    log.info(
                        "[recall] bound bot_id=%s langs=%s",
                        bot_id,
                        target_languages,
                    )
                if isinstance(evt.get("audio"), str):
                    try:
                        pcm_chunk = base64.b64decode(evt["audio"])
                    except Exception:
                        log.warning("[recall] base64 audio decode failed")

            binary = msg.get("bytes")
            if binary is not None:
                pcm_chunk = binary

            if pcm_chunk is None:
                continue

            # Lazy-open all language sessions on first audio frame. We need
            # bot_id (from a prior metadata frame) to slug the transcript
            # filenames; if Recall sends audio before any metadata, fall back
            # to the wall-clock as the slug.
            if bridge is None:
                if not bot_id:
                    bot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    target_languages = (
                        target_languages or DEFAULT_TARGET_LANGUAGES
                    )
                    log.info(
                        "[recall] no bot_id metadata yet — using time-slug %s",
                        bot_id,
                    )
                bridge = MeetingBridge(bot_id, target_languages)
                await bridge.open_all()

            await bridge.fan_out(pcm_chunk)
    except WebSocketDisconnect:
        log.info("[recall] audio WS disconnected")
    except Exception:
        log.exception("[recall->openai] bridge loop error")
    finally:
        if bridge is not None:
            await bridge.close_all()
        log.info("[bridge] cleanup done | bot_id=%s", bot_id)


# --------------------------------------------------------------------------- #
# Optional: TTS injection back into the meeting                               #
# --------------------------------------------------------------------------- #


async def send_audio_to_recall_meeting(
    bot_id: str, pcm_24k: bytes, lang: str
) -> None:
    """Inject translated audio back into the meeting via Recall's output channel.

    NOT USED in the default subtitles-only deployment — feeding translated
    audio back into the same room as the source mic causes infinite
    translation loops (see ../SKILL.md ## Latency considerations, "Echo
    cancellation"). Enable only when each listener has a separate playback
    device and the meeting microphone cannot capture the playback.

    TODO: confirm with vendor docs — Recall's API for sending audio FROM
    your bot INTO the meeting is `[UNCONFIRMED]` in the OpenAI research
    doc and has historically required a separate "output media" channel
    or REST endpoint. Common Recall shapes:

        - Separate WebSocket: bot.real_time_media.audio_input
        - REST: POST /api/v1/bot/<bot_id>/output_audio/
        - WebRTC track if you opted into Recall's WebRTC output

    Until confirmed, this function logs what would be sent.
    """
    log.info(
        "[recall] would send %d bytes pcm16@24k to meeting (bot=%s lang=%s) — wiring TODO",
        len(pcm_24k),
        bot_id,
        lang,
    )
    # TODO: replace the log line above with the actual Recall delivery call
    # once you've confirmed the channel shape with your Recall plan.


# --------------------------------------------------------------------------- #
# Self-check guard for event-vocabulary regressions                           #
# --------------------------------------------------------------------------- #
#
# Every occurrence of `input_audio_buffer.append` in this file MUST be either
# (a) prefixed with `session.` on an actual ws.send call, or (b) appear inside
# a comment marked CONTRAST that explicitly contrasts with the conversational
# endpoint. Run:
#
#   grep -n 'input_audio_buffer.append' examples/meeting-translator.py \
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
        "meeting-translator:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level="info",
    )
