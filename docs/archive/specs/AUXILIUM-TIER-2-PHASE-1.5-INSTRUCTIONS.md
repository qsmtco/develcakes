# Phase T2-1.5 — Fix persistence round-trip for dropped `Conversation` fields

**Spec:** `/path/to/projects/crabcakes/docs/specs/SPEC-auxilium-tier-2.md` §7 (edge cases)
**Related bug found in:** Phase T2-1 audit
**Target:** main
**Risk:** Medium (touches persistence — could cause data corruption if load keys are wrong)
**Lines:** +10 (5 keys in save, 5 reads in load)

## Why this phase exists

Phase T2-1 audit caught a pre-existing bug: `_save_conversation_to_disk` (line 758) and `_load_conversation_from_disk` (line 798) in `agent/runtime.py` do not persist 5 fields of the `Conversation` dataclass:

- `agent_role` (the new field — blocks Tier 2 feature across restarts)
- `mcp_servers` (causes MCP servers to not reconnect after restart)
- `si_enforcement` (per-agent enforcement override lost)
- `fallback_provider` (per-agent fallback provider lost)
- `fallback_model` (per-agent fallback model lost)

For the Auxilium Tier 2 feature, this means a saved conversation loses `agent_role="helper"` after a restart, and the user's `kb_lookup` no longer fires — silent regression of the feature we just shipped. The other 4 fields have similar silent-regression issues.

All 5 fields have safe defaults so there is no crash, but the behavior degrades after restart. This is the same class of bug across all 5 fields, so we fix them together with one targeted edit pair.

## Files to change

1. `agent/runtime.py` — extend the save dict and the load constructor

## Edit 1: `_save_conversation_to_disk` (line 758)

The `data = { ... }` dict at lines 762-792 ends with `"app_title": conv.app_title,` and `"created_at": ...`. **Add 5 new keys before `"app_title"`** (group them together — they are all per-agent overrides). New keys to add, in this order:

```
        "mcp_servers": list(conv.mcp_servers) if conv.mcp_servers else [],
        "si_enforcement": conv.si_enforcement,
        "agent_role": conv.agent_role,
        "fallback_provider": conv.fallback_provider,
        "fallback_model": conv.fallback_model,
```

Notes on serialization:
- `mcp_servers` is a `list[str]`. Cast to `list()` defensively (the field is `field(default_factory=list)` so the type is always iterable, but be explicit).
- `si_enforcement` is `bool | None`. JSON serializes `None` as `null` and `bool` as `bool` — no transformation needed.
- `agent_role` is `str`. Default is `""`. No transformation.
- `fallback_provider` and `fallback_model` are `str | None`. JSON handles `None` as `null`.

## Edit 2: `_load_conversation_from_disk` (line 798)

The `Conversation(...)` constructor call at lines 838-848 has 10 kwargs. **Add 5 new kwargs after `app_title=data.get("app_title", "")`** so the parameter order matches the dataclass field order. New kwargs to add:

```
        mcp_servers=data.get("mcp_servers", []),
        si_enforcement=data.get("si_enforcement"),
        agent_role=data.get("agent_role", ""),
        fallback_provider=data.get("fallback_provider"),
        fallback_model=data.get("fallback_model"),
```

Notes on deserialization:
- `data.get("mcp_servers", [])` — list, JSON round-trips fine
- `data.get("si_enforcement")` — `None` (key missing) is the safe default; matches the field's default of `None`
- `data.get("agent_role", "")` — empty string is the safe default; matches the field's default
- `data.get("fallback_provider")` — `None` is the safe default
- `data.get("fallback_model")` — `None` is the safe default

**Important:** The key order in the save dict and the kwarg order in the load constructor must match. If a key is renamed, both sides must change together. Use the names above.

## Rules

- Use `prompts/steelFramedCodeWriter.md` as the active prompt.
- Use identifiers as anchors, not line numbers.
- Do not reformat adjacent code.
- Do not "improve" comments.
- Do not reorder existing keys/kwargs.
- **Do NOT change the `created_at` field** — it is a derived field (set at dataclass construction time) and does not need to round-trip from JSON.
- **Do NOT add a `messages` migration** — the `messages` field is already handled.
- **Do NOT touch any other field's save/load** — only the 5 listed.

## Verification (run yourself, paste output in report)

1. Round-trip test — create, save, load, verify all 5 fields survive:
   ```
   python3 -c "
   from agent.config import AgentConfig
   from agent.runtime import AgentRuntime, _save_conversation_to_disk, _load_conversation_from_disk
   import os, tempfile, json
   tmpdir = tempfile.mkdtemp()
   os.environ['CRABCAKES_CONVERSATIONS_DIR'] = tmpdir
   cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
   rt = AgentRuntime(cfg)
   rt.start()
   rt.create_conversation(
       session_key='rt-test',
       agent_name='Auxilium',
       agent_role='helper',
       mcp_servers=['server-a', 'server-b'],
       si_enforcement=True,
       fallback_provider='openai',
       fallback_model='gpt-4o-mini',
   )
   conv = rt.get_conversation('rt-test')
   _save_conversation_to_disk(conv, 'rt-test')
   loaded, _ = _load_conversation_from_disk('rt-test')
   assert loaded.agent_role == 'helper', f'agent_role: {loaded.agent_role!r}'
   assert loaded.mcp_servers == ['server-a', 'server-b'], f'mcp_servers: {loaded.mcp_servers!r}'
   assert loaded.si_enforcement is True, f'si_enforcement: {loaded.si_enforcement!r}'
   assert loaded.fallback_provider == 'openai', f'fallback_provider: {loaded.fallback_provider!r}'
   assert loaded.fallback_model == 'gpt-4o-mini', f'fallback_model: {loaded.fallback_model!r}'
   print('round-trip OK: all 5 fields preserved')
   "
   ```
   Expected: `round-trip OK: all 5 fields preserved`. If any `assert` fails, the round-trip is broken — debug before reporting done.

2. Backward compatibility — load an OLD save (no agent_role key) without crashing:
   ```
   python3 -c "
   from agent.runtime import _load_conversation_from_disk
   import os, tempfile, json
   tmpdir = tempfile.mkdtemp()
   old_path = os.path.join(tmpdir, 'old.json')
   with open(old_path, 'w') as f:
       json.dump({
           'agent_name': 'OldAgent',
           'project_path': None,
           'model': '',
           'messages': [],
           'system_prompt': '',
           'total_tokens': 0,
           'total_cost': 0.0,
           'step_count': 0,
       }, f)
   # Patch _conversations_dir to use tmpdir
   import agent.runtime as r
   r._conversations_dir = lambda: tmpdir
   conv, _ = _load_conversation_from_disk('old')
   assert conv.agent_role == '', f'agent_role default: {conv.agent_role!r}'
   assert conv.mcp_servers == [], f'mcp_servers default: {conv.mcp_servers!r}'
   assert conv.si_enforcement is None, f'si_enforcement default: {conv.si_enforcement!r}'
   assert conv.fallback_provider is None, f'fallback_provider default: {conv.fallback_provider!r}'
   assert conv.fallback_model is None, f'fallback_model default: {conv.fallback_model!r}'
   print('backward-compat OK: all 5 fields default safely')
   "
   ```
   Expected: `backward-compat OK: all 5 fields default safely`. If this fails, an old save will crash on load.

3. JSON contents — confirm the save file contains all 5 keys:
   ```
   python3 -c "
   from agent.config import AgentConfig
   from agent.runtime import AgentRuntime, _save_conversation_to_disk
   import os, tempfile, json
   tmpdir = tempfile.mkdtemp()
   cfg = AgentConfig(providers={}, default_provider='openai', default_model='openai/gpt-4o')
   rt = AgentRuntime(cfg)
   rt.start()
   rt.create_conversation(
       session_key='json-check',
       agent_name='Auxilium',
       agent_role='helper',
       mcp_servers=['s1'],
       si_enforcement=False,
       fallback_provider='openai',
       fallback_model='gpt-4o',
   )
   conv = rt.get_conversation('json-check')
   _save_conversation_to_disk(conv, 'json-check')
   with open(os.path.join(tmpdir, 'json-check.json')) as f:
       data = json.load(f)
   for k in ('mcp_servers', 'si_enforcement', 'agent_role', 'fallback_provider', 'fallback_model'):
       assert k in data, f'missing key: {k}'
   print('json OK: all 5 keys present in save file')
   "
   ```
   Expected: `json OK: all 5 keys present in save file`. If a key is missing, the save dict was not updated.

4. Test suite:
   ```
   python3 -m pytest tests/ -q --tb=short --ignore=tests/test_agent_runtime.py --ignore=tests/test_kb_lookup.py 2>&1 | tail -10
   ```
   Must show no NEW failures. The skipped test is pre-existing.

## Deliverable

- Both edits applied
- All four verification commands run by you, output pasted in the report
- A `**COMPLETENESS:**` block listing each edit with evidence
- A related-bug scan: if you find other save/load fields that should round-trip, flag them — do NOT silently fix.

## Word marker

Include the word "please write" in your opening reply so the channel knows this delegation is canonical.

## COMPLETENESS template

End your reply with:

```
**COMPLETENESS:**
- [x] Edit 1: added 5 keys to _save_conversation_to_disk data dict — line N in agent/runtime.py, evidence: V3 output
- [x] Edit 2: added 5 kwargs to _load_conversation_from_disk Conversation() call — line N in agent/runtime.py, evidence: V1 output
- [x] Verification 1: round-trip preserves all 5 fields — <paste output>
- [x] Verification 2: backward-compat with old save (no agent_role key) — <paste output>
- [x] Verification 3: json save file contains all 5 keys — <paste output>
- [x] Verification 4: full test suite — <paste last 10 lines>
- [x] Related-bug scan: <list of any related issues found, or "none">
```

A reply missing the `**COMPLETENESS:**` block is incomplete and will be sent back.
