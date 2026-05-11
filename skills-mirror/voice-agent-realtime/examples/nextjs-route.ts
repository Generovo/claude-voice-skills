/**
 * examples/nextjs-route.ts — Mint OpenAI Realtime client_secret for browser WebRTC.
 *
 * Place at: app/api/voice/session/route.ts in a Next.js 16 App Router project.
 *
 * Env required:
 *   OPENAI_API_KEY    — your OpenAI server key (NEVER exposed to the browser)
 *
 * Browser flow:
 *   1. Client POSTs /api/voice/session with optional { model, voice, instructions }.
 *   2. This handler mints an ephemeral client_secret via OpenAI's REST API.
 *   3. Returns { client_secret, session_id, expires_at, webrtc_endpoint } to the client.
 *   4. Client opens RTCPeerConnection against webrtc_endpoint with
 *      `Authorization: Bearer <client_secret>` and Content-Type: application/sdp.
 *
 * Sources (verified 2026-05-11; see research snapshot for full details):
 *   - https://developers.openai.com/api/docs/models/gpt-realtime-2
 *   - https://developers.openai.com/api/docs/guides/realtime-webrtc
 *   - https://developers.openai.com/api/docs/guides/realtime-conversations
 *   - Snapshot: /Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md
 *
 * Note on imports: this file uses the Web standard `Request`/`Response` types
 * which Next.js 16 Route Handlers fully support. If your project prefers
 * `NextRequest`/`NextResponse`, swap the signature — the body is identical.
 */

// Next.js runtime selector. The Vercel "knowledge-update" note flags that
// Edge runtime is no longer recommended; default to Node.js (Fluid Compute).
export const runtime = "nodejs";

// Standalone-tsc shim: in a real Next.js project, `process.env` is typed via
// @types/node. We declare just what we use so this single-file example
// type-checks without node_modules.
declare const process: { env: Record<string, string | undefined> };

// ---------------------------------------------------------------------------
// Constants — all values traced to the research snapshot.
// ---------------------------------------------------------------------------

/** Endpoint that mints a short-lived client_secret bound to a session.
 *  Source: developers.openai.com/api/docs/guides/realtime-webrtc */
const OPENAI_CLIENT_SECRETS_URL =
  "https://api.openai.com/v1/realtime/client_secrets";

/** WebRTC SDP exchange endpoint that the browser POSTs its SDP offer to.
 *  Source: developers.openai.com/api/docs/guides/realtime-webrtc */
const OPENAI_WEBRTC_CALLS_URL =
  "https://api.openai.com/v1/realtime/calls";

/** Default conversational model — confirmed live via GET /v1/models. */
const DEFAULT_MODEL = "gpt-realtime-2";

/** Default voice — research file notes `marin` is the voice shown in the
 *  WebRTC default example in OpenAI's docs. */
const DEFAULT_VOICE = "marin";

/** The 10 voices supported by `audio.output.voice` for gpt-realtime-2.
 *  Source: developers.openai.com/api/docs/guides/realtime-conversations */
const VOICES = [
  "alloy",
  "ash",
  "ballad",
  "coral",
  "echo",
  "sage",
  "shimmer",
  "verse",
  "marin",
  "cedar",
] as const;
type Voice = (typeof VOICES)[number];

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Body the browser may POST to this route. All fields optional. */
type RequestBody = {
  model?: string;
  voice?: string;
  instructions?: string;
};

/** Response we send back to the browser. */
type SessionResponse = {
  client_secret: string;
  session_id: string;
  expires_at: number;
  webrtc_endpoint: string;
  model: string;
  voice: Voice;
};

/** Shape we send to OpenAI's client_secrets endpoint.
 *  The research snapshot lists the conversational session fields:
 *    type, model, output_modalities, audio (with .output.voice),
 *    instructions, voice, tools, tool_choice, ...
 *  Source: developers.openai.com/api/docs/guides/realtime-conversations
 *
 *  TODO [UNCONFIRMED in research]: the exact request body shape for
 *  POST /v1/realtime/client_secrets is not enumerated field-by-field in the
 *  fetched docs. The structure below mirrors `session.update` payload shape
 *  (which IS documented) wrapped in `{ session: ... }` — the most likely
 *  shape per the WebRTC guide's reference. If OpenAI rejects this body,
 *  inspect their error message and adjust the wrapper field. */
type ClientSecretRequest = {
  session: {
    type: "realtime";
    model: string;
    instructions?: string;
    audio: {
      output: {
        voice: Voice;
      };
    };
  };
};

/** Shape of OpenAI's client_secrets response.
 *  TODO [UNCONFIRMED in research]: exact response field names are not shown
 *  verbatim in the fetched WebRTC guide. The fields below are the
 *  industry-standard names used by ephemeral-token mint endpoints. Likely
 *  variants:
 *    - { client_secret: { value, expires_at }, session: { id } }
 *    - { value, expires_at, session_id }
 *  The handler probes both shapes defensively. */
type OpenAIClientSecretResponse = {
  // Nested form
  client_secret?: { value?: string; expires_at?: number };
  session?: { id?: string };
  // Flat form
  value?: string;
  expires_at?: number;
  session_id?: string;
  id?: string;
};

// ---------------------------------------------------------------------------
// Validation helpers
// ---------------------------------------------------------------------------

function isVoice(v: unknown): v is Voice {
  return typeof v === "string" && (VOICES as readonly string[]).includes(v);
}

function validateModel(m: unknown): string {
  if (typeof m !== "string") return DEFAULT_MODEL;
  // Server-side guard: only allow gpt-realtime* family. Never trust the client.
  if (!m.startsWith("gpt-realtime")) {
    throw new HttpError(400, `unsupported model: ${m}`);
  }
  return m;
}

function validateInstructions(i: unknown): string | undefined {
  if (i === undefined || i === null) return undefined;
  if (typeof i !== "string") {
    throw new HttpError(400, "instructions must be a string");
  }
  // Cap length to keep payloads sane.
  if (i.length > 8000) {
    throw new HttpError(400, "instructions too long (max 8000 chars)");
  }
  return i;
}

class HttpError extends Error {
  public readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

// ---------------------------------------------------------------------------
// POST handler
// ---------------------------------------------------------------------------

export async function POST(req: Request): Promise<Response> {
  // 1. Read & validate request body.
  let body: RequestBody;
  try {
    const raw: unknown = await req.json().catch(() => ({}));
    body = (raw && typeof raw === "object" ? raw : {}) as RequestBody;
  } catch {
    return jsonResponse({ error: "invalid JSON body" }, 400);
  }

  let model: string;
  let voice: Voice;
  let instructions: string | undefined;
  try {
    model = validateModel(body.model ?? DEFAULT_MODEL);
    voice = body.voice === undefined
      ? DEFAULT_VOICE
      : (isVoice(body.voice)
          ? body.voice
          : (() => { throw new HttpError(400, `unknown voice: ${String(body.voice)}`); })());
    instructions = validateInstructions(body.instructions);
  } catch (err) {
    if (err instanceof HttpError) {
      return jsonResponse({ error: err.message }, err.status);
    }
    return jsonResponse({ error: "invalid request" }, 400);
  }

  // 2. Look up server-only API key.
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    return jsonResponse(
      { error: "server misconfigured: OPENAI_API_KEY missing" },
      500,
    );
  }

  // 3. Build the mint request. See ClientSecretRequest comment above for
  //    UNCONFIRMED items.
  const mintRequest: ClientSecretRequest = {
    session: {
      type: "realtime",
      model,
      ...(instructions !== undefined ? { instructions } : {}),
      audio: { output: { voice } },
    },
  };

  // 4. Call OpenAI. We use the standard fetch API — no SDK dependency.
  let upstream: Response;
  try {
    upstream = await fetch(OPENAI_CLIENT_SECRETS_URL, {
      method: "POST",
      headers: {
        "authorization": `Bearer ${apiKey}`,
        "content-type": "application/json",
        // OpenAI-Beta: realtime=v1 — research notes this header was required
        // on the previous Realtime API generation and may no longer be needed.
        // Sending it does no harm and helps if OpenAI rolls back.
        "openai-beta": "realtime=v1",
      },
      body: JSON.stringify(mintRequest),
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : "network error";
    return jsonResponse(
      { error: "failed to reach OpenAI", detail: message },
      502,
    );
  }

  if (!upstream.ok) {
    // Surface OpenAI's error but NEVER leak the API key. We forward status
    // and a text body — OpenAI's error envelope usually carries
    // { error: { message, type, code } }.
    const text = await upstream.text();
    let detail: unknown = text;
    try { detail = JSON.parse(text); } catch { /* keep as text */ }
    return jsonResponse(
      { error: "OpenAI rejected the mint request", status: upstream.status, detail },
      upstream.status >= 500 ? 502 : 400,
    );
  }

  // 5. Parse response defensively (see OpenAIClientSecretResponse note).
  const parsed = (await upstream.json()) as OpenAIClientSecretResponse;
  const clientSecret =
    parsed.client_secret?.value ?? parsed.value ?? undefined;
  const expiresAt =
    parsed.client_secret?.expires_at ?? parsed.expires_at ?? undefined;
  const sessionId =
    parsed.session?.id ?? parsed.session_id ?? parsed.id ?? undefined;

  if (!clientSecret || typeof expiresAt !== "number" || !sessionId) {
    return jsonResponse(
      {
        error: "OpenAI response missing expected fields",
        seen: Object.keys(parsed),
      },
      502,
    );
  }

  const out: SessionResponse = {
    client_secret: clientSecret,
    session_id: sessionId,
    expires_at: expiresAt,
    // The browser will POST its SDP offer here with Bearer <client_secret>.
    // The model is encoded as a query param per the WebRTC guide.
    webrtc_endpoint: `${OPENAI_WEBRTC_CALLS_URL}?model=${encodeURIComponent(model)}`,
    model,
    voice,
  };
  return jsonResponse(out, 200);
}
