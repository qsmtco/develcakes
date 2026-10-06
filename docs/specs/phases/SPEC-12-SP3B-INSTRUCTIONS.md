# SPEC-12 SP3b — wire the 11 read sites + terminal clear

**Spec:** `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md` §2c (BUG#17/#20a/#21/#25, R5 REV 3)
**Pre-flight:** `docs/specs/phases/SPEC-12-PREFLIGHT-DECISIONS.md` (D2/D3)
**Base:** `docs/specs/phases/SPEC-12-SP3A-INSTRUCTIONS.md` (mechanism + resolvers landed)
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** ONE file — `ui/handlers/agent_runtime_handler.py`. No test edits (SP3c).

Read the regions named below before editing.

---

## Site map (verified current lines — anchor by identifier, they will drift)

| Line | Method | Current | New |
|---|---|---|---|
| 2581 | `_do_response_complete` (crabcard tab_key) | `self._resolve_mount_key(session_key) or session_key` | `reply_key` |
| 2605 | `_do_response_complete` (end_streaming mount_key) | `self._resolve_mount_key(session_key)` | `reply_key` |
| 2617 | `_do_response_complete` (render_sync fallback mount_key) | `self._resolve_mount_key(session_key)` | `reply_key` |
| 2633 | `_do_response_complete` (crabcard tab_key) | `self._resolve_mount_key(session_key) or session_key` | `reply_key` |
| 2645 | `_do_response_complete` (render_sync mount_key) | `self._resolve_mount_key(session_key)` | `reply_key` |
| 2786 | `_do_compaction_bubble` (end_streaming mount_key) | `self._resolve_mount_key(session_key)` | `self._reply_key(session_key)` |
| 2805 | `_do_compaction_bubble` (render_sync mount_key) | `self._resolve_mount_key(session_key)` | `self._reply_key(session_key)` |
| 2841 | `_do_usage_warning` (end_streaming mount_key) | `self._resolve_mount_key(session_key)` | `self._reply_key(session_key)` |
| 2845 | `_do_usage_warning` (render_sync mount_key) | `self._resolve_mount_key(session_key)` | `self._reply_key(session_key)` |
| 2955 | `_do_error` (end_streaming mount_key) | `self._resolve_mount_key(session_key)` | `reply_key` |
| 2970 | `_do_error` (render_sync mount_key) | `self._resolve_mount_key(session_key)` | `reply_key` |

Do NOT change lines 1246 (inside `send_to_special_agent` — the set-on-entry default)
or 1652 (inside `_reply_key` itself).

## Edit 1 — `_do_response_complete`: bind local, use it, clear in `finally`

The method currently early-returns on a stale token BEFORE rendering. Leave that
stale-token guard intact (a stale completion must NOT clear the CURRENT turn's slot).
Immediately AFTER it, bind the local and wrap the rest in try/finally:

```python
        if complete_token is not None:
            current_token = self._turn_tokens.get(session_key)
            if complete_token is not current_token:
                logger.debug("_do_response_complete: stale completion (token mismatch) for %s, skipping", session_key)
                return

        # SPEC-12 (R5 REV 3): bind the turn's reply key ONCE and use it at
        # every render/mount site below. The pop is in the finally so EVERY
        # exit path clears the slot — AFTER the reads (never at the
        # _ended_sessions marker above, BUG#17/#21).
        reply_key = self._reply_key(session_key)
        try:
            ... existing body, re-indented one level, with the 5 sites using reply_key ...
        finally:
            self._turn_reply_target.pop(session_key, None)
```

Replace the 5 in-method reads with the local variable:
- `card_data.metadata["tab_key"] = reply_key` (was `self._resolve_mount_key(session_key) or session_key`) — BOTH occurrences (2581, 2633)
- `mount_key=reply_key` (was `self._resolve_mount_key(session_key)`) — THREE occurrences (2605, 2617, 2645)

(Re-indent the whole wrapped body by 4 spaces. Mechanical — ruff will confirm no
syntax/indent drift.)

## Edit 2 — `_do_error`: bind local, use it, clear in `finally`

Same pattern after its stale-token guard:

```python
        if error_token is not None:
            current_token = self._turn_tokens.get(session_key)
            if error_token is not current_token:
                logger.debug("_do_error: stale error (token mismatch) for %s, skipping", session_key)
                return

        # SPEC-12: bind once; pop in the finally (after the reads).
        reply_key = self._reply_key(session_key)
        try:
            ... existing body, re-indented one level, 2 sites use reply_key ...
        finally:
            self._turn_reply_target.pop(session_key, None)
```

The 2 sites: `mount_key=reply_key` at 2955 and 2970.

## Edit 3 — `_do_compaction_bubble` / `_do_usage_warning`: READ only (no clear)

These fire MID-turn (token-breakdown / usage-warning callbacks) — they must honor
the turn's reply target but must NOT clear the slot (the turn continues). Change
their 4 `self._resolve_mount_key(session_key)` reads to `self._reply_key(session_key)`
(2 each). No binding, no finally.

## Edit 4 — BUG#3 cleanup (the `or session_key` idiom is now dead)

Sites 2581/2633 used `... or session_key`; after Edit 1 they use `reply_key`, which
already never returns None. Verify `grep -n "_resolve_mount_key(session_key) or session_key"` → **0**.

---

## Behavior expectations
- Group turn: slot = `project:<name>` (set by the fan-out send) → every read resolves
  the project surface, and the slot is popped at turn end.
- `/ask` turn: slot = the agent key → reply renders in the private tab; popped at end,
  so the member's NEXT group reply routes to the project again (BUG#11).
- Mid-turn compaction/usage events: read `_reply_key` (their turn's target), no clear.

## What must NOT change
- The stale-token guards' early `return` (do NOT wrap them — a stale turn must not
  clear the current slot).
- `send_to_special_agent` (SP3a), `_reply_key`, the resolvers (SP3a).
- The 8 non-`_resolve_mount_key` lines, streaming/`_ended_sessions`/`_turn_tokens`.

## Verification battery (paste all outputs)
- `cd /home/mushy/projects/develcakes && xvfb-run -a .venv/bin/python -m pytest tests/test_agent_runtime.py -q` → report counts (the 2 known R7-precedence REDs remain; NO NEW failures expected)
- Probe (paste the script + output): set `handler._turn_reply_target["k"]="project:alpha"`, call `_do_response_complete("k", text)` (with `_crh` mocked as in the existing tests) → assert the slot is `"k" not in handler._turn_reply_target` afterward (cleared) AND the render used `project:alpha`. Repeat for `_do_error`.
- `~/.local/bin/ruff check ui/handlers/agent_runtime_handler.py` → **≤25** (HEAD baseline) and no NEW findings
- `.venv/bin/pyright ui/handlers/agent_runtime_handler.py` → 0 errors
- `grep -n "_resolve_mount_key(session_key) or session_key" ui/handlers/agent_runtime_handler.py` → 0

## Report back (mandatory COMPLETENESS)
```
COMPLETENESS:
- [x/not done] Edit 1: _do_response_complete local + finally (5 sites) — evidence: diff hunks
- [x/not done] Edit 2: _do_error local + finally (2 sites) — evidence: diff hunks
- [x/not done] Edit 3: compaction/usage reads → _reply_key (4 sites) — evidence: diff hunks
- [x/not done] Edit 4: `or session_key` grep = 0 — pasted
- [x/not done] pytest counts + clear-probe (response_complete + error) — pasted
- [x/not done] ruff / pyright — pasted
- [x/not done] Related issues found, NOT fixed
```

Invoke `prompts/steelFramedCodeWriter.md` before writing. Please write the change per
this brief and report when done.