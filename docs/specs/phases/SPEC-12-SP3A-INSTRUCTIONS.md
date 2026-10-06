# SPEC-12 SP3a — agent_runtime_handler: turn-scoped reply target + R7 resolvers

**Spec:** `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md` §2c
**Pre-flight:** `docs/specs/phases/SPEC-12-PREFLIGHT-DECISIONS.md` (D2/D3/D4/D5)
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** ONE file — `ui/handlers/agent_runtime_handler.py` (MECHANISM + RESOLVERS
only). The 11 read sites and the terminal clear are SP3b (next). No test edits here
(existing tests stay green).

Read the file in full? No — read the regions named below (it is 3000+ lines; read
`send_to_special_agent`, `_resolve_mount_key`, `_resolve_chat_box`, `__init__`).

---

## Edit 1 — `__init__`: add the turn-scoped reply-target slot

Near the other per-session dicts (after `self._turn_tokens: dict[str, object] = {}`,
~line 176), add:

```python
        # SPEC-12 (R5 REV 3): turn-scoped reply target. session_key → the
        # DISPLAY key the CURRENT turn's reply renders under. Set on ENTRY of
        # every send_to_special_agent (reply_target or the routing key);
        # consumed by _reply_key() at the 11 render/mount sites; cleared at
        # the END of _do_response_complete/_do_error (SP3b). A per-send slot
        # (never a session-scoped mark) so a member's private /ask reply does
        # not mute its LATER group replies.
        self._turn_reply_target: dict[str, str] = {}
```

## Edit 2 — `send_to_special_agent`: new `reply_target` param + set-on-entry

Signature:
```python
    def send_to_special_agent(self, session_key: str, text: str,
                              reply_target: str | None = None) -> None:
```

Set the slot on ENTRY, **AFTER** the two existing early-return guards (the
`agent_def is None` guard AND the `self._active_project is None` guard) so only
sends that will actually run a turn set it. Insert immediately after the
`self._active_project is None` guard block returns and BEFORE the
`if self._active_project is not None:` project_name resolution:

```python
        # SPEC-12 (BUG#18/#26): ALWAYS normalize the slot for a send that will
        # actually run — a non-targeting caller defaults to the routing
        # (project) key, so no stale private target survives. Placed AFTER
        # both early-return guards (an unrouted/no-project send renders
        # nothing and must not disturb the slot).
        self._turn_reply_target[session_key] = (
            reply_target if reply_target is not None
            else self._resolve_mount_key(session_key)
        )
```

Add to the docstring:
```
        reply_target: SPEC-12 (R5 REV 3) — the DISPLAY key this turn's reply
            renders under. `/ask`+`/delegate`/bubble-forward pass the agent's
            own key (private view); group fan-out passes "project:<name>";
            None → the routing key (the project). Never a persistent mark.
```

## Edit 3 — `_reply_key` helper + `_resolve_mount_key` R7 respec

Replace the current `_resolve_mount_key` body (R7 revision — drop the direct-tab
preference, add the active-project fallback):

```python
    def _reply_key(self, session_key: str) -> str:
        """SPEC-12 (R5 REV 3): the CURRENT turn's reply/display key — the
        per-send private target when set, else the routing (project) key.
        NEVER None (falls back to the session key), so a mount_key passed to
        render_sync/render_async is always a real key."""
        return (self._turn_reply_target.get(session_key)
                or self._resolve_mount_key(session_key)
                or session_key)

    def _resolve_mount_key(self, session_key: str) -> str | None:
        """SPEC-12 R7: the agent's project, else the ACTIVE open project,
        else the raw session key (no project open). Turn-scoped private
        targets are applied by the CALLER via _reply_key (R5 REV 3)."""
        project_name = None
        if self._agent_to_project is not None:
            project_name = self._agent_to_project.get_project(session_key)
        if project_name is None and self._active_project is not None:
            project_name = self._active_project[0]
        if project_name is not None:
            return f"project:{project_name}"
        return session_key
```

**NOTE — behavior change (intended, R7):** the old `_resolve_mount_key` returned
`session_key` when a direct tab existed (tab precedence). The new one does NOT
check for a direct tab — under SPEC-12 the display key is the project, and a
private view is expressed through `reply_target`/`_reply_key`, not tab precedence.
Existing tests that pin the OLD direct-tab precedence (`test_streaming_direct_tab_branch_takes_precedence`)
will need updating in SP3c — report which ones go RED, do NOT edit tests here.

## Edit 4 — `_resolve_chat_box` R7 respec (spec §2c, BUG#27)

Replace the body with the slot-first + routing + R7-fallback form:

```python
    def _resolve_chat_box(self, session_key: str):
        """SPEC-12 R7: resolve the box from the turn reply key (slot first),
        else the direct tab, else the agent's project, else the ACTIVE
        project, else None (no project open, no tab)."""
        key = self._turn_reply_target.get(session_key) or session_key
        direct = self._mc.get_chat_box_for_session(key)
        if direct is not None:
            return direct
        project_name = None
        if self._agent_to_project is not None:
            project_name = self._agent_to_project.get_project(session_key)
        if project_name is None and self._active_project is not None:
            project_name = self._active_project[0]
        if project_name is not None:
            return self._mc.get_chat_box_for_session(f"project:{project_name}")
        return None
```

(The R7 active-project fallback is the BUG#27 fix: an unrouted agent with a project
open now resolves the project box, so the `if chat_box is not None:` gate in
`_do_error`/non-streaming does NOT drop its row.)

---

## What must NOT change (this sub-phase)
- The 11 `_resolve_mount_key(session_key)` READ sites (SP3b re-points them).
- `_do_response_complete` / `_do_error` bodies (SP3b adds the clear).
- Streaming structures, `_ended_sessions`, `_session_completed`, `_turn_tokens`.

## Verification battery (paste all outputs)
- `cd /home/mushy/projects/develcakes && xvfb-run -a .venv/bin/python -m pytest tests/test_agent_runtime.py -q` → report pass/fail (some `_resolve_mount_key`-precedence pins may go RED — SP3c's job; list them)
- `~/.local/bin/ruff check ui/handlers/agent_runtime_handler.py` → **BASELINE IS NOT 0**:
  HEAD has **25 pre-existing findings** (9 BLE001, 1 F841, 3 I001, 1 PERF102, 2 S110,
  4 UP017, 1 UP035, 4 UP037 — verified via `git show HEAD:ui/handlers/agent_runtime_handler.py`).
  Requirement: **no NEW findings** — count ≤25 and none on your changed lines. Do NOT
  fix the pre-existing 25.
- `.venv/bin/pyright ui/handlers/agent_runtime_handler.py` → 0 errors (verified 0 on HEAD)
- `wc -l ui/handlers/agent_runtime_handler.py` → 3003 baseline

## Report back (mandatory COMPLETENESS)
```
COMPLETENESS:
- [x/not done] Edit 1: _turn_reply_target init — evidence: diff hunk
- [x/not done] Edit 2: send_to_special_agent reply_target + set-on-entry — evidence: diff hunk
- [x/not done] Edit 3: _reply_key + _resolve_mount_key R7 — evidence: diff hunk
- [x/not done] Edit 4: _resolve_chat_box R7 — evidence: diff hunk
- [x/not done] pytest pass/fail + the RED list (expected) — pasted
- [x/not done] ruff rule-profile diff vs HEAD + pyright + wc — pasted
- [x/not done] Related issues found, NOT fixed
```

Invoke `prompts/steelFramedCodeWriter.md` before writing. Please write the change
per this brief and report when done.