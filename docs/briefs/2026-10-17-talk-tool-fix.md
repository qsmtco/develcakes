# Brief: talk tool — OpenRouter TTS endpoint fix

## Problem (verified live, 2026-10-17)

`talk` tool fails with "speech request failed". Root causes, in order:

1. **User config**: stored endpoint is the base URL `https://openrouter.ai/api/v1`
   — missing the `/audio/speech` path. POST to the base URL returns 404.
   The Settings UI placeholder already shows the correct form
   (`https://host/api/v1/audio/speech`).
2. **Missing `voice`**: `utils/speech.py::_fetch_audio` sends no `voice` field.
   OpenRouter's Gemini TTS rejects with 400: "An explicit voice is required
   for this TTS provider."
3. **Wrong `response_format`**: hardcoded `"mp3"`. OpenRouter's Gemini TTS
   rejects with 400: "Gemini TTS only supports response_format=\"pcm\"."

Live probe evidence (token redacted):

```
POST <base>/v1                      → 404 application/json
POST <base>/v1/audio/speech         → 400 "An explicit voice is required..."
POST + voice=algenib + fmt=mp3      → 400 "Gemini TTS only supports response_format=\"pcm\"."
```

## Required changes

### 1. `utils/speech_config.py` — add `voice` (and optional `format`) key

- Extend `_KEYS` handling: store `voice` (string, may be provider-specific).
  Keep backward compat: absent `voice` in speech.yaml must not error for
  providers that don't need it — make it optional in `load_speech_config`
  (return it when present, `""` when absent) but require it on save only if
  non-blank... (decide: simplest is optional key, default `""`).
- Add optional `format` key with default `"mp3"`, values `"mp3" | "pcm"`.
  Validate on save.

### 2. `utils/speech.py` — send voice + honor format; play PCM correctly

- `_fetch_audio`: include `voice` in the POST body when non-empty; use the
  configured `response_format` (`cfg.get("format") or "mp3"`).
- Temp file suffix: `.mp3` for mp3, `.raw` (or `.pcm`) for pcm.
- `_play` for PCM: `pw-play --format=f32 --rate=48000 --channels=1 <file>`
  may be wrong — Gemini TTS via OpenRouter returns PCM at 24kHz 16-bit mono
   (`audio/pcm;rate=24000` per OpenRouter docs). Use `pw-play --format=s16
  --rate=24000 --channels=1`, or better `ffplay -f s16le -ar 24000 -ch_layout
  mono -nodisp -autoexit -loglevel quiet`. Prefer ffplay for PCM if available,
  else pw-play with explicit flags. Content-Type header from the response
  may carry the exact rate — parse `rate=` from `audio/pcm;rate=NNNN` when
  present, default 24000.
- Keep: token never logged, error strings never leak endpoint/token.

### 3. Settings UI (`ui/views/settings_dialog.py` speech page)

- Add a "Voice" entry next to model (optional, placeholder e.g. `algenib`).
- Add a "Format" dropdown/entry: `mp3` | `pcm` (default mp3).
- Save via `save_speech_config(endpoint, model, token, voice=..., format=...)`.

### 4. Fix the user's config (one-time, this project)

After the code fix lands, update `~/.config/develcakes/speech.yaml`:
endpoint → `https://openrouter.ai/api/v1/audio/speech`, voice → `algenib`,
format → `pcm`.

### 5. Tests

- `tests/test_speech_speak.py`: cover voice-in-body, pcm format request,
  mp3 still default, content-type `audio/pcm;rate=24000` parsing, ffplay
  command construction for PCM, error paths (404/400) unchanged messages.
- `tests/test_speech_config.py`: voice/format round-trip, defaults, blank
  voice allowed, invalid format rejected on save.

## Constraints

- Token stays out of logs/results (existing invariant — keep it).
- No new deps; urllib + existing players only.
- All tests green: `python3 -m pytest tests/test_speech_speak.py tests/test_speech_config.py -q`
  and full suite smoke if cheap.
