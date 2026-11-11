# SPEC-62: Fresh-install agent edit drawer + repair the global default provider

**Date:** 2026-11-04
**Status:** Draft — for implementation (v3)
**Target:** main, after `e6489bc7`
**Pre-flight:** two Debugger adversarial audits (v1: 5 bugs, v2: 8 bugs) — all folded in below. PM ruling: **option (A)**, repair the global-default contract. No product code written yet; only this spec and `.crabcakes/phase*-spec62.md` exist.

---

## Problem

On a fresh install, double-clicking a built-in agent (Coder/Debugger/Supervisor) silently does nothing, or the rows are absent entirely. Root cause chain, verified by reproduction and two audits:

1. `prompts/default_agents/*.yaml` ship `llm_name: local-kb` — a placeholder naming a provider the user has never configured. `utils/agent_defs.py:666-680` rejects non-empty unknown llm_name → after the first provider save, `load_agent_defs()` drops all 3 defs → `load_for_edit(name)` → None → `open_agent_drawer` returns False before creating a Revealer (`ui/views/left_panel.py:331-332`). Silent no-op.
2. If the provider is configured BEFORE first launch, `load_agent_defs()` = 0 at boot → `ARH.get_special_agents()` = `{}` → zero agent rows.
3. `_on_providers_changed` (`ui/window.py:2279-2288`) never invalidates the cached registry, so rows and live defs disagree in both orderings.
4. **There is no working global-default path at all**: `agent.json`'s `default_provider` AND `default_model` are `""` and nothing writes either. `_resolve_agent_model` (`ui/agent_runtime/provider.py:24-27`) returns `None` on a falsy llm_name → `conv.model` ends up `""` → the wire call has no model. The dev machine works only because its defs name real cards and its agent.json was hand-edited.
5. `_seed_defaults()` never overwrites an existing file (`utils/agent_defs.py:200-208` isfile guard) — so **existing installs keep the broken `local-kb` copy**. Removing the line from the shipped YAMLs fixes only machines that never launched DevelCakes.
6. With exactly ONE provider card, Agent Builder's fallback dropdown excludes the primary (`ui/views/agent_builder.py:537`) while Save requires a fallback (`:940`) → Save permanently disabled.
7. A wheel install contains zero `prompts/` entries (no `prompts/__init__.py`, no package-data/MANIFEST.in). Deferred — documented install is `pip install -e .`.

## PM ruling: option (A)

**Repair the global-default contract**, with a one-time migration for already-seeded configs. Do not hard-bind seeded YAMLs to a card name.

Authority: `docs/ARCHITECTURE.md` §3.21 + `agent/special_agents.py:42` say `llm_name` is optional (`None → global default`). `agent/config.py:85-86` already declares `default_provider`/`default_model`; `agent/config.py:351` already has an atomic `set_agent_setting`. This spec makes the code conform. No ARCHITECTURE.md change.

## Scope

### Phase 1 — data + validator + tests (Coder, main tree)
1. `prompts/default_agents/{coder,debugger,supervisor}.yaml` — remove `llm_name: local-kb`; keep `fallback_provider: openrouter` and every other field. Update the stale comment. Loaded dict must have FALSY llm_name.
2. `utils/agent_defs.py`:
   - `validate_agent_def`: `llm_name` OPTIONAL — remove `Missing required field: llm_name`; KEEP the `if llm_name:` unknown-provider error; keep `fallback_provider` required and the helper-role exemption. Update docstring.
   - **Migration in `_seed_defaults()`** (after the per-file copy loop): for the three built-in filenames, if the user's copy contains `llm_name: local-kb` (the exact shipped placeholder), rewrite ONLY that key to `""` in place (atomic write, preserve all other fields/comments where practical — a line-level rewrite of the `llm_name:` line is acceptable). Never touch a copy whose llm_name is any other value (user customization). Idempotent: rerunning changes nothing. Log one INFO per migrated file. Track with a marker key `spec62_migrated: true` in agent.json via `get_agent_setting`/`set_agent_setting` so it runs at most once per install — OR simply gate on the placeholder string being present, which is self-idempotent. Prefer the string gate (no new config key).
3. Tests:
   - `tests/test_agent_defs.py` — fresh-install regression class (temp XDG dir; seed; configure ONE provider; `load_agent_def("Coder"/"Debugger"/"Supervisor")` non-None; same with zero providers; validator absent-vs-unknown assertions; **migration test**: pre-create `coder.yaml` with `llm_name: local-kb`, call `_seed_defaults()`, assert the placeholder is gone and other fields intact; assert a customized `llm_name: my-card` is NOT rewritten). Prove RED on unmodified code.
   - Edit (not delete) the two string-pin tests to the new contract: `tests/test_agent_defs.py::TestValidateAgentDef::test_missing_required_fields`, `tests/test_agent_builder_no_provider_keys.py::TestValidateAgentDef::test_missing_provider_still_errors`.
   - `tests/test_tools.py::TestToolSchemaSizeBudget` reads the three YAMLs — verify unchanged budgets (removing a key cannot change schema size, but run it).

### Phase 2 — global default + UI (AlphaCoder, own worktree, parallel-safe disjoint files)
4. `ui/handlers/settings_handler.py` — after `save_providers(providers)` in `add_or_update`: if `get_agent_setting("default_provider", "")` is empty or names a card no longer present, `set_agent_setting("default_provider", provider.name)` and `set_agent_setting("default_model", provider.default_model)`. Skip non-chat cards: DecisionModel/ImproveModel/JEV-like cards have `caller == ""` (allow_empty_caller) — never make those the default; if it's the only card, leave defaults empty. In `remove(name)`: if the removed card WAS default, retarget to the FIRST remaining **chat** card (caller non-empty) or `""` if none; likewise for default_model. Use `agent/config.py` getters/setters; do not import GTK.
5. `ui/agent_runtime/provider.py` `_resolve_agent_model` — when `agent_def.llm_name` is falsy, resolve `config.default_provider`'s card and return its `default_model` (or `f"{card}/{default_model}"` when it lacks a `/`), same shape as the llm_name branch. Return None only when no such card exists. This dissolves the `default_model` gap (v2 BUG#1) and makes `refresh_provider_config` propagation (BUG#6) unnecessary for new turns.
6. `ui/agent_runtime/provider.py` `refresh_provider_config` — additionally propagate `fresh.default_provider` / `fresh.default_model` to each cached runtime's `cfg` (two assignments next to the existing `tool_output_offload_chars` line) so in-flight runtimes pick up the new default on the next turn.
7. `ui/window.py` `_on_providers_changed` — after `arh.refresh_provider_config()`, call `agent.special_agents.reload_registry()`, re-register ARH's agents via a NEW lightweight method on `AgentRuntimeHandler` (NOT `reload_agents_and_mcp()` — that tears down MCP on the GTK thread while loops are live, v2 BUG#4): e.g. `arh.refresh_agent_registry()` which does only `reload_registry()` + `self._agents.clear()` + re-register from `get_special_agents()`, then `self._left_panel.set_special_agents(arh)`. Guarded like the existing ARH check. Add that method in `ui/handlers/agent_runtime_handler.py` (Phase 2 scope).
8. `ui/views/agent_builder.py` — single-card Save: if `len(self._providers) == 1`, include the primary card in the fallback dropdown AND auto-select it (index 1) when the form has no stored fallback; runtime already tolerates `fallback == primary` (`agent/runtime.py:3247` collapses the chain). Also handle the edit case where stored `fallback_provider` names no configured card: default that dropdown to the primary when it's the only card, so Save isn't permanently disabled. Keep `has_fallback` in `_update_save_button`.
9. `ui/window.py` `_build_agent_form` — when `load_for_edit` returns None, emit a user-visible status via `self._on_command_text(session_key, text)` (same channel as revert failures — verified exists at `ui/window.py:923`) in addition to the warning. Use the active session key from `self._active_session_key` (or equivalent existing accessor); if none, skip the toast rather than inventing an API.
10. Tests for 4–9: default_provider/default_model persistence (add, add DecisionModel-first, remove-retarget, remove-all); `_resolve_agent_model` with falsy llm_name resolves to the default card; `refresh_agent_registry` doesn't touch MCP (spy `disconnect_all`/`connect_servers` not called); single-card Save **enabled** under xvfb with a real widget; provider-configure-before-launch produces 3 rows; double-click opens the drawer on a fresh install with ONE provider; `load_for_edit` failure produces a visible status.

**Files NOT changed:** `agent/special_agents.py` (only its `reload_registry` is CALLED), `agent/runtime.py`, `agent/config.py` (writers exist), `ui/views/left_panel.py`, `pyproject.toml` (packaging bug deferred), `docs/ARCHITECTURE.md`, `transport/`, `render/`.

## Acceptance criteria

- [ ] Fresh install + ONE provider: 3 defs load, 3 rows exist (both orderings), double-click opens the full-form drawer under the row, Save is enabled, and saving works.
- [ ] Existing install that already seeded `local-kb`: after the migration, `load_agent_def("Coder")` non-None; a user-customized llm_name is untouched.
- [ ] `agent.json` gains `default_provider` + `default_model` on first chat-card save; DecisionModel-first does not poison either; remove retargets/clears correctly.
- [ ] A def with no llm_name can start a real turn: `_resolve_agent_model` returns the default card's model, ARH `:873-881` resolves a provider, no `RuntimeError`, no empty model on the wire.
- [ ] `validate_agent_def`: absent llm_name → no "llm_name" error; non-empty unknown → `Unknown provider`; missing fallback still errors.
- [ ] Provider save does NOT call `disconnect_all`/`connect_servers`; cached runtimes receive the new default_provider/default_model.
- [ ] `load_for_edit` failure yields a visible status, not only a log line.
- [ ] Named tests edited, not deleted; full suite baseline `e6489bc7` = 14 failed / 5432 passed / 16 skipped — the same 14 (pre-existing) after this spec, no new failures.
- [ ] Wheel/prompts packaging gap recorded in the post-mortem as deferred, not silently fixed.

## Phases / loop

1. Phase 1 (Coder, main tree): items 1–3.
2. Phase 2 (AlphaCoder, `.worktrees/agent-alphacoder` synced to the Phase-1 commit): items 4–10. Disjoint files — no merge conflicts.
3. Each phase: mandatory adversarial audit (`adversarialDebugger.md`, 11 sections) via Debugger + Flagger in parallel (standing protocol), Supervisor independent verification (tests/diffs/greps, fresh-install probe), fix round → re-audit until clean.
4. §6-format post-mortem at `docs/post-mortems/2026-11-04-SPEC-62-FRESH-INSTALL-AGENT-EDIT-POST-MORTEM.md`, commit, push, PM summary, completion protocol (project_memory + canvas + talk).

## Edge cases

| Case | Expected |
|---|---|
| User customized coder.yaml with llm_name: my-card | Migration leaves it; validation still requires my-card to exist. |
| User already migrated (placeholder gone) | `_seed_defaults()` is a no-op for that file. |
| DecisionModel card saved first (caller="") | Not selected as default; defaults stay empty until a chat card is saved. |
| Provider renamed via same-name replace | `add_or_update` replaces; default still points to that name. |
| Remove the default card, other chat cards remain | Retarget to first chat card; rows reload. |
| Remove ALL cards | defaults → `""`; existing behavior preserved. |
| llm_name `""` / omitted / None | All unset; no error; resolves to global default. |
| helper-role def | Existing exemption stays. |
| `fallback_provider: openrouter` with no openrouter card | Unchanged (validation only requires non-empty). |
| Two+ provider cards | Fallback dropdown behavior unchanged. |
| Wheel install (non-editable) | Known deferred gap; post-mortem note. |
