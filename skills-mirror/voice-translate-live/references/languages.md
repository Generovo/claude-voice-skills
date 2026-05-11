# Languages — voice-translate-live

Reference for the language coverage of `gpt-realtime-translate`. All facts here trace to `/Users/youssef/AgentVocalOpco/docs/research/openai-realtime-api-2026-05-11.md`. Items the research file did not enumerate are tagged `[UNCONFIRMED]`.

## Input languages (70+)

The OpenAI announcement states the model accepts **70+ input languages**. The exhaustive list is **not** enumerated in any of the source pages fetched (neither the model card at `https://developers.openai.com/api/docs/models/gpt-realtime-translate` nor the translation guide at `https://developers.openai.com/api/docs/guides/realtime-translation`).

`[UNCONFIRMED — confirm the exhaustive list at https://developers.openai.com/api/docs/models/gpt-realtime-translate before relying on a specific language for production.]`

The single concrete language code shown in any source is `"es"` (Spanish, ISO-639-1). All input-language identifiers are assumed to follow the same standard (two-letter ISO-639-1 codes such as `en`, `fr`, `de`, `ar`, `zh`, `ja`, `hi`, `ta`, `te`, …).

**Languages with explicit announcement coverage** (cited in the OpenAI announcement's quoted benchmark):
- Hindi (`hi`)
- Tamil (`ta`)
- Telugu (`te`)
  - Benchmark: 12.5% lower Word Error Rate than competing models on these three (BolnaAI eval cited in the announcement).

For any other specific language: assume supported if the announcement's "70+" headline number includes it (high likelihood for major world languages), but verify with a small live probe before committing production traffic.

## Output languages (13)

The OpenAI announcement states **13 output languages**. The exhaustive list is **not** enumerated in any of the source pages fetched.

`[UNCONFIRMED — confirm the exhaustive list at https://developers.openai.com/api/docs/models/gpt-realtime-translate before relying on a specific language for production.]`

Output language is configured via `audio.output.language` in the `session.update` payload (see `references/session-config.md`). The translation guide example uses the ISO-639-1 code `"es"`. All output-language codes are assumed to follow ISO-639-1.

Likely candidates for the 13 output languages, based on typical commercial translation product coverage (English, Spanish, French, German, Italian, Portuguese, Dutch, Chinese (Simplified), Japanese, Korean, Arabic, Hindi, Russian) — but this is **inference, not documentation**. `[UNCONFIRMED — verify before relying on any specific code.]`

## Pair-selection tips

- **Pick the closest supported output language to your audience.** If your listener is a Brazilian Portuguese speaker and only `pt` (European Portuguese) is supported as output, expect mild dialectal mismatch. Where dialect codes (`pt-BR`, `zh-TW`) are supported, prefer them; `[UNCONFIRMED]` whether subtag codes are accepted at all — the only code seen in any example is the bare two-letter `"es"`.
- **Latency increases for low-resource source languages.** Source languages with thin training coverage tend to produce visibly slower translations and lower WER. Where a speaker is bilingual, prefer the source language with stronger model coverage.
- **Code-switching / mixed-language input** (a speaker switching between two languages mid-utterance): `[UNCONFIRMED]` — neither the model card nor the translation guide addressed this. Heuristic: real-world translation models often degrade quality on code-switched audio. Test on representative samples before committing.
- **Right-to-left script targets** (Arabic, Hebrew): the audio output is unaffected by script direction, but downstream **transcript rendering** in your UI must handle RTL. The `session.output_transcript.delta` stream emits target-language text — wrap it in `<bdi>` or set `dir="auto"` in HTML.
- **Tonal source languages** (Mandarin, Vietnamese, Thai): tone-bearing source audio at telephony quality (8 kHz μ-law) suffers more than European source audio. Resample to PCM16 24 kHz upstream of the API call for these sources.
- **One session per target language.** If two listeners want different target languages, open two sessions and fan the input audio out to both — do **not** try to switch `audio.output.language` mid-session for one listener and then another. See `SKILL.md ## Multi-speaker architecture`.

## Sources

- `https://developers.openai.com/api/docs/models/gpt-realtime-translate` — model card (70+/13 referenced; exhaustive list not present)
- `https://developers.openai.com/api/docs/guides/realtime-translation` — translation guide (only `"es"` code shown)
- `https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api/` — announcement (70+/13 stated; Hindi/Tamil/Telugu benchmark cited)
