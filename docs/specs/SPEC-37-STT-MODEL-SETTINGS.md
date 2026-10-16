# SPEC-37: Configurable STT model — settings page + config store

Source request: PM, live session 2026-10-16. Dropdown of measured models
plus "Custom…" free-text entry. Settings page alongside Telegram Bridge /
Decision Model.

## Goal

The dictation model (`microsoft/mai-transcribe-2`, currently hardcoded as
`_STT_MODEL` in utils/stt.py) becomes user-configurable: a new "STT Model"
page in the Settings dialog, persisted app-level, resolved per-transcription.

## New file: utils/stt_config.py

Pure Python, no GTK. Mirrors telegram_bridge.yaml persistence pattern.

- `DEFAULT_STT_MODEL = "microsoft/mai-transcribe-2"`
- `STT_MODEL_CHOICES`: list of (model_id, label) from the measured table:
  - microsoft/mai-transcribe-2 — "keyword biasing + diarization (default)"
  - mistralai/voxtral-mini-3b-2507 — "cheapest accurate"
  - mistralai/voxtral-small-24b-2507-stt — "larger voxtral"
  - openai/gpt-4o-transcribe — "accurate"
  - openai/whisper-large-v3 — "cheap; number-format quirks"
- `get_stt_model() -> str` — env STT_MODEL → stt.yaml `model` → default.
  Invalid/blank stored value → default + WARNING.
- `set_stt_model(model_id: str)` — atomic write to `<config_dir>/stt.yaml`
  (`model:` key, same chmod discipline as providers.yaml).
- `is_bias_supported(model_id) -> bool` — True only for models measured to
  accept provider.options.azure.phraseList (currently: mai-transcribe-2).
  Custom/unknown models → False.

## utils/stt.py changes

- Remove `_STT_MODEL` constant; import nothing at module level from
  stt_config (lazy import inside the request path, like requests).
- `_build_request_body` takes the model id; caller resolves
  `stt_config.get_stt_model()` per transcription (NOT cached in __init__ —
  settings changes must apply on the next dictation without app restart).
- phraseList block emitted ONLY when `is_bias_supported(model)` is True.
  Non-Azure models never receive provider.options.azure (would 400 or drop).

## Settings UI — ui/views/settings_dialog.py

New section, same Stack pattern as Telegram Bridge (§SPEC-15 SP2) and
Decision Model (§SPEC-22 §2.8):

- `self._stack.add_titled(stt_page, "stt", "STT Model")` — placed after
  Decision Model.
- Intro label: what the setting controls, cost scale (~$0.0004/15s typical).
- `Gtk.DropDown` from STT_MODEL_CHOICES labels + final entry "Custom…".
  Selecting Custom… reveals a `Gtk.Entry` (placeholder: "provider/model-id")
  + Apply button. Current value pre-selected on open (custom id shows in the
  entry).
- **ROUND 2 (audit BUG #1): selecting a KNOWN model persists immediately** —
  `_on_stt_dropdown_changed` calls `set_stt_model(self._stt_model_ids[selected])`
  when the index is a known model (not Custom…), wrapped in try/except →
  status label on failure. The dropdown is the primary control; it must work
  without touching the Custom row.
- **ROUND 2 (audit BUG #2): `_refresh_stt_section` syncs BOTH widgets** —
  `show = index == len(self._stt_model_ids)`; `custom_entry.set_visible(show)`
  AND `_stt_apply_btn.set_visible(show)`. No stray Apply next to a hidden
  entry, ever — including fresh open and post-Apply collapse.
- **ROUND 2 (audit BUG #3): page-level tests in tests/test_settings_dialog.py**
  (or a new tests/test_stt_settings_page.py, xvfb/GTK-marked like existing
  settings tests): (i) selecting a known index persists it (fake store or
  tmp config dir, assert on-disk value); (ii) fresh open with known model on
  disk ⇒ Apply button NOT visible; (iii) degraded store ⇒ all three controls
  insensitive.
- Save button persists via `set_stt_model`. Read errors render the section
  inert with a dim label (honest, no dead controls — Telegram pattern).
- Hint label: "Keyword biasing (DevelCakes/Crabcakes) applies only to
  mai-transcribe-2."
- Pure view: no file I/O in the page beyond calling stt_config functions.

## Tests — NEW tests/test_stt_config.py

Unit only, no GTK/network:
- get_stt_model: unset → default; env override; yaml value; blank yaml → default+WARNING
- set_stt_model round-trip (tmp config dir); atomic-write leaves old file on failure
- is_bias_supported: default True, custom False, other-known False
- stt.py integration via fakes: request body carries selected model id;
  phraseList present for mai-transcribe-2, absent for a custom model;
  STT_PHRASES=empty + bias-supported model → no phraseList (env still wins)

## Docs

- ARCHITECTURE.md: §2 tree (stt_config.py), §3.16 note (model resolved per-call
  from stt.yaml; biasing gated on is_bias_supported), env table + STT_MODEL.
- README: Voice Input section — model is configurable in Settings → STT Model.

## Non-goals

- Fetching/enumerating models from the OpenRouter API at runtime
- Per-model advanced options (diarization, timestamps)
- Changing the mic button UX or capture path
- Touching media_handler.py or window.py wiring

## Verification plan (Supervisor)

- pytest tests/test_stt_config.py tests/test_stt_cloud.py tests/test_media_handler.py -q
- Mutation spot-check on the new test file (granted rule, pre-commit)
- Debugger adversarial audit before commit
