# OpenAI Realtime Voice — 3 Claude Code Skills — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build 3 Claude Code user-global skills (`voice-agent-realtime`, `voice-translate-live`, `voice-transcribe-stream`) covering OpenAI's May 2026 Realtime API models, with TS + Python examples for SIP/Twilio, WhatsApp, Web/WebRTC, mobile, and meeting-bot channels, plus runnable eval/latency scripts.

**Architecture:** Three sibling skills under `~/.claude/skills/`. Each is a stand-alone directory with `SKILL.md` (~300-500 lines), `references/` (deep-dive markdown loaded on demand), `examples/` (runnable code), `templates/` (system prompts), and `scripts/` (latency + eval Python tools). No shared parent — independence ensures trigger isolation.

**Tech Stack:** Markdown (SKILL.md + references), TypeScript 5 (Next.js 16 App Router, React 19, AI SDK v6) for web/server examples, Python 3.13 (asyncio + websockets + FastAPI) for telephony/WhatsApp/meeting bots + scripts. OpenAI Realtime API: WebSocket and WebRTC transports.

**Design doc:** `docs/plans/2026-05-11-openai-realtime-voice-skills-design.md`

**Source of truth for API surface (fetch live, do NOT rely on memory):**
- https://developers.openai.com/api/docs/models/gpt-realtime-2
- https://developers.openai.com/api/docs/models/gpt-realtime-translate
- https://developers.openai.com/api/docs/models/gpt-realtime-whisper
- https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api/
- The Realtime API event reference (link discovered during Task 1).

---

## Phase 0 — Prep & Live Docs Capture

### Task 0.1: Verify environment

**Files:**
- Read: `~/.claude/skills/` (confirm exists, `ui-ux-pro-max` present)
- Verify: Python 3.13+, Node 24+, `tsc` available, `OPENAI_API_KEY` env var set

**Step 1: Check tooling**

Run:
```bash
python3 --version
node --version
npx tsc --version 2>/dev/null || echo "tsc missing"
test -n "$OPENAI_API_KEY" && echo "OPENAI_API_KEY set" || echo "OPENAI_API_KEY MISSING"
ls ~/.claude/skills/
```

Expected: Python ≥3.13, Node ≥24, `tsc` resolvable, env var set, `ui-ux-pro-max` listed.

**Step 2: Stop and ask the user**

If anything fails: pause and ask the user to install/set. Do NOT proceed.

**Step 3: Commit nothing — this is a check task.**

---

### Task 0.2: Fetch live OpenAI Realtime docs

**Files:**
- Create: `docs/research/openai-realtime-api-2026-05-11.md` (notes file, working scratchpad)

**Step 1: Use WebFetch on each of the 4 source URLs**

For each URL, extract:
- Model IDs and aliases
- Supported modalities (input/output)
- Pricing (per-minute or per-token)
- Endpoint paths
- Session-config field names (exact JSON keys)
- Event names (client→server and server→client, exact strings)
- Tool calling format
- Reasoning levels (for gpt-realtime-2)
- Audio formats supported (PCM16, g711_ulaw, etc.)
- Sample rates
- Context window limits

**Step 2: Discover the full event reference**

WebFetch `https://platform.openai.com/docs/api-reference/realtime` (or whichever URL the model pages link to) and capture the canonical event list with field signatures.

**Step 3: Write everything to `docs/research/openai-realtime-api-2026-05-11.md`**

Structure:
```
# OpenAI Realtime API — Notes captured 2026-05-11

## gpt-realtime-2
- Model ID: ...
- Endpoint: ...
- Events client→server: [exhaustive list with payload shape]
- Events server→client: [exhaustive list]
- Pricing: ...
- (etc.)

## gpt-realtime-translate
...

## gpt-realtime-whisper
...

## Sources fetched
- URL — timestamp
```

This file is the single source of truth for all subsequent tasks. If a task needs an event name, it MUST come from here.

**Step 4: Commit (if repo)**

If `git status` works:
```bash
cd /Users/youssef/AgentVocalOpco
git add docs/research/openai-realtime-api-2026-05-11.md
git commit -m "docs: capture OpenAI Realtime API surface (2026-05-11 snapshot)"
```
If not a git repo: skip, but tell the user.

---

### Task 0.3: Create skill directory skeletons

**Files:**
- Create: `~/.claude/skills/voice-agent-realtime/{references,examples,templates,scripts}/.gitkeep`
- Create: `~/.claude/skills/voice-translate-live/{references,examples,scripts}/.gitkeep`
- Create: `~/.claude/skills/voice-transcribe-stream/{references,examples,scripts}/.gitkeep`

**Step 1: Create directories**

```bash
mkdir -p ~/.claude/skills/voice-agent-realtime/{references,examples,templates,scripts}
mkdir -p ~/.claude/skills/voice-translate-live/{references,examples,scripts}
mkdir -p ~/.claude/skills/voice-transcribe-stream/{references,examples,scripts}
```

**Step 2: Verify**

```bash
tree ~/.claude/skills/voice-agent-realtime ~/.claude/skills/voice-translate-live ~/.claude/skills/voice-transcribe-stream -L 2
```

Expected: 3 skill dirs, correct subdirs each.

---

### Task 0.4: Write the validation harness

**Files:**
- Create: `scripts/validate-skills.sh`

**Step 1: Write a bash script that validates every skill we build**

```bash
#!/usr/bin/env bash
# scripts/validate-skills.sh — sanity-check all 3 voice skills

set -euo pipefail
SKILLS_ROOT="$HOME/.claude/skills"
SKILLS=("voice-agent-realtime" "voice-translate-live" "voice-transcribe-stream")
FAIL=0

for skill in "${SKILLS[@]}"; do
  d="$SKILLS_ROOT/$skill"
  echo "== $skill =="

  # 1. SKILL.md exists
  test -f "$d/SKILL.md" || { echo "  MISSING SKILL.md"; FAIL=1; continue; }

  # 2. Frontmatter has name + description
  head -10 "$d/SKILL.md" | grep -q "^name: $skill$" || { echo "  bad/missing name"; FAIL=1; }
  head -20 "$d/SKILL.md" | grep -q "^description: " || { echo "  bad/missing description"; FAIL=1; }

  # 3. All Python examples parse
  if compgen -G "$d/examples/*.py" >/dev/null; then
    for f in "$d/examples/"*.py "$d/scripts/"*.py 2>/dev/null; do
      [ -f "$f" ] || continue
      python3 -m py_compile "$f" || { echo "  py compile FAIL: $f"; FAIL=1; }
    done
  fi

  # 4. All TS examples type-check (single file mode)
  if compgen -G "$d/examples/*.ts" >/dev/null || compgen -G "$d/examples/*.tsx" >/dev/null; then
    for f in "$d/examples/"*.ts "$d/examples/"*.tsx; do
      [ -f "$f" ] || continue
      npx -y tsc --noEmit --jsx preserve --esModuleInterop --target es2022 --module esnext --moduleResolution bundler --skipLibCheck "$f" \
        || { echo "  tsc FAIL: $f"; FAIL=1; }
    done
  fi

  # 5. Markdown links to references exist
  while IFS= read -r ref; do
    test -f "$d/$ref" || { echo "  broken ref: $ref"; FAIL=1; }
  done < <(grep -oE 'references/[a-z0-9-]+\.md' "$d/SKILL.md" | sort -u)
done

if [ "$FAIL" -ne 0 ]; then
  echo "VALIDATION FAILED"
  exit 1
fi
echo "ALL SKILLS VALID"
```

**Step 2: Make executable**

```bash
chmod +x /Users/youssef/AgentVocalOpco/scripts/validate-skills.sh
```

**Step 3: Run it now (should pass — no skills yet, nothing to fail beyond missing dirs)**

```bash
mkdir -p /Users/youssef/AgentVocalOpco/scripts
bash /Users/youssef/AgentVocalOpco/scripts/validate-skills.sh
```

Expected: `MISSING SKILL.md` for all 3 skills → `VALIDATION FAILED` → exit 1. That's correct at this stage; we'll re-run after each phase.

**Step 4: Commit if repo**

```bash
git add scripts/validate-skills.sh
git commit -m "tooling: add skills validation harness"
```

---

## Phase 1 — `voice-agent-realtime` (richest skill, built first)

### Task 1.1: Draft the frontmatter + opening section

**Files:**
- Create: `~/.claude/skills/voice-agent-realtime/SKILL.md`

**Step 1: Write the frontmatter and "When to use / When NOT to use" sections**

Pull the `description` from the design doc verbatim (no improvisation — the trigger words are deliberate). After the frontmatter, write:
- `# Voice Agent Realtime (GPT-Realtime-2)`
- `## When to use this skill` — 5 bullets (voicebot, callbot, vocal assistant, function-calling vocal, multi-turn agent)
- `## When NOT to use` — pointing to `voice-translate-live` and `voice-transcribe-stream`

**Step 2: Run validation**

```bash
bash /Users/youssef/AgentVocalOpco/scripts/validate-skills.sh || true
```
Expected: `voice-agent-realtime` passes the frontmatter check; broken-ref checks fire for the references we'll add later — that's fine for now.

**Step 3: Commit**

```bash
git add ~/.claude/skills/voice-agent-realtime/SKILL.md
git commit -m "feat(voice-agent-realtime): scaffold SKILL.md with frontmatter and when-to-use"
```
(All commits below assume git is initialized; if not, batch a single commit at the end.)

---

### Task 1.2: Write "Model at a glance" + "Connection patterns"

**Files:**
- Modify: `~/.claude/skills/voice-agent-realtime/SKILL.md`

**Step 1: Append two sections**

`## Model at a glance` — compact table with: model ID, modalities, max context, default voice, reasoning levels, pricing. **Numbers come from `docs/research/openai-realtime-api-2026-05-11.md`, never from memory.**

`## Connection patterns` — table: WebRTC (client) vs WebSocket (server), each with: when to use, latency profile, channel fit (web=WebRTC, telephony/WhatsApp/meeting=WebSocket).

**Step 2: Verify by re-reading the file** — content matches research notes.

**Step 3: Commit**

```bash
git commit -am "feat(voice-agent-realtime): add model overview and connection patterns"
```

---

### Task 1.3: Write "Session lifecycle" section

**Files:**
- Modify: `~/.claude/skills/voice-agent-realtime/SKILL.md`

**Step 1: Append `## Session lifecycle`**

Walk through the 6-step protocol — each step is the exact event name from the research notes, with a 2-line description of what it does, and a single-line example JSON payload. No invented event names.

**Step 2: Commit**

```bash
git commit -am "feat(voice-agent-realtime): document session lifecycle"
```

---

### Task 1.4: Write `references/events-websocket.md`

**Files:**
- Create: `~/.claude/skills/voice-agent-realtime/references/events-websocket.md`

**Step 1: Build the canonical event catalog**

Two tables: client→server events and server→client events. Each row: event name (exact), purpose, key payload fields, when emitted. Sourced 100% from `docs/research/...`. No memorized fields.

**Step 2: Cross-check**

Open the research file side-by-side; every event in the reference must appear in the research notes.

**Step 3: Commit**

```bash
git commit -am "docs(voice-agent-realtime): add full WebSocket event catalog"
```

---

### Task 1.5: Write `references/tool-calling.md`

**Files:**
- Create: `~/.claude/skills/voice-agent-realtime/references/tool-calling.md`

**Step 1: Sections**

- Tool definition shape (`name`, `description`, `parameters` JSON schema — exact field names from research).
- Parallel tool calls — what's new in GPT-Realtime-2, when the model emits multiple `response.function_call_arguments.delta` for distinct call IDs.
- Submitting tool outputs (event sequence).
- A small end-to-end example: weather + calendar called in parallel from a single user turn.

**Step 2: Commit**

```bash
git commit -am "docs(voice-agent-realtime): add tool-calling reference"
```

---

### Task 1.6: Write `references/preambles-interruptions.md`

**Files:**
- Create: `~/.claude/skills/voice-agent-realtime/references/preambles-interruptions.md`

**Step 1: Sections**

- Preambles: prompt patterns ("avant d'appeler un outil long, dis une phrase courte du type 'je vérifie'").
- Interruption handling: VAD server-side vs client-side, `response.cancel`, truncating audio buffer on user barge-in.
- Common bugs: cancel timing, audio bleed-through, silence-detection thresholds.

**Step 2: Commit**

```bash
git commit -am "docs(voice-agent-realtime): add preambles and interruptions reference"
```

---

### Task 1.7: Write `references/pricing-limits.md`

**Files:**
- Create: `~/.claude/skills/voice-agent-realtime/references/pricing-limits.md`

**Step 1: Pricing block + rate limits + context limits**

Numbers from research file only. Add 3 worked examples: a 10-min support call, a 30-call/hour callbot, a single complex multi-tool turn.

**Step 2: Commit**

```bash
git commit -am "docs(voice-agent-realtime): add pricing and limits reference"
```

---

### Task 1.8: Write remaining `SKILL.md` sections (Tools, Preambles, Channel transport, Reasoning, Pitfalls, Eval, Ref index)

**Files:**
- Modify: `~/.claude/skills/voice-agent-realtime/SKILL.md`

**Step 1: Append all remaining sections**

Each section is a short summary (5-15 lines) that points to the matching reference for the long version. Channel transport gets a sub-section per channel (Web, SIP/Twilio, WhatsApp, mobile, meeting bots) each ending with `→ see examples/<file>`.

**Step 2: Re-run validation harness**

```bash
bash /Users/youssef/AgentVocalOpco/scripts/validate-skills.sh
```
Expected: `voice-agent-realtime` passes frontmatter + ref-link checks for the references we've created. Examples missing will fail; ignore for now.

**Step 3: Commit**

```bash
git commit -am "feat(voice-agent-realtime): complete SKILL.md body"
```

---

### Task 1.9: Write `examples/nextjs-route.ts`

**Files:**
- Create: `~/.claude/skills/voice-agent-realtime/examples/nextjs-route.ts`

**Step 1: Write a Next.js 16 App Router route handler**

A `POST /api/voice/session` handler that:
- Reads `OPENAI_API_KEY` from env.
- Calls OpenAI to mint an ephemeral session token for client-side WebRTC.
- Returns the token + ICE config to the browser.

Use **only** the exact endpoint path and payload from the research file. Header comment with run command and required env vars.

**Step 2: Type-check**

```bash
npx -y tsc --noEmit --jsx preserve --esModuleInterop --target es2022 --module esnext --moduleResolution bundler --skipLibCheck \
  ~/.claude/skills/voice-agent-realtime/examples/nextjs-route.ts
```
Expected: 0 errors.

**Step 3: Commit**

```bash
git commit -am "feat(voice-agent-realtime): add Next.js ephemeral-token route example"
```

---

### Task 1.10: Write `examples/webrtc-client.tsx`

**Files:**
- Create: `~/.claude/skills/voice-agent-realtime/examples/webrtc-client.tsx`

**Step 1: Write a React 19 client component**

- `"use client"` directive.
- Fetches the ephemeral token from `/api/voice/session`.
- Opens an `RTCPeerConnection` against the OpenAI Realtime WebRTC endpoint (path from research file).
- Push-to-talk button + server-side VAD config.
- Displays the live transcript via the data channel events.
- Cleanly handles barge-in (cancel response on user start).

**Step 2: Type-check** with same tsc flags. Expected: 0 errors.

**Step 3: Commit**

```bash
git commit -am "feat(voice-agent-realtime): add WebRTC client example"
```

---

### Task 1.11: Write `examples/twilio-bridge.py`

**Files:**
- Create: `~/.claude/skills/voice-agent-realtime/examples/twilio-bridge.py`

**Step 1: Write a FastAPI bridge**

- Twilio Media Streams WebSocket inbound.
- OpenAI Realtime WebSocket outbound.
- Convert µ-law 8kHz ↔ PCM16 24kHz (use `audioop` stdlib + manual resampling, or `numpy` if available).
- Forward audio in both directions, log every event.
- Handle Twilio `start`, `media`, `stop` frames; OpenAI session lifecycle events.
- Header comment: run command, required env vars, Twilio webhook URL template.

**Step 2: Compile-check**

```bash
python3 -m py_compile ~/.claude/skills/voice-agent-realtime/examples/twilio-bridge.py
```
Expected: 0 errors.

**Step 3: Commit**

```bash
git commit -am "feat(voice-agent-realtime): add Twilio bridge example"
```

---

### Task 1.12: Write `examples/whatsapp-call.py`

**Files:**
- Create: `~/.claude/skills/voice-agent-realtime/examples/whatsapp-call.py`

**Step 1: Write a FastAPI webhook handler**

Two endpoints:
- `POST /webhook` — WhatsApp Cloud API inbound (verify signature, parse audio note).
- Audio handler: download audio, stream to OpenAI Realtime (audio-in/audio-out mode), POST the reply audio back as a WhatsApp voice message.
- Verbose comment about WhatsApp's audio note flow vs full-duplex call (WhatsApp call audio routes through a Business Solution Provider — comment a clear note).

**Step 2: py_compile.** **Step 3: commit.**

---

### Task 1.13: Write `examples/meeting-bot.py`

**Files:**
- Create: `~/.claude/skills/voice-agent-realtime/examples/meeting-bot.py`

**Step 1: Write a Recall.ai pattern**

- Comment header explicitly states: "Pattern using Recall.ai as the meeting-bot transport. Native Zoom/Meet/Teams SDKs are out of scope — see Recall docs for production setup."
- Webhook receives audio stream from Recall.
- Forwards to OpenAI Realtime in listen-mostly mode (agent can interject if instructed).
- Posts transcript + agent interjections back to Recall.

**Step 2: py_compile.** **Step 3: commit.**

---

### Task 1.14: Write `templates/system-prompt.md`

**Files:**
- Create: `~/.claude/skills/voice-agent-realtime/templates/system-prompt.md`

**Step 1: Write the parametrized prompt**

Sections:
- Rôle + persona (`{{company}}`, `{{persona_name}}`).
- Règles vocales (concision <2 phrases par tour, pas de markdown, pas de listes énumérées).
- Signaux d'écoute ("d'accord", "je comprends", "un instant").
- Patterns de preamble par catégorie d'outil.
- Gestion des silences (relancer après 6s).
- Gestion des interruptions (s'arrêter immédiatement, ne pas reprendre la phrase).
- Fallback ("je ne suis pas sûr de comprendre, pouvez-vous reformuler ?").
- Escalade (`{{escalation_policy}}`).
- Liste injectée des outils (`{{tools_available}}`).

**Step 2: Commit**

```bash
git commit -am "feat(voice-agent-realtime): add system-prompt template"
```

---

### Task 1.15: Write `scripts/test-latency.py` — RED step

**Files:**
- Create: `~/.claude/skills/voice-agent-realtime/scripts/test-latency.py`
- Create: `~/.claude/skills/voice-agent-realtime/scripts/test_latency_unit.py`

**Step 1: Write the test FIRST (TDD)**

```python
# test_latency_unit.py
import asyncio
from unittest.mock import AsyncMock, patch
import json
import test_latency  # the script under test

def test_parse_first_audio_delta_timestamp():
    """Given a stream of events, returns time-to-first-audio."""
    events = [
        ("session.created", 0.0),
        ("response.audio.delta", 0.412),
        ("response.audio.delta", 0.520),
        ("response.done", 1.834),
    ]
    metrics = test_latency.compute_metrics(events)
    assert metrics["ttfa_seconds"] == 0.412
    assert metrics["done_seconds"] == 1.834

def test_compute_metrics_missing_audio_delta_returns_none():
    events = [("session.created", 0.0), ("response.done", 1.0)]
    metrics = test_latency.compute_metrics(events)
    assert metrics["ttfa_seconds"] is None
```

**Step 2: Run the test**

```bash
cd ~/.claude/skills/voice-agent-realtime/scripts
python3 -m pytest test_latency_unit.py -v
```
Expected: ImportError on `test_latency` — RED.

**Step 3: Commit the failing test**

```bash
git add ~/.claude/skills/voice-agent-realtime/scripts/test_latency_unit.py
git commit -m "test(voice-agent-realtime): add latency-metrics unit test (failing)"
```

---

### Task 1.16: `scripts/test-latency.py` — GREEN step (implement)

**Files:**
- Modify: `~/.claude/skills/voice-agent-realtime/scripts/test-latency.py`

**Step 1: Implement**

```python
"""
test-latency.py — measure OpenAI Realtime API latency by reasoning level.

Run: python3 test-latency.py --level high --runs 5 [--audio sample.wav]
Env: OPENAI_API_KEY
"""
import argparse, asyncio, base64, json, os, time, statistics
import websockets

MODEL_ID = "..."   # FROM RESEARCH NOTES, not memory
WS_URL = "..."     # FROM RESEARCH NOTES

def compute_metrics(events):
    """events: list[(event_type, t_seconds_since_session_start)]"""
    ttfa = next((t for ev, t in events if ev == "response.audio.delta"), None)
    done = next((t for ev, t in events if ev == "response.done"), None)
    return {"ttfa_seconds": ttfa, "done_seconds": done}

async def run_one(level: str, audio_b64: str) -> dict:
    headers = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
               "OpenAI-Beta": "..."}   # FROM RESEARCH
    events = []
    start = time.monotonic()
    async with websockets.connect(WS_URL, additional_headers=headers) as ws:
        # session.update with reasoning level, then audio.append, then response.create
        # ... record (event_type, time.monotonic() - start) for each frame
        ...
    return compute_metrics(events)

async def main(args):
    audio_b64 = base64.b64encode(open(args.audio, "rb").read()).decode() if args.audio else _default_sample()
    results = [await run_one(args.level, audio_b64) for _ in range(args.runs)]
    ttfas = [r["ttfa_seconds"] for r in results if r["ttfa_seconds"]]
    print(f"Level: {args.level}  Runs: {args.runs}")
    print(f"  TTFA median: {statistics.median(ttfas):.3f}s   p95: {sorted(ttfas)[max(0,int(0.95*len(ttfas))-1)]:.3f}s")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--level", choices=["minimal","low","medium","high","xhigh"], default="low")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--audio", default=None)
    asyncio.run(main(p.parse_args()))
```

Plus a `_default_sample()` returning a short embedded base64 PCM16 sample.

**Step 2: Run the test**

```bash
python3 -m pytest test_latency_unit.py -v
```
Expected: PASS.

**Step 3: Smoke-test the script itself (compile only, no API call)**

```bash
python3 -m py_compile test-latency.py
```

**Step 4: Commit**

```bash
git commit -am "feat(voice-agent-realtime): implement test-latency.py"
```

---

### Task 1.17: `scripts/eval-prompt.py` — RED step

**Files:**
- Create: `~/.claude/skills/voice-agent-realtime/scripts/test_eval_prompt_unit.py`

**Step 1: Write the failing test**

```python
import eval_prompt

def test_scorer_extracts_pass_fail_from_judge_json():
    judge_output = '{"verdict": "pass", "reason": "agent correctly..."}'
    assert eval_prompt.score(judge_output) == ("pass", "agent correctly...")

def test_aggregate_pass_rate():
    rows = [("c1","pass",""),("c2","fail",""),("c3","pass",""),("c4","pass","")]
    assert eval_prompt.pass_rate(rows) == 0.75
```

**Step 2: Run → RED. Step 3: Commit failing test.**

---

### Task 1.18: `scripts/eval-prompt.py` — GREEN step

**Files:**
- Create: `~/.claude/skills/voice-agent-realtime/scripts/eval-prompt.py`
- Create: `~/.claude/skills/voice-agent-realtime/scripts/evals/adversarial.jsonl` (10 starter cases)

**Step 1: Implement**

- Loads `evals/adversarial.jsonl` (one JSON per line: `{id, audio_path|text, expected_behavior}`).
- For each case: opens a Realtime session, sends the input, captures the full transcript.
- Submits transcript + expected_behavior to a judge call (text completion with strict JSON output: `{verdict, reason}`).
- Aggregates pass rate, prints breakdown by category, writes `evals/results-<timestamp>.jsonl`.

**Step 2: Tests pass.** **Step 3: py_compile.** **Step 4: commit.**

---

### Task 1.19: Phase 1 wrap — run validation

**Step 1**

```bash
bash /Users/youssef/AgentVocalOpco/scripts/validate-skills.sh
```
Expected: `voice-agent-realtime` fully green; other 2 skills still missing.

**Step 2: Trigger probe — manual test**

Open a fresh Claude Code conversation in another terminal/instance, type:
> "Aide-moi à construire un voicebot WhatsApp qui prend des réservations chez le coiffeur."

Verify Claude announces it's loading `voice-agent-realtime`. If it loads `voice-translate-live` or `voice-transcribe-stream` instead, the description triggers overlap — adjust before continuing Phase 2.

**Step 3: Commit any trigger-tuning edits.**

---

## Phase 2 — `voice-translate-live`

### Task 2.1: SKILL.md frontmatter + when-to-use

Same pattern as Task 1.1 but for translation. **Critically:** the `When NOT to use` section explicitly redirects "agent that responds / function calling" → `voice-agent-realtime`, and "transcription only" → `voice-transcribe-stream`. This is the #1 confusion risk.

Commit.

### Task 2.2: Write `SKILL.md` body

Sections from the design doc, adapted: no tool calling section, no reasoning levels, but add **Multi-speaker architecture** (one session per output language + separate audio tracks per speaker) with an ASCII diagram.

Commit.

### Task 2.3: Write `references/languages.md`

Two columns: input languages (70+), output languages (13). Exact list from research notes. Commit.

### Task 2.4: Write `references/session-config.md`

Exact JSON shape for the `/v1/realtime/translations` session, every field with type and example. Commit.

### Task 2.5: Write `references/pricing-limits.md`

$0.034/min + 3 worked examples (10-min international call, 1h conference, 24/7 support hotline). Commit.

### Task 2.6: Write `examples/twilio-interpreter.py`

FastAPI bridge — Twilio Media Streams → OpenAI translate → second Twilio leg (3-way call interpreter pattern). py_compile + commit.

### Task 2.7: Write `examples/web-conference.tsx`

React/Next: per-participant WebRTC track, one translate session per (speaker, target-language) pair. tsc check + commit.

### Task 2.8: Write `examples/whatsapp-translate.py`

Translate WhatsApp voice notes between two parties (FR↔AR or EN↔FR). py_compile + commit.

### Task 2.9: Write `examples/meeting-translator.py`

Recall.ai pattern, but only-output mode. py_compile + commit.

### Task 2.10: `scripts/test-translation-quality.py` — TDD RED + GREEN

**Step 1: failing test for `compute_bleu(reference, hypothesis)` and `compute_comet(...)` (mocked).**
**Step 2: implement.** **Step 3: ship 3-pair sample (FR→EN, EN→FR, AR→FR) with reference translations in `samples/`.**

Commit each step separately.

### Task 2.11: Phase 2 wrap

Run validation harness. Trigger probe:
> "Configure une traduction live pour un appel commercial Paris ↔ Casablanca."

Should load `voice-translate-live`, not the other two. Fix description if not.

---

## Phase 3 — `voice-transcribe-stream`

### Task 3.1: SKILL.md frontmatter + when-to-use

Pattern from Task 1.1 / 2.1. Redirects in "When NOT to use".

### Task 3.2: SKILL.md body

Centerpiece: **Latency tuning profiles** section with the 3 profiles table (0.4s / 0.8-1.2s / 1.5-2s) and "which one for which use case". Commit.

### Task 3.3: `references/streaming-deltas.md`

How `response.text.delta`-style events flow; UX pattern for displaying partial words before finalization; rollback semantics when a delta is corrected. Commit.

### Task 3.4: `references/latency-tuning.md`

Deep-dive on the 3 profiles: what each parameter controls, trade-offs, sample audio results. Commit.

### Task 3.5: `references/pricing-limits.md`

$0.017/min + context limits (16K / 2K out) + chunking strategy for long sessions. Commit.

### Task 3.6: `examples/live-captions.tsx`

React component: live captions overlay with smooth delta append. tsc + commit.

### Task 3.7: `examples/twilio-transcribe.py`

FastAPI: Twilio stream → transcribe → push transcript to a webhook. py_compile + commit.

### Task 3.8: `examples/whatsapp-voice-notes.py`

WhatsApp voice note → transcribed text reply. py_compile + commit.

### Task 3.9: `examples/meeting-minutes.py`

Recall.ai stream → live transcript + post-meeting summary (chain to a non-realtime model). py_compile + commit.

### Task 3.10: `scripts/test-latency-vs-quality.py` — TDD

RED: tests for `compute_wer(ref, hyp)` and profile-comparison aggregation.
GREEN: implementation that runs each profile on the same sample audio, prints latency + WER table.

Commit each step.

### Task 3.11: Phase 3 wrap

Validation + trigger probe:
> "Génère des sous-titres en direct pour ma réunion."

Should load `voice-transcribe-stream`. Adjust if not.

---

## Phase 4 — Cross-skill validation

### Task 4.1: Full harness run

```bash
bash /Users/youssef/AgentVocalOpco/scripts/validate-skills.sh
```
Expected: `ALL SKILLS VALID`.

### Task 4.2: Trigger isolation matrix

Test all 6 cross-prompts in a fresh Claude session and record which skill loads:

| Prompt | Expected skill |
|---|---|
| "Construis un callbot téléphonique qui prend des RDV" | voice-agent-realtime |
| "Ajoute la traduction live FR↔AR à mon support" | voice-translate-live |
| "Sous-titres en direct pour mon meeting Zoom" | voice-transcribe-stream |
| "Voicebot WhatsApp Cloud API" | voice-agent-realtime |
| "Transcription temps réel des appels du call center" | voice-transcribe-stream |
| "Interprète automatique pour vidéos YouTube live" | voice-translate-live |

Record results in `docs/research/skill-triggers-2026-05-11.md`. If any wrong, tune `description` and re-test. Commit fixes.

### Task 4.3: Run each script once for real (smoke)

With `OPENAI_API_KEY` set:
```bash
cd ~/.claude/skills/voice-agent-realtime/scripts
python3 test-latency.py --level low --runs 1
```
Then same for `test-translation-quality.py` and `test-latency-vs-quality.py`. Each must complete without exception (latency/quality numbers don't need to be perfect — we're checking the wiring).

If any script fails: fix and commit.

### Task 4.4: Final commit + summary

If a repo: tag `voice-skills-v1`. If not: prompt the user to consider `git init`. Print a final summary of:
- Files created (count per skill).
- Total lines.
- Trigger matrix results.
- Known limitations / TODOs.

---

## Notes for the executor

- **Never invent OpenAI event names, endpoints, or field names.** Always source from `docs/research/openai-realtime-api-2026-05-11.md`. If something is missing from the research file, stop and WebFetch the right doc page first.
- **Use `Context7 query-docs`** for the Next.js 16 / React 19 / AI SDK v6 APIs if their syntax surfaces in examples — these change fast.
- **Commits are per-step** when a step produces a meaningful artifact (test red, test green, file done). Don't batch.
- **Skill descriptions are sacred** — they're the trigger mechanism. Don't paraphrase from the design doc; copy verbatim and only tune after Phase 4 trigger probes show conflicts.
- **Run the validation harness after every phase.** If it fails, fix before moving on.

---

## Execution Handoff

Plan complete and saved to `docs/plans/2026-05-11-openai-realtime-voice-skills.md`. Two execution options:

**1. Subagent-Driven (this session)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Best if you want to stay engaged and unblock decisions as they come up.

**2. Parallel Session (separate)** — Open a new session pointed at this plan, batch execution with checkpoints at phase boundaries. Best if you want to run the build in the background while you work on something else.

**Which approach?**
