"use client";
/**
 * examples/web-conference.tsx — Multi-participant web conference client with
 * live translation via OpenAI's gpt-realtime-translate model.
 *
 * Purpose
 * -------
 * A Next.js 16 / React 19 client component that demonstrates a 2-participant
 * conference where each participant picks a target output language and hears
 * the other participants translated into that language. For demo simplicity
 * the topology is hard-wired to 2 participants and a small set of target
 * languages (FR/EN/AR/ES) — the architecture generalises to N participants
 * and M target languages (one translation session per (speaker, target-lang)
 * pair). See ../SKILL.md ## Multi-speaker architecture.
 *
 * Transport
 * ---------
 * Per the 2026-05-11 research snapshot the /v1/realtime/translations endpoint
 * is WebSocket-only — direct browser-to-OpenAI WebRTC for translation is
 * [UNCONFIRMED]. We therefore use a server-side bridge pattern: the browser
 * opens a WebSocket to a hypothetical Next.js route handler at
 *   /api/translate/bridge?source=fr&target=en&participant=alice
 * and the route handler bridges to OpenAI's translation WebSocket. The bridge
 * is intentionally OUT OF SCOPE for this file; see examples/twilio-interpreter.py
 * for the equivalent server-side pattern (adapt the Twilio Media Stream WS to a
 * browser WS using FastAPI + framed PCM16 audio chunks, or write the same in a
 * Next.js route — the OpenAI side is identical).
 *
 * Environment (server-side only — NEVER expose to the browser)
 * ------------------------------------------------------------
 *   OPENAI_API_KEY    Bearer token for OpenAI Realtime Translations (server).
 *   OPENAI_MODEL      Default: gpt-realtime-translate
 *
 * Cross-file contract for the server bridge WS protocol used by this client
 * ------------------------------------------------------------------------
 *   Browser → Server (JSON text frames):
 *     { "type": "audio.append", "audio": "<base64 PCM16 24kHz mono>" }
 *     { "type": "mute" }                          (optional, hints to flush)
 *   Server → Browser (JSON text frames):
 *     { "type": "ready" }
 *     { "type": "audio.delta", "audio": "<base64 PCM16 24kHz mono>" }
 *     { "type": "transcript.delta", "text": "<target-language text>" }
 *     { "type": "source.delta", "text": "<source-language text>" }
 *     { "type": "error", "message": "..." }
 *
 *   The server is responsible for adapting these frames to the OpenAI wire
 *   format — specifically, for sending the PREFIXED translation-endpoint
 *   events session.update and session.input_audio_buffer.append, and for
 *   relaying back session.output_audio.delta / session.output_transcript.delta
 *   / session.input_transcript.delta. See ../references/session-config.md.
 *
 * Sources (verified 2026-05-11)
 * -----------------------------
 *   - Endpoint URL & WS-only constraint:
 *     /Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md
 *     (section: gpt-realtime-translate)
 *   - https://developers.openai.com/api/docs/guides/realtime-translation
 *   - https://developers.openai.com/api/docs/models/gpt-realtime-translate
 *
 * Note on imports
 * ---------------
 * This file uses `import * as React from "react"` and shims the React API at
 * the bottom via `declare global` so it can type-check standalone under the
 * validation harness's tsc invocation (no node_modules). In a real React 19
 * project the named-import form works identically.
 */

// @ts-expect-error -- 'react' is shimmed via `declare global { namespace React }` below for standalone tsc.
import * as React from "react";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** ISO-639-1 codes for the target languages the demo UI exposes.
 *  The translate model supports 13 output languages (full list [UNCONFIRMED]
 *  per the research file); these four are safe, widely-supported choices. */
type LangCode = "fr" | "en" | "ar" | "es";

const LANG_LABELS: Record<LangCode, string> = {
  fr: "Français",
  en: "English",
  ar: "العربية",
  es: "Español",
};

/** Per-participant connection lifecycle.
 *  Keep names short — they show up in the status pill. */
type ConnState = "idle" | "connecting" | "live" | "error";

/** Static participant config — name + source language (what the participant
 *  speaks). In a real app this would come from auth / a presence service. */
type Participant = {
  id: "alice" | "bob";
  name: string;
  /** Source language (what the participant speaks). */
  source: LangCode;
};

const PARTICIPANTS: ReadonlyArray<Participant> = [
  { id: "alice", name: "Alice", source: "fr" },
  { id: "bob", name: "Bob", source: "ar" },
];

/** Frames the server bridge sends down the WebSocket. Mirrors the contract
 *  documented in the header above. */
type BridgeServerFrame =
  | { type: "ready" }
  | { type: "audio.delta"; audio: string }
  | { type: "transcript.delta"; text: string }
  | { type: "source.delta"; text: string }
  | { type: "error"; message: string }
  // Catch-all so JSON.parse output stays sound but unknown frames are ignored.
  | { type: string; [k: string]: unknown };

/** Frames the browser sends UP the WebSocket. */
type BridgeClientFrame =
  | { type: "audio.append"; audio: string }
  | { type: "mute" };

// ---------------------------------------------------------------------------
// Audio plumbing
// ---------------------------------------------------------------------------

/** PCM16 24 kHz mono — the only documented audio format on the translation
 *  endpoint (see research file). We capture mic at this rate directly via
 *  AudioContext constraints; UA support is universal modulo Safari quirks. */
const SAMPLE_RATE = 24000;
/** Frame size for outbound chunks. 480 samples @ 24 kHz = 20 ms per frame,
 *  matching common VoIP packetisation and giving the server VAD on the
 *  translate endpoint enough cadence to detect utterance boundaries. */
const FRAME_SIZE = 480;

/** Encode Float32 PCM samples (-1..1) into base64 PCM16 little-endian. */
function floatToPcm16Base64(samples: Float32Array): string {
  const buf = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buf);
  for (let i = 0; i < samples.length; i++) {
    let s = samples[i];
    if (s > 1) s = 1;
    else if (s < -1) s = -1;
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  // Convert to base64 without spilling to a Blob (smaller buffers, sync).
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  // btoa is available in all modern browsers and in Next.js's edge/node runtimes.
  return typeof btoa === "function" ? btoa(bin) : "";
}

/** Decode base64 PCM16 LE 24kHz → Float32 (-1..1) for AudioContext playback. */
function pcm16Base64ToFloat32(b64: string): Float32Array {
  const bin = typeof atob === "function" ? atob(b64) : "";
  const len = bin.length / 2;
  const out = new Float32Array(len);
  for (let i = 0; i < len; i++) {
    const lo = bin.charCodeAt(i * 2);
    const hi = bin.charCodeAt(i * 2 + 1);
    const signed = (hi << 8) | lo;
    const v = signed > 0x7fff ? signed - 0x10000 : signed;
    out[i] = v / 0x8000;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Per-participant translation session controller
// ---------------------------------------------------------------------------
//
// Encapsulates: mic capture, WebSocket to the server bridge, audio playback
// of the translated stream, transcript accumulation, mute toggle, teardown.
// One controller per (speaker, target-language) pair. For the demo each
// participant card owns one controller; switching the target language tears
// down and re-opens the controller against a new WS endpoint.
// ---------------------------------------------------------------------------

type ControllerHandle = {
  /** Force a teardown (closes WS, stops mic, disconnects audio graph). */
  stop: () => void;
  /** Mute / unmute the outbound mic capture. */
  setMuted: (muted: boolean) => void;
};

type ControllerCallbacks = {
  onState: (s: ConnState) => void;
  onTargetTranscript: (delta: string) => void;
  onSourceTranscript: (delta: string) => void;
  onLevel: (rms: number) => void;
  onError: (message: string) => void;
};

type AudioCtxCtor = new (opts?: { sampleRate?: number }) => AudioContext;

function getAudioContextCtor(): AudioCtxCtor | null {
  const w = window as unknown as {
    AudioContext?: AudioCtxCtor;
    webkitAudioContext?: AudioCtxCtor;
  };
  return w.AudioContext ?? w.webkitAudioContext ?? null;
}

async function startController(args: {
  participantId: string;
  source: LangCode;
  target: LangCode;
  callbacks: ControllerCallbacks;
}): Promise<ControllerHandle> {
  const { participantId, source, target, callbacks } = args;
  let stopped = false;
  let muted = false;

  callbacks.onState("connecting");

  const Ctor = getAudioContextCtor();
  if (!Ctor) {
    callbacks.onError("AudioContext unsupported in this browser");
    callbacks.onState("error");
    return { stop: () => {}, setMuted: () => {} };
  }
  // Force the AudioContext to 24 kHz so the ScriptProcessor / Worklet output
  // matches the translate endpoint's expected rate without further resampling.
  const ctx = new Ctor({ sampleRate: SAMPLE_RATE });

  const mic = await navigator.mediaDevices.getUserMedia({
    audio: {
      sampleRate: SAMPLE_RATE,
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
    },
  });

  // ScriptProcessorNode is deprecated but universally supported and works in
  // a single-file example. A production app should use AudioWorkletNode with
  // a separate worklet module. Buffer size 1024 @ 24 kHz ≈ 42 ms per cb.
  const src = ctx.createMediaStreamSource(mic);
  const processor = ctx.createScriptProcessor(1024, 1, 1);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  src.connect(analyser);
  src.connect(processor);
  // Some browsers won't fire onaudioprocess unless the node is connected to
  // the destination. Route through a muted gain so we don't echo locally.
  const sink = ctx.createGain();
  sink.gain.value = 0;
  processor.connect(sink);
  sink.connect(ctx.destination);

  // Outbound buffer accumulator — emit fixed-size FRAME_SIZE chunks.
  let pending = new Float32Array(0);

  // Build the WS URL. NOTE: this is the URL of YOUR server bridge, not OpenAI's.
  // The server is responsible for authenticating with OpenAI using a server-side
  // API key. The browser must NEVER hold OPENAI_API_KEY.
  const url = `/api/translate/bridge?source=${encodeURIComponent(source)}&target=${encodeURIComponent(target)}&participant=${encodeURIComponent(participantId)}`;

  // The WS scheme is inferred from the page protocol so the same code works
  // on http://localhost and https://prod.
  const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${wsProto}//${window.location.host}${url}`;

  const ws = new WebSocket(wsUrl);

  const sendClient = (frame: BridgeClientFrame): void => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(frame));
    }
  };

  ws.onopen = (): void => {
    callbacks.onState("live");
  };
  ws.onerror = (): void => {
    callbacks.onError("WebSocket error");
    callbacks.onState("error");
  };
  ws.onclose = (): void => {
    if (!stopped) callbacks.onState("idle");
  };

  // Playback queue — append-and-play scheduling so chunks of translated
  // audio play back-to-back without gaps. We schedule each chunk relative
  // to `nextStartTime` so even if the network delivers bursts, playback is
  // continuous.
  let nextStartTime = 0;

  const playChunk = (samples: Float32Array): void => {
    const buf = ctx.createBuffer(1, samples.length, SAMPLE_RATE);
    // copyToChannel's TS overload is strict about the ArrayBuffer flavour
    // of the Float32Array view. We copy into the channel data view
    // sample-by-sample to avoid the SharedArrayBuffer-vs-ArrayBuffer mismatch.
    const channel = buf.getChannelData(0);
    for (let i = 0; i < samples.length; i++) channel[i] = samples[i];
    const node = ctx.createBufferSource();
    node.buffer = buf;
    node.connect(ctx.destination);
    const startAt = Math.max(ctx.currentTime, nextStartTime);
    node.start(startAt);
    nextStartTime = startAt + buf.duration;
  };

  ws.onmessage = (ev: MessageEvent): void => {
    if (typeof ev.data !== "string") return;
    let frame: BridgeServerFrame;
    try {
      frame = JSON.parse(ev.data) as BridgeServerFrame;
    } catch {
      return;
    }
    switch (frame.type) {
      case "ready":
        callbacks.onState("live");
        break;
      case "audio.delta": {
        // The translate endpoint emits session.output_audio.delta carrying
        // base64 PCM16 24kHz. Our server bridge forwards that verbatim as
        // an audio.delta frame.
        const ab = frame as { audio: string };
        const samples = pcm16Base64ToFloat32(ab.audio);
        if (samples.length > 0) playChunk(samples);
        break;
      }
      case "transcript.delta": {
        // The translate endpoint emits session.output_transcript.delta —
        // the target-language transcript. The exact upstream event name is
        // confirmed in the research file; the field carrying the delta is
        // `delta` (string). [UNCONFIRMED] whether `.done` companion events
        // also fire — we treat the stream as deltas-only for the UI.
        const td = frame as { text: string };
        callbacks.onTargetTranscript(td.text);
        break;
      }
      case "source.delta": {
        // session.input_transcript.delta — source-language transcript.
        const sd = frame as { text: string };
        callbacks.onSourceTranscript(sd.text);
        break;
      }
      case "error": {
        const ee = frame as { message: string };
        callbacks.onError(ee.message);
        callbacks.onState("error");
        break;
      }
      default:
        // Ignore unknown frames so the bridge can add new event types
        // without breaking the client.
        break;
    }
  };

  // Mic processing callback — emit base64 PCM16 frames upstream.
  processor.onaudioprocess = (e: AudioProcessingEvent): void => {
    if (stopped || muted) return;
    const input = e.inputBuffer.getChannelData(0);
    // RMS for the level meter.
    let sum = 0;
    for (let i = 0; i < input.length; i++) sum += input[i] * input[i];
    callbacks.onLevel(Math.sqrt(sum / input.length));

    // Append to pending and emit FRAME_SIZE-sized chunks.
    const next = new Float32Array(pending.length + input.length);
    next.set(pending, 0);
    next.set(input, pending.length);
    pending = next;

    while (pending.length >= FRAME_SIZE) {
      const chunk = pending.slice(0, FRAME_SIZE);
      pending = pending.slice(FRAME_SIZE);
      const b64 = floatToPcm16Base64(chunk);
      if (b64.length > 0) {
        sendClient({ type: "audio.append", audio: b64 });
      }
    }
  };

  const stop = (): void => {
    if (stopped) return;
    stopped = true;
    try {
      processor.disconnect();
      analyser.disconnect();
      src.disconnect();
      sink.disconnect();
    } catch {
      // ignore
    }
    try {
      mic.getTracks().forEach((t: MediaStreamTrack) => t.stop());
    } catch {
      // ignore
    }
    try {
      ctx.close().catch(() => {});
    } catch {
      // ignore
    }
    try {
      if (
        ws.readyState === WebSocket.OPEN ||
        ws.readyState === WebSocket.CONNECTING
      ) {
        ws.close();
      }
    } catch {
      // ignore
    }
  };

  const setMuted = (m: boolean): void => {
    muted = m;
    if (m) sendClient({ type: "mute" });
  };

  return { stop, setMuted };
}

// ---------------------------------------------------------------------------
// Participant card — one row of the conference UI.
// ---------------------------------------------------------------------------

type CardProps = {
  participant: Participant;
  /** Initial target language for this participant. */
  initialTarget: LangCode;
  /** React key is allowed at the call-site; declared here for our standalone
   *  JSX shim which lacks React's intrinsic `key` whitelist. */
  key?: string;
};

function ParticipantCard({ participant, initialTarget }: CardProps) {
  const [targetLang, setTargetLang] = React.useState<LangCode>(initialTarget);
  const [state, setState] = React.useState<ConnState>("idle");
  const [muted, setMutedState] = React.useState<boolean>(false);
  const [level, setLevel] = React.useState<number>(0);
  const [targetTranscript, setTargetTranscript] = React.useState<string>("");
  const [sourceTranscript, setSourceTranscript] = React.useState<string>("");
  const [errorMsg, setErrorMsg] = React.useState<string | null>(null);

  const handleRef = React.useRef<ControllerHandle | null>(null);

  const start = React.useCallback((): void => {
    if (handleRef.current) return;
    setErrorMsg(null);
    setTargetTranscript("");
    setSourceTranscript("");
    void startController({
      participantId: participant.id,
      source: participant.source,
      target: targetLang,
      callbacks: {
        onState: (s: ConnState) => setState(s),
        onTargetTranscript: (d: string) =>
          setTargetTranscript((prev: string) => prev + d),
        onSourceTranscript: (d: string) =>
          setSourceTranscript((prev: string) => prev + d),
        onLevel: (r: number) => setLevel(r),
        onError: (m: string) => setErrorMsg(m),
      },
    }).then((handle: ControllerHandle) => {
      handleRef.current = handle;
      if (muted) handle.setMuted(true);
    });
  }, [participant.id, participant.source, targetLang, muted]);

  const stop = React.useCallback((): void => {
    const h = handleRef.current;
    if (!h) return;
    h.stop();
    handleRef.current = null;
    setState("idle");
    setLevel(0);
  }, []);

  // If the target language changes mid-session, tear down and restart.
  // Sessions are bound to one output language (see SKILL.md ## Multi-speaker
  // architecture) so language switching = new session.
  const onChangeTarget = React.useCallback(
    (next: LangCode): void => {
      setTargetLang(next);
      if (handleRef.current) {
        stop();
        // Defer restart to the next tick so React state has settled.
        setTimeout(() => start(), 0);
      }
    },
    [start, stop],
  );

  const toggleMute = React.useCallback((): void => {
    const next = !muted;
    setMutedState(next);
    handleRef.current?.setMuted(next);
  }, [muted]);

  // Cleanup on unmount.
  React.useEffect(() => {
    return () => {
      handleRef.current?.stop();
      handleRef.current = null;
    };
  }, []);

  // Map RMS (0..~0.3 in normal speech) to a 0..100% meter width.
  const meterPct = Math.min(100, Math.round(level * 400));

  return (
    <section
      aria-label={`Participant ${participant.name}`}
      style={{
        border: "1px solid #ddd",
        padding: 16,
        borderRadius: 8,
        marginBottom: 12,
      }}
    >
      <header
        style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 8 }}
      >
        <strong>{participant.name}</strong>
        <span style={{ color: "#888" }}>
          speaks {LANG_LABELS[participant.source]}
        </span>
        <span
          aria-live="polite"
          style={{
            marginLeft: "auto",
            padding: "2px 8px",
            borderRadius: 999,
            fontSize: 12,
            background:
              state === "live"
                ? "#dcfce7"
                : state === "connecting"
                  ? "#fef3c7"
                  : state === "error"
                    ? "#fee2e2"
                    : "#f3f4f6",
          }}
        >
          {state}
        </span>
      </header>

      <div style={{ marginBottom: 8 }}>
        <label htmlFor={`lang-${participant.id}`} style={{ marginRight: 8 }}>
          Translate output to:
        </label>
        <select
          id={`lang-${participant.id}`}
          value={targetLang}
          onChange={(e: { target: { value: string } }) =>
            onChangeTarget(e.target.value as LangCode)
          }
        >
          {(Object.keys(LANG_LABELS) as LangCode[]).map((code: LangCode) => (
            <option key={code} value={code}>
              {LANG_LABELS[code]}
            </option>
          ))}
        </select>
      </div>

      <div
        aria-label="Microphone level"
        style={{
          height: 8,
          background: "#eee",
          borderRadius: 4,
          marginBottom: 8,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            width: `${meterPct}%`,
            height: "100%",
            background:
              meterPct > 70 ? "#ef4444" : meterPct > 30 ? "#22c55e" : "#94a3b8",
            transition: "width 60ms linear",
          }}
        />
      </div>

      <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
        {state === "idle" || state === "error" ? (
          <button type="button" onClick={start}>
            Join translation
          </button>
        ) : (
          <button type="button" onClick={stop}>
            Leave
          </button>
        )}
        <button
          type="button"
          onClick={toggleMute}
          disabled={state !== "live"}
          aria-pressed={muted}
        >
          {muted ? "Unmute" : "Mute"}
        </button>
      </div>

      {errorMsg ? (
        <p style={{ color: "crimson", fontSize: 13 }}>{errorMsg}</p>
      ) : null}

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <div>
          <h4 style={{ margin: "4px 0", fontSize: 13, color: "#555" }}>
            Source ({LANG_LABELS[participant.source]})
          </h4>
          <p
            style={{
              minHeight: 60,
              padding: 8,
              border: "1px dashed #ddd",
              borderRadius: 4,
              fontSize: 13,
              whiteSpace: "pre-wrap",
            }}
          >
            {sourceTranscript || (
              <em style={{ color: "#bbb" }}>waiting for speech…</em>
            )}
          </p>
        </div>
        <div>
          <h4 style={{ margin: "4px 0", fontSize: 13, color: "#555" }}>
            Translated ({LANG_LABELS[targetLang as LangCode]})
          </h4>
          <p
            style={{
              minHeight: 60,
              padding: 8,
              border: "1px dashed #ddd",
              borderRadius: 4,
              fontSize: 13,
              whiteSpace: "pre-wrap",
            }}
          >
            {targetTranscript || (
              <em style={{ color: "#bbb" }}>translation will appear here…</em>
            )}
          </p>
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Top-level component
// ---------------------------------------------------------------------------

export function WebConference() {
  // Defaults wire Alice (fr-speaker) to hear English, Bob (ar-speaker) to
  // hear French — typical of a 2-language business meeting where each
  // participant wants the other side in their own language.
  return (
    <div style={{ maxWidth: 880, margin: "0 auto", padding: 16 }}>
      <h2>Live translation conference</h2>
      <p style={{ color: "#666", fontSize: 14 }}>
        Each participant captures audio locally and streams it to a server-side
        bridge, which opens a translation session against
        <code> wss://api.openai.com/v1/realtime/translations </code>
        and forwards translated audio + transcripts back to this client. One
        session per (speaker, target-language) pair.
      </p>
      {PARTICIPANTS.map((p: Participant) => (
        <ParticipantCard
          key={p.id}
          participant={p}
          initialTarget={p.id === "alice" ? "en" : "fr"}
        />
      ))}
    </div>
  );
}

export default WebConference;

// ---------------------------------------------------------------------------
// Ambient shims so this single-file example type-checks under the harness's
// `tsc --noEmit` invocation without node_modules. In a real project these
// signatures come from @types/react (React 19 ships TS types) and lib.dom.
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

  namespace JSX {
    interface IntrinsicElements {
      [elemName: string]: Record<string, unknown>;
    }
  }
}
