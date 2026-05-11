# Smoke-test findings — 2026-05-11 (corrected)

Real API probes against OpenAI Realtime, ~4 days after the May 7 release of `gpt-realtime-2`, `gpt-realtime-translate`, `gpt-realtime-whisper`.

> **Correction** : un test initial avait conclu à tort que `gpt-realtime-2` n'était pas dispo sur le WS endpoint. **Les 3 modèles sont en réalité 100% disponibles sur OpenAI direct.** Le faux négatif venait d'un header de routage incorrect (`OpenAI-Beta: realtime=v1` envoyé par les scripts). Ce document remplace la version initiale.

## Setup

- OS: macOS 14.x (Darwin 25.4)
- Python: 3.12.8
- Auth: `OPENAI_API_KEY` (sk-proj prefix) in `.env`
- Cert bundle: `SSL_CERT_FILE=$(python3 -m certifi)` required on macOS — system Python doesn't trust public CAs out of the box.

## Finding 1 — Les 3 modèles V2 fonctionnent dès la release

| Modèle | WS URL | Statut |
|---|---|---|
| `gpt-realtime-2` | `wss://api.openai.com/v1/realtime?model=gpt-realtime-2` | ✅ |
| `gpt-realtime-translate` | `wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate` | ✅ |
| `gpt-realtime-whisper` | `wss://api.openai.com/v1/realtime?model=gpt-realtime-whisper` | ✅ |

Bonus : `gpt-realtime-whisper` utilise **le même endpoint base** que l'agent conversationnel (`/v1/realtime`), pas un `/v1/realtime/transcription_sessions` séparé. Le protocole distingue via `session.type = "transcription"`. Ça résout un `[UNCONFIRMED]` du fichier de recherche.

## Finding 2 — **NE PAS envoyer `OpenAI-Beta: realtime=v1` pour les modèles V2**

Le message d'erreur exact du serveur :

> `Model "gpt-realtime-2" is only available on the GA API.`

Avec le header `OpenAI-Beta: realtime=v1` la requête est routée vers la **beta API** qui n'a PAS les modèles V2. Sans le header → **GA API** qui les a tous.

**Fix appliqué** : header retiré de tous les fichiers Python des 3 skills (`twilio-bridge.py`, `whatsapp-call.py`, `meeting-bot.py`, `twilio-interpreter.py`, `whatsapp-translate.py`, `meeting-translator.py`, `twilio-transcribe.py`, `whatsapp-voice-notes.py`, `meeting-minutes.py`, `test-latency.py`, `eval-prompt.py`, `test-latency-vs-quality.py`).

## Finding 3 — `turn_detection: null` (pas `{"type": "none"}`) pour désactiver VAD

Pour les flows manuels (commit + response.create explicites), la doc beta utilisait `{"type": "none"}` mais la GA API rejette avec :

> `Invalid value: 'none'. Supported values are: 'server_vad' and 'semantic_vad'.`

La forme acceptée est **`turn_detection: null`** (la valeur JSON `null`, pas une string). Live-confirmé que ça désactive complètement le VAD :

```json
"turn_detection_resolved": null
```

**Fix appliqué** dans `test-latency.py`, `eval-prompt.py`, `whatsapp-call.py`.

## Finding 4 — `audio.output.format.rate` est OBLIGATOIRE

La doc beta acceptait `format: {"type": "audio/pcm"}`. La GA API exige aussi `rate` :

> `Missing required parameter: 'session.audio.output.format.rate'.`

**Fix appliqué** : tous les session.update incluent maintenant `format: {"type": "audio/pcm", "rate": 24000}` côté output.

## Finding 5 — Numbers réels mesurés

`test-latency.py --level low --runs 2 --model gpt-realtime-2` (sine wave 1s en entrée) :

```
run 1: ttfa=0.820s  done=1.454s
run 2: ttfa=0.697s  done=3.053s
median TTFA: 0.758s
```

Les reasoning levels `low` (default) donnent du TTFA sub-secondaire pour des entrées triviales. Plus la complexité monte (reasoning `high`/`xhigh`), plus le TTFA monte.

## Finding 6 — Events réels émis par gpt-realtime-2

Capturés via probe direct sur un round-trip complet (1s audio sine wave → modèle répond brièvement) :

```
session.created
session.updated
input_audio_buffer.committed
response.created
response.output_item.added
response.content_part.added
response.output_audio.delta × 2
response.output_audio_transcript.delta × 2
response.output_audio.done
response.output_audio_transcript.done
response.content_part.done
response.output_item.done
conversation.item.added × 2
conversation.item.done × 2
response.done
```

Confirme que les noms d'events utilisés dans les skills sont corrects. **Aucun event n'a été inventé.**

## Finding 7 — `OPENAI_API_KEY` valide après nettoyage du header

L'auth Bearer fonctionne. Le 1er error path (`invalid_model`) venait du HEADER de routage et NON de la clé. La clé est bonne.

## Actions appliquées dans le code

Tous les fichiers fixés (live + mirror) :

1. Retrait de la ligne `("OpenAI-Beta", "realtime=v1"),` (sed sur 18 fichiers).
2. `turn_detection: {"type": "none"}` → `turn_detection: None` (3 fichiers).
3. Ajout de `"rate": 24000` dans `audio.output.format` (3 fichiers).
4. py_compile re-vérifié pour tous → OK.
5. 28/28 unit tests verts.
6. Validation harness : `ALL SKILLS VALID`.

## Action recommandée pour toi

1. **Régénère la clé OpenAI** `sk-proj-...` (elle est passée dans l'historique de chat — révoque-la depuis https://platform.openai.com/api-keys).
2. **Régénère la KEY 2 Azure** sur `myvoiceagents` (même raison).
3. **Set `OPENAI_MODEL=gpt-realtime-2`** dans ton `.env` quand tu déploies — les modèles sont prêts. Plus besoin d'attendre.
4. Si tu veux smoke avec un audio réel : ajoute un WAV mono 16-bit 24kHz dans `~/.claude/skills/voice-agent-realtime/scripts/samples/` puis `python3 test-latency.py --audio samples/your.wav`.
