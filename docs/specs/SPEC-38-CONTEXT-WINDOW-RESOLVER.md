# SPEC-38: Context Window is the provider's window — remove the 200k clamp

Source: PM directive, live session 2026-10-16 ("The Context Window in provider
settings must be the window the agent uses"). Verified against source before
spec-writing; all claims confirmed.

## Problem

The Settings field labeled "Context Window" saves its number as `max_tokens`
only (`_collect_from_form`). A second field, `context_window`, is copied
unchanged on Save. `_compute_model_max` uses `context_window` when > 0;
otherwise `max_tokens` but ONLY up to `_MAX_SANE_WINDOW` (200_000). The
system-prompt budget (create_conversation, _ensure_system_prompt,
_rebuild_conversation_context) reads raw `max_tokens` off the DEFAULT
provider — ignoring `context_window` AND the conversation's own provider.
Test Connection can write a discovered window into `max_tokens` and leave
`context_window` stale. Net effect: the meter lies (60% of a phantom 200k),
UI edits desync from stored fields, and cards above 200k are silently clamped.

## Changes

### 1. agent/runtime.py — delete the clamp

- Remove `_MAX_SANE_WINDOW = 200_000` (:310) and the `min(int(max_tokens),
  _MAX_SANE_WINDOW)` in `_compute_model_max` (:1395).
- The resolver order becomes: `context_window` (int > 0) → `max_tokens`
  (int > 0, UNCAPPED) → `caller_default_max_tokens(caller)` → `128_000`.
  Update its docstring (:1360-1372) to match.

### 2. ui/views/settings_dialog.py `_collect_from_form` — dual write

- The Context Window spin value goes into BOTH `max_tokens` and
  `context_window`. No code path may keep an old `context_window` while
  `max_tokens` changes.

### 3. ui/handlers/settings_handler.py — Test Connection dual write

- Where the discovered window is stored (:267-276, `replace(p, max_tokens=...)`),
  write the SAME number into both fields. PRESERVE the existing
  `user_has_customized` sentinel logic exactly (BUG #7 guard): when the user
  HAS customized, nothing is written to either field.

### 4. System-prompt budget uses the resolver

- `create_conversation`, `_ensure_system_prompt`, and
  `_rebuild_conversation_context` must derive the budget window via the
  `_compute_model_max` resolver FOR THE CONVERSATION'S PROVIDER — never a
  raw `max_tokens` read off the default provider. Audit each function's
  provider-resolution path (conversation's provider first, default provider
  only as fallback when the conversation names none).

### 5. docs/ARCHITECTURE.md §3.11b

- Remove the "capped at `_MAX_SANE_WINDOW` (200_000)" wording (:905);
  document the uncapped order and the settings dual-write.

## Leave ALONE (do not touch)

- `compaction_threshold` semantics (0.60 = heavy shrink at 60% of resolved window)
- The landing goal: `min(15% of window, 24_000)`
- The Anthropic REQUEST field `max_tokens: 4096` (reply-length cap, different beast)
- `CALLER_DEFAULT_MAX_TOKENS` incl. Anthropic 200_000 / MiniMax 1_048_576
- `~/.config/crabcakes` (v1 dir — never written)

## Tests

1. RETARGET `tests/test_runtime_compaction.py::test_custom_threshold_per_provider`:
   `max_tokens=1_048_576, context_window=0` → `hard == 1_048_576` (not 200_000).
2. RETARGET `tests/test_context_strategy_tier0.py::test_fat_max_tokens_is_capped_without_context_window`
   (rename appropriately): `max_tokens=1_300_000`, no `context_window` →
   hard ceiling is `1_300_000`; keep asserting landing goal 24_000 and the
   context_window-wins test (:40) unchanged.
3. NEW settings test (real dialog, tmp config dir — test_stt_settings_page.py
   pattern): spin set to 1_000_000 with existing card `context_window=200_000`
   → Save → BOTH fields on disk are 1_000_000.
4. NEW runtime test: after such a save, `_compute_model_max` AND the
   system-prompt budget both resolve 1_000_000.

## Verification plan (Supervisor)

- xvfb: settings-page tests + test_settings_dialog.py
- pytest: test_runtime_compaction.py test_context_strategy_tier0.py
- Mutation spot-checks on new/retargeted tests
- Debugger adversarial audit before commit
