"""
meeting-minutes.py — Recall.ai meeting bot -> streaming Whisper transcription
                     -> post-meeting structured minutes (markdown).

Purpose
-------
A FastAPI service that:

  1. POST /create-bot: dispatches a Recall.ai bot into a Zoom/Meet/Teams
     meeting and tells it to stream audio to /recall-audio.
  2. WS /recall-audio: receives raw PCM16 16 kHz mono frames from Recall,
     resamples to 24 kHz, and pushes them through a single OpenAI Realtime
     *transcription* session (gpt-realtime-whisper).
  3. Maintains a per-meeting transcript buffer: every `.completed` event
     appends a timestamped line to `transcript-<bot_id>.txt`.
  4. WS /recall-events: receives Recall bot-lifecycle events. On `bot.done`
     or `bot.left`, triggers the post-meeting summarization step.
  5. Summarization step calls `chat/completions` with `gpt-4o-mini` and
     writes `minutes-<bot_id>.md`:
        - Summary (3-5 sentences)
        - Topics Discussed (bullets)
        - Decisions (bullets)
        - Action Items (markdown table)

Why a separate model for summarization?
  - gpt-realtime-whisper is optimized for streaming speech-to-text; it
    doesn't produce structured documents.
  - gpt-4o-mini is cheap and fast for the medium-length text-in -> text-out
    summarization job. You can swap in any chat-completions model via
    OPENAI_MODEL_SUMMARIZE without touching the transcription path.

Sister files in this skill:
  - examples/twilio-transcribe.py        — phone call shape, same session.
  - examples/whatsapp-voice-notes.py     — WhatsApp shape, same session.
  - scripts/test-latency-vs-quality.py   — benchmark profile tuning.

Dependencies
------------
    pip install fastapi uvicorn websockets httpx pydantic
    # audioop is stdlib (Python 3.12); for 3.13+:
    # pip install audioop-lts  (and change `import audioop` accordingly)

Run
---
    uvicorn examples.meeting-minutes:app --host 0.0.0.0 --port 8080
    ngrok http 8080

    # Then dispatch a bot:
    curl -X POST http://localhost:8080/create-bot \\
      -H "Content-Type: application/json" \\
      -d '{
            "meeting_url": "https://zoom.us/j/...",
            "bot_name": "AI Minutes",
            "languages": ["fr","en"]
          }'

Environment
-----------
    OPENAI_API_KEY                Bearer for OpenAI Realtime + chat (required).
    OPENAI_MODEL_TRANSCRIBE       Default "gpt-realtime-whisper".
    OPENAI_MODEL_SUMMARIZE        Default "gpt-4o-mini".
    RECALL_API_KEY                Recall.ai API token (required).
    PUBLIC_WS_URL                 Public WSS URL of this service's
                                  /recall-audio endpoint, written into the
                                  create-bot payload.
    LATENCY_PROFILE               Default "balanced".
    LANGUAGE                      Optional ISO-639-1 hint.
    SUMMARIZE_PROMPT_FILE         Optional path overriding the default
                                  summarization system prompt.

Sources (verified 2026-05-11)
-----------------------------
- OpenAI Realtime transcription guide:
  https://developers.openai.com/api/docs/guides/realtime-transcription
- OpenAI chat-completions:
  https://platform.openai.com/docs/api-reference/chat
- Recall.ai bot API + real-time audio destination:
  https://docs.recall.ai/
- Project snapshot:
  /Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md

[UNCONFIRMED] items propagated
------------------------------
- Recall.ai's exact bot-creation field names (real_time_media vs
  real_time_transcription vs websocket_audio_destination_url). Shape below
  matches voice-agent-realtime/examples/meeting-bot.py and is the most
  commonly documented variant — adjust on first live integration.
- Recall's WS frame schema: JSON metadata frames vs raw binary PCM. Handled
  both shapes defensively (mirrors voice-agent-realtime/meeting-bot.py).
- OpenAI WS URL for transcription endpoint: try PRIMARY + FALLBACK.
- `OpenAI-Beta: realtime=v1` header still required (sent defensively).
- Speaker labels: Recall historically exposes `participant` per audio frame
  on some plans. We thread it into the transcript when present and fall
  back to `<unknown>` otherwise.
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
from pathlib import Path
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
log = logging.getLogger("meeting-minutes")


# --------------------------------------------------------------------------- #
# Env / config                                                                #
# --------------------------------------------------------------------------- #


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


OPENAI_MODEL_TRANSCRIBE: str = os.environ.get(
    "OPENAI_MODEL_TRANSCRIBE", "gpt-realtime-whisper"
)
OPENAI_MODEL_SUMMARIZE: str = os.environ.get(
    "OPENAI_MODEL_SUMMARIZE", "gpt-4o-mini"
)
PUBLIC_WS_URL: str = os.environ.get(
    "PUBLIC_WS_URL", "wss://example.invalid/recall-audio"
)
LATENCY_PROFILE: str = os.environ.get("LATENCY_PROFILE", "balanced")
LANGUAGE: str = os.environ.get("LANGUAGE", "")
SUMMARIZE_PROMPT_FILE: str = os.environ.get("SUMMARIZE_PROMPT_FILE", "")

# [UNCONFIRMED] same dual-URL pattern as twilio-transcribe.py.
OPENAI_TRANSCRIBE_WS_URL_PRIMARY: str = (
    f"wss://api.openai.com/v1/realtime?model={OPENAI_MODEL_TRANSCRIBE}"
)
OPENAI_TRANSCRIBE_WS_URL_FALLBACK: str = (
    f"wss://api.openai.com/v1/realtime/transcription_sessions"
    f"?model={OPENAI_MODEL_TRANSCRIBE}"
)
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

# Recall.ai's documented raw-audio output for real-time streaming is PCM16
# mono 16 kHz. [UNCONFIRMED] — flag retained per skill convention; if your
# Recall plan emits a different rate, adjust RECALL_RATE and the WS handler.
RECALL_RATE = 16000
OPENAI_RATE = 24000
PCM_WIDTH = 2

RECALL_API_BASE = "https://api.recall.ai"

# Per-bot state. Keyed by bot_id; replaced with Redis or similar for
# multi-replica deployments. Each entry tracks:
#   transcript_path: Path to transcript-<bot_id>.txt
#   languages:       list of ISO-639-1 hints (drives summarization prompt)
#   created_at:      ISO timestamp
#   ended:           bool — set when /recall-events sees bot.done/bot.left
_meetings: dict[str, dict[str, Any]] = {}

# Default summarization system prompt. Override via SUMMARIZE_PROMPT_FILE.
_DEFAULT_SUMMARIZE_PROMPT = """\
You are a meticulous meeting minutes writer. You will receive a raw
transcript of a meeting (timestamped lines, speaker labels where available)
and must produce a structured Markdown minutes document.

Output exactly these sections, in this order:

# Meeting Minutes

## Summary
3 to 5 sentences. Plain English, no jargon, no filler.

## Topics Discussed
- Bullet points. One topic per bullet, in the order they came up.
- Group related sub-points under their parent bullet when sensible.

## Decisions
- One bullet per concrete decision the participants converged on.
- If no decisions were made, write "_None._".

## Action Items
A Markdown table with three columns: Owner | Action | Due.
If the transcript doesn't name an owner, use "_Unassigned_".
If the transcript doesn't name a due date, use "_TBD_".
If no action items were mentioned, write "_None._" instead of a table.

Style:
- Be concise; the minutes should be skimmable in under a minute.
- Quote participants directly only for decisions or quoted action items.
- Match the meeting's primary language(s). If multilingual, write the
  Summary in the first language listed and translate the rest as needed.
- Do not invent content the transcript doesn't support.
"""


# --------------------------------------------------------------------------- #
# FastAPI app                                                                 #
# --------------------------------------------------------------------------- #


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-redef]
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
        "meeting-minutes started | transcribe=%s | summarize=%s | "
        "public_ws=%s | profile=%s",
        OPENAI_MODEL_TRANSCRIBE,
        OPENAI_MODEL_SUMMARIZE,
        PUBLIC_WS_URL,
        LATENCY_PROFILE,
    )
    yield
    log.info("meeting-minutes stopped")


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "transcribe_model": OPENAI_MODEL_TRANSCRIBE,
        "summarize_model": OPENAI_MODEL_SUMMARIZE,
        "profile": LATENCY_PROFILE,
        "language": LANGUAGE or None,
        "active_meetings": list(_meetings.keys()),
    }


# --------------------------------------------------------------------------- #
# /create-bot                                                                 #
# --------------------------------------------------------------------------- #


class CreateBotRequest(BaseModel):
    meeting_url: str
    bot_name: str | None = "AI Minutes"
    languages: list[str] | None = None


@app.post("/create-bot")
async def create_bot(req: CreateBotRequest) -> dict[str, Any]:
    """Dispatch a Recall bot configured to stream audio to /recall-audio.

    [UNCONFIRMED] exact Recall.ai field names. Body shape below mirrors
    voice-agent-realtime/examples/meeting-bot.py — adjust per your plan.
    """
    api_key = _require_env("RECALL_API_KEY")
    body: dict[str, Any] = {
        "meeting_url": req.meeting_url,
        "bot_name": req.bot_name or "AI Minutes",
        # [UNCONFIRMED] — Recall has used `real_time_media` and
        # `real_time_transcription` historically. We want the raw-audio
        # stream here (we do our own transcription via OpenAI), so the
        # `real_time_media` shape is the right one.
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
        bot_id = str(data.get("id") or "")
        if bot_id:
            _meetings[bot_id] = {
                "transcript_path": Path(f"transcript-{bot_id}.txt"),
                "minutes_path": Path(f"minutes-{bot_id}.md"),
                "languages": req.languages or (
                    [LANGUAGE] if LANGUAGE else []
                ),
                "created_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "ended": False,
            }
            # Initialize transcript file with a header line so the user can
            # see something even if the WS never receives audio.
            try:
                with open(_meetings[bot_id]["transcript_path"], "w") as fh:
                    fh.write(
                        f"# meeting-minutes transcript bot_id={bot_id} "
                        f"created={_meetings[bot_id]['created_at']}\n\n"
                    )
            except Exception:
                log.exception("[recall] failed to init transcript file")
            log.info(
                "[recall] bot created id=%s -> %s",
                bot_id,
                _meetings[bot_id]["transcript_path"],
            )
        return data


# --------------------------------------------------------------------------- #
# Audio resampling                                                            #
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


# --------------------------------------------------------------------------- #
# OpenAI transcription session bootstrap                                      #
# --------------------------------------------------------------------------- #


def vad_for_profile(profile: str) -> dict[str, Any]:
    """Same VAD mapping as the other examples in this skill."""
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
    return {
        "type": "server_vad",
        "threshold": 0.5,
        "prefix_padding_ms": 300,
        "silence_duration_ms": 500,
    }


async def _open_openai_ws() -> ClientConnection:
    """Connect to OpenAI transcription endpoint, retrying with FALLBACK URL."""
    headers = [
        ("Authorization", f"Bearer {_require_env('OPENAI_API_KEY')}"),
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
        except Exception as e:  # noqa: BLE001 — fall through
            last_err = e
            log.warning("[openai] connect failed for %s: %s", url, e)
    assert last_err is not None
    raise last_err


async def _send_session_update(ws: ClientConnection) -> None:
    transcription_cfg: dict[str, Any] = {"model": OPENAI_MODEL_TRANSCRIBE}
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
    await ws.send(
        json.dumps({"type": "session.update", "session": session_obj})
    )
    log.info(
        "[openai] session.update sent | profile=%s | language=%s",
        LATENCY_PROFILE,
        LANGUAGE or "<auto>",
    )


def _extract_final_text(evt: dict[str, Any]) -> str:
    """Pull finalized text from a .completed event (probe known field names)."""
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


# --------------------------------------------------------------------------- #
# /recall-audio — live transcription path                                     #
# --------------------------------------------------------------------------- #


@app.websocket("/recall-audio")
async def recall_audio(ws_recall: WebSocket) -> None:
    """Recall -> us -> OpenAI -> per-meeting transcript-<bot_id>.txt."""
    await ws_recall.accept()
    log.info("[recall] audio WS accepted")

    bot_id: str | None = None
    in_state = ResampleState()  # Recall 16k -> OpenAI 24k
    ws_oai: ClientConnection | None = None
    # current speaker label (from the last metadata frame that carried one)
    current_speaker: str = "<unknown>"
    # item_id -> running speaker label captured at first-delta time
    item_speakers: dict[str, str] = {}

    def _meeting() -> dict[str, Any] | None:
        if not bot_id:
            return None
        return _meetings.get(bot_id)

    def _append_transcript_line(speaker: str, text: str) -> None:
        m = _meeting()
        if not m:
            return
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with open(m["transcript_path"], "a", encoding="utf-8") as fh:
                fh.write(f"[{ts}] {speaker}: {text}\n")
        except Exception:
            log.exception("[recall] failed to append transcript line")

    async def pump_recall_to_openai() -> None:
        """Read Recall frames (JSON metadata or binary PCM) -> OpenAI."""
        nonlocal bot_id, ws_oai, current_speaker
        try:
            while True:
                msg = await ws_recall.receive()
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
                    etype = evt.get("event") or evt.get("type")
                    if etype:
                        log.debug("[recall] frame type=%s", etype)
                    # [UNCONFIRMED] Recall metadata shape; defensively read
                    # a handful of likely fields.
                    incoming_bot = evt.get("bot_id")
                    if incoming_bot and bot_id is None:
                        bot_id = str(incoming_bot)
                        log.info("[recall] bound bot_id=%s", bot_id)
                    speaker_field = (
                        evt.get("participant")
                        or evt.get("speaker")
                        or (evt.get("data") or {}).get("participant")
                    )
                    if isinstance(speaker_field, dict):
                        current_speaker = (
                            speaker_field.get("name")
                            or speaker_field.get("id")
                            or current_speaker
                        )
                    elif isinstance(speaker_field, str):
                        current_speaker = speaker_field
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
                    await _send_session_update(ws_oai)

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
        finally:
            # Commit pending audio so the server emits a final .completed.
            if ws_oai is not None:
                try:
                    await ws_oai.send(
                        json.dumps({"type": "input_audio_buffer.commit"})
                    )
                except Exception:
                    pass

    async def pump_openai_to_transcript() -> None:
        """Read OpenAI events, append finalized segments to transcript file."""
        nonlocal ws_oai
        # Wait until the OpenAI WS is open (kicked off by first audio frame).
        while ws_oai is None:
            await asyncio.sleep(0.05)
        try:
            async for raw in ws_oai:
                try:
                    evt = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(evt, dict):
                    continue
                etype = str(evt.get("type", ""))

                if etype.endswith(".transcription.delta") or (
                    etype.endswith(".delta") and "transcript" in etype
                ):
                    item_id = str(evt.get("item_id", ""))
                    if item_id and item_id not in item_speakers:
                        # Lock in the speaker label at first-delta time so
                        # speaker swaps mid-utterance don't rewrite history.
                        item_speakers[item_id] = current_speaker
                elif etype.endswith(".completed") and (
                    "transcription" in etype or "transcript" in etype
                ):
                    item_id = str(evt.get("item_id", ""))
                    final_text = _extract_final_text(evt)
                    speaker = item_speakers.pop(item_id, current_speaker)
                    if final_text:
                        log.info(
                            "[openai] completed item=%s speaker=%s text=%r",
                            item_id,
                            speaker,
                            final_text[:80],
                        )
                        _append_transcript_line(speaker, final_text)
                elif etype == "session.created":
                    log.info("[openai] session.created")
                elif etype == "session.updated":
                    log.info("[openai] session.updated")
                elif etype in ("error", "invalid_request_error") or (
                    etype.endswith(".error")
                ):
                    log.error("[openai] error: %s", evt)
                else:
                    log.debug("[openai] %s", etype)
        except websockets.ConnectionClosed:
            log.info("[openai] closed")
        except Exception:
            log.exception("[openai->transcript] pump error")

    try:
        t_in = asyncio.create_task(pump_recall_to_openai())
        t_out = asyncio.create_task(pump_openai_to_transcript())
        await asyncio.gather(t_in, t_out)
    finally:
        if ws_oai is not None:
            try:
                await ws_oai.close()
            except Exception:
                pass
        log.info("[bridge] cleanup done | bot_id=%s", bot_id)
        # Trigger summarization if the lifecycle WS hasn't already.
        if bot_id and bot_id in _meetings and not _meetings[bot_id]["ended"]:
            _meetings[bot_id]["ended"] = True
            asyncio.create_task(_summarize_meeting(bot_id))


# --------------------------------------------------------------------------- #
# /recall-events — bot lifecycle                                              #
# --------------------------------------------------------------------------- #


@app.websocket("/recall-events")
async def recall_events(ws: WebSocket) -> None:
    """Recall lifecycle events. On bot.done/bot.left, fire summarization."""
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
            event_name = str(
                evt.get("event") or evt.get("type") or ""
            ).lower()
            log.info("[recall] event %s", event_name or evt)
            bot_id = str(evt.get("bot_id") or "")
            if event_name in ("bot.done", "bot.left") and bot_id in _meetings:
                if not _meetings[bot_id]["ended"]:
                    _meetings[bot_id]["ended"] = True
                    asyncio.create_task(_summarize_meeting(bot_id))
    except WebSocketDisconnect:
        log.info("[recall] events WS disconnected")


# --------------------------------------------------------------------------- #
# Post-meeting summarization                                                  #
# --------------------------------------------------------------------------- #


def _load_summarize_prompt() -> str:
    if SUMMARIZE_PROMPT_FILE:
        try:
            return Path(SUMMARIZE_PROMPT_FILE).read_text(encoding="utf-8")
        except Exception:
            log.exception(
                "[summarize] could not read prompt file %s; "
                "falling back to default",
                SUMMARIZE_PROMPT_FILE,
            )
    return _DEFAULT_SUMMARIZE_PROMPT


async def _summarize_meeting(bot_id: str) -> None:
    """Read the transcript, ask the summarizer, write minutes-<bot_id>.md."""
    m = _meetings.get(bot_id)
    if not m:
        log.warning("[summarize] no meeting state for bot_id=%s", bot_id)
        return

    transcript_path: Path = m["transcript_path"]
    minutes_path: Path = m["minutes_path"]
    languages: list[str] = m.get("languages") or []

    try:
        transcript = transcript_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        log.warning(
            "[summarize] transcript missing for bot_id=%s (path=%s)",
            bot_id,
            transcript_path,
        )
        return

    # Strip the header line(s) so the model isn't distracted by metadata.
    payload_lines = [
        ln for ln in transcript.splitlines() if not ln.startswith("#")
    ]
    payload = "\n".join(payload_lines).strip()

    if not payload:
        log.info(
            "[summarize] empty transcript for bot_id=%s — skipping summary",
            bot_id,
        )
        try:
            minutes_path.write_text(
                "# Meeting Minutes\n\n_Transcript was empty — "
                "no minutes generated._\n",
                encoding="utf-8",
            )
        except Exception:
            log.exception("[summarize] failed to write empty-minutes stub")
        return

    system_prompt = _load_summarize_prompt()
    user_prompt = _build_user_prompt(payload, languages)

    body = {
        "model": OPENAI_MODEL_SUMMARIZE,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
    }
    headers = {
        "Authorization": f"Bearer {_require_env('OPENAI_API_KEY')}",
        "Content-Type": "application/json",
    }

    log.info(
        "[summarize] bot_id=%s | transcript=%d chars | model=%s",
        bot_id,
        len(payload),
        OPENAI_MODEL_SUMMARIZE,
    )
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=120.0) as http:
            resp = await http.post(
                OPENAI_CHAT_URL,
                headers=headers,
                content=json.dumps(body),
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        log.exception(
            "[summarize] chat-completions call failed for bot_id=%s", bot_id
        )
        return

    try:
        minutes_md = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        log.error("[summarize] unexpected chat-completions shape: %s", data)
        return

    try:
        minutes_path.write_text(minutes_md, encoding="utf-8")
        log.info(
            "[summarize] minutes written to %s | elapsed=%.2fs",
            minutes_path,
            time.monotonic() - t0,
        )
    except Exception:
        log.exception(
            "[summarize] failed to write minutes for bot_id=%s", bot_id
        )


def _build_user_prompt(transcript: str, languages: list[str]) -> str:
    """Wrap the raw transcript with a header the summarizer can use."""
    lang_hint = ""
    if languages:
        lang_hint = f"Primary languages: {', '.join(languages)}.\n"
    return (
        f"{lang_hint}"
        f"Raw transcript follows (timestamped lines in the form "
        f"`[ISO_TS] Speaker: text`):\n\n"
        f"---\n"
        f"{transcript}\n"
        f"---\n"
    )


# --------------------------------------------------------------------------- #
# Entrypoint hint                                                             #
# --------------------------------------------------------------------------- #


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "meeting-minutes:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        log_level="info",
    )
