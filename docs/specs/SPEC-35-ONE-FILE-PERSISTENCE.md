# SPEC-35: One file per conversation

**Date:** 2026-10-15
**Author:** Supervisor
**Status:** Draft — for implementation
**Implements:** the One File decision (2026-10-15). Delete the transcript store. Do not rewrite it.
**Depends on:** none
**Target branch:** main

> Architecture: `agent/persistence.py` owns conversation files (ARCHITECTURE.md §3.21m). This spec does not add a module. It removes `utils/transcript_store.py` and makes the JSON file the only copy.

---

## DISCOVERY

- Read `agent/persistence.py`: `save_conversation_to_disk(conv, session_key) -> str` builds the JSON dict at lines 91–129, then `open(path, "w")` (line 130) which truncates the live file before `json.dump`. It then calls `_get_store().set_session_meta` and `_append_conversation_delta`. `load_conversation_from_disk(session_key) -> tuple[Conversation, dict] | None` reads `<session_key>.json` when that path `isfile`; on `JSONDecodeError` or `OSError` it returns `None` (lines 567–571) and does not try the store. When the file is absent it calls `_load_conversation_from_store`. `delete_session_rows(session_key) -> int` calls `TranscriptStore.delete_session`. `migrate_conversation_files() -> int` rewrites JSON to strip `api_key`. `migrate_conversations_to_store` / `run_store_migration_once` rename files to `.json.migrated`. `resolve_api_key_for_conversation(data) -> str | None` reads `providers.yaml` via `load_providers()`; it never reads `data["api_key"]`. `resolve_session_workspace(project_path, session_key) -> str` is unrelated scratch space; colon in the key is sanitized only there (`special:coder` → `special-coder`). Conversation filenames are not sanitized: the live file is `special:coder.json`.
- Read `models/conversation.py`: `MessageRole.TOOL_RESULT` value is `"tool"`. `Message` fields used by the save dict: `role`, `content`, `tool_calls` (`call_id`, `tool_name`, `arguments`), `tool_call_id`, `tokens_used`, `timestamp` (`isoformat`). `is_summary` is not written. `Conversation.created_at` is written and is not passed back into `Conversation(...)` by `_conversation_from_data`. Load forces `project_path=None` and `system_prompt=""`.
- Read `agent/runtime.py`: `__init__` calls `migrate_conversation_files()` (line 634) and, when `_MIGRATE_STORE_ON_INIT` (`get_env("MIGRATE_STORE") == "1"`, lines 120–122), `_start_store_migration()`. `_running = False` and `_stopped = False` are set in that same block (lines 651–652) and `start()` / `stop()` depend on them. Imports from persistence (lines 56–63): `conversations_dir`, `load_conversation_from_disk`, `migrate_conversation_files`, `resolve_session_workspace`, `run_store_migration_once`, `save_conversation_to_disk`. Save callers: `stop()` line 1199, `_auto_save` line 3934, `save_conversation` line 3946. All three already catch `Exception`.
- Read `main.py` lines 44–60: if `get_env("MIGRATE_STORE") is None`, set `DEVELCAKES_MIGRATE_STORE=1` before the GTK import. `main()` calls `migrate_v1_config()` (line 386) before the app runs.
- Read `utils/config.py`: `_MIGRATION_DB_FILES = ("transcript.db", "transcript.db-wal", "transcript.db-shm")` (line 69). `migrate_v1_config` concatenates it into the presence guard (line 260) and the file-copy loop (line 273). `get_config_dir()` is `~/.config/develcakes` unless `$XDG_CONFIG_HOME` is set. `get_env(name) -> str | None`.
- Read `ui/agent_runtime/session.py` `clear_conversation`: in-place reset, then `os.remove(<config>/conversations/<session_key>.json)` catching `FileNotFoundError` and `OSError`, then `delete_session_rows`. Refusal when `rt.is_loop_active(session_key)` returns before any delete.
- Read `ui/handlers/agent_runtime_handler.py` lines 889–892 and 1247–1332: `set_on_store_migration` plus `_on_store_migration_complete` / `_on_store_migration_progress`.
- Read `tests/conftest.py` lines 21–33 and 65–85: process-wide `CRABCAKES_MIGRATE_STORE=0`, autouse `isolate_transcript_store` constructing a real `TranscriptStore`.
- Architecture owner: `agent/persistence.py`. Callers stay on `save_conversation_to_disk` and `load_conversation_from_disk`. No new module.

---

## 1. Overview

### Problem

A conversation is one document. Masking and compaction edit it. The save truncates that document with `open(path, "w")` before the new bytes exist. SPEC-08 added a SQLite ledger beside it. Load still prefers the JSON file, and the ledger cannot record an edit, so the two copies diverge. Supervisor on this machine is the proof: 442 store rows, 188 messages in the JSON, 185 mismatches, `diverged=1`.

### Solution

One file, `<config_dir>/conversations/<session_key>.json`, written to a temp file and moved into place with `os.replace`. Delete the store, the migration sweep, the flag, and the banner. Do not read `transcript.db`. Do not rename `.json.migrated` back. After the code lands, delete the database files and the archive names by hand, once. The app does not do that.

### Scope

| In | Out |
|---|---|
| Atomic replace in `save_conversation_to_disk` | A new SQLite schema, stable message ids, epochs, watermarks |
| Delete store code, flag, banner, tests | Reading or merging old rows into JSON |
| Manual delete of the old database and archive names, once, after the code lands | A startup sweeper, a migrator, a banner, a retry latch |
| `/clear` deletes the one JSON file | `delete_session_rows` |
| v1 config copy stops copying `transcript.db*` | Changing `resolve_session_workspace`, the JSON dict, or load's `project_path=None` / `system_prompt=""` |

### Principles

The file after `os.replace` is either the previous complete file or the new complete file. `api_key` is still never serialized. Fewer functions, not a second persistence stack.

---

## 2. Changes by File

### `agent/persistence.py`

Keep: `conversations_dir`, `save_conversation_to_disk`, `load_conversation_from_disk`, `_naive_now`, `_message_from_data`, `_conversation_from_data`, `_apply_loaded_fallbacks`, `resolve_api_key_for_conversation`, `resolve_session_workspace`.

Delete: `_store_override`, `_store_singleton`, `_get_store`, `_same_turn`, `_shape_message`, `_append_conversation_delta`, `_load_conversation_from_store`, `delete_session_rows`, `_CONVERSATION_MIGRATION_DONE`, `migrate_conversation_files`, `_MIGRATION_PROGRESS_INTERVAL`, `migrate_conversations_to_store`, `_shape_json_message`, `_STORE_MIGRATION_DONE`, `run_store_migration_once`, and the `TranscriptStore` import.

`save_conversation_to_disk` keeps the dict at lines 91–129. Replace the truncate-write, the chmod-after, and both store `try` blocks with:

```python
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
except Exception:
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise
return path
```

`os.fdopen` owns `fd`. Do not `os.close(fd)` as well. `os.open` mode `0o600` is already owner-only under any normal umask, so no `chmod` after replace. `json.dump` can raise `TypeError` / `ValueError`. `os.open`, `os.fsync`, `os.replace`, and `os.remove` raise `OSError`. The `except Exception` removes a leftover temp and re-raises. Callers in `runtime.py` (`stop`, `_auto_save`, `save_conversation`) already catch `Exception`. Return value is the final path, unchanged. Do not catch-and-log inside save.

`load_conversation_from_disk`: if the `.json` is not a file, return `None`. If `json.load` raises `JSONDecodeError` or `OSError`, return `None`. Otherwise `_conversation_from_data`, `_apply_loaded_fallbacks`, return `(conv, data)`. No store branch. Do not read `data["api_key"]`. Do not pass `created_at` into `Conversation`. Do not delete a corrupt `.json` from inside load.

Do not add a startup sweeper. Deleting `transcript.db`, its sidecars, `.json.migrated`, `.json.tmp`, and `.bak` names is a one-time manual delete after the code lands. The app never unlinks them.

`conversations_dir()` stays as it is, including `chmod 0o700` when the directory is created.

### `agent/runtime.py`

Drop `migrate_conversation_files` and `run_store_migration_once` from the persistence import. Delete `_MIGRATE_STORE_ON_INIT`, `_migrate_store_async`, `_log_store_migration_banner`, `_start_store_migration`, and `set_on_store_migration`.

In `AgentRuntime.__init__`, replace the `migrate_conversation_files()` call and the whole store-migration block with:

```python
self._running = False
self._stopped = False
```

Those two flags stay: `start()` sets `_running`, `stop()` sets `_stopped`.

### `main.py`

Delete lines 44–60 (the `DEVELCAKES_MIGRATE_STORE` default). Delete the SPEC-08 comment above `migrate_v1_config()` in `main()` (lines 370–380). Leave the `migrate_v1_config()` call.

### `utils/config.py`

Delete `_MIGRATION_DB_FILES`. At lines 260 and 273, concatenate `_MIGRATION_FILES + _MIGRATION_DIRS` and `_MIGRATION_FILES` only. Remove `transcript.db` from the `migrate_v1_config` docstring (lines 196 and 210–212). The v1 copier must not create those three files. Do not delete anything inside the v1 config dir.

### `ui/agent_runtime/session.py`

Delete the `delete_session_rows` block (lines 124–141). `/clear` still deletes only `<config_dir>/conversations/<session_key>.json` via `os.remove`, still catches `FileNotFoundError` and `OSError`, and still returns `False` before that delete when `is_loop_active` is true.

### `ui/handlers/agent_runtime_handler.py`

Delete the `rt.set_on_store_migration(...)` call (lines 884–892). Delete `_on_store_migration_complete` and `_on_store_migration_progress` (lines 1247–1332). Leave `_on_turn_start`.

### Delete these files

- `utils/transcript_store.py`
- `tests/test_transcript_store.py`
- `tests/test_migration.py`

### `tests/conftest.py`

Delete the `CRABCAKES_MIGRATE_STORE=0` assignment (lines 21–33) and the `isolate_transcript_store` fixture (lines 65–85). Keep `_isolate_config_resolution`.

### Tests to rewrite, not extend

- `tests/test_agent_persistence.py`: delete every test that constructs `TranscriptStore`, patches `_store_override` / `_get_store`, or renames a file to `.migrated` and expects store hydration. Keep a roundtrip: `save_conversation_to_disk` then `load_conversation_from_disk` returns the same roles, content, tool calls, and `tool_call_id`, and the file contains no `api_key`. Add two tests:
  - `json.dump` raising leaves the previous file bytes unchanged and leaves no `.json.tmp`.
  - A file that contains `"api_key": "sk-SECRET"` loads with `conv.api_key` equal to whatever `resolve_api_key_for_conversation` returns for that provider, never `sk-SECRET`.
- `tests/test_conversation.py`: delete class `TestMigrateConversationFiles`.
- `tests/test_live_bridge.py`: delete `test_clear_conversation_deletes_store_rows`. In `test_arh_clear_conversation_deletes_store_rows_behaviorally`, drop the store. Assert the in-memory reset, the active-loop refusal, and that a JSON file written beside the session is removed on success and still present on refusal.
- `tests/test_env_divergence.py`: delete `test_migrate_store_latch_reads_new_and_fallback` and `test_main_setdefault_respects_old_name_kill_switch`.
- `tests/test_config_migration.py`: where a test expects `transcript.db`, `transcript.db-wal`, or `transcript.db-shm` to be copied, expect them to be left in v1 and absent from the new dir.
- `tests/test_spec23_context_working_set.py` line 160: delete the `patch("agent.persistence._get_store")`. The save/load around it stays.

`tests/test_error_surfacing.py` and `tests/test_config_invalidation.py` only mention the old calls in comments. Update the comments. Do not change the tests' behavior.

### Files NOT changed

- `models/conversation.py` — the JSON dict already matches `Message` / `Conversation`. `is_summary` stays unpersisted.
- `resolve_session_workspace` — scratch dirs, not transcripts.
- `agent/runtime.py` save call sites — signatures do not change.
- `utils/providers_store.py` — `load_providers()` is still how the key is resolved.

---

## 3. Data Flow

Turn ends → `_terminate_turn` → `_auto_save` → `save_conversation_to_disk`. The dict is the same one the file has used. Bytes go to `session.json.tmp`, `fsync`, `os.replace` onto `session.json`. Process exit or crash before replace leaves the previous `session.json`.

A send whose runtime has no conversation → `load_conversation` → `load_conversation_from_disk`. File present and valid → `Conversation` with `project_path=None`, `system_prompt=""`, `api_key` from `providers.yaml`. File missing or unreadable → `None` → `create_conversation`.

`/clear` → memory reset → `os.remove` of that session's `.json` only. Next launch loads nothing for that key.

---

## 4. File Change Summary

| File | Change | Risk |
|---|---|---|
| `agent/persistence.py` | Atomic save; delete store and both sweeps | med — this is the data path |
| `agent/runtime.py` | Drop migration thread and flag | low |
| `main.py` | Drop `DEVELCAKES_MIGRATE_STORE` default | low |
| `utils/config.py` | Stop copying the three db files from v1 | low |
| `ui/agent_runtime/session.py` | `/clear` deletes only the JSON | low |
| `ui/handlers/agent_runtime_handler.py` | Delete banner methods | low |
| `utils/transcript_store.py` | Delete the file | low |
| `tests/test_transcript_store.py`, `tests/test_migration.py` | Delete | low |
| `tests/conftest.py` and the test files in §2 | Drop store fixtures and rewrite the pins | med |

Net code goes down. `persistence.py` loses the delta, the diverged guard, and both sweeps (roughly lines 30–61, 138–188, 191–307, 406–459, 520–533, 586–949).

---

## 5. Implementation Order

1. Rewrite `save_conversation_to_disk` and `load_conversation_from_disk`. Delete store helpers in the same file. Run `tests/test_agent_persistence.py` after the new pins exist.
2. Delete the runtime flag, thread, and `set_on_store_migration`. Keep `_running = False` and `_stopped = False` in `__init__`.
3. Delete the banner methods and the `set_on_store_migration` call. Strip `/clear`'s store block.
4. Remove `_MIGRATION_DB_FILES` and the `main.py` env default.
5. Delete `utils/transcript_store.py`, `tests/test_transcript_store.py`, `tests/test_migration.py`. Fix conftest and the remaining tests.
6. Grep. Then delete `transcript.db`, `transcript.db-wal`, `transcript.db-shm`, and conversation names ending in `.json.migrated` or `.json.tmp` or containing `.bak`, by hand, once. Do not add code that does this.

---

## 6. Acceptance Criteria

- [ ] `save_conversation_to_disk` / `load_conversation_from_disk` roundtrip messages, including tool calls, and the file mode is `0o600`.
- [ ] A raised `json.dump` does not change the previous file and does not leave `*.json.tmp`.
- [ ] `api_key` is absent from the written JSON. A file that already contains `api_key` does not supply `conv.api_key`.
- [ ] Missing file and unreadable file both return `None`.
- [ ] `/clear` removes that session's `.json` and does not import a store. Active-loop refusal deletes nothing.
- [ ] `migrate_v1_config` does not copy `transcript.db` or its sidecars.
- [ ] `rg` over `*.py` finds no `transcript_store`, `TranscriptStore`, `delete_session_rows`, `migrate_conversations_to_store`, `run_store_migration_once`, `MIGRATE_STORE`, or `_get_store`.
- [ ] After the manual delete, `~/.config/develcakes/transcript.db`, `-wal`, and `-shm` are gone, and `conversations/` contains no `.json.migrated`, `.json.tmp`, or `.bak` name. Live `*.json` files are still there. The app has no code that performs this delete.
- [ ] `pytest` for the touched test modules is green. Paste the count.

---

## 7. Edge Cases

| Case | Behavior |
|---|---|
| Crash while writing `.json.tmp` | Previous `.json` unchanged. The temp file is removed if the save's exception handler runs. |
| `os.replace` raises | Temp removed, exception propagates, caller logs. Previous file unchanged because replace did not succeed. |
| Session only in `.json.migrated` or only in `transcript.db` | Not loaded. Not merged. Those files are deleted by hand once, not by the app. |
| `special:coder` | Filename stays `special:coder.json`. Do not substitute the workspace sanitizer. |
| Corrupt `.json` | `load_conversation_from_disk` returns `None`. Load does not delete it. The next successful save replaces it. |
| `/clear` during a tool loop | `False`, file kept, memory kept. |
| Old `transcript.db` still on disk | Ignored. Nothing opens it. |
| v1 dir still has `transcript.db` | Left alone. The copier no longer brings it across. |

---

## 8. ARCHITECTURE.md Updates Required

Under §3.21m, add one sentence: conversation persistence is one JSON file per `session_key` in `<config_dir>/conversations/`, replaced atomically; there is no `transcript.db`. Do not touch the sentence that says the runtime owns the in-memory conversation store. That word means `_conversations`, not SQLite.

---

## Self-audit

- Save sample: `os.fdopen` closes `fd`; the `except` only unlinks the temp; `os.replace` is in the `try`, so a failed replace does not look like success. Return path matches today's `str`.
- Load sample: same `None` conditions as lines 549 and 567–571, minus the store call. `_conversation_from_data` still requires `data["agent_name"]` (`KeyError` if missing). That is the current JSON path. Do not catch `KeyError`.
- `/clear` still uses `os.remove` and the same two handlers. `delete_session_rows` is gone, so the `except Exception` around it goes too.
- `_running` / `_stopped` stay initialized in `__init__`. Removing them would `AttributeError` in `stop()` before `start()`.
- No sample calls `TranscriptStore`, `set_session_meta`, or `append_delta`.
