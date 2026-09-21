# Phase 1 of 1 — Auxilium KB Per-Turn Cache Fix (HIGH bugs from adversarial audit)

**Supervisor:** Qaster
**Builder:** QTR
**Date:** 2026-06-18
**Trigger:** Adversarial audit of the KB synthesis per-turn cache change (`agent/runtime.py:1183-1230` and `tests/test_auxilium_tier2.py:127-166`) found one HIGH-severity bug and one HIGH-severity related symptom in the cache invariant. The change claims "kb_lookup only runs once per _run_loop invocation" but only delivers on that claim for successful lookups.

**Severity:** HIGH (2 bugs, same root cause)

**Root cause:** The cache populate block in `_prepare_kb_synthesis` is gated on `if chunks:` (only sets `new_cache` on non-empty results), but the cache check is gated on `if new_cache is None:`. Asymmetric. If `kb_lookup` returns `[]` or raises (swallowed by `except Exception: pass`), `new_cache` stays `None` and the next iteration re-fires `kb_lookup`. This means:
- A user message with no KB coverage: kb_lookup called once per tool-loop iteration (was the original problem the change was meant to solve)
- A failing KB backend: gets hammered N times for one user message (DoS amplification)

**Verified by adversarial probes:**
- `agent_role="helper"`, `kb_lookup` always returns `[]`, LLM makes 1 tool call → `kb_call_count=2` (should be 1)
- `agent_role="helper"`, `kb_lookup` raises `RuntimeError`, LLM makes 1 tool call → `kb_call_count=2` (should be 1)

---

## Files to change

1. **`agent/runtime.py`** — lines 1214-1223 (the cache populate block in `_prepare_kb_synthesis`)
2. **`agent/runtime.py`** — lines 1202-1205 (the helper docstring)
3. **`tests/test_auxilium_tier2.py`** — add 2 new test cases after `test_kb_lookup_called_once_per_run_loop_invocation`

---

## Edit 1: Fix the cache populate in `_prepare_kb_synthesis` (FIX 1)

**File:** `agent/runtime.py`
**Location:** lines 1214-1223 (the block beginning with `new_cache = kb_cache`)

**Current code:**
```python
        # Per-turn cache: only fetch on first call within a turn
        new_cache = kb_cache
        if new_cache is None:
            try:
                from agent.kb_lookup import kb_lookup
                chunks = kb_lookup(text, top_k=5, min_score=0.35)
                if chunks:
                    new_cache = _format_chunks_for_llm(chunks)
            except Exception:
                pass  # kb_lookup is fail-soft
```

**Replace with:**
```python
        # Per-turn cache: only fetch on first call within a turn.
        # After the first call, new_cache is ALWAYS set (to the formatted
        # string for matches, or to "" for no-results / exception). The
        # empty-string sentinel is what makes this an actual cache invariant
        # rather than "sometimes a cache when KB has something to say."
        new_cache = kb_cache
        if new_cache is None:
            try:
                from agent.kb_lookup import kb_lookup
                chunks = kb_lookup(text, top_k=5, min_score=0.35)
                new_cache = _format_chunks_for_llm(chunks)
            except Exception:
                new_cache = ""  # queried, but failed; do not retry
```

**Why this works:** After the first call within a turn, `new_cache` is always a string (possibly empty). The next iteration's `if new_cache is None:` check is False, so `kb_lookup` is not re-invoked. The downstream `if kb_context:` gate at line 1226 is still correct because empty string is falsy, so `messages_for_call = messages` (no injection) — behavior is identical to the previous "no results" path. The `except Exception` branch now sets `new_cache = ""` instead of leaving it None, which prevents the retry storm on a failing backend.

**Verification command:**
```bash
grep -n "new_cache = " agent/runtime.py
```
Expect 2 matches: the assignment at the top of the block and the assignments inside the try/except. The old `if chunks:` line must be gone.

**Verification command (removal check):**
```bash
grep -n "if chunks:" agent/runtime.py
```
Expect 0 matches inside `_prepare_kb_synthesis`. (May still appear in other functions; that's fine.)

---

## Edit 2: Update the helper docstring (FIX 3)

**File:** `agent/runtime.py`
**Location:** lines 1190-1205 (the docstring of `_prepare_kb_synthesis`)

**Current docstring (last paragraph):**
```
        Called once per tool-loop iteration, but kb_lookup itself only
        runs once per _run_loop invocation (gated by the cache).
```

**Replace with:**
```
        Called once per tool-loop iteration. kb_lookup is invoked at most
        once per _run_loop invocation: the per-turn cache (passed in via
        kb_cache, returned via the new_cache element of the tuple) is set
        to a non-None value on the first call — the formatted string for
        matches, or the empty string for no-results or exceptions. The
        empty-string sentinel is what makes the cache an actual invariant
        (rather than "cached only on success"); it prevents re-querying a
        failing backend on every iteration and prevents re-querying for
        off-topic user messages that have no KB coverage.
```

**Why:** The previous wording claimed an invariant the code did not actually deliver. After Edit 1, the invariant is real, and the docstring should match.

---

## Edit 3: Add 2 regression tests (FIX 2)

**File:** `tests/test_auxilium_tier2.py`
**Location:** Insert both new test methods inside `class TestKBLookupFiresForAuxilium`, immediately AFTER the existing `test_kb_lookup_called_once_per_run_loop_invocation` method (which ends at line 166). Insert BEFORE `test_kb_lookup_called_for_case_insensitive_helper_role` (which starts at line 168).

**Test 1: cache engages when kb_lookup returns no chunks**

```python
    def test_kb_lookup_cached_when_returns_empty_chunks(self):
        """Regression: HIGH bug from 2026-06-18 adversarial audit.

        The per-turn cache must engage even when kb_lookup returns [].
        Previously, new_cache stayed None when chunks was empty, causing
        kb_lookup to be re-invoked on every tool-loop iteration — the
        exact problem the cache was meant to solve.

        This test verifies that an off-topic user message (KB has no
        coverage) does NOT trigger repeated kb_lookup calls during a
        tool-loop.
        """
        rt, sk = _make_runtime(agent_role="helper")

        call_count = [0]
        llm_call_count = [0]

        def empty_lookup(question, *, top_k, min_score):
            """Always returns [] — simulates a KB index with no matches."""
            call_count[0] += 1
            return []

        def fake_call(sk, messages, tools):
            llm_call_count[0] += 1
            if llm_call_count[0] == 1:
                # First call: trigger the tool loop with a tool_calls response
                return {
                    "choices": [{"message": {
                        "content": "",
                        "tool_calls": [{"id": "t1", "type": "function",
                                        "function": {"name": "read_file", "arguments": "{}"}}],
                    }}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            # Second call: normal answer
            return {
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

        with patch("agent.kb_lookup.kb_lookup", side_effect=empty_lookup):
            with patch.object(rt, "_call_llm", side_effect=fake_call):
                rt._run_loop(sk, "an obscure question with no KB matches")

        # Tool loop fired (2 LLM calls), but kb_lookup was called only once
        # (gated by the cache; the empty result sets new_cache="" and
        # prevents re-querying on iter 2).
        assert llm_call_count[0] >= 2, f"expected >= 2 LLM calls, got {llm_call_count[0]}"
        assert call_count[0] == 1, (
            f"expected 1 kb_lookup call (cache should engage even on empty result), "
            f"got {call_count[0]}"
        )
```

**Test 2: cache engages when kb_lookup raises**

```python
    def test_kb_lookup_cached_when_raises(self):
        """Regression: HIGH bug from 2026-06-18 adversarial audit.

        The per-turn cache must engage even when kb_lookup raises an
        exception. Previously, the except clause left new_cache as None,
        causing kb_lookup to be re-invoked on every tool-loop iteration —
        hammering a failing backend N times for one user message.
        """
        rt, sk = _make_runtime(agent_role="helper")

        call_count = [0]
        llm_call_count = [0]

        def raising_lookup(question, *, top_k, min_score):
            call_count[0] += 1
            raise RuntimeError("simulated KB backend down")

        def fake_call(sk, messages, tools):
            llm_call_count[0] += 1
            if llm_call_count[0] == 1:
                return {
                    "choices": [{"message": {
                        "content": "",
                        "tool_calls": [{"id": "t1", "type": "function",
                                        "function": {"name": "read_file", "arguments": "{}"}}],
                    }}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            return {
                "choices": [{"message": {"content": "answer"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

        with patch("agent.kb_lookup.kb_lookup", side_effect=raising_lookup):
            with patch.object(rt, "_call_llm", side_effect=fake_call):
                rt._run_loop(sk, "any question")

        # kb_lookup was called only once even though it raised. A failing
        # backend must not be retried on every iteration.
        assert llm_call_count[0] >= 2, f"expected >= 2 LLM calls, got {llm_call_count[0]}"
        assert call_count[0] == 1, (
            f"expected 1 kb_lookup call (exception should not bypass cache), "
            f"got {call_count[0]}"
        )
```

**Why both tests:** They pin down the two branches of the corrected cache populate (`try` returning `[]`, `except` raising). Without these tests, a future refactor could re-introduce the asymmetric gating and the existing test would not catch it (because the existing test uses a non-empty return, which the old broken code handled correctly).

---

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md` for every edit
- Read both files in full before editing (`agent/runtime.py` is 1941 lines, `tests/test_auxilium_tier2.py` is 504 lines)
- Do NOT change anything else in either file. Scope is exactly 3 edits.
- Do NOT touch `_inject_kb_context`, `_format_chunks_for_llm`, or any other helper. The fix is local to `_prepare_kb_synthesis`.
- Do NOT change the existing `test_kb_lookup_called_once_per_run_loop_invocation` test. Add 2 new tests alongside it.
- Do NOT silently expand scope. If you find a related bug while reading, note it in the COMPLETENESS checklist under "related issues" and stop. The supervisor decides what to do.

## Verification commands to run

After all 3 edits:

1. **Run the new test file:**
   ```bash
   cd /path/to/projects/crabcakes && python3 -m pytest tests/test_auxilium_tier2.py -v
   ```
   Expect: **17 passed** (15 existing + 2 new). The 2 new tests must show as `test_kb_lookup_cached_when_returns_empty_chunks` and `test_kb_lookup_cached_when_raises`.

2. **Run the full test suite:**
   ```bash
   cd /path/to/projects/crabcakes && python3 -m pytest -x -q
   ```
   Expect: no new failures. Pre-existing failures (if any) must be attributed correctly.

3. **Removal check (Edit 1 verification):**
   ```bash
   cd /path/to/projects/crabcakes && grep -n "if chunks:" agent/runtime.py
   ```
   Expect: 0 matches inside `_prepare_kb_synthesis`. Other functions may still use `if chunks:`; that's expected and fine.

4. **Docstring check (Edit 2 verification):**
   ```bash
   cd /path/to/projects/crabcakes && grep -A 6 "kb_lookup is invoked at most" agent/runtime.py
   ```
   Expect: 6 lines of docstring text matching the new wording.

5. **New test count check (Edit 3 verification):**
   ```bash
   cd /path/to/projects/crabcakes && python3 -m pytest tests/test_auxilium_tier2.py --collect-only -q | grep "test_kb_lookup_cached"
   ```
   Expect: 2 matches (the 2 new test methods).

## Report

When done, send back a completion report with:
- Files changed with line numbers
- Full pytest output for the new test file
- Full pytest output for the full suite
- Output of the 4 verification commands
- COMPLETENESS checklist (see template below)
- Any related issues found (flagged, not fixed)

```
COMPLETENESS:
- [x] Edit 1: fixed _prepare_kb_synthesis cache populate — line N in agent/runtime.py, evidence: <grep output>
- [x] Edit 2: updated helper docstring — line N in agent/runtime.py, evidence: <grep output>
- [x] Edit 3: added 2 new tests — line N in tests/test_auxilium_tier2.py, evidence: <pytest output>
- [x] Verification 1: 17 tests pass in test_auxilium_tier2.py — evidence: <output>
- [x] Verification 2: full test suite no new failures — evidence: <output>
- [x] Verification 3: 0 matches for "if chunks:" in _prepare_kb_synthesis — evidence: <output>
- [x] Verification 4: docstring matches new wording — evidence: <output>
- [x] Verification 5: 2 new test methods collected — evidence: <output>
- [x] Related-bug scan: <list any related issues found, or "none">
```

Do not skip the COMPLETENESS checklist. The supervisor will send the work back if it is missing.
