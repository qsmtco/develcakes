# SPEC-AUDIT-CLEANUP-1 Phase 2 — Class B Imports, C1, Bug 2 (label drift), Bug 3 (AuditLog leak)

**Spec:** `docs/specs/SPEC-AUDIT-CLEANUP-1-LATENT-BUG-FIXES.md` — READ IN FULL. §"Class B", §"Class C", §"Bug 2", §"Bug 3" are authoritative.
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load fresh, Discovery block first, every rule.
**Supervisor:** special:supervisor | **Builder:** special:coder | **Auditor:** special:debugger

**Baseline:** tree clean at `bf854b5` (pushed). **Three sub-phases, 2a → 2b → 2c.** One delegation each; Debugger audits between them. Do NOT work ahead of the current sub-phase.

**Test-runner note (supersedes the spec's `-B` shorthand):** use `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest <suite> -q` and purge `__pycache__` (`find . -name __pycache__ -type d -not -path "./.git/*" -exec rm -rf {} +`) before any red-green cycle. `pytest -B` is not a valid pytest flag — the intent is "no stale bytecode".

**GTK segfault constraint (pre-existing, NOT ours):** `test_chat_render_handler.py` crashes at collection; `test_feed_handler.py` completes then segfaults at teardown. Run suites individually; a suite that segfaults at teardown still reports its results — capture them. Never claim green from a multi-suite run that aborted.

---

## Sub-phase 2a — Class B annotation imports (B1–B9) + C1 + comment nit

**10 sites, each 1–3 lines. All are annotation-only — never evaluated at runtime today. The fix goal is pyflakes-clean + future-proof (get_type_hints/IDE).** Verify each site is really annotation-only before editing: grep the module for any RUNTIME use of the name; if you find one, STOP and report (spec's classification would be wrong).

Per-site fixes (add `from typing import TYPE_CHECKING` where absent):

| # | File | Fix |
|---|---|---|
| B1 | `agent/persistence.py:42,134` | `if TYPE_CHECKING: from models.conversation import Conversation` — mirrors the existing pattern in `agent/runtime.py` (same package). |
| B2 | `ui/handlers/feed_handler.py:70,87,839,1243` | `if TYPE_CHECKING: from gi.repository import Gtk` — TYPE_CHECKING (not runtime) keeps the handler GTK-import-free; file already has `from __future__ import annotations` (:4). |
| B3 | `ui/views/left_panel.py:191,195,199` | TYPE_CHECKING import of `FeedTab` from `ui.views.feed_tab` (avoids view↔view cycle risk). |
| B4 | `ui/views/settings_dialog.py:359` | Extend the EXISTING `if TYPE_CHECKING:` block (~:20) with `from ui.handlers.settings_handler import SettingsHandler`. |
| B5 | `ui/handlers/chat_render_handler.py:324` | `from typing import Callable` at runtime (zero-cost); TYPE_CHECKING import of `FeedCardData` from `models.feed_card`. |
| B6 | `ui/handlers/command_handler.py:240` | Extend the existing runtime import `from models.command import Command, CommandResult, CommandRegistry` (:30) with `MentionResolution` — the module is already a runtime dependency; zero new cost. |
| B7 | `ui/handlers/auxilium_wizard_handler.py:355` | TYPE_CHECKING import of `ProviderConfig` from `models.providers` (module is deliberately import-light). |
| B8 | `utils/gtk_safe_link.py:84` | `if TYPE_CHECKING: from gi.repository import Gtk` — do NOT add a top-level runtime GTK import; the deferred import at :119 is a deliberate design (utils purity), keep it. |
| B9 | `utils/mcp_config.py:51` | `if TYPE_CHECKING: from mcp import StdioServerParameters` — runtime import stays deferred at :60. |
| C1 | `scripts/rebuild_kb_index.py:170` | `if TYPE_CHECKING: import numpy as np` at top — the quoted return annotation `-> "np.ndarray"` then resolves; the existing runtime import inside `embed_chunks` (~:173) STAYS (it carries the user-facing install hint). |
| nit | `tests/test_render_error_callbacks.py` | Tighten the two inline comments: Python's actual message is `NameError: cannot access free variable 'exc' where it is not associated with a value in enclosing scope` — update the `# was: NameError: ...` comments to quote it correctly (Debugger BUG #1). |

**Commits (2):** `fix(imports): resolve annotation-only undefined names (B1-B9, C1)` and `docs(tests): correct NameError comment wording (Debugger BUG #1)`.

**2a verification:**
1. `/tmp/pf-venv/bin/pyflakes agent ui models utils gateway scripts main.py | grep -c "undefined name"` → **must be 0**. Paste the full command + count.
2. Import smoke for every touched module: `PYTHONDONTWRITEBYTECODE=1 python3 -c "import <module>"` for each (use dotted paths; for scripts, `python3 -m py_compile scripts/rebuild_kb_index.py`).
3. Run the suites that exercise these modules, individually: `test_feed_handler.py` (teardown segfault expected — capture results first), `test_render_error_callbacks.py`, `test_command_handler.py`, `test_activity_drawer.py`, plus the existing suites for auxilium/settings/mcp/persistence if they exist — grep `tests/` for each module name; if no suite imports a module, say so explicitly.
4. `TYPE_CHECKING`-block placement check: no runtime code accidentally indented under the `if TYPE_CHECKING:`.

## Sub-phase 2b — Bug 2: activity label/duration dedup (drift + None-crash fix)

**Verified drift (2026-09-07):** `models/activity.py:179` `_type_label` maps 9 types incl. `lifecycle_end`, `tool_start`, `tool_end`, `tool_error`, with a falsy guard. The drawer copy (`ui/views/activity_drawer.py:26-40`) misses those 4 mappings (renders raw `tool_error` etc.) and its `_format_duration` (:41-47) lacks the models version's None guard (`if ms is None or ms <= 0: return ""` at models :218) → **TypeError on `None`**.

**Red-before-green (2 failing tests FIRST):**
1. Test the drawer for label drift: `_type_label("tool_error") == "tool"`, `_type_label("lifecycle_end") == "lifecycle"`, `_type_label("") == ""` — via the drawer module's implementation as it exists. Confirm red.
2. Test `_format_duration(None) == ""` via the drawer. Confirm red (TypeError today).
Both tests go in `tests/test_activity_drawer.py` (extending `test_type_label_mapping` per spec Bug 2.4 — cover ALL 9 mappings + unknown-type passthrough + None guard).

**Fix:**
1. In `models/activity.py`: promote to public names — `activity_type_label()` and `format_duration()` — keeping bodies identical to today's models versions. Update the internal call site (`:163` `"type_label": _type_label(self.type)`). Delete the old private defs (grep first: if any other module/test imports `_type_label`/`_format_duration` from models, update those references — report each).
2. In `ui/views/activity_drawer.py`: DELETE the local `_type_label` and `_format_duration` defs; import the public names from `models.activity`. The `:428` fallback (`row.get("type_label", "") or _type_label(...)`) keeps working via the import — update it to the public name.
3. Update stale "keep in sync / Mirrors the helper" docstrings in BOTH files (models docstrings currently say "Mirrors the helper in ui/views/activity_drawer.py" — no longer true).

**Commits (1):** `fix(activity): single models-layer label/duration impl — restores 4 missing type mappings + None guard in drawer`.

**2b verification:** the 2 new tests green (paste), full `test_activity_drawer.py` green, `test_agent_audit`-style module smoke (`python3 -c "from models.activity import activity_type_label, format_duration"`), grep `_type_label\|_format_duration` shows zero duplicate definitions outside tests.

## Sub-phase 2c — Bug 3: AuditLog cap + turn-end auto-flush

**Anchor (supervisor-verified):** the single terminal path is `_terminate_turn` in `agent/runtime.py` (~:630-770; grep `def _terminate_turn`). ALL outcomes — COMPLETED, FAILED, CANCELLED — funnel through it (SPEC-RUNTIME-TERMINAL-PATH-CONSOLIDATION). It already wraps `_auto_save` in try/except — mirror that pattern.

**Red-before-green (2 failing tests FIRST):**
1. Cap test (in `tests/test_agent_audit.py`): record `MAX_ENTRIES + 500` entries → assert `len(log.entries) == MAX_ENTRIES` AND the oldest 500 are gone (check by timestamp order/user field). Red today (list grows unbounded).
2. Flush-on-turn-end test (in `tests/test_agent_runtime.py`, mirroring existing `_terminate_turn` tests — find them and copy their TurnResult construction): spy/monkeypatch `flush_audit_log`, drive `_terminate_turn` to COMPLETED, assert it was called. Also assert a FAILED turn calls it. Red today.

**Fix:**
1. `agent/audit.py`: module constant `MAX_ENTRIES = 2000`. In `record()`, inside the existing lock, after append: if `len(self._entries) > MAX_ENTRIES`, drop oldest via `del self._entries[:len(self._entries) - MAX_ENTRIES]`. Update the `AuditLog` docstring (cap + auto-flush; old text says "flush to disk via flush_audit_log()" only).
2. `agent/runtime.py` `_terminate_turn`: after the persist/`_auto_save` block, add:
   ```python
   try:
       self._audit_log.flush_audit_log()
   except Exception:
       logger.exception("_terminate_turn: audit flush failed for %s", sk)
   ```
   Audit flush must NEVER break the turn. Runs for all three terminal outcomes (it's on the shared path — verify it's outside `if should_persist:` and outside the state lock).

**HERMETICITY (hard requirement — favorites.json lesson from the last loop):** `flush_audit_log()` with no path writes to the REAL `~/.config/crabcakes/audit-log.jsonl`. Existing runtime tests that record audit entries and hit `_terminate_turn` would now write user data.
1. Your spy test must monkeypatch the flush (no real write).
2. Verify no OTHER existing runtime test writes it: `ls -la ~/.config/crabcakes/audit-log.jsonl` before and after running the full `test_agent_runtime.py`; if any file appears/changes, add the narrowest possible guard (module-scoped fixture in that test file patching `flush_audit_log`), NOT a global conftest autouse. Report the before/after `ls` output.

**Commits (1):** `fix(audit): cap AuditLog at 2000 entries + auto-flush on every terminal turn`.

**2c verification:** 2 new tests green (paste), `tests/test_agent_audit.py` fully green, `tests/test_agent_runtime.py` green (or pre-existing failures only — attribute each), hermeticity before/after `ls` pasted, cap arithmetic edge: `len == MAX_ENTRIES` exactly → no deletion; `MAX_ENTRIES + 1` → exactly one dropped.

---

## COMPLETENESS checklist (each sub-phase delivery)
- [ ] Discovery block (files read in full, per-site verification notes)
- [ ] Red-before-green evidence for 2b/2c (failing output FIRST, then fix, then green)
- [ ] Commit(s) per the sub-phase's message spec
- [ ] Per-sub-phase verification outputs pasted in full (no "tests pass" summaries)
- [ ] pyflakes undefined-name count == 0 (2a gate; re-paste at 2b/2c close)
- [ ] Hermeticity evidence (2c)
- [ ] Spec-drift flags; related issues flagged, not silently fixed
- [ ] Suites run individually (GTK constraint); unrunnable suites named + why

**After each sub-phase:** STOP and report. Debugger audits, then the next sub-phase is delegated. Do not batch sub-phases.