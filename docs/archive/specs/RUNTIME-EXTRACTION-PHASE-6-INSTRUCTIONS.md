# Phase 6 Instructions — Conversation Persistence Extraction

**Spec:** `docs/specs/SPEC-RUNTIME-EXTRACTION-PHASE-6.md`
**Files:** `agent/persistence.py` (NEW) + `agent/runtime.py` + `tests/test_agent_persistence.py` (NEW) + `tests/test_low2_file_sandbox.py` + `tests/test_conversation.py`

**FIRST ACTION: Load the steelFramedCodeWriter prompt fresh.** Read `/path/to/projects/crabcakes/prompts/steelFramedCodeWriter.md` IN FULL. Activate it. Begin with Discovery Phase block.

Read `agent/runtime.py` lines 350-650 (the 6 persistence functions) in full before editing.

---

## Overview

Move 6 stateless persistence functions from runtime.py to a new `agent/persistence.py`. Drop the leading underscore from names (they're public in their own module now). Update all call sites in runtime.py AND in 2 test files.

---

## Edit 1 — Create `agent/persistence.py` (NEW FILE)

Create this file with the 6 functions moved VERBATIM from runtime.py. The functions are (current line numbers approximate):

1. `_conversations_dir()` (line ~352) → `conversations_dir()`
2. `_save_conversation_to_disk(conv, session_key)` (line ~370) → `save_conversation_to_disk(conv, session_key)`
3. `_resolve_api_key_for_conversation(data)` (line ~427) → `resolve_api_key_for_conversation(data)`
4. `_load_conversation_from_disk(session_key)` (line ~462) → `load_conversation_from_disk(session_key)`
5. `_migrate_conversation_files()` (line ~553) → `migrate_conversation_files()` + the `_CONVERSATION_MIGRATION_DONE` flag
6. `_resolve_session_workspace(project_path, session_key)` (line ~606) → `resolve_session_workspace(project_path, session_key)`

**CRITICAL — this is a VERBATIM MOVE:**
- Copy each function body EXACTLY. Do not change any logic.
- Inside each function, update calls to sibling functions to use the new names (e.g., `conversations_dir()` not `_conversations_dir()`).
- The `_CONVERSATION_MIGRATION_DONE` global flag moves to persistence.py. The `global` statement inside `migrate_conversation_files` refers to the persistence.py module-level flag.
- Keep ALL lazy imports inside functions (e.g., `from utils.config import get_config_dir` inside `conversations_dir`, `from models.conversation import ...` inside `load_conversation_from_disk`). Do NOT move them to module level — lazy imports prevent circular dependencies.
- The `logger` references inside functions: persistence.py needs its own `logger = logging.getLogger(__name__)` at module level. Add `import logging` and `logger = logging.getLogger(__name__)` at the top.

Module structure:
```python
"""Conversation persistence — disk I/O for conversation state.

Extracted from agent/runtime.py (Phase 6). Stateless module-level helpers
for saving/loading conversations to ~/.config/crabcakes/conversations/.

Security:
  - HIGH-3: api_key is NEVER serialized. Re-resolved from providers.yaml on load.
  - LOW-2: session workspace validation prevents path escapes.
  - Conversation files are chmod 0600 after write.

Pure Python — no GTK, no network, no agent.runtime imports.
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# ... 6 functions ...
```

## Edit 2 — `agent/runtime.py`: add import block

Near the other `from agent.` imports at the top, add:
```python
from agent.persistence import (
    conversations_dir,
    load_conversation_from_disk,
    migrate_conversation_files,
    resolve_api_key_for_conversation,
    resolve_session_workspace,
    save_conversation_to_disk,
)
```

## Edit 3 — `agent/runtime.py`: remove the 6 inline functions

Delete the 6 function definitions AND the `_CONVERSATION_MIGRATION_DONE` flag from runtime.py (approximately lines 350-650, including section comments). These are now in `agent/persistence.py`.

**Use grep to find the exact boundaries before deleting:**
```bash
grep -n "def _conversations_dir\|def _save_conversation_to_disk\|def _resolve_api_key_for_conversation\|def _load_conversation_from_disk\|def _migrate_conversation_files\|def _resolve_session_workspace\|_CONVERSATION_MIGRATION_DONE\|# ── Conversation persistence\|# ── HIGH-3\|# ── LOW-2" agent/runtime.py
```

Delete from the first function (`_conversations_dir`) through the last (`_resolve_session_workspace`), including all section comments and the migration flag.

## Edit 4 — `agent/runtime.py`: update ALL call sites

Grep for all underscored references and update each to the non-underscored name:
```bash
grep -n "_save_conversation_to_disk\|_load_conversation_from_disk\|_conversations_dir\|_resolve_api_key_for_conversation\|_migrate_conversation_files\|_resolve_session_workspace" agent/runtime.py
```

Expected call sites (line numbers approximate, may have drifted):
- `_migrate_conversation_files()` → `migrate_conversation_files()` (~line 717)
- `_save_conversation_to_disk(conv, sk)` → `save_conversation_to_disk(conv, sk)` (~line 812)
- `_resolve_session_workspace(conv.project_path, session_key)` → `resolve_session_workspace(...)` (~line 1586)
- `_save_conversation_to_disk(conv, session_key)` → `save_conversation_to_disk(...)` (~lines 2070, 2082)
- `_load_conversation_from_disk(session_key)` → `load_conversation_from_disk(...)` (~line 2087)
- `_conversations_dir()` → `conversations_dir()` (~line 2185)

**Every match must be updated.** After editing, re-run the grep to confirm 0 matches remain.

## Edit 5 — `tests/test_low2_file_sandbox.py`: update 24 references

This file has ~24 references to `_resolve_session_workspace`. Update:
1. The import: `from agent.persistence import resolve_session_workspace`
2. All call sites: `_resolve_session_workspace` → `resolve_session_workspace`

Grep first to confirm the count:
```bash
grep -c "_resolve_session_workspace" tests/test_low2_file_sandbox.py
```

## Edit 6 — `tests/test_conversation.py`: update references

Grep for all 6 function names:
```bash
grep -n "_save_conversation_to_disk\|_load_conversation_from_disk\|_conversations_dir\|_resolve_api_key_for_conversation\|_migrate_conversation_files\|_resolve_session_workspace" tests/test_conversation.py
```

Update the import and all call sites to use `agent.persistence` with non-underscored names.

## Edit 7 — Create `tests/test_agent_persistence.py` (NEW FILE)

Create the tests from spec §3.3. Tests exercise persistence WITHOUT instantiating AgentRuntime:
- TestConversationsDir: test_creates_directory
- TestSaveLoadRoundtrip: test_save_and_load_preserves_messages, test_saved_file_does_not_contain_api_key
- TestResolveSessionWorkspace: test_valid_session_key, test_empty_project_path_raises, test_path_escape_rejected, test_colon_sanitized
- TestMigrateConversationFiles: test_idempotent

---

## Verification

1. `grep -c "def _save_conversation_to_disk\|def _load_conversation_from_disk\|def _conversations_dir\|def _resolve_api_key_for_conversation\|def _migrate_conversation_files\|def _resolve_session_workspace" agent/runtime.py` → **0**
2. `grep -c "from agent.persistence import" agent/runtime.py` → **1**
3. `grep -c "_save_conversation_to_disk\|_load_conversation_from_disk\|_conversations_dir\|_resolve_session_workspace\|_migrate_conversation_files\|_resolve_api_key_for_conversation" agent/runtime.py` → **0** (all call sites updated)
4. `python3 -c "from agent.persistence import save_conversation_to_disk, load_conversation_from_disk, conversations_dir, resolve_session_workspace, migrate_conversation_files; print('OK')"` → OK
5. `python3 -c "from agent.runtime import AgentRuntime; print('OK')"` → OK
6. `python3 -m pytest tests/test_agent_persistence.py -v` → all pass
7. `python3 -m pytest tests/test_low2_file_sandbox.py -q` → all pass (24 refs updated)
8. `python3 -m pytest tests/test_conversation.py -q` → all pass

## COMPLETENESS checklist (mandatory)
```
COMPLETENESS:
- [x/not done] Edit 1: Created agent/persistence.py — evidence: <python import>
- [x/not done] Edit 2: Added import block to runtime.py — evidence: <grep>
- [x/not done] Edit 3: Removed 6 inline functions from runtime.py — evidence: <grep -c = 0>
- [x/not done] Edit 4: Updated all call sites in runtime.py — evidence: <grep -c = 0>
- [x/not done] Edit 5: Updated test_low2_file_sandbox.py — evidence: <grep -c = 0>
- [x/not done] Edit 6: Updated test_conversation.py — evidence: <grep>
- [x/not done] Edit 7: Created test_agent_persistence.py — evidence: <pytest>
- [x/not done] Runtime imports OK — evidence: <python output>
```
