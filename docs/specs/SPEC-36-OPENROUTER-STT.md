# SPEC-36: OpenRouter cloud STT — replace local faster-whisper

Source proposal: docs/proposals/DEVELCAKES-V2-CHANGE-LIST.md §6.1 (verified live).
Scope: STT half ONLY. The Improve button is OUT OF SCOPE.

## Goal

The dictation ("prompt") button keeps its exact push-to-talk UX but transcription
moves from local faster-whisper to OpenRouter's cloud STT endpoint. Local mic
CAPTURE stays (arecord/PipeWire 16kHz mono WAV); local TRANSCRIPTION is deleted.

## New file: utils/stt.py (REWRITE — same path, same public class name)

Public API UNCHANGED: `STTEngine(on_result=cb)`, `.start()`, `.stop_async(on_done=)`,
`.stop() -> str`, `.is_recording`. MediaHandler's STT paths work unchanged EXCEPT
the round-2 BUG #2 gate (on_stt_click re-reads is_recording after start()).

New internals:

1. CAPTURE (unchanged): arecord -D <device> -f S16_LE -t wav -r 16000 -c 1,
   background daemon thread, frames buffered in memory.
2. TRANSCRIBE (new): POST to OpenRouter:
   - URL: https://openrouter.ai/api/v1/audio/transcriptions
   - Body: { "model": "microsoft/mai-transcribe-2",
             "input_audio": { "data": "<base64 wav>", "format": "wav" },
             "provider": { "options": { "azure": {
                 "phraseList": { "phrases": [ ... bias list ... ] } } } } }
   - Auth: Bearer <OPENROUTER_API_KEY>
   - Response: { "text": "...", "usage": { "seconds": ..., "cost": ... } }
   - Timeout: 20s. On HTTP error or empty text → return "" (same as old engine
     on silent audio) and log a WARNING with status code + response body (truncated
     to 500 chars).
3. Keyword biasing (THE reason this model was chosen):
   - Default phrases: ["DevelCakes", "Crabcakes", "crabcakes"]
   - Config: env var STT_PHRASES — comma-separated, overrides default. Empty string
     → send NO phraseList at all.
4. API key resolution order:
   a. env OPENROUTER_API_KEY
   b. agent config: `agent.config.get_api_key("openrouter")`
   c. None → start() logs a WARNING and refuses to record (state stays idle).
      MediaHandler.on_stt_click MUST re-read engine.is_recording after start()
      to set UI state (refused start ⇒ UI stays idle) — round-2 amendment, BUG #2.
   - Do NOT hardcode keys. Use the existing get_api_key helper.
5. HTTP: use `requests` (already a project dep) in the background thread only.
   NO new dependencies. NO requests import at module level — import inside the
   transcribe method (keeps module import cheap and test-friendly).
6. GLib dispatch: keep the existing pattern — try gi.repository GLib.idle_add for
   the callback; plain call on ImportError (already in the old stop_async worker).

## File changes

| File | Change |
|------|--------|
| utils/stt.py | Full rewrite per above (219 lines → cloud engine) |
| ui/handlers/media_handler.py | MINIMAL (round 2, audit BUG #2/#5): `on_stt_click` re-reads `is_recording` AFTER `start()` to set UI state; comment fixes only otherwise |
| ui/window.py:314 | Comment-only: drop "whisper.cpp" reference (audit BUG #5) |
| tests/test_low6_8_9_10_11.py | DELETE (LOW-6 model-size tests are moot) |
| tests/test_low8_9_10_11.py | NEW — verbatim LOW-8/9/10/11 split |
| tests/test_stt_cloud.py | NEW (round 2, audit BUG #6): unit tests — phrases parsing, request body, payload guards, declining start, on_stt_click sync. No network, no GTK. |
| README.md | Voice Input section + env table (STT_MODEL_SIZE → STT_PHRASES), dep swap |
| pyproject.toml | faster-whisper → requests |
| docs/ARCHITECTURE.md | Update the stt.py + media_handler descriptions in the file tree + §3.16 |

Round-2 amendments (Debugger audit):
- Capture is `-t raw` (headerless PCM) — BUG #1: `-t wav` emitted a RIFF header that
  `_transcribe_frames` re-wrapped, double-headering the payload.
- `_worker` wraps `_transcribe_frames` in try/except → log + callback("") so no
  silent dead threads — BUG #1b.
- `_transcribe_frames` validates `isinstance(data, dict)` and `isinstance(text, str)`
  before use; anything else → WARNING + "" — BUG #3.
- Any all-blank STT_PHRASES value disables biasing (documented; "" is just the
  common case) — BUG #7.

## Non-goals

- Improve button (§6.2) — do not touch
- Diarization/timestamps (verbose_json) — do not add
- UI changes of any kind
- Changing MediaHandler, window.py wiring, or main_content.py

## Verification plan (Supervisor runs these — Coder does NOT run tests)

- pytest tests/test_media_handler.py -q
- pytest tests/test_architecture.py -q
- py_compile on all changed files
- Review-layer diff review
- Debugger adversarial audit

## Traps (from the proposal — all measured)

- phraseList shape: {"phrases": ["str", ...]} ONLY. Objects inside the array → 400.
- Top-level fields like keywords/prompt/vocabulary are silently DROPPED (200 OK) —
  do not use them.
- Do not pick a different model — mai-transcribe-2 is the only one with biasing.
- Latency budget ~1s typical, ~4s worst; the spinner state ("recording") is fine.
