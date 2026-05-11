# claude-voice-skills

> Plugin Claude Code de **Generovo** — trois skills pour construire des voicebots avec l'API OpenAI Realtime (release du 7 mai 2026).

[![Validation](https://img.shields.io/badge/validation-passing-green)]() [![Skills](https://img.shields.io/badge/skills-3-blue)]() [![License](https://img.shields.io/badge/license-MIT-lightgrey)]()

## Skills inclus

| Skill | Modèle OpenAI | Cas d'usage | Tarif |
|---|---|---|---|
| `voice-agent-realtime` | `gpt-realtime-2` | Voicebots, callbots, assistants vocaux avec tool calling | $32 in / $64 out par 1M tokens audio |
| `voice-translate-live` | `gpt-realtime-translate` | Traduction vocale live (70+ in / 13 out) | $0.034/min |
| `voice-transcribe-stream` | `gpt-realtime-whisper` | Transcription streaming basse latence | $0.017/min |

Chaque skill ship :
- `SKILL.md` — when to use, when not, session lifecycle, connection patterns
- `references/` — catalogue d'événements, pricing, latency tuning
- `examples/` — code TypeScript (Next.js 16 + React 19 + WebRTC) + Python (FastAPI + Twilio / WhatsApp / Recall.ai)
- `scripts/` — utilitaires de latency et eval testés en TDD
- `templates/` (voice-agent uniquement) — prompt système bilingue paramétré

## Installation (collaborateurs Generovo)

### Une fois par machine

```bash
# Ajoute la marketplace Generovo (une seule fois)
/plugin marketplace add Generovo/claude-voice-skills

# Installe le plugin
/plugin install claude-voice-skills@generovo-voice
```

Les 3 skills sont automatiquement disponibles dans toutes tes sessions Claude Code. Pas besoin de redémarrer.

### Vérification

Tape une demande dans Claude Code :

> "Construis un voicebot WhatsApp qui prend des réservations"

Claude doit annoncer qu'il charge `voice-agent-realtime`. Si tu demandes une traduction live ou des sous-titres, il chargera les autres skills.

### Mise à jour

```bash
/plugin update claude-voice-skills
```

## Configuration

Les exemples de code utilisent ces variables d'env (à mettre dans le `.env` de ton projet voicebot, jamais dans le plugin) :

```bash
# Authentification OpenAI
OPENAI_API_KEY=sk-proj-...
OPENAI_MODEL=gpt-realtime-2          # défaut

# Twilio (si tu utilises les bridges téléphoniques)
TWILIO_AUTH_TOKEN=...
PUBLIC_WS_URL=wss://<tunnel>/media

# WhatsApp Cloud API (si tu utilises whatsapp-call.py)
WHATSAPP_ACCESS_TOKEN=...
WHATSAPP_PHONE_NUMBER_ID=...
WHATSAPP_VERIFY_TOKEN=...
WHATSAPP_APP_SECRET=...

# Recall.ai (si tu utilises meeting-bot.py)
RECALL_API_KEY=...
```

**macOS uniquement** : si tu lances les scripts Python depuis une session Claude Code sur Mac, exporte :
```bash
export SSL_CERT_FILE=$(python3 -m certifi)
```
(Sinon SSL handshake échoue avec le Python système.)

## Important — Modèles V2 et headers HTTP

Les 3 modèles V2 (announce 7 mai 2026) sont **GA-only**. **NE PAS envoyer** le header `OpenAI-Beta: realtime=v1` — il route vers la beta API qui n'a pas V2. Les exemples du plugin sont déjà nettoyés. Détails dans [docs/research/smoke-findings-2026-05-11.md](docs/research/smoke-findings-2026-05-11.md).

## Triggers (comment Claude Code charge le bon skill)

| Tu demandes... | Skill chargé |
|---|---|
| « callbot téléphonique qui prend des RDV » | `voice-agent-realtime` |
| « assistant vocal WhatsApp avec function calling » | `voice-agent-realtime` |
| « traduction live FR↔AR pour un appel commercial » | `voice-translate-live` |
| « interprète automatique pour une vidéo YouTube live » | `voice-translate-live` |
| « sous-titres live pour un meeting Zoom » | `voice-transcribe-stream` |
| « transcription temps réel des appels du call center » | `voice-transcribe-stream` |

Les descriptions de skills sont structurées pour éviter les chevauchements. Chaque SKILL.md a une section explicite « When NOT to use » qui redirige vers les bons skills voisins.

## Pour contribuer

```bash
git clone https://github.com/Generovo/claude-voice-skills.git
cd claude-voice-skills

# Validation locale
bash scripts/validate-skills.sh
# → ALL SKILLS VALID

# Unit tests
for s in voice-agent-realtime voice-translate-live voice-transcribe-stream; do
  (cd skills/$s/scripts && python3 -m pytest -q)
done
# → 28 passed total
```

Structure du repo :

```
claude-voice-skills/
├── .claude-plugin/
│   ├── plugin.json           # Manifest du plugin
│   └── marketplace.json      # Marketplace generovo-voice
├── skills/                   # Les 3 skills
│   ├── voice-agent-realtime/
│   ├── voice-translate-live/
│   └── voice-transcribe-stream/
├── scripts/
│   └── validate-skills.sh    # Harness de validation
├── docs/
│   ├── plans/                # Design + plan d'implémentation
│   └── research/             # Snapshot API OpenAI 2026-05-11
└── README.md
```

## État

- ✅ 3 skills `ALL SKILLS VALID` côté harness
- ✅ 28/28 unit tests verts (TDD : latency, eval-prompt, translation-quality, latency-vs-quality)
- ✅ Smoke réel validé sur `gpt-realtime-2` : TTFA ~700-800ms, round-trip complet
- ✅ tsc + py_compile clean pour tous les exemples
- ✅ Compatible Python 3.12+ / Node 24+

## Liens

- [Design doc](docs/plans/2026-05-11-openai-realtime-voice-skills-design.md)
- [Implementation plan](docs/plans/2026-05-11-openai-realtime-voice-skills.md)
- [API research snapshot (2026-05-11)](docs/research/openai-realtime-api-2026-05-11.md)
- [Smoke findings (V2 GA gotchas)](docs/research/smoke-findings-2026-05-11.md)

## Licence

MIT — voir [LICENSE](LICENSE).
