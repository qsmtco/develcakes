# Phase 1 of 1 — Stale delta race fix in agent_runtime_handler.py

**Spec:** `docs/specs/SPEC-STALE-DELTA-RACE-FIX.md` (§2.1)
**Master prompt:** `prompts/steelFramedCodeWriter.md` — invoke it. Read it first.
**Scope:** ONE file: `ui/handlers/agent_runtime_handler.py`. No other files.

## Why (the bug)

Agent messages truncate to the first word. Root cause: a race condition.
`_on_text_delta` and `_on_response_complete` both queue work via
`GLib.idle_add` independently. Completion can execute BEFORE a queued text
delta. The stale delta then starts a NEW streaming bubble showing only that
delta's partial text.

## Task — FOUR edits in `ui/handlers/agent_runtime_handler.py`

Read the file first. Find each edit by its surrounding context. Make ONLY the
change shown.

### Edit 1: Add `_delta_generation` dict to `__init__` (after line ~106)

Find `self._ended_sessions: set[str] = set()` and add AFTER it:

```python
        # RACE-FIX: per-session generation counter. Incremented on each
        # _on_response_complete. Deltas capture the generation at dispatch
        # time; stale deltas (old generation) are dropped in _do_text_delta.
        self._delta_generation: dict[str, int] = {}
```

### Edit 2: Capture generation in `_on_text_delta` (line ~970)

Current:
```python
    def _on_text_delta(self, session_key: str, text: str) -> None:
        """
        AgentRuntime text delta callback.
        → Start or update a streaming bubble in the UI.
        """
        if self._GLib is not None:
            self._GLib.idle_add(self._do_text_delta, session_key, text)
        else:
            self._do_text_delta(session_key, text)
```

Fixed — capture gen and pass it through:
```python
    def _on_text_delta(self, session_key: str, text: str) -> None:
        """
        AgentRuntime text delta callback.
        → Start or update a streaming bubble in the UI.
        """
        gen = self._delta_generation.get(session_key, 0)
        if self._GLib is not None:
            self._GLib.idle_add(self._do_text_delta, session_key, text, gen)
        else:
            self._do_text_delta(session_key, text, gen)
```

### Edit 3: Add `delta_gen` parameter + stale check in `_do_text_delta` (line ~978)

Current signature:
```python
    def _do_text_delta(self, session_key: str, text: str) -> None:
```

Fixed signature (add `delta_gen` param with default 0 for backward compat):
```python
    def _do_text_delta(self, session_key: str, text: str, delta_gen: int = 0) -> None:
```

Then, AFTER the existing `if self._crh is None: return` and `if not text:
return` guards, and BEFORE the `self._streaming_text[...]` accumulation line,
add the stale check:

```python
        # RACE-FIX: If this delta's generation is stale (completion already
        # incremented the counter), drop it. This prevents stale idle callbacks
        # from starting a new streaming bubble after completion has rendered
        # the final bubble.
        current_gen = self._delta_generation.get(session_key, 0)
        if delta_gen < current_gen:
            logger.debug(
                "_do_text_delta: dropping stale delta (gen %d < current %d) for %s",
                delta_gen, current_gen, session_key,
            )
            return
```

### Edit 4: Increment generation in `_on_response_complete` (line ~1397)

Current:
```python
    def _on_response_complete(self, session_key: str, text: str) -> None:
        """
        AgentRuntime response complete callback.
        → End streaming and render the final text bubble.
        """
        if self._GLib is not None:
            self._GLib.idle_add(self._do_response_complete, session_key, text)
        else:
            self._do_response_complete(session_key, text)
```

Fixed — increment generation BEFORE dispatching (so any deltas that arrive
after this point see the new generation):

```python
    def _on_response_complete(self, session_key: str, text: str) -> None:
        """
        AgentRuntime response complete callback.
        → End streaming and render the final text bubble.
        """
        # RACE-FIX: Increment generation so stale _do_text_delta callbacks
        # (queued before completion but not yet executed) know they're outdated.
        self._delta_generation[session_key] = self._delta_generation.get(session_key, 0) + 1
        if self._GLib is not None:
            self._GLib.idle_add(self._do_response_complete, session_key, text)
        else:
            self._do_response_complete(session_key, text)
```

## Rules

- **One file only:** `ui/handlers/agent_runtime_handler.py`.
- **Do NOT change `_do_response_complete`** — it doesn't need the generation.
  The generation is incremented in `_on_response_complete` (the caller),
  BEFORE `_do_response_complete` is queued.
- **Do NOT change `_do_tool_call_start` or any other method.**
- **`delta_gen: int = 0` default is critical** — it ensures backward
  compatibility if any other code path calls `_do_text_delta` without gen.
- **The stale check goes BEFORE text accumulation** (line `self._streaming_text
  = ...`) so stale text isn't re-accumulated after completion already cleared it.

## Verify (run these, paste full output)

1. Compile:
   ```
   python3 -m py_compile ui/handlers/agent_runtime_handler.py && echo COMPILE_OK
   ```

2. `_delta_generation` added:
   ```
   grep -n "_delta_generation" ui/handlers/agent_runtime_handler.py
   ```
   Expected: 5+ matches (init, 2 in _on_text_delta, 1 in _do_text_delta, 1 in _on_response_complete)

3. `_do_text_delta` signature changed:
   ```
   grep -n "def _do_text_delta" ui/handlers/agent_runtime_handler.py
   ```
   Expected: includes `delta_gen: int = 0`

4. Stale check present:
   ```
   grep -n "delta_gen < current_gen" ui/handlers/agent_runtime_handler.py
   ```
   Expected: 1 match

5. Run existing tests (if any GTK-free tests exist):
   ```
   python3 -m pytest tests/ -q -k "agent_runtime or handler" --co 2>&1 | tail -5
   ```
   Report what shows (collection may segfault — environmental).

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] _delta_generation dict added to __init__ — evidence: <grep>
- [x/not done] _on_text_delta captures gen and passes to _do_text_delta — evidence: <grep>
- [x/not done] _do_text_delta signature has delta_gen: int = 0 — evidence: <grep>
- [x/not done] stale check (delta_gen < current_gen) added before accumulation — evidence: <grep>
- [x/not done] _on_response_complete increments generation — evidence: <grep>
- [x/not done] py_compile passes — evidence: COMPILE_OK
```

Please write per the steelFramedCodeWriter prompt.
