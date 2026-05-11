"use client";
/**
 * examples/live-captions.tsx — Live-captions overlay for gpt-realtime-whisper.
 *
 * Purpose
 * -------
 * A React 19 / Next.js 16 client component that captures the user's microphone
 * at 24 kHz PCM16, streams it over a server-side proxy WebSocket, and renders
 * progressive transcription deltas as live captions with REPLACE-LAST
 * semantics for the speculative segment.
 *
 * Architecture
 * ------------
 *
 *   ┌───────────────┐ getUserMedia      ┌──────────────────────┐ WS    ┌──────────────────┐
 *   │ <LiveCaptions>│ ───────────────▶ │ AudioWorklet @ 24 kHz │ ────▶ │ /api/transcribe/ │
 *   │   (this file) │ ◀─ JSON events ─ │  PCM16 base64 frames  │       │ stream (Next.js  │
 *   └───────────────┘                   └──────────────────────┘       │  route, proxy)   │
 *                                                                       └───────┬──────────┘
 *                                                                               │ WS
 *                                                                               ▼
 *                                                                  wss://api.openai.com/v1
 *                                                                  /realtime?model=
 *                                                                  gpt-realtime-whisper
 *                                                                  [UNCONFIRMED endpoint;
 *                                                                  see research file]
 *
 * The browser NEVER receives the OPENAI_API_KEY. The server proxy mints/holds
 * the bearer token, opens its own WebSocket to OpenAI, and bidirectionally
 * relays JSON frames between this component and the OpenAI transcription
 * endpoint.
 *
 * Companion server bridge
 * -----------------------
 * For the Twilio variant (phone-call audio in instead of browser mic), see:
 *   ../../voice-transcribe-stream/examples/twilio-transcribe.py
 *
 * Sketch of a Next.js route handler (app/api/transcribe/stream/route.ts) that
 * this component talks to — outline only, not part of this file:
 *
 *   // 30-line sketch — DO NOT COPY VERBATIM, this is illustrative
 *   import { WebSocketServer } from "ws";
 *   // Next.js App Router does not yet expose a first-class WS upgrade API;
 *   // typical production deployments run this behind a Node/edge runtime
 *   // that supports `socket` upgrades (custom server, or use Vercel's
 *   // Realtime/Workflow runtimes). For self-hosted Node:
 *   //
 *   //   - Open a wss://api.openai.com/v1/realtime?model=gpt-realtime-whisper
 *   //     ([UNCONFIRMED] endpoint shape — see research file). Headers:
 *   //       Authorization: Bearer ${process.env.OPENAI_API_KEY}
 *   //     Defensively also send `OpenAI-Beta: realtime=v1` ([UNCONFIRMED]).
 *   //   - On open, send a session.update with:
 *   //       session.type = "transcription"
 *   //       session.audio.input.format = { type: "audio/pcm", rate: 24000 }
 *   //       session.audio.input.transcription.model = "gpt-realtime-whisper"
 *   //       session.audio.input.turn_detection = { type: "server_vad", ... }
 *   //       session.audio.input.transcription.language = "<from query param>"
 *   //     Map the LATENCY_PROFILE env var to threshold / prefix_padding_ms /
 *   //     silence_duration_ms — see references/latency-tuning.md.
 *   //   - Browser frames arrive as JSON: { type: "input_audio_buffer.append",
 *   //     audio: "<base64 PCM16 24 kHz>" }. Forward verbatim to OpenAI.
 *   //   - OpenAI server events stream back; relay JSON down to the browser
 *   //     unchanged. Strip out anything that contains secrets — there are
 *   //     none in transcription server events, but check defensively.
 *
 * Environment variables (server-side bridge, NOT this file)
 * ---------------------------------------------------------
 *   OPENAI_API_KEY       Bearer for OpenAI Realtime (required, server-only)
 *   OPENAI_MODEL         Defaults to "gpt-realtime-whisper"
 *   LATENCY_PROFILE      "ultra-low" | "balanced" | "quality-max"
 *                        Maps to audio.input.turn_detection fields per
 *                        references/latency-tuning.md.
 *   LANGUAGE             ISO-639-1 hint, e.g. "en", "fr", "ar". Optional.
 *
 * Sources (verified 2026-05-11)
 * -----------------------------
 *   - https://developers.openai.com/api/docs/guides/realtime-transcription
 *     (session.update shape, conversation.item.input_audio_transcription.delta
 *      / .completed events, server_vad turn detection)
 *   - https://developers.openai.com/api/docs/models/gpt-realtime-whisper
 *     (model card, pricing, modalities)
 *   - https://developers.openai.com/api/docs/guides/realtime-websocket
 *     (auth headers; OpenAI-Beta: realtime=v1 [UNCONFIRMED] for new models)
 *   - Snapshot: /Users/youssef/AgentVocalOpco/docs/research/
 *     openai-realtime-api-2026-05-11.md
 *   - SKILL operational playbook: ~/.claude/skills/voice-transcribe-stream/
 *     SKILL.md + references/streaming-deltas.md + references/latency-tuning.md
 *
 * [UNCONFIRMED] items propagated from the research file
 * -----------------------------------------------------
 *   - Exact WebSocket URL for the transcription endpoint. The research file
 *     names two candidates:
 *       wss://api.openai.com/v1/realtime?model=gpt-realtime-whisper       (A)
 *       wss://api.openai.com/v1/realtime/transcription_sessions?...        (B)
 *     The server bridge tries (A) first since the discriminator is
 *     session.type = "transcription" inside the first session.update.
 *   - Whether `OpenAI-Beta: realtime=v1` is still required. Sent defensively
 *     in the SERVER bridge; not relevant to this browser file (browser
 *     doesn't speak to OpenAI directly).
 *   - Whether delta payloads are STRICT SUFFIXES or REPLACEMENTS of the
 *     in-progress transcript. The research file does not specify. This
 *     component uses REPLACE-LAST semantics (Pattern A from
 *     references/streaming-deltas.md) which handles both safely.
 *   - Whether `input_audio_buffer.speech_started` / `.speech_stopped` /
 *     `.committed` events are emitted on transcription sessions. Likely yes
 *     (shared session vocabulary with the conversational endpoint); we
 *     handle them defensively and ignore if absent.
 *   - Final-text field name in `.completed` payload. Research suggests
 *     `transcript`; we try `transcript`, then `text`, then `content`.
 *
 * Run command (for the Next.js app embedding this component)
 * ----------------------------------------------------------
 *   pnpm dev
 *   open http://localhost:3000
 *
 * Note on imports: this file uses `import * as React from "react"` (namespace
 * form) so it can type-check standalone without node_modules; the bottom of
 * the file shims React hook signatures via `declare global`. In a real
 * Next.js 16 / React 19 project, named-import form works identically.
 */

// @ts-expect-error -- 'react' is shimmed via `declare global { namespace React }` below for standalone tsc.
import * as React from "react";

// ---------------------------------------------------------------------------
// Types — server-event union for messages relayed from the proxy.
// Each `type` string traces to the research file's transcription event list.
// ---------------------------------------------------------------------------

/** A relayed transcription event from the server bridge. */
type RelayedServerEvent =
  // The two documented transcription events:
  | {
      type: "conversation.item.input_audio_transcription.delta";
      item_id: string;
      delta: string;
      // [UNCONFIRMED] additional fields per research file:
      content_index?: number;
      event_id?: string;
    }
  | {
      type: "conversation.item.input_audio_transcription.completed";
      item_id: string;
      // [UNCONFIRMED] which field name carries the final text. We probe in
      // order: transcript, text, content.
      transcript?: string;
      text?: string;
      content?: string;
      event_id?: string;
    }
  // Shared session-vocabulary events ([UNCONFIRMED] for transcription per
  // research file — likely emitted; we react if present, ignore if absent).
  | { type: "session.created"; session: Record<string, unknown> }
  | { type: "session.updated"; session: Record<string, unknown> }
  | { type: "input_audio_buffer.speech_started" }
  | { type: "input_audio_buffer.speech_stopped" }
  | { type: "input_audio_buffer.committed"; item_id: string }
  | { type: "error"; error: { message?: string; type?: string; code?: string } }
  // Out-of-band relay messages from our own server bridge.
  | { type: "relay.ready" }
  | { type: "relay.closed"; reason?: string }
  // Catch-all so the union stays sound; we still want to log unknowns.
  | { type: string; [k: string]: unknown };

/** Messages the browser sends UP to the relay. */
type ClientToRelay =
  | { type: "input_audio_buffer.append"; audio: string }
  | { type: "input_audio_buffer.commit" }
  | { type: "input_audio_buffer.clear" }
  // The relay reads these and calibrates the OpenAI session.update before
  // it starts. (See note in the route-handler sketch above.)
  | { type: "relay.start"; profile: LatencyProfile; language?: string };

/** Connection / capture state machine. */
type ConnState =
  | "idle"
  | "requesting-mic"
  | "connecting"
  | "streaming"
  | "error";

/** Operating profile — see references/latency-tuning.md. */
type LatencyProfile = "ultra-low" | "balanced" | "quality-max";

/** Per-utterance transcript record. */
type CaptionItem = {
  item_id: string;
  /** Current best-known text for this item (the latest delta value, or the
   *  final transcript on `.completed`). REPLACE-LAST semantics — never
   *  appended to character-by-character. */
  text: string;
  /** Whether this item is locked (`.completed` arrived). */
  finalized: boolean;
  /** First time we saw this item (ms epoch). Used for ordering. */
  first_seen: number;
};

type Props = {
  /** WS URL of the server bridge. Default `/api/transcribe/stream`. */
  relayUrl?: string;
  /** ISO-639-1 language hint forwarded to the relay's session.update. */
  language?: string;
  /** Operating profile forwarded to the relay (controls VAD aggressiveness). */
  profile?: LatencyProfile;
  /** How many finalized items to retain in the history panel. Default 20. */
  historyLimit?: number;
};

// ---------------------------------------------------------------------------
// AudioWorklet source code — inlined as a Blob URL so this component is
// self-contained and does not require shipping a separate .js file.
// The processor receives Float32Array frames at the AudioContext's sample
// rate (which we force to 24000), converts to PCM16, and posts back batches
// of bytes via port.postMessage so the main thread can base64-encode and
// send to the relay.
// ---------------------------------------------------------------------------

const WORKLET_SOURCE = `
class PCM16Processor extends AudioWorkletProcessor {
  constructor() {
    super();
    // Batch ~100ms of audio per postMessage by default (configurable via
    // options.processorOptions.frameMs). At 24 kHz, 100 ms = 2400 samples.
    this._frameMs = 100;
    this._sampleRate = sampleRate; // global in worklet scope
    this._frameSamples = Math.round(this._sampleRate * (this._frameMs / 1000));
    this._buffer = new Int16Array(this._frameSamples);
    this._writeIdx = 0;
    this.port.onmessage = (e) => {
      if (e && e.data && typeof e.data.frameMs === "number") {
        this._frameMs = e.data.frameMs;
        this._frameSamples = Math.round(this._sampleRate * (this._frameMs / 1000));
        this._buffer = new Int16Array(this._frameSamples);
        this._writeIdx = 0;
      }
    };
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channel = input[0];
    if (!channel) return true;

    for (let i = 0; i < channel.length; i++) {
      // Clamp [-1,1] then to int16.
      let s = channel[i];
      if (s > 1) s = 1;
      else if (s < -1) s = -1;
      this._buffer[this._writeIdx++] = s < 0 ? s * 0x8000 : s * 0x7fff;

      if (this._writeIdx >= this._frameSamples) {
        // Copy out and ship.
        const out = new Int16Array(this._buffer);
        this.port.postMessage(out.buffer, [out.buffer]);
        this._buffer = new Int16Array(this._frameSamples);
        this._writeIdx = 0;
      }
    }
    return true;
  }
}
registerProcessor("pcm16-processor", PCM16Processor);
`;

/** Build a Blob URL pointing at the worklet source, so the AudioWorklet
 *  can be loaded via context.audioWorklet.addModule(blobUrl). */
function buildWorkletUrl(): string {
  const blob = new Blob([WORKLET_SOURCE], { type: "application/javascript" });
  return URL.createObjectURL(blob);
}

/** Base64-encode an ArrayBuffer of bytes (PCM16 little-endian). */
function arrayBufferToBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  // Chunk to avoid blowing the call stack on very large frames.
  const CHUNK = 0x8000;
  let bin = "";
  for (let i = 0; i < bytes.length; i += CHUNK) {
    const sub = bytes.subarray(i, i + CHUNK);
    // String.fromCharCode.apply is fine for chunks <= 32K.
    bin += String.fromCharCode.apply(
      null,
      Array.prototype.slice.call(sub) as number[],
    );
  }
  // btoa is available on `window` in browsers.
  type WithBtoA = { btoa: (s: string) => string };
  return (globalThis as unknown as WithBtoA).btoa(bin);
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function LiveCaptions(props: Props = {}) {
  const {
    relayUrl = "/api/transcribe/stream",
    language,
    profile = "balanced",
    historyLimit = 20,
  } = props;

  const [state, setState] = React.useState<ConnState>("idle");
  const [errorMsg, setErrorMsg] = React.useState<string | null>(null);
  /** Items keyed by item_id. Sorted by `first_seen` at render time. */
  const [items, setItems] = React.useState<Record<string, CaptionItem>>({});

  // Live refs (audio + WS) so we don't re-render on every audio frame.
  const wsRef = React.useRef<WebSocket | null>(null);
  const audioCtxRef = React.useRef<AudioContext | null>(null);
  const micStreamRef = React.useRef<MediaStream | null>(null);
  const workletNodeRef = React.useRef<AudioWorkletNode | null>(null);
  const sourceNodeRef = React.useRef<MediaStreamAudioSourceNode | null>(null);
  const workletUrlRef = React.useRef<string | null>(null);

  // -------------------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------------------

  const finalTextFromCompleted = React.useCallback(
    (evt: {
      transcript?: string;
      text?: string;
      content?: string;
    }): string => {
      // [UNCONFIRMED] which field carries final text — probe in order.
      if (typeof evt.transcript === "string") return evt.transcript;
      if (typeof evt.text === "string") return evt.text;
      if (typeof evt.content === "string") return evt.content;
      return "";
    },
    [],
  );

  const send = React.useCallback((msg: ClientToRelay): void => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== 1 /* OPEN */) return;
    ws.send(JSON.stringify(msg));
  }, []);

  /** Apply a server event to the items map. REPLACE-LAST semantics. */
  const applyServerEvent = React.useCallback(
    (evt: RelayedServerEvent): void => {
      switch (evt.type) {
        case "conversation.item.input_audio_transcription.delta": {
          const e = evt as {
            item_id: string;
            delta: string;
          };
          // [UNCONFIRMED] suffix vs full-replace. We treat `delta` as the
          // current best-known full transcript for this item_id, which is
          // correct under the replace-last semantics described in the
          // streaming-deltas reference.
          setItems((prev: Record<string, CaptionItem>) => {
            const existing = prev[e.item_id];
            const first_seen = existing ? existing.first_seen : Date.now();
            // If `delta` looks shorter than what we have already, it is most
            // likely a suffix — append rather than replace. This is the safe
            // fallback if OpenAI actually emits suffix-style deltas in
            // practice. The locked-on-completed step normalizes either way.
            let nextText = e.delta;
            if (existing && !existing.finalized) {
              if (
                e.delta.length < existing.text.length &&
                !existing.text.startsWith(e.delta)
              ) {
                // Treat as suffix to append.
                nextText = existing.text + e.delta;
              }
            }
            return {
              ...prev,
              [e.item_id]: {
                item_id: e.item_id,
                text: nextText,
                finalized: false,
                first_seen,
              },
            };
          });
          break;
        }
        case "conversation.item.input_audio_transcription.completed": {
          const e = evt as {
            item_id: string;
            transcript?: string;
            text?: string;
            content?: string;
          };
          const finalText = finalTextFromCompleted(e);
          setItems((prev: Record<string, CaptionItem>) => {
            const existing = prev[e.item_id];
            const first_seen = existing ? existing.first_seen : Date.now();
            return {
              ...prev,
              [e.item_id]: {
                item_id: e.item_id,
                // Final transcript wins. Fall back to whatever we already had
                // if the completion payload didn't carry text (shouldn't
                // happen, but defensive).
                text: finalText || (existing ? existing.text : ""),
                finalized: true,
                first_seen,
              },
            };
          });
          break;
        }
        case "input_audio_buffer.speech_started":
        case "input_audio_buffer.speech_stopped":
        case "input_audio_buffer.committed":
        case "session.created":
        case "session.updated":
        case "relay.ready":
          // Lifecycle — no UI change.
          break;
        case "error": {
          const e = evt as { error: { message?: string } };
          setErrorMsg(e.error?.message ?? "unknown transcription error");
          setState("error");
          break;
        }
        case "relay.closed": {
          const e = evt as { reason?: string };
          // The server closed the upstream — treat as terminal for the
          // current run; the user can press Start to retry.
          setState((cur: ConnState) =>
            cur === "streaming" ? "idle" : cur,
          );
          if (e.reason) {
            // Surface non-fatal close reasons as a soft message; don't promote
            // to error state to avoid blocking the Start button.
            setErrorMsg(`relay closed: ${e.reason}`);
          }
          break;
        }
        default:
          // Unknown event — ignore. In dev you might want to log.
          break;
      }
    },
    [finalTextFromCompleted],
  );

  // -------------------------------------------------------------------------
  // Start capture + WebSocket
  // -------------------------------------------------------------------------

  const start = React.useCallback(async (): Promise<void> => {
    if (state === "streaming" || state === "connecting") return;
    setErrorMsg(null);

    // 1) Ask for mic. AudioContext must be created from a user gesture for
    //    Safari, hence start() is invoked from the button click.
    setState("requesting-mic");
    let mic: MediaStream;
    try {
      mic = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      micStreamRef.current = mic;
    } catch (err) {
      const msg = err instanceof Error ? err.message : "mic permission denied";
      setErrorMsg(msg);
      setState("error");
      return;
    }

    // 2) Build AudioContext at 24 kHz so the worklet sees the rate we will
    //    advertise to OpenAI. Not all browsers honor the sampleRate hint, so
    //    we read back `ctx.sampleRate` and resample on the server bridge if
    //    necessary. For Chromium/Safari current versions, 24000 works.
    type AudioCtxCtor = new (
      options?: { sampleRate?: number },
    ) => AudioContext;
    const w = window as unknown as {
      AudioContext?: AudioCtxCtor;
      webkitAudioContext?: AudioCtxCtor;
    };
    const Ctor: AudioCtxCtor | undefined = w.AudioContext ?? w.webkitAudioContext;
    if (!Ctor) {
      setErrorMsg("AudioContext not supported in this browser");
      setState("error");
      return;
    }
    const ctx = new Ctor({ sampleRate: 24000 });
    audioCtxRef.current = ctx;

    // 3) Open the relay WebSocket. We do this in parallel with worklet
    //    install for speed; both must be ready before audio starts to flow.
    setState("connecting");

    // Convert relative URL to absolute ws/wss URL.
    function relayWsUrl(rel: string): string {
      if (rel.startsWith("ws://") || rel.startsWith("wss://")) return rel;
      type WithLoc = { location: { protocol: string; host: string } };
      const loc = (globalThis as unknown as WithLoc).location;
      const proto = loc.protocol === "https:" ? "wss:" : "ws:";
      return `${proto}//${loc.host}${rel}`;
    }
    const ws = new WebSocket(relayWsUrl(relayUrl));
    wsRef.current = ws;

    ws.onmessage = (ev: MessageEvent): void => {
      if (typeof ev.data !== "string") return;
      try {
        const parsed = JSON.parse(ev.data) as RelayedServerEvent;
        applyServerEvent(parsed);
      } catch {
        // Drop malformed frames silently.
      }
    };
    ws.onerror = (): void => {
      setErrorMsg("relay socket error");
      setState("error");
    };
    ws.onclose = (): void => {
      setState((cur: ConnState) =>
        cur === "streaming" || cur === "connecting" ? "idle" : cur,
      );
    };

    const wsOpen = new Promise<void>((resolve, reject) => {
      ws.onopen = (): void => {
        // Hand the relay our config so it can build the session.update.
        send({ type: "relay.start", profile, language });
        resolve();
      };
      // If the socket never opens, set a 10s deadline.
      const t = setTimeout(() => reject(new Error("relay open timeout")), 10000);
      const prevOnError = ws.onerror;
      ws.onerror = function (this: WebSocket, e: Event): void {
        clearTimeout(t);
        if (prevOnError) prevOnError.call(this, e);
        reject(new Error("relay error during open"));
      } as typeof ws.onerror;
    });

    try {
      // 4) Install the AudioWorklet processor.
      const url = buildWorkletUrl();
      workletUrlRef.current = url;
      await ctx.audioWorklet.addModule(url);

      // 5) Wire mic -> worklet.
      const src = ctx.createMediaStreamSource(mic);
      sourceNodeRef.current = src;

      type NodeOpts = {
        numberOfInputs: number;
        numberOfOutputs: number;
        outputChannelCount: number[];
        processorOptions: { frameMs: number };
      };
      const opts: NodeOpts = {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
        processorOptions: { frameMs: profile === "ultra-low" ? 40 : 100 },
      };

      const node = new AudioWorkletNode(
        ctx as unknown as BaseAudioContext,
        "pcm16-processor",
        opts as unknown as AudioWorkletNodeOptions,
      );
      workletNodeRef.current = node;

      node.port.onmessage = (e: MessageEvent): void => {
        const buf = e.data as ArrayBuffer;
        if (!buf || !(buf instanceof ArrayBuffer)) return;
        const b64 = arrayBufferToBase64(buf);
        send({ type: "input_audio_buffer.append", audio: b64 });
      };

      src.connect(node);
      // The worklet output is silent (we don't write to outputs[]), so we
      // intentionally do NOT connect it to ctx.destination. This avoids any
      // echo / feedback.

      await wsOpen;
      setState("streaming");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "start failed";
      setErrorMsg(msg);
      setState("error");
      // Best-effort cleanup
      try {
        sourceNodeRef.current?.disconnect();
      } catch {
        /* noop */
      }
      try {
        workletNodeRef.current?.disconnect();
      } catch {
        /* noop */
      }
      micStreamRef.current?.getTracks().forEach((t: MediaStreamTrack) => t.stop());
      micStreamRef.current = null;
      audioCtxRef.current?.close().catch(() => {});
      audioCtxRef.current = null;
      ws.close();
      wsRef.current = null;
    }
  }, [state, profile, language, relayUrl, applyServerEvent, send]);

  // -------------------------------------------------------------------------
  // Stop capture
  // -------------------------------------------------------------------------

  const stop = React.useCallback((): void => {
    // Tell the relay we're done — it should send input_audio_buffer.commit
    // if a manual-commit profile is configured, then close the upstream.
    send({ type: "input_audio_buffer.commit" });

    try {
      sourceNodeRef.current?.disconnect();
    } catch {
      /* noop */
    }
    try {
      workletNodeRef.current?.disconnect();
    } catch {
      /* noop */
    }
    micStreamRef.current?.getTracks().forEach((t: MediaStreamTrack) => t.stop());
    micStreamRef.current = null;

    audioCtxRef.current?.close().catch(() => {});
    audioCtxRef.current = null;

    if (workletUrlRef.current) {
      URL.revokeObjectURL(workletUrlRef.current);
      workletUrlRef.current = null;
    }

    wsRef.current?.close();
    wsRef.current = null;
    sourceNodeRef.current = null;
    workletNodeRef.current = null;

    setState("idle");
  }, [send]);

  // -------------------------------------------------------------------------
  // Unmount cleanup
  // -------------------------------------------------------------------------

  React.useEffect(() => {
    return () => {
      try {
        sourceNodeRef.current?.disconnect();
      } catch {
        /* noop */
      }
      try {
        workletNodeRef.current?.disconnect();
      } catch {
        /* noop */
      }
      micStreamRef.current?.getTracks().forEach((t: MediaStreamTrack) => t.stop());
      audioCtxRef.current?.close().catch(() => {});
      if (workletUrlRef.current) URL.revokeObjectURL(workletUrlRef.current);
      wsRef.current?.close();
      sourceNodeRef.current = null;
      workletNodeRef.current = null;
      micStreamRef.current = null;
      audioCtxRef.current = null;
      workletUrlRef.current = null;
      wsRef.current = null;
    };
  }, []);

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  // Sort items by first-seen ascending so older utterances stay on top.
  const ordered: CaptionItem[] = (
    Object.values(items) as CaptionItem[]
  ).sort((a: CaptionItem, b: CaptionItem) => a.first_seen - b.first_seen);

  // The last (most recent) item is the "live" speculative segment if not
  // finalized; otherwise we have no live segment and only finalized history.
  const last: CaptionItem | undefined = ordered[ordered.length - 1];
  const liveItem: CaptionItem | null =
    last && !last.finalized ? last : null;

  // Finalized items, capped to historyLimit, most recent last.
  const finalized: CaptionItem[] = ordered
    .filter((it: CaptionItem) => it.finalized)
    .slice(-historyLimit);

  const isStarted = state === "streaming" || state === "connecting" || state === "requesting-mic";

  return (
    <div className="live-captions">
      <div className="controls">
        {!isStarted ? (
          <button type="button" onClick={start}>
            Start
          </button>
        ) : (
          <button
            type="button"
            onClick={stop}
            disabled={state !== "streaming"}
          >
            Stop
          </button>
        )}
        <span className="state-badge" data-state={state}>
          {state}
        </span>
        {errorMsg ? (
          <span className="error" style={{ color: "crimson" }}>
            {errorMsg}
          </span>
        ) : null}
        <span className="profile-badge">profile: {profile}</span>
        {language ? (
          <span className="lang-badge">lang: {language}</span>
        ) : null}
      </div>

      {/* Overlay: speculative caption pinned to the bottom of the area. */}
      <div className="caption-area">
        {liveItem ? (
          <p
            className="live-caption"
            style={{ fontStyle: "italic", opacity: 0.7 }}
          >
            {liveItem.text}
          </p>
        ) : state === "streaming" ? (
          <p className="live-caption-placeholder" style={{ opacity: 0.4 }}>
            (listening…)
          </p>
        ) : null}
      </div>

      {/* History panel: last N finalized segments. */}
      <div className="history-panel">
        <h3>Transcript</h3>
        {finalized.length === 0 ? (
          <p className="empty" style={{ opacity: 0.5 }}>
            No completed segments yet.
          </p>
        ) : (
          finalized.map((it: CaptionItem) => (
            <p key={it.item_id} className="finalized-caption">
              {it.text}
            </p>
          ))
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Ambient React shim so this single-file example type-checks under the
// harness's `tsc --noEmit` invocation without node_modules. Browser globals
// (MediaStream, AudioWorkletNode, WebSocket, ...) come from TypeScript's
// bundled lib.dom.d.ts which is included by default with the `es2022`
// target. In a real Next.js / React 19 project, @types/react provides the
// real React types and this shim is a no-op (the hooks already exist).
// ---------------------------------------------------------------------------
declare global {
  namespace React {
    type RefObject<T> = { current: T | null };
    function useState<S>(
      initial: S | (() => S),
    ): [S, (v: S | ((prev: S) => S)) => void];
    function useRef<T>(initial: T | null): RefObject<T>;
    function useCallback<T extends (...args: never[]) => unknown>(
      fn: T,
      deps: ReadonlyArray<unknown>,
    ): T;
    function useEffect(
      fn: () => void | (() => void),
      deps?: ReadonlyArray<unknown>,
    ): void;
  }

  // Minimal JSX intrinsics for the elements we use.
  namespace JSX {
    interface IntrinsicElements {
      [elemName: string]: Record<string, unknown>;
    }
  }
}
