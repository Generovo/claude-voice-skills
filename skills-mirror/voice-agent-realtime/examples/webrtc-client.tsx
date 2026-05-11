"use client";
/**
 * examples/webrtc-client.tsx — Push-to-talk WebRTC client for OpenAI Realtime.
 *
 * Companion to examples/nextjs-route.ts (POST /api/voice/session).
 *
 * Browser support: Chromium, Safari 16+, Firefox 100+. Requires mic permission.
 *
 * Flow:
 *   1. POST /api/voice/session → { client_secret, webrtc_endpoint, ... }.
 *   2. Create RTCPeerConnection.
 *   3. addTrack(localMicStream) for outbound audio.
 *   4. createDataChannel("oai-events") for client→server JSON events.
 *   5. createOffer / setLocalDescription → POST SDP to webrtc_endpoint with
 *      Authorization: Bearer <client_secret> + Content-Type: application/sdp.
 *   6. Apply the SDP answer via setRemoteDescription.
 *   7. ontrack fires with the remote audio MediaStream — pipe to <audio>.
 *   8. Data channel messages stream JSON events (transcripts, lifecycle).
 *
 * Sources (verified 2026-05-11):
 *   - https://developers.openai.com/api/docs/guides/realtime-webrtc
 *     (oai-events data channel name, /v1/realtime/calls SDP POST)
 *   - https://developers.openai.com/api/docs/guides/realtime-conversations
 *     (event names: response.output_audio_transcript.delta, response.cancel,
 *      input_audio_buffer.commit, response.create, response.done, ...)
 *   - Snapshot: /Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md
 *
 * Note on imports: this file uses `import * as React from "react"` (namespace
 * form) so it can type-check standalone without node_modules; the bottom of
 * the file shims the React hook signatures via `declare global`. In a real
 * React 19 project, the named-import form `import { useState, useRef, ... }
 * from "react"` works identically.
 */

// @ts-expect-error -- 'react' is shimmed via `declare global { namespace React }` below for standalone tsc.
import * as React from "react";

// ---------------------------------------------------------------------------
// Types — narrow union of the data-channel events we actually handle.
// Every `type` string below traces to the research file's event lexicon.
// ---------------------------------------------------------------------------

/** Cross-file contract — must match the SessionResponse type in nextjs-route.ts. */
type SessionResponse = {
  client_secret: string;
  session_id: string;
  expires_at: number;
  webrtc_endpoint: string;
  model: string;
  voice: string;
};

/** Events the server sends down the `oai-events` data channel.
 *  Source: realtime-conversations guide event list. */
type ServerEvent =
  | { type: "session.created"; session: Record<string, unknown> }
  | { type: "session.updated"; session: Record<string, unknown> }
  | { type: "response.created"; response: Record<string, unknown> }
  | {
      type: "response.output_audio_transcript.delta";
      delta: string;
      item_id?: string;
      response_id?: string;
    }
  | {
      type: "response.output_audio_transcript.done";
      transcript?: string;
      item_id?: string;
    }
  | { type: "response.output_audio.delta"; delta: string }
  | { type: "response.output_audio.done" }
  | { type: "response.done"; response: Record<string, unknown> }
  | { type: "response.cancelled" }
  | { type: "input_audio_buffer.speech_started" }
  | { type: "input_audio_buffer.speech_stopped" }
  | { type: "input_audio_buffer.committed"; item_id: string }
  | { type: "error"; error: { message?: string; type?: string; code?: string } }
  // Catch-all so the narrow union stays sound but we still handle unknown events.
  | { type: string; [k: string]: unknown };

/** Events the client sends up the channel.
 *  Source: realtime-conversations guide. */
type ClientEvent =
  | { type: "input_audio_buffer.commit" }
  | { type: "input_audio_buffer.clear" }
  | { type: "response.create"; response?: Record<string, unknown> }
  | { type: "response.cancel" }
  | { type: "session.update"; session: Record<string, unknown> };

type ConnState =
  | "idle"
  | "connecting"
  | "connected"
  | "listening"
  | "responding"
  | "error";

type Props = {
  /** When true, microphone activity during agent response sends
   *  `response.cancel`. Default true. Pass false to require PTT release before
   *  interrupting (stricter half-duplex). */
  allowBargeIn?: boolean;
  /** RMS threshold (0..1) above which we consider the user is speaking
   *  during agent playback. Default 0.06 — tune per environment. */
  bargeInThreshold?: number;
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function VoiceAgentClient(props: Props = {}) {
  const { allowBargeIn = true, bargeInThreshold = 0.06 } = props;

  const [state, setState] = React.useState<ConnState>("idle");
  const [errorMsg, setErrorMsg] = React.useState<string | null>(null);
  /** The current in-flight agent utterance (assembled from `.delta` events). */
  const [liveTranscript, setLiveTranscript] = React.useState<string>("");
  /** History of completed agent utterances. */
  const [history, setHistory] = React.useState<string[]>([]);

  // Refs holding live WebRTC / audio objects.
  const pcRef = React.useRef<RTCPeerConnection | null>(null);
  const dcRef = React.useRef<RTCDataChannel | null>(null);
  const micStreamRef = React.useRef<MediaStream | null>(null);
  const micSenderRef = React.useRef<RTCRtpSender | null>(null);
  const remoteAudioRef = React.useRef<HTMLAudioElement | null>(null);
  const audioCtxRef = React.useRef<AudioContext | null>(null);
  const analyserRef = React.useRef<AnalyserNode | null>(null);
  const rafRef = React.useRef<number | null>(null);
  /** Tracks whether agent audio is currently playing (set on response.created,
   *  cleared on response.done / response.cancelled). Used by the barge-in
   *  monitor without forcing a re-render every audio frame. */
  const respondingRef = React.useRef<boolean>(false);

  // -------------------------------------------------------------------------
  // Data channel send helper
  // -------------------------------------------------------------------------
  const sendEvent = React.useCallback((evt: ClientEvent): void => {
    const dc = dcRef.current;
    if (!dc || dc.readyState !== "open") return;
    dc.send(JSON.stringify(evt));
  }, []);

  // -------------------------------------------------------------------------
  // Barge-in monitor — RMS-based VAD on the local mic.
  // We attach an AnalyserNode to the mic stream and poll it via rAF.
  // When the user starts speaking while the agent is responding, we send
  // `response.cancel` over the data channel.
  // -------------------------------------------------------------------------
  const startBargeInMonitor = React.useCallback((stream: MediaStream): void => {
    // Build an AudioContext lazily — Safari requires user-gesture context creation.
    type AudioCtxCtor = new () => AudioContext;
    const w = window as unknown as { AudioContext?: AudioCtxCtor; webkitAudioContext?: AudioCtxCtor };
    const Ctor: AudioCtxCtor | undefined = w.AudioContext ?? w.webkitAudioContext;
    if (!Ctor) return;
    const ctx = new Ctor();
    const src = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    src.connect(analyser);

    audioCtxRef.current = ctx;
    analyserRef.current = analyser;

    const buf = new Float32Array(analyser.fftSize);
    const tick = (): void => {
      const a = analyserRef.current;
      if (!a) return;
      a.getFloatTimeDomainData(buf);
      // RMS
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      if (allowBargeIn && respondingRef.current && rms > bargeInThreshold) {
        // Tell the model to stop generating. Locally we also clear our buffer.
        sendEvent({ type: "response.cancel" });
        respondingRef.current = false;
        setState("listening");
      }
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
  }, [allowBargeIn, bargeInThreshold, sendEvent]);

  // -------------------------------------------------------------------------
  // Handle messages from the data channel.
  // -------------------------------------------------------------------------
  const handleServerEvent = React.useCallback((raw: string): void => {
    let evt: ServerEvent;
    try { evt = JSON.parse(raw) as ServerEvent; } catch { return; }

    switch (evt.type) {
      case "response.created": {
        respondingRef.current = true;
        setState("responding");
        break;
      }
      case "response.output_audio_transcript.delta": {
        // Assemble the agent's in-flight transcript.
        const deltaEvt = evt as { delta: string };
        setLiveTranscript((prev: string) => prev + deltaEvt.delta);
        break;
      }
      case "response.output_audio_transcript.done":
      case "response.done": {
        // Finalize: flush live buffer to history, prepare for next utterance.
        respondingRef.current = false;
        setState("connected");
        setLiveTranscript((cur: string) => {
          if (cur.length > 0) setHistory((h: string[]) => [...h, cur]);
          return "";
        });
        break;
      }
      case "response.cancelled": {
        respondingRef.current = false;
        setState("connected");
        setLiveTranscript((cur: string) => {
          if (cur.length > 0) setHistory((h: string[]) => [...h, cur + " [cancelled]"]);
          return "";
        });
        break;
      }
      case "error": {
        const e = evt as { error: { message?: string } };
        setErrorMsg(e.error?.message ?? "unknown realtime error");
        setState("error");
        break;
      }
      default:
        // Many events (session.created, input_audio_buffer.speech_started, etc.)
        // are fine to ignore for this basic UI.
        break;
    }
  }, []);

  // -------------------------------------------------------------------------
  // Connect: mint session, set up RTCPeerConnection, exchange SDP.
  // -------------------------------------------------------------------------
  const connect = React.useCallback(async (): Promise<void> => {
    if (pcRef.current) return; // already connected
    setErrorMsg(null);
    setState("connecting");

    try {
      // 1. Mint client_secret server-side.
      const sessRes = await fetch("/api/voice/session", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!sessRes.ok) {
        throw new Error(`session mint failed: ${sessRes.status}`);
      }
      const session = (await sessRes.json()) as SessionResponse;

      // 2. Build RTCPeerConnection.
      const pc = new RTCPeerConnection();
      pcRef.current = pc;

      // 3. Remote audio track → <audio> element. The peer connection emits
      //    `track` events for each inbound MediaStreamTrack; we attach the
      //    first stream we receive.
      pc.ontrack = (e: RTCTrackEvent): void => {
        const audioEl = remoteAudioRef.current;
        if (!audioEl) return;
        if (e.streams && e.streams[0]) {
          audioEl.srcObject = e.streams[0];
        }
      };

      // 4. Data channel — name MUST be "oai-events" per WebRTC guide.
      const dc = pc.createDataChannel("oai-events");
      dcRef.current = dc;
      dc.onopen = (): void => {
        setState("connected");
      };
      dc.onmessage = (ev: MessageEvent): void => {
        // Channel carries JSON strings.
        if (typeof ev.data === "string") handleServerEvent(ev.data);
      };
      dc.onerror = (): void => {
        setErrorMsg("data channel error");
        setState("error");
      };

      // 5. Capture mic & add as outbound track. We start with the track
      //    DISABLED — the PTT button enables it on press. This means the
      //    user must explicitly start talking; the WebRTC pipe is silent
      //    by default. To switch to server VAD instead, after dc.onopen
      //    send: { type: "session.update", session: { turn_detection: { type: "server_vad" } } }
      //    — and leave track.enabled = true.
      const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
      micStreamRef.current = mic;
      const audioTrack = mic.getAudioTracks()[0];
      audioTrack.enabled = false;
      micSenderRef.current = pc.addTrack(audioTrack, mic);

      // Start the barge-in monitor (it polls but only acts when allowBargeIn).
      startBargeInMonitor(mic);

      // 6. SDP offer/answer exchange.
      //    The browser creates an offer and POSTs the SDP to OpenAI's
      //    /v1/realtime/calls endpoint. The body content-type is
      //    `application/sdp` and the bearer token is the ephemeral key.
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);

      const sdpRes = await fetch(session.webrtc_endpoint, {
        method: "POST",
        headers: {
          "authorization": `Bearer ${session.client_secret}`,
          "content-type": "application/sdp",
        },
        body: offer.sdp ?? "",
      });
      if (!sdpRes.ok) {
        throw new Error(`SDP exchange failed: ${sdpRes.status}`);
      }
      const answerSdp = await sdpRes.text();
      await pc.setRemoteDescription({ type: "answer", sdp: answerSdp });

      // If dc.onopen hasn't fired yet, state will advance to "connected"
      // once the channel opens.
    } catch (err) {
      const msg = err instanceof Error ? err.message : "unknown connect error";
      setErrorMsg(msg);
      setState("error");
      // Best-effort teardown
      pcRef.current?.close();
      pcRef.current = null;
      dcRef.current = null;
    }
  }, [handleServerEvent, startBargeInMonitor]);

  // -------------------------------------------------------------------------
  // Push-to-talk handlers
  // -------------------------------------------------------------------------
  const onPttDown = React.useCallback((): void => {
    if (state !== "connected" && state !== "responding") return;
    const sender = micSenderRef.current;
    if (!sender || !sender.track) return;
    // If the agent was mid-response, cancel it (explicit barge-in via PTT).
    if (respondingRef.current && allowBargeIn) {
      sendEvent({ type: "response.cancel" });
      respondingRef.current = false;
    }
    sender.track.enabled = true;
    setState("listening");
  }, [state, allowBargeIn, sendEvent]);

  const onPttUp = React.useCallback((): void => {
    const sender = micSenderRef.current;
    if (!sender || !sender.track) return;
    if (state !== "listening") return;
    sender.track.enabled = false;
    // Commit the audio buffer and ask the model to produce a response.
    sendEvent({ type: "input_audio_buffer.commit" });
    sendEvent({ type: "response.create" });
    setState("connected");
  }, [state, sendEvent]);

  // -------------------------------------------------------------------------
  // Cleanup on unmount.
  // -------------------------------------------------------------------------
  React.useEffect(() => {
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      analyserRef.current?.disconnect();
      audioCtxRef.current?.close().catch(() => {});
      micStreamRef.current?.getTracks().forEach((t: MediaStreamTrack) => t.stop());
      dcRef.current?.close();
      pcRef.current?.close();
      pcRef.current = null;
      dcRef.current = null;
      micStreamRef.current = null;
      micSenderRef.current = null;
      analyserRef.current = null;
      audioCtxRef.current = null;
      rafRef.current = null;
    };
  }, []);

  // -------------------------------------------------------------------------
  // UI
  // -------------------------------------------------------------------------
  const pttDisabled =
    state === "idle" ||
    state === "connecting" ||
    state === "error" ||
    (state === "responding" && !allowBargeIn);

  return (
    <div>
      <audio ref={remoteAudioRef} autoPlay playsInline />

      <div>
        <p>Status: {state}</p>
        {errorMsg ? <p style={{ color: "crimson" }}>{errorMsg}</p> : null}
      </div>

      {state === "idle" || state === "error" ? (
        <button type="button" onClick={connect}>
          Start voice session
        </button>
      ) : (
        <button
          type="button"
          disabled={pttDisabled}
          onPointerDown={onPttDown}
          onPointerUp={onPttUp}
          onPointerLeave={onPttUp}
          onPointerCancel={onPttUp}
        >
          {state === "listening" ? "Release to send" : "Hold to talk"}
        </button>
      )}

      <div>
        <h3>Transcript</h3>
        {history.map((utt: string, i: number) => (
          <p key={i}>{utt}</p>
        ))}
        {liveTranscript ? <p><em>{liveTranscript}</em></p> : null}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Ambient shims so this single-file example type-checks under the harness's
// `tsc --noEmit` invocation without node_modules. In a real project these
// signatures come from @types/react (React 19 ships TS types).
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

  // Minimal JSX intrinsics for the elements we use. Strings as values are
  // permitted by the catch-all index.
  namespace JSX {
    interface IntrinsicElements {
      [elemName: string]: Record<string, unknown>;
    }
  }
}
