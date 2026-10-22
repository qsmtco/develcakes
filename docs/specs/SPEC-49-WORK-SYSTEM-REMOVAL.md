# SPEC-49: Total Removal of the Work-Unit / Task System

**Date:** 2026-10-21
**Author:** Supervisor (PM direction, 2026-10-21)
**Status:** Draft — for implementation
**Implements:** PM direction: "complete and total removal of the work system and the task system." Amends `docs/proposals/PROPOSAL-META.md` Program 4 from "build a run loop" to "remove the system."
**Depends on:** nothing. No spec consumes work-unit state.
**Target branch:** main

> Architecture compliance: `models/` stays pure data; `utils/` stays pure Python/file I/O;
> command routing lives in `ui/handlers/command_handler.py`; `ui/window.py` is the
> composition root. This spec **deletes** three modules and their wiring rather than
> adding any. `docs/ARCHITECTURE.md` remains authoritative; §8 lists its edits.

---

## DISCOVERY

- Read `ui/handlers/work_handler.py` (1,046 lines): class `WorkHandler` owns all `/work` semantics — `cmd_work` (create/list/start/done/blocked/unblock/cancel/assign/priority/spec-ready/status), `load_for_project` (`:77`, calls `load_or_migrate_work_units` + `work_store.replace_all`), `close_project` (`:86`). Constructor takes `project_handler`, `work_store`, `agent_runtime_handler`, display callbacks, `GLib_module`. `/work start` hands off to Supervisor via `send_to_special_agent`.
- Read `models/work_unit.py` (314 lines): `WorkLease` (`:68`), `WorkUnit` (`:128`), `WorkUnitStore` (`:264` — `create/get/update/list_all/list_by_status/delete/replace_all`), `WORK_PRIORITIES`, `WORK_STATUS_LABELS`, `WORK_PRIORITY_LABELS`, `_work_next_id`.
- Read `utils/work_persistence.py` (715 lines): `load_or_migrate_work_units`, `save_work_units` (persists `work.json` AND regenerates `tasks.md`), lease API `claim_work`/`release_work`/`assert_lease`/`find_live_lease`, legacy migration `_parse_legacy_tasks_markdown` from old `tasks.md`.
- Read `models/__init__.py`: creates the global singleton `work_store = WorkUnitStore()` (`:28`) and exports it (`:20`, `:63`). `ui/handlers/project_handler.py:19` and `ui/window.py:1591` import it via `from models import work_store`.
- Read `ui/window.py`: imports `WorkHandler` (`:47`), constructs it (`:673-681`), passes it to `CommandHandler` (`:716`), wires `load_for_project`/`close_project` to project-open/close (`:873-876`), and `_remote_status_summary` (`:1591-1599`) reads `work_store.list_all()` for the phone status.
- Read `ui/handlers/command_handler.py`: registers `/work` plus legacy aliases `/task`, `/tasks`, `/start`, `/done`, `/blocked`, `/cancel`, `/assign`, `/priority` when `work_handler is not None` (`:102-120`). The comment at `:99` explains the no-`aliases=` rule. `/status`, `/agents`, `/review` etc. are unaffected registrations.
- Read `ui/handlers/project_handler.py`: `cmd_status` (`:630-673`) counts units from the global `work_store` and prints a "Work units: N pending, ..." line (`:656`).
- Read `agent/runtime.py`: `_release_session_lease` (`:3109-3120`) calls `find_live_lease`/`release_work`; called from three turn-end sites (`:2836`, `:2916`, `:2920`).
- Read `ui/handlers/agent_runtime_handler.py`: `_worktree_cwd_for_turn` (`:940-`) — a writer enters its worktree if it holds a **live lease** OR its session key is suffixed (`special:coder-2`). Calls `find_live_lease` (`:968-970`).
- Read `utils/project_awareness.py`: `build_awareness_snapshot(project_path, work_store=None)` (`:892`) puts `_get_task_info(work_store)` (`:902`) into the snapshot under `"tasks"` (`:915`). `_get_task_info` (`:970`) returns zero-counts for a `None` store. `_work_units_for_awareness` (`:1008-1018`) lazily loads units; it has **exactly one caller** (`:1249`, inside the cached awareness builder in this same module). `snapshot["tasks"]` has **two readers**, both in this same module: `:1089-1096` (`build_awareness_block` "Work:" line, guarded by `total > 0`) and `:1258-1265` (identical block in the cached builder).
- Read `utils/status_report.py`: `_collect_work` (`:1020`) and `_parse_tasks_summary` (`:930`, reads generated `.crabcakes/tasks.md`) feed the `"work"` report section (`:1664`), consumed by `scripts/crab_status.py`.
- Read `agent/tools.py:2353-2361` + `agent/group_tools.py:54,378,434-445` + `models/feed_card.py:118,222,260`: `feed_card` carries an optional `task_id` string ("Optional work unit id") that is persisted in feed-card metadata and never read back by anything.
- Read `utils/workflow_state.py`: **no work-unit references** — owns `.crabcakes/workflow.md` phase tracking only. STAYS.
- Read `utils/worktree_manager.py`: **no work-persistence imports** — worktree creation is standalone. STAYS.
- Read `models/conversation.py`: `task_card` (`:329`) is a per-conversation text card (a prompt segment), written by `agent/runtime.py:4204`, rendered by `chat_render_handler.render_task_card` (`:1073`). **Not part of the work-unit system** — no references to `WorkUnit`/`work_store`. STAYS untouched.
- Read `tests/`: `test_work_unit.py`, `test_work_handler.py`, `test_work_persistence.py` test the removed system (delete). `test_task_redesign_prompts.py` asserts the current prompt text that this spec changes (delete or amend). `test_worktree_manager.py`, `test_workflow_state.py`, `test_chat_task_segment.py` (tests `task_card` rendering) test kept systems (keep).
- Read `prompts/`: `system/crabcakes-commands.md` (work-unit command reference, `:21-33`), `cc-task-planning.md` (redirect), `cc-spec-planning.md` (creates Work Units via `/work`), `cc-workflow-guide.md` (workflow driven by `/work start #N`) — all describe the removed system.
- Read `knowledge/commands.md` `:174-176`: describes a **`TaskHandler` in `ui/handlers/task_handler.py` with `models/task.py` `TaskStore`** — neither file exists. This is stale pre-redesign documentation that describes code that was already removed. `knowledge/overview.md` also has work-unit references.
- Read `.crabcakes/work.json`: `{"version":..., "work_units":[]}` — **empty since birth, no git history**. `.crabcakes/tasks.md`: 3-line stub. Zero data loss.
- Architecture owner: command routing is `CommandHandler`; the store singleton is `models/__init__.py`; persistence is `utils/work_persistence.py`; the workflow-file system (`utils/workflow_state.py`) is a **separate** owner that stays.
- Existing pattern to copy: the Auxilium/OpenClaw removals used deletion + a `tests/test_no_kb_residuals.py` guard that greps the tree for resurrected references. This spec copies that pattern.

---

## 1. Overview

**Problem.** The work-unit system (~2,000 lines across `work_handler.py`, `work_unit.py`, `work_persistence.py`, plus wiring in `window.py`, `command_handler.py`, `project_handler.py`, `agent/runtime.py`, `agent_runtime_handler.py`, `project_awareness.py`, `status_report.py`, and four prompt files) has never been used. `.crabcakes/work.json` has been empty since the day it was created; not one spec was ever tracked through a unit; the PM's actual workflow — proposal → spec → phases → implementation loop, driven by chat delegation — never touches the system. The legacy `/task` command set is already just aliases into it. Meanwhile the system has tendrils into the turn lifecycle (`_release_session_lease`), worktree path selection (`find_live_lease`), awareness snapshots, and status reports, which makes "just ignore it" worse than removal: dead code on hot paths.

**Solution.** Total removal: three modules deleted, one singleton deleted, all command registrations and legacy aliases deleted, three call-site tendrils excised, awareness and status surfaces rewritten to stop reporting work units, four prompt files rewritten, stale knowledge documentation corrected, and a residuals guard test added that fails if any of the removed names reappear. Worktree selection simplifies to the suffix rule only.

**Scope**

| In | Out |
|---|---|
| Delete `ui/handlers/work_handler.py`, `models/work_unit.py`, `utils/work_persistence.py` | `utils/workflow_state.py` — phase tracking, untouched |
| Delete the `work_store` singleton and its two consumers' use of it | `utils/worktree_manager.py` — untouched |
| Delete all `/work` registrations + legacy `/task` `/tasks` `/start` `/done` `/blocked` `/cancel` `/assign` `/priority` aliases | `models/conversation.py` `task_card` — a prompt segment, untouched |
| Excise `_release_session_lease`, `find_live_lease` from `agent/runtime.py`, and the lease branch from `_worktree_cwd_for_turn` | `feed_card` tool — untouched (`task_id` is a free string, never a unit) |
| Rewrite `build_awareness_snapshot`/`_get_task_info` and `/status` to drop unit counts | `agent/enforcement.py`, `agent/tools.py` — untouched |
| Rewrite `status_report._collect_work` to drop unit fields | `prompts/implementationLoop.md` — the loop prompt, untouched |
| Rewrite the four prompt files + `knowledge/commands.md` + `knowledge/overview.md` | `.crabcakes/` files in projects — out of the app's tree |
| Delete `tests/test_work_unit.py`, `test_work_handler.py`, `test_work_persistence.py`, `test_task_redesign_prompts.py` | Deleting `.crabcakes/work.json` in user projects — NOT done by the app |
| Add `tests/test_no_work_residuals.py` guard | |

**Principles**

1. A clean removal, not a deprecation: no stubs, no shims, no `NotImplemented` branches.
2. Kept systems must not change behavior: worktrees still open for suffixed writers; `/status` still answers; awareness snapshots still build.
3. A removed name that returns is a regression: the residuals test guards it.
4. Docs and prompts must not describe commands that do not exist.

---

## 2. Changes by File

### 2.1 DELETE `ui/handlers/work_handler.py`

Delete the whole file (1,046 lines). It owns only work-unit command semantics. Nothing outside the removal set imports it — verified: `grep -rn "work_handler" --include="*.py"` outside `window.py`/`command_handler.py`/tests finds nothing.

### 2.2 DELETE `models/work_unit.py`

Delete the whole file (314 lines): `WorkUnit`, `WorkLease`, `WorkUnitStore`, `WORK_PRIORITIES`, `WORK_STATUS_LABELS`, `WORK_PRIORITY_LABELS`, `_work_next_id`, `_validate_dependencies`, `_work_init_counter`.

### 2.3 DELETE `utils/work_persistence.py`

Delete the whole file (715 lines): all load/save/migrate/lease functions. `claim_work` has no caller anywhere in the app (only `release_work`/`find_live_lease` are called, from the two sites in §2.5/§2.6).

### 2.4 `models/__init__.py` — delete the singleton

- Delete `work_store = WorkUnitStore()` (line ~28).
- Delete the `from .work_unit import ...` line (~20) and `"work_store"` from `__all__` (~63).
- No other module of `models/` imports `work_unit` — verified by grep.

### 2.5 `agent/runtime.py` — excise the lease release

- Delete `_release_session_lease` (`:3109-3120`) entirely.
- Delete its three call sites: `:2836`, `:2916`, `:2920`. Each is one line inside existing `try`/`finally` blocks; removing the line leaves the surrounding structure valid (verified by reading each site — they are standalone statements, not `else` branches or sole `try` bodies).
- Behavior after removal: no lease release at turn end. Since no code ever **claims** a lease (§2.3), this is dead-path removal, not a behavior change.

### 2.6 `ui/handlers/agent_runtime_handler.py` — simplify worktree selection

`_worktree_cwd_for_turn` (`:940`): delete the `find_live_lease` import (`:968`) and the `leased = ...`/`if not leased and ...` lease branch. The rule becomes: a writer enters its worktree iff its session key is suffixed (`special:coder-2` etc.); primary `special:<role>` writers run in the project root. Update the docstring (`:940-953`) to match — the two-case list becomes one case. This **is** a behavior change for a hypothetical primary writer holding a lease, but no lease can exist after §2.3, so it is behavior-preserving in practice.

### 2.7 `ui/window.py` — delete the wiring

- Delete `from ui.handlers.work_handler import WorkHandler` (`:47`).
- Delete `self._work_handler = None` (`:92`) and the whole `WorkHandler(...)` construction block (`:673-681`).
- Delete `work_handler=self._work_handler` from the `CommandHandler(...)` call (`:716`).
- Delete the two project-open/close wires (`:873-876`: `load_for_project` / `close_project` lambdas).
- `_remote_status_summary` (`:1577-1599`): delete the `from models import work_store` block (`:1591-1594`) and the unit-count lines; keep Members / Review lines. New summary shape:

```
Project: {name}
Members: {len(members)}
Review: {review_status}
```

  Drop the try/except only if nothing inside it remains; keep it around whatever stays (follow the existing pattern — the Members/Review lines are outside the current try already).

### 2.8 `ui/handlers/command_handler.py` — delete registrations

- Delete the whole `if work_handler is not None:` registration block (`:102-120`): `work`, `task`, `tasks`, `start`, `done`, `blocked`, `cancel`, `assign`, `priority` — nine registrations.
- Delete the `work_handler=None` constructor parameter (`:58`), the `self._work_handler` assignment (`:74`), and the explanatory comment at `:97-100`.
- The comment block at `:99` (no-aliases rule) dies with the block.
- `/unblock`, `/work status`, `/spec-ready` route through the same deleted methods — gone with them.

### 2.9 `ui/handlers/project_handler.py` — `/status` drops unit counts

- Delete `from models import work_store` (`:19`).
- In `cmd_status` (`:630-673`): delete `all_units = work_store.list_all()` through the `done = ...` lines and the `"Work units: ..."` entry in `lines` (`:656`). Keep Project / Members / Review / Solo DM / provider-failure lines and all `sk.startswith("project:")` gating.

### 2.10 `utils/project_awareness.py` — awareness snapshot without units

- `build_awareness_snapshot` (`:892`): delete the `work_store` parameter, the `task_info = _get_task_info(work_store)` call (`:902`), and the `"tasks": task_info` entry (`:915`). Callers pass no `work_store` today — verified: `project_handler.py:207,597` call it with one positional arg. **The second caller is inside this same module (`:1250-1252`): the cached awareness builder calls `build_awareness_snapshot(project_path, _work_units_for_awareness(project_path))` (`:1249`). Delete the `_work_units_for_awareness(project_path)` argument there too — see below.**
- Delete `_get_task_info` (`:970-1006`) and `_work_units_for_awareness` (`:1008-1018`). `_work_units_for_awareness` has exactly one caller (`:1249`, deleted in the bullet above), so after that call site goes the function is dead and dies with it.
- **Two readers of `snapshot["tasks"]` exist in this module and die with the key:**
  - `:1089-1096` in `build_awareness_block` — `tasks = snapshot.get("tasks", {})`; the `if tasks.get("total", 0) > 0:` block appends a `"Work: N in progress, N spec-ready, N spec-pending, N done"` line. Delete the whole 8-line block (`:1089-1096`).
  - `:1258-1265` in the cached awareness builder — an identical 8-line block. Delete it too.
  Both are guarded by `total > 0`, which is already 0 today (the store is empty), so deleting them is behavior-preserving in practice and removes the dead "Work:" line from the awareness text.
- Delete the `from models.work_unit import WorkUnitStore` import (`:42`).
- Update the module docstring block (`:22-25`) and the `Gathers:` line (`:896`) to drop "work unit summary".

### 2.11 `utils/status_report.py` — `work` section without units

- `_collect_work` (`:1020`): delete `section["unit"], section["unit_status"] = _parse_tasks_summary(project_path)` (`:1035`). Keep the rest (git posture, write-scan evidence).
- Delete `_parse_tasks_summary` (`:930-...`).
- `report["work"].get("newest_write_ts")` consumers (`:1351`, `:1376`, `:1517`, `:1668`) read `newest_write_ts`, not unit fields — no other change. Verified by grep: `"unit"`/`"unit_status"` appear only in `_collect_work`/`_parse_tasks_summary`.

### 2.12 Prompts — describe reality

- `prompts/system/crabcakes-commands.md`: delete the Work Units section (`:21-33`) — the `/work` table row and the legacy alias paragraphs. The command table keeps every non-work command.
- `prompts/cc-task-planning.md`: it is already a redirect; delete the file, or replace its body with a one-line "superseded" note pointing at `cc-spec-planning.md`. Prefer deletion — `cc-spec-planning.md` already exists as the real prompt.
- `prompts/cc-spec-planning.md`: remove the Work-Unit creation steps (§ around `:9-21`: "Replace the old flat-task model…", "Create approved Work Units using `/work` commands"). The spec-discovery substance stays; the recording step becomes "record the approved spec list in chat and in `.crabcakes/workflow.md`".
- `prompts/cc-workflow-guide.md`: rewrite the phase table and narration so Phase 3 output is "approved specs (recorded in chat + workflow.md)" and Phase 4 is triggered by PM delegation in chat, not `/work start #N`. Keep the read-workflow.md ritual.
- **Files NOT changed**: `prompts/implementationLoop.md` (the loop itself — no work-unit references), `prompts/system/supervisor.md` (its `/work` mentions are only the onboarding checklist line about workflow.md, which stays), `prompts/system/project-awareness.md` (line `:21` mentions `/task list` — see §2.13).

### 2.13 `knowledge/commands.md` + `knowledge/overview.md`

- `commands.md`: delete the stale TaskHandler/TaskStore section (`:174-176` — describes files that do not exist), delete the `/tasks`-in-no-payload list mention (`:22`) and the task-counts line in `/status` (`:76`), and remove `/delegate @Agent "task"` wording that implies the task store (the `/delegate` command itself stays — "task" there is prose).
- `overview.md`: remove work-unit/work.json references the same way.
- `prompts/system/project-awareness.md:21` ("suggest running /task run… review with /task list"): update to suggest loading the spec-planning prompt and reviewing approved specs in chat.

### 2.14 Tests

- DELETE `tests/test_work_unit.py`, `tests/test_work_handler.py`, `tests/test_work_persistence.py` — they test deleted modules.
- DELETE `tests/test_task_redesign_prompts.py` — it asserts the exact prompt text §2.12 changes.
- KEEP `tests/test_worktree_manager.py` (worktrees stay), `tests/test_workflow_state.py` (workflow.md stays), `tests/test_chat_task_segment.py` (tests `task_card`, which stays).
- ADD `tests/test_no_work_residuals.py` (pattern copied from `tests/test_no_kb_residuals.py`):

```python
# Walk the repo source tree (agent/, models/, ui/, utils/, transport/,
# scripts/, main.py) and fail if any removed name appears.
REMOVED_NAMES = (
    "WorkUnit", "WorkLease", "WorkUnitStore", "work_store",
    "work_handler", "WorkHandler",
    "load_or_migrate_work_units", "save_work_units",
    "claim_work", "release_work", "find_live_lease", "assert_lease",
    "load_work_units", "render_tasks_summary", "write_tasks_summary",
    "cmd_work", "_release_session_lease", "_parse_tasks_summary",
    "_get_task_info", "_work_units_for_awareness",
)
```

  For each `.py` file under those roots (skipping `.venv`, `scratch/`, `.crabcakes/`, `tests/` — comments and docstrings in kept files may legitimately mention the history; the guard targets code), assert none of the names appear. Also assert the three deleted files do not exist. A test that finds a name must print file:line for each hit.

### 2.15 Files NOT changed (already verified clean)

- `agent/tools.py`, `agent/group_tools.py`, `models/feed_card.py` — `feed_card`'s `task_id` is a free-form string stored in card metadata, never a work-unit reference. Keep, including its parameter (removing it would churn feed-card persistence for zero gain).
- `models/conversation.py` — `task_card` is a conversation prompt segment with its own render path. Keep.
- `agent/enforcement.py`, `agent/context_strategy.py`, `agent/special_agents.py`, `utils/agent_defs.py` — no work-unit references (grep-verified).
- `utils/worktree_manager.py`, `utils/workflow_state.py`, `transport/`, `scripts/crab_status.py` (calls `status_report.collect`; the `work` section shrinks but the script needs no edit).
- `ui/views/main_content.py`, `ui/views/chat_surface.py`, `ui/handlers/chat_render_handler.py` — no work-unit references (grep-verified; `main_content.py` hits were `replace_all`/toolbar).
- `docs/proposals/PROPOSAL-META.md` — Program 4 amendment is PM-approved separately (this spec Implements it).

---

## 3. Data Flow

**Today (removed path):** PM types `/work "Title"` → `CommandHandler.process_input` → `WorkHandler.cmd_work` → `load_or_migrate_work_units` → `work_store.replace_all` → `save_work_units` writes `work.json` + regenerates `tasks.md`. `/work start #N` → `cmd_work_start` → claims lease → `send_to_special_agent` to Supervisor. Turn end → `_release_session_lease`. Awareness snapshot reads `work_store.list_all()` into `snapshot["tasks"]`. `/status` counts units from the global store.

**After:** PM types `/work anything` → `CommandHandler` finds no registration → falls through to chat send (same as any unknown command today). No store exists. Turn end does nothing lease-shaped. Worktree selection is suffix-only. Awareness snapshot has no `"tasks"` key (the two in-module readers at `:1089-1096`/`:1258-1265` die with it — see §2.10). `/status` and `_remote_status_summary` answer Members/Review/Solo DM only.

**Kept path (unchanged):** `init_workflow`/`advance_phase` still write `.crabcakes/workflow.md`; the implementation loop prompt still drives Coder→Debugger→Supervisor; `task_card` still renders.

---

## 4. File Change Summary

| File | Change | Lines | Risk |
|---|---|---|---|
| `ui/handlers/work_handler.py` | DELETE | −1046 | Low — nothing imports it after this spec |
| `models/work_unit.py` | DELETE | −314 | Low |
| `utils/work_persistence.py` | DELETE | −715 | Low |
| `models/__init__.py` | Remove singleton + exports | −4 | Low |
| `agent/runtime.py` | Delete `_release_session_lease` + 3 calls | −15 | Medium — turn-end path; verified standalone statements |
| `ui/handlers/agent_runtime_handler.py` | Drop lease branch in worktree selection | −12 | Medium — behavior-preserving in practice (no claims exist) |
| `ui/window.py` | Delete wiring + status counts | −40 | Low |
| `ui/handlers/command_handler.py` | Delete 9 registrations + param | −30 | Low |
| `ui/handlers/project_handler.py` | `/status` drops counts | −12 | Low |
| `utils/project_awareness.py` | Drop `work_store` param, `_get_task_info` | −45 | Low — callers pass 1 arg |
| `utils/status_report.py` | Drop unit fields | −30 | Low — consumers read `newest_write_ts` only |
| 4 prompt files + 2 knowledge files | Rewrite work refs | ~±60 | Low |
| 3 test files + `test_task_redesign_prompts.py` | DELETE | ~−1500 | Low |
| `tests/test_no_work_residuals.py` | ADD | +60 | Low |
| `docs/ARCHITECTURE.md` | §8 edits | ~±20 | Low |

Net: roughly **−3,100 lines.**

---

## 5. Implementation Order

Each step ends with its own verification. Do not advance until the step's check passes.

1. **Delete the three modules.** Then: `python -m py_compile models/__init__.py` — the import of the missing `work_unit` fails here first. Fix `models/__init__.py` in the same step. Verify: `grep -rn "from models.work_unit\|from utils.work_persistence\|from ui.handlers.work_handler" --include="*.py" agent/ ui/ utils/ models/ transport/ scripts/ main.py` → only the removal-set files still listed, then zero after step 2.
2. **Excise consumers in dependency order:** `agent/runtime.py` (§2.5), `agent_runtime_handler.py` (§2.6), `window.py` (§2.7), `command_handler.py` (§2.8), `project_handler.py` (§2.9), `project_awareness.py` (§2.10), `status_report.py` (§2.11). Verify after each: `python -m py_compile <file>`, then `ruff check <file>` — new findings must be 0 vs baseline.
3. **Delete the four test files** (§2.14) — they import deleted modules and will not even collect. Verify: `python -m pytest tests/test_worktree_manager.py tests/test_workflow_state.py tests/test_chat_task_segment.py -q` → these three still pass.
4. **Prompts + knowledge** (§2.12–2.13). Verify: `grep -rn "work unit\|Work Unit\|/work\b\|work.json\|tasks.md" prompts/ knowledge/` → only history-appropriate mentions remain (e.g. a "the work-unit system was removed in SPEC-49" note is allowed; a how-to is not).
5. **Add the residuals guard** (§2.14). Verify it: `python -m pytest tests/test_no_work_residuals.py -q` → passes on the post-removal tree.
6. **Full gate:** `python -m pytest tests/ -q -x --ignore=tests/test_mvp_integration.py` (or the project's standard invocation) — zero failures; plus `python -m scripts.acceptance rows` → the SPEC-47 gate must stay green (it is the arbiter of "clean tree").
7. **Docs:** `docs/ARCHITECTURE.md` §8 updates, META Program 4 amendment if not already landed.

---

## 6. Acceptance Criteria

- [ ] The three files `ui/handlers/work_handler.py`, `models/work_unit.py`, `utils/work_persistence.py` do not exist, and `models/__init__.py` exports no work names.
- [ ] `/work`, `/task`, `/tasks`, `/start`, `/done`, `/blocked`, `/unblock`, `/cancel`, `/assign`, `/priority` are unknown commands (fall through to chat send), verified from a project tab.
- [ ] `/status` answers Project / Members / Review / Solo DM / provider-failures — no "Work units" line. `_remote_status_summary` (Telegram path) matches.
- [ ] A suffixed writer (`special:coder-2`) still enters its worktree on its next turn; a primary `special:coder` runs in the project root.
- [ ] No code path in `agent/runtime.py` references leases at turn end.
- [ ] `build_awareness_snapshot(path)` returns a dict without a `"tasks"` key and saves fine; `awareness.json` regenerates on project open.
- [ ] `scripts/crab_status.py` still runs and reports; its `work` section has no unit/unit_status fields.
- [ ] `tests/test_no_work_residuals.py` passes on the tree and **fails** if a file re-adding any removed name is created (spot-check by temporarily re-adding one import in a scratch copy, then reverting).
- [ ] `python -m pytest tests/ -q` is green; `scripts/acceptance.py rows` exits 0.
- [ ] Prompts and knowledge contain no instructions to run work-unit commands.
- [ ] `.crabcakes/workflow.md`, worktrees, `task_card`, `feed_card`, and the implementation loop behave exactly as before.

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| PM types `/work list` after removal | Unknown command → falls through to chat send, like any unregistered token |
| PM types `/done` or `/start` (legacy aliases) | Same fall-through; nothing registers them |
| Old `.crabcakes/work.json` in a user project | Ignored. The app neither reads nor deletes it. No migration |
| Old generated `tasks.md` | Ignored. `status_report` no longer reads it |
| `awareness.json` from a previous session still has `"tasks"` | Overwritten on next snapshot save; no reader of the key exists |
| Agent session previously held a lease (pre-removal state on disk) | Nothing reads it; no claim or release path exists |
| Primary writer previously in a worktree via lease | Next turn uses project root (suffix rule only). Worktree directories are not deleted |
| A test imports `WorkUnitStore` post-removal | `tests/test_no_work_residuals.py` is the enforcement; deleted tests are gone |
| Feed card with `task_id` set | Still loads/saves — `task_id` is a free string, untouched |
| `cc-task-planning.md` referenced from an old workflow.md in a project | File deleted; the phase-history table in `workflow_state.py` keeps its own names (`"spec-planning"`), unaffected |
| Residuals guard finds a name in a docstring of a KEPT file | Allowed — guard scans code, excludes `tests/`, `scratch/`, `.crabcakes/`; comments may mention history |

---

## 8. ARCHITECTURE.md Updates Required

1. **§ Work-handler / command-routing paragraph** (wherever `ui/handlers/work_handler.py` is described): delete the paragraph; note the system was removed by SPEC-49.
2. **§ `models/` layer list**: remove `work_unit.py` / the `work_store` singleton sentence.
3. **§ `utils/` list**: remove `work_persistence.py`; keep `workflow_state.py` and `worktree_manager.py` entries.
4. **§ lease/worktree selection** (the SPEC-09-era paragraph describing lease-driven worktree entry): rewrite to suffix-only.
5. **§ awareness snapshot**: drop the work-unit summary sentence.
6. **§ command reference table**: remove the `/work` row and legacy alias rows.

---

## 9. Self-Audit

1. Every file listed in §2 was opened and read this session; line numbers are from the current tree (`git status` clean at base except the META amendment).
2. Consumers were found by grep across `agent/ ui/ utils/ models/ transport/ scripts/ main.py`, excluding `.venv`, `scratch/`, `.crabcakes/`, `tests/`. The removal set in §2 is exhaustive against that sweep; §2.15 lists the near-misses that stay.
3. `claim_work` has no caller — the lease system is release-only, i.e. dead; removing it is behavior-preserving (§2.5, §2.6).
4. `build_awareness_snapshot` callers pass one positional arg (`project_handler.py:207, 597`) — dropping the second parameter breaks no call site (§2.10).
5. `status_report` consumers read `newest_write_ts`, never `unit`/`unit_status` (grep-verified) — dropping those fields breaks nothing (§2.11).
6. `task_card` (models/conversation.py) and `feed_card`'s `task_id` are name-collisions, not system parts — explicitly kept, tests kept (§2.14, §2.15).
7. `workflow_state.py`'s phase table keeps `"spec-planning"` etc. as its own literals — untouched by the prompt-file rewrites (§2.12).
8. The residuals test pattern is copied from an existing passing guard (`test_no_kb_residuals.py`), not invented.
9. Rule-10 checks: scope checklist is §2.1–§2.14 (14 entries, each named); the full-suite + acceptance-gate run is §5.6; the pattern sweep is the residuals test itself.