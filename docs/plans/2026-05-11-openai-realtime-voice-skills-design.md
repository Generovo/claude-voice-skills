# OpenAI Realtime Voice — 3 Claude Code Skills

**Date:** 2026-05-11
**Auteur:** Youssef
**Statut:** Design validé, prêt pour la phase de planification d'implémentation.

## Contexte

Le 7 mai 2026, OpenAI a annoncé trois nouveaux modèles audio temps réel :

| Modèle | Rôle | Entrée | Sortie | Tool calling | Prix |
|---|---|---|---|---|---|
| `gpt-realtime-2` | Agent vocal qui raisonne et agit | texte / audio / image | texte / audio | ✅ + parallèle | $32 in / $64 out par 1M tokens audio |
| `gpt-realtime-translate` | Traduction vocale live | audio | audio + texte | ❌ | $0.034 / min |
| `gpt-realtime-whisper` | Transcription streaming basse latence | audio | texte | ❌ | $0.017 / min |

Sources :
- https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api/
- https://developers.openai.com/api/docs/models/gpt-realtime-2
- https://developers.openai.com/api/docs/models/gpt-realtime-translate
- https://developers.openai.com/api/docs/models/gpt-realtime-whisper

L'objectif est de créer **3 skills Claude Code globaux** pour accompagner la construction de voicebots multi-canaux (SIP/Twilio, WhatsApp, Web/WebRTC, mobile, meeting bots).

## Décisions de design

| Décision | Choix |
|---|---|
| Découpage | 3 skills séparés (1 par modèle) |
| Stack de code | TypeScript principal, Python pour téléphonie / WhatsApp / bridges |
| Canaux couverts | SIP/Twilio, WhatsApp, Web (WebRTC), mobile, meeting bots |
| Profondeur | Skill complet : `SKILL.md` + `references/` + `examples/` + `templates/` + `scripts/` |
| Emplacement | `~/.claude/skills/` (utilisateur global, à côté de `ui-ux-pro-max`) |

## Architecture

```
~/.claude/skills/
├── voice-agent-realtime/
│   ├── SKILL.md
│   ├── references/
│   │   ├── events-websocket.md
│   │   ├── tool-calling.md
│   │   ├── preambles-interruptions.md
│   │   └── pricing-limits.md
│   ├── examples/
│   │   ├── nextjs-route.ts
│   │   ├── webrtc-client.tsx
│   │   ├── twilio-bridge.py
│   │   ├── whatsapp-call.py
│   │   └── meeting-bot.py
│   ├── templates/
│   │   └── system-prompt.md
│   └── scripts/
│       ├── test-latency.py
│       └── eval-prompt.py
│
├── voice-translate-live/
│   ├── SKILL.md
│   ├── references/
│   │   ├── languages.md
│   │   ├── session-config.md
│   │   └── pricing-limits.md
│   ├── examples/
│   │   ├── twilio-interpreter.py
│   │   ├── web-conference.tsx
│   │   ├── whatsapp-translate.py
│   │   └── meeting-translator.py
│   └── scripts/
│       └── test-translation-quality.py
│
└── voice-transcribe-stream/
    ├── SKILL.md
    ├── references/
    │   ├── streaming-deltas.md
    │   ├── latency-tuning.md
    │   └── pricing-limits.md
    ├── examples/
    │   ├── live-captions.tsx
    │   ├── twilio-transcribe.py
    │   ├── whatsapp-voice-notes.py
    │   └── meeting-minutes.py
    └── scripts/
        └── test-latency-vs-quality.py
```

## Triggers (frontmatter `description`)

Chaque `description` doit charger les bons mots-clés pour que Claude active le skill au bon moment et qu'aucun ne se déclenche à la place d'un autre.

### `voice-agent-realtime`
> Use when building conversational voice agents (voicebots, callbots, vocal assistants) that need reasoning and tool calling with OpenAI's GPT-Realtime-2 model — including telephony (SIP/Twilio), WhatsApp calls, web (WebRTC), mobile, and meeting bots. Covers session config, audio formats, function calling in parallel, preambles, interruption handling, reasoning levels, and the WebSocket/WebRTC event protocol.

### `voice-translate-live`
> Use when building live voice translation features with OpenAI's GPT-Realtime-Translate model — interpreters, multilingual calls, conferences, live video translation. Covers the dedicated `/v1/realtime/translations` endpoint, multi-speaker session architecture, language support (70+ in / 13 out). Do NOT use for conversational agents (see voice-agent-realtime).

### `voice-transcribe-stream`
> Use when building streaming speech-to-text with OpenAI's GPT-Realtime-Whisper model — live captions, meeting transcription, voice notes, low-latency transcripts. Covers progressive deltas, latency tuning profiles (0.4s / 0.8-1.2s / 1.5-2s), and the streaming protocol. Do NOT use for conversational agents (see voice-agent-realtime) or translation (see voice-translate-live).

## Squelette commun de `SKILL.md`

1. When to use this skill
2. When NOT to use (redirige vers les 2 autres skills)
3. Model at a glance (table compacte : ID, prix, contexte, modalités)
4. Connection patterns (WebRTC vs WebSocket, decision matrix par canal)
5. Session lifecycle (6 étapes du protocole)
6. Tool calling — `voice-agent-realtime` uniquement
7. Preambles & interruption handling — `voice-agent-realtime` uniquement
8. Channel-specific transport (sections courtes par canal)
9. Reasoning levels — `voice-agent-realtime` uniquement
10. Common pitfalls
11. Evaluation
12. Reference index (pointeurs vers `references/`)

## Spécificités par skill

### `voice-agent-realtime` (le plus riche)
- Tool calling parallèle (nouveauté GPT‑Realtime‑2)
- Preambles (« je vérifie… », « un instant »)
- Reasoning levels : minimal / low (default) / medium / high / xhigh
- Image en entrée (cas rares mais supportés)
- Adversarial eval pattern (style Zillow 69→95%)

### `voice-translate-live`
- Endpoint dédié `/v1/realtime/translations`
- Architecture multi-locuteurs (1 session par langue de sortie + pistes audio séparées)
- Tableau langues (70+ entrée / 13 sortie) → `references/languages.md`
- Patterns de glissement quand 2 personnes parlent en même temps

### `voice-transcribe-stream`
- Deltas progressifs (UX sous-titres avant fin de phrase)
- 3 profils latency/quality : 0.4s ultra-low / 0.8-1.2s équilibré / 1.5-2s qualité max
- Contexte limité (16K tokens / 2K sortie) → pattern rotation pour longues sessions

## Scripts exécutables

| Skill | Script | Rôle |
|---|---|---|
| voice-agent-realtime | `test-latency.py` | Mesure TTFA + first_text + done par reasoning level |
| voice-agent-realtime | `eval-prompt.py` | Pass rate sur set adversarial avec LLM-as-judge |
| voice-translate-live | `test-translation-quality.py` | BLEU + COMET par paire de langues |
| voice-transcribe-stream | `test-latency-vs-quality.py` | Compare latency profiles, mesure WER |

Tous les scripts en Python (asyncio + websockets), lus depuis variables `.env`.

## Templates

`voice-agent-realtime/templates/system-prompt.md` — prompt système type :
- Rôle + persona
- Règles vocales (concision, pas de markdown, signaux d'écoute)
- Patterns de preamble
- Gestion silences / interruptions
- Fallback
- Variables `{{company}}`, `{{tools_available}}`, `{{escalation_policy}}`

## Examples (code prêt à copier-coller)

| Fichier | Stack | Rôle |
|---|---|---|
| `nextjs-route.ts` | Next.js 16 App Router | Ephemeral token côté serveur pour WebRTC client |
| `webrtc-client.tsx` | React 19 + AI SDK v6 | Connexion WebRTC, push-to-talk + VAD, transcript |
| `twilio-bridge.py` | FastAPI + WebSocket | Bridge Twilio Media Streams ↔ OpenAI Realtime |
| `whatsapp-call.py` | FastAPI | Webhook WhatsApp Cloud API + audio notes |
| `meeting-bot.py` | Python | Pattern bot Zoom via Recall.ai |

Chaque example a un header commenté avec commande de run et variables d'env requises.

## Anti-bloat

- Pas de mock complet d'API Twilio/WhatsApp — on pointe vers leurs docs.
- Pas de wrapper SDK maison — on utilise l'API officielle OpenAI directement.
- Pas de tests unitaires des examples — les scripts d'eval suffisent.
- Pas de section « histoire de l'API audio chez OpenAI » — uniquement ce qui sert au build.

## Critères de succès

1. Quand on demande « construis un voicebot WhatsApp qui prend des réservations », `voice-agent-realtime` se trigger automatiquement.
2. Quand on demande « ajoute des sous-titres live à mon meeting », `voice-transcribe-stream` se trigger.
3. Quand on demande « traduis en direct l'appel », `voice-translate-live` se trigger.
4. Aucun chevauchement de triggers sur les 3 skills.
5. Les scripts d'eval tournent tels quels avec `OPENAI_API_KEY` en env.
6. Les examples compilent / runnent tels quels (TS strict / Python 3.13).

## Prochaine étape

Invoquer le skill `writing-plans` pour produire un plan d'implémentation détaillé : ordre de création des fichiers, dépendances entre eux, points de vérification (lecture des docs OpenAI live pour valider les noms d'events/endpoints), tests manuels.
