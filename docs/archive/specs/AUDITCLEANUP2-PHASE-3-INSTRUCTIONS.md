# SPEC-AUDIT-CLEANUP-2 Phase 3 — Dead Functions, Unused Locals, Unused Imports

**Spec:** `docs/specs/SPEC-AUDIT-CLEANUP-2-DEAD-CODE-SWEEP.md` — READ IN FULL. §"Phase 3" is authoritative.
**Builder playbook:** `prompts/steelFramedCodeWriter.md` — load fresh, Discovery block first, every rule.
**Supervisor:** special:supervisor | **Builder:** special:coder | **Auditor:** special:debugger
**Baseline:** tree clean at `1e3ea2d`. Standing rules in force (no resets, xvfb-run -a, pyflakes gate, re-grep before EVERY deletion).

## The rule for this phase

Every deletion is grep-verified zero-reference **immediately before deleting** (pattern = the identifier; exclude the defining file's own hit and `.git`). ANY live hit = report, don't delete. When the instructions and the tree disagree, the tree wins — report the drift.

## Part A — dead functions (verified zero-reference 2026-09-07 morning; re-verify now)

| What | Where | Note |
|---|---|---|
| `get_recent_commits` | `utils/git_ops.py:228` | duplicates `log` (:215) — confirm `log` still exists and is used before deleting |
| `diff_stat_against` | `utils/git_ops.py:158` | |
| `hex_to_rgb` | `models/colors.py:127` | |
| `all_palette_css_classes` | `models/colors.py:110` | |
| `display_name_from_row` | `ui/views/session_menu.py:213` | |
| `html_escape` | `ui/views/chat_bubble.py:1113` | orphan — no utils counterpart exists; also grep for *callers within chat_bubble itself* (a local def can be called in-file) |
| `_load_crabcakes_doc` | `agent/context.py:694` | docstring admits unused; ~22 lines |
| `get_index_path` | `agent/kb_lookup.py:84` | when deleting, ALSO update the module docstring line (~:20) that lists it in the API comment |
| `DEFAULT_SKIP_PATTERNS` | `agent/enforcement.py:124-138` | real default lives in `EnforcementConfig.skip_patterns` (`agent/config.py`). `tests/test_enforcement.py:573` mentions it in a COMMENT only — update that comment to name `EnforcementConfig.skip_patterns` instead. Do NOT delete the config field. |
| `set_approval_callback` METHOD + `_approval_callback` field | `agent/runtime.py:564, 592-594` | **CRITICAL:** only the METHOD on AgentRuntime and its field. The MODULE-LEVEL function `agent/tools.py:70` `set_approval_callback` is LIVE (test fallback, 13 call sites in test_tools.py) — DO NOT TOUCH IT. Also check `agent/tools.py`'s docstring/comment that references the runtime method if any — update, don't delete the tools function. |
| unused locals | `agent/runtime.py:1799` (`workspace`), `:2659` (`tokens_after`) | remove the assignment lines only; verify the RHS has no side effects that matter (read the surrounding code — if the RHS is a pure call whose value is discarded, deleting is safe; if it has load-bearing side effects like makedirs, report instead) |

## Part B — unused imports (pyflakes-authoritative)

Authoritative list: `/tmp/pf-venv/bin/pyflakes agent ui models utils gateway scripts main.py | grep "imported but unused"` — intersect with the spec's named files:
- `agent/runtime.py` — the spec names `re`, `time`, `Iterator`, `AuditEntry`, `convert_*_for_anthropic`, SSL-retry constants, `urllib.*` (lines ~16-32, ~190-278)
- `ui/handlers/feed_handler.py:11,29` — `dataclass`, `ConversationSnapshot`
- `ui/handlers/agent_runtime_handler.py:23` — `FeedCardData` top-level
- `agent_runtime_handler.py:106-111` — `_session_completed` (redundant with `_ended_sessions` — NOT an import; grep first, keep if referenced)
- Plus: `ui/window.py` (`datetime`, `get_gateway_url` — flagged by Coder in Phase 2), `ui/handlers/work_handler.py:22` `WorkUnitStore` (verify: annotation-only usage is NOT dead — check whether any annotation in the file still references it after Phase 2's comment edits; if nothing references it, delete the import)

**Procedure for each:** pyflakes flags → grep the identifier in the file (in-file usage pyflakes may know about via re-export patterns: `__all__`, re-assignment) → if genuinely unused in-file AND not re-exported via `__all__` → delete just the name from the import line (keep other names on the line). If the identifier appears in `__all__` or is re-exported, report, don't delete.

## Verification (all pasted)

1. Per-deletion grep outputs (a single scripted loop is fine, pasted once).
2. Import smoke: `PYTHONDONTWRITEBYTECODE=1 python3 -c "import agent.runtime; import agent.tools; import ui.window; import models"` → OK.
3. Suites (xvfb-run -a, run individually or in safe batches): `tests/test_tools.py` (the approval-callback suite — MUST stay green, this is the guard against the CRITICAL mistake), `tests/test_agent_audit.py`, `tests/test_gateway.py`, `tests/test_chat_render_handler.py`, `tests/test_colors.py` (grep for the colors test file name first — find the right one), `tests/test_agent_context.py` or nearest (grep tests/ for `agent.context` importers), `tests/test_kb_lookup.py` or nearest for kb_lookup, `tests/test_enforcement.py` (comment-updated), `tests/test_git_ops.py` if exists (grep first).
4. pyflakes full-tree: undefined-name count **0** (regression gate) AND the unused-import count **strictly decreased** vs baseline (paste both counts; baseline = before your edits).
5. LOC accounting: `git show <commit> --stat` pasted.

## Commit (1)

`refactor(cleanup): remove 10 verified-dead functions + pyflakes-flagged unused imports/locals (SPEC-AUDIT-CLEANUP-2 Phase 3)`

## COMPLETENESS checklist
- [ ] Discovery block (all touched files read in full)
- [ ] Per-deletion greps pasted
- [ ] CRITICAL check: `agent/tools.py` set_approval_callback untouched (paste `grep -n "def set_approval_callback" agent/tools.py` showing it still present)
- [ ] All suite outputs pasted
- [ ] pyflakes before/after counts pasted
- [ ] Related issues flagged, not silently fixed

**STOP after this phase.** Phase 4 (conversation shims) is separate.
