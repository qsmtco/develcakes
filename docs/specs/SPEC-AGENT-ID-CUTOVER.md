# SPEC: Agent-id cutover — remove the `special:` key system

**Date:** 2026-10-22
**Author:** Supervisor (PM directive: full cutover, no backwards compatibility)
**Status:** Ready for implementation
**Depends on:** SPEC-AGENT-ID-AND-ROLES (225175e0) — this spec REPLACES its compatibility provisions
**Target branch:** main

---

## 1. Overview

The PM has ruled: everyone runs on the new system. The `special:` session-key
system is removed outright. Every local agent's session key is
`agent:<slug>` — built-ins and user agents alike. There is no migration path,
no legacy fallback, no `special:` string anywhere in the codebase. Old
conversation histories are deleted, not converted.

### Scope

| In | Out |
|---|---|
| One key scheme: `agent:<slug>` for every local agent | Migrating/converting old `special:*` data (PM deletes it) |
| Built-in keys `agent:supervisor`, `agent:coder`, `agent:debugger` | Keeping any `special:` literal alive |
| `is_local_agent_key` == `startswith("agent:")` | Gateway keys (unchanged, out of scope) |
| Built-in role behavior stays keyed on the ROLE word | Changing `coder.md`/`debugger.md`/`supervisor.md` or role gates |
| Built-ins stay on the project root; every other writer gets a worktree | — |

### Principles

- `agent:<slug>` is the only local key format. No dual-prefix logic.
- No code branch may special-case a `special:` string.
- The old conversation files are deleted by the PM/Supervisor, so the loader
  never sees one. Do not write code to handle them.

---

## 2. Changes by file

### 2.1 `agent/special_agents.py`

- `_load_registry`: default for a missing `session_key` becomes
  `agent:<role-slug>` (slug rule: lowercase, spaces→hyphens, ASCII
  alphanumeric + `._-` only, collapse separators, strip edges). Delete the
  `special:<role>` fallback and the comment block about it.
- `is_local_agent_key`: `startswith("agent:")` only.
- Docstrings: replace every `special:coder`-style example with
  `agent:coder` / `agent:alphacoder`.
- Duplicate-key warning text: drop the mention of `'session_key'` collision
  with legacy defaults; keep the warning itself.

### 2.2 `utils/agent_defs.py`

- `BUILTIN_SESSION_KEYS` → `("agent:supervisor", "agent:coder", "agent:debugger")`.
- `_assign_session_key`: unchanged logic (preserve explicit key, else mint
  `agent:<name-slug>`, refuse built-in/taken) — only the constant changes.
- Delete any `special:<role>` / `special:<role>-N` minting path.
- Docstring examples updated.

### 2.3 `ui/handlers/agent_runtime_handler.py`

- `_ROOT_KEYS` → `frozenset({"agent:supervisor", "agent:coder", "agent:debugger"})`.
- `runtime_for` gate: `startswith("agent:")`.
- `special:live-bridge` literal keys → `agent:live-bridge` (both sites).
- Docstrings/comments referencing `special:` updated.

### 2.4 `agent/group_tools.py`

- `_resolve_peer` / `_require_supervisor` gates: accept keys starting with
  `agent:` only (currently `special:` at lines 81/105).

### 2.5 Every other `startswith("special:")` gate → `startswith("agent:")`

- `ui/views/left_panel.py` (2 sites), `ui/handlers/project_handler.py` (3
  sites), `ui/agent_runtime/session.py` (3 sites),
  `utils/feedback_processor.py`.

### 2.6 Literal-key fixes

- `ui/handlers/telegram_bridge_handler.py`: `SUPERVISOR_KEY = "agent:supervisor"`.
- `ui/feed/review.py`: default target `["agent:supervisor"]`.
- `agent/tools.py` session-filter doc example → `agent:coder`.
- All remaining docstring/comment examples (`agent/callbacks.py`,
  `agent/tool_middleware.py`, `agent/persistence.py`,
  `ui/handlers/project_handler.py`, `ui/handlers/agent_command_handler.py`,
  `models/review_state.py`, `utils/worktree_manager.py`,
  `ui/agent_runtime/session.py`): reword to `agent:...`.

### 2.7 Data (Supervisor does this, not Coder)

- `~/.config/develcakes/agents/*.yaml`: `session_key` → `agent:coder`,
  `agent:debugger`, `agent:supervisor`, `agent:alphacoder`.
- Delete `conversations/special:*.json` (old histories gone per PM).
- `.crabcakes/team.json` → new keys.
- Delete `.worktrees/special-coder-2`.

### 2.8 Tests

- Update every test that asserts a `special:` key or gate. A test that feeds
  `special:coder` to a gate now expects the OLD (reject/gateway) behavior or
  is rewritten to `agent:coder`. No test keeps a passing `special:` path.

### 2.9 Acceptance

- `grep -rn "special:" agent ui utils models scripts render transport`
  returns only innocuous English words (e.g. "special agent" in prose) — no
  `special:` key literals, no `startswith("special")`.
- Full pytest suite green.
- App restart: all four agents open with `agent:*` keys, conversations
  persist to `agent:*.json`.