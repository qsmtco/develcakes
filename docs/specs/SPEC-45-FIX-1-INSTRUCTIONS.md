# SPEC-45 Fix Round 1 — Debugger audit findings (FIX-FIRST)

**Base:** uncommitted tree at HEAD `985666f` (post-audit, diff SHA `75fbd4d3`).
**Source:** Debugger adversarial audit, verdict FIX-FIRST, 7 findings.
**Rules:** Use `prompts/steelFramedCodeWriter.md`. Edits via `edit_file`/
`write_file` only — never exec heredocs. You do NOT run pytest (PM ruling);
`py_compile` + `ruff` only. Supervisor runs the tests.

Fix BUG #1, #2, #3 (ship-blockers), then #4 and #6 (safe corrections).
BUG #5 is optional. BUG #7 is escalated to the PM — do NOT touch
`tests/test_spec24_facts.py`.

---

## FIX 1 (BUG #1, CRITICAL) — feed_card unreachable on existing installs

`utils/agent_defs.py:169`:

```python
for tool in ("diary", "project_memory", "jev_ask", "canvas", "talk", "agent_context"):
```

Add `"feed_card"` to the tuple (wrap lines if over length). This is the
migration path that pushes built-in tools into existing installs;
`_seed_defaults()` only copies YAMLs when absent, so without this the tool
never reaches any existing install (two gates then block it: API schema
filter at `agent/runtime.py:1973` and the allowed_tools gate at
`agent/tools.py:2440`).

**Regression test (required — the audit proved no test can catch this today):**
extend `tests/test_builtin_tool_migration.py` with a test that simulates a
pre-SPEC-45 install (existing YAMLs WITHOUT feed_card in tools), runs the
migration (`_seed_defaults()` / `ensure_builtin_tool`), and asserts `feed_card`
ends up in all three agents' tool lists. Second assertion: the tuple at the
migration site contains feed_card — a direct pin so removal goes RED.

## FIX 2 (BUG #2, HIGH) — GTK on the tool-loop thread + three false claims

`ui/handlers/feed_handler.py`, `add_card`:

- Line `:448` — `self._update_batch_bar_for_active_project(card_data.project_name)`
  runs on the calling thread BEFORE the `idle_add`. Move the call INSIDE the
  `_append_html` closure (which already hops to main via
  `self._GLib.idle_add(_append_html)` at `:449`), as its last step.
- Line `:461` (no-feed-document path) — wrap the same call in
  `self._GLib.idle_add(...)` so it also hops to main.

`update_batch_bar` touches `Gtk.Button.set_label/set_visible/get_label` —
GTK4 is not thread-safe; `feed_card` is the first off-main-thread caller.

**Correct the three false claims:**
1. `ui/window.py:1347-1350` — docstring says add_card "is thread-safe by
   construction ... defers its own GTK work". Rewrite to state the truth
   post-fix: add_card defers ALL GTK work (batch-bar update included) to
   `GLib.idle_add`.
2. `ui/handlers/feed_handler.py:376` — "Thread-safe: GTK operations via
   `GLib.idle_add()`" — after the fix this becomes TRUE; verify and keep, or
   sharpen to name the batch-bar deferral explicitly.
3. `docs/ARCHITECTURE.md:3238-3240` — "no GTK work happens in it" claim;
   after the fix it is true — verify wording matches the shipped code.

**Test:** extend `tests/test_feed_handler.py` (or the batch-bar tests) with a
worker-thread `add_card` asserting `update_batch_bar` executes on the main
thread only (fake GLib capturing call site, per the audit's probe technique).

## FIX 3 (BUG #3, MEDIUM) — guard and callback read different project sources

Guard at `ui/agent_runtime/turn_ui.py:211` reads `self._active_project`;
callback at `ui/window.py:1364` reads
`self._project_handler.get_active_project_name()`. When they disagree
(runtime has a project, project handler does not) the feed_card call FAILS
with "feed_card needs an open project" while its agent_action card was
already suppressed — an invisible failure.

**Fix direction:** `_post_feed_card` falls back to the agent runtime
handler's active project when the project handler reports none:

```python
project_name = self._project_handler.get_active_project_name()
if not project_name:
    arh = self._agent_runtime_handler  # however window holds it
    if arh is not None and getattr(arh, "_active_project", None):
        project_name = arh._active_project[0]
if not project_name:
    return ToolResult(success=False, error="feed_card needs an open project")
```

Result: whenever the guard suppressed the agent_action card (because
`_active_project` is set), the callback now also finds a project and posts.
When BOTH are None, no feed cards exist at all — uniform, no divergence.
Do NOT unify project_name vs tab_key — that divergence is a design pin
(SPEC-12 R7); this fix is only about the project EXISTENCE check.

**Test:** in `tests/test_feed_card_tool.py` stub tests — add one driving
`_post_feed_card` with `get_active_project_name() → None` but the runtime
handler's `_active_project` set: expect success, card posted with the runtime
project's name.

## FIX 4 (BUG #4, MEDIUM, docs only) — add_cards_batch is dead product code

The spec's "other producers still use it" claim is FALSE — after SPEC-45's
deletions, `add_cards_batch` has zero product callers (only tests). Do NOT
delete the method (out of scope; PM may decide separately). Correct:

- `ui/handlers/feed_handler.py:477` — comment claims a batch producer
  exists. Rewrite: kept for API compatibility / batch inserts from tests;
  no product caller since SPEC-45.
- `docs/ARCHITECTURE.md:3022`, `:3024`, `:4031` — still describe
  `add_cards_batch` as a live ordering/seq/persist path. Mark as retained-
  but-currently-unused (or note "no product callers post-SPEC-45").

## FIX 6 (BUG #6, MEDIUM, test hardening) — mutation-resistant forward test

`tests/test_feed_card_tool.py:208-218`
(`test_public_path_minimum_arguments`) asserts `additions is None` /
`deletions is None` — indistinguishable from a dropped forward (passes
against the SPEC-44-style mutant). Harden:

- Keep the value-forward proof in
  `test_public_path_forwards_every_parameter` (it catches the mutant).
- In the minimum test, replace the two `is None` assertions with an
  exact-key-set check: callback received exactly the 7 field keys plus
  `session_key`, and the omitted params default correctly.
- Add one registration-shape pin:
  `inspect.signature(agent.tools._TOOLS["feed_card"][1])` first seven
  params are `["type","title","body","file","additions","deletions","task_id"]`.

## FIX 5 (BUG #5, LOW, optional) — path filter first-segment-only colon check

`agent/group_tools.py:406-413`: `":" in candidate.split("/", 1)[0]` misses
`a/C:/x.py`. If you touch it: reject any `":"` in the candidate, and update
the escaping-path rejection tests to add `a/C:/x.py`. Skip if risky — LOW.

---

## Verification (you run — NOT pytest)

```bash
.venv/bin/python -m py_compile utils/agent_defs.py ui/handlers/feed_handler.py \
  ui/window.py ui/agent_runtime/turn_ui.py tests/test_feed_card_tool.py \
  tests/test_builtin_tool_migration.py tests/test_feed_handler.py
.venv/bin/ruff check utils/agent_defs.py ui/handlers/feed_handler.py \
  ui/window.py ui/agent_runtime/turn_ui.py tests/test_feed_card_tool.py \
  tests/test_builtin_tool_migration.py tests/test_feed_handler.py
```

## Report back

Per-fix: what changed, file:line, and the COMPLETENESS checklist with
evidence. Include the marker phrase "please proceed".
