# AgentVocalOpco — OpenAI Realtime Voice Skills

Three Claude Code skills for building voicebots, live translation, and streaming transcription using OpenAI's May 2026 Realtime API models.

## What this is

This repo holds:
- The **design** for 3 voice skills (`docs/plans/`)
- The **research snapshot** of the OpenAI Realtime API as of 2026-05-11 (`docs/research/`)
- **Mirror copies** of the live skills (`skills-mirror/`) — version-controlled snapshots of the skills installed at `~/.claude/skills/voice-*/`

The skills themselves run from `~/.claude/skills/voice-{agent-realtime,translate-live,transcribe-stream}/`.

## Quick start

```bash
# Source the env (Azure or OpenAI direct key)
set -a && source .env && set +a

# On macOS, also export the cert bundle (Python stdlib cert fix)
export SSL_CERT_FILE=$(python3 -m certifi)

# Verify all skills are valid
bash scripts/validate-skills.sh
# → ALL SKILLS VALID

# Unit tests across all 3 skills
for s in voice-agent-realtime voice-translate-live voice-transcribe-stream; do
  (cd ~/.claude/skills/$s/scripts && python3 -m pytest -q)
done
# → 28 passed total
```

## Skills overview

| Skill | Model | Use case | Pricing |
|---|---|---|---|
| `voice-agent-realtime` | `gpt-realtime-2` | Voicebots, callbots, vocal assistants with tool calling | $32 in / $64 out per 1M audio tokens |
| `voice-translate-live` | `gpt-realtime-translate` | Live voice translation (70+ in / 13 out) | $0.034/min |
| `voice-transcribe-stream` | `gpt-realtime-whisper` | Streaming low-latency transcription | $0.017/min |

Each skill ships:
- `SKILL.md` — when to use, when not to use, model overview, session lifecycle, connection patterns
- `references/` — exhaustive event catalog, pricing math, tuning profiles
- `examples/` — runnable TypeScript (Next.js + React 19 + WebRTC) and Python (FastAPI + Twilio / WhatsApp / Recall.ai)
- `scripts/` — TDD-tested latency & quality measurement scripts
- `templates/` (voice-agent only) — parametrized system prompt for voicebots

## Channels covered (per skill, where applicable)

- **Web / Mobile** — WebRTC client examples
- **Telephony** — Twilio Media Streams bridge
- **WhatsApp** — Cloud API voice notes + BSP relay pattern note
- **Meeting bots** — Recall.ai pattern (Zoom/Meet/Teams via single SDK)

## Status

- ✅ All 3 skills loadable by Claude Code
- ✅ All 3 SKILL.md pass frontmatter + reference-link checks
- ✅ All Python examples compile (`py_compile`)
- ✅ All TS examples type-check (`tsc --noEmit`)
- ✅ 28/28 unit tests pass (TDD-built scripts: latency, eval-prompt, translation-quality, latency-vs-quality)
- ⚠️ Real API smoke: WebSocket+SSL connection verified. `gpt-realtime-2` exists in `/v1/models` but is **rejected by the Realtime WS endpoint as of 2026-05-11** — see [smoke-findings](docs/research/smoke-findings-2026-05-11.md). Use `OPENAI_MODEL=gpt-realtime` until OpenAI wires v2 to the WS endpoint (estimated 2-8 weeks).

## Security notes

- **Rotate the API keys** that were shared in this session's chat history:
  - Azure: regenerate KEY 2 on `myvoiceagents` resource via Azure Portal.
  - OpenAI: revoke the `sk-proj-...` key and create a fresh one.
- `.env` is in `.gitignore` and never committed. The mirror in `skills-mirror/` contains no secrets.

## Docs

- [Design doc](docs/plans/2026-05-11-openai-realtime-voice-skills-design.md)
- [Implementation plan](docs/plans/2026-05-11-openai-realtime-voice-skills.md)
- [API research snapshot](docs/research/openai-realtime-api-2026-05-11.md)
- [Smoke findings](docs/research/smoke-findings-2026-05-11.md)
