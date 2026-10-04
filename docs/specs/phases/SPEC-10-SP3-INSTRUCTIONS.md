# SPEC-10 SP3 — ReviewBar queue view + batch buttons + window wiring + feed-card bridge

**Spec:** `docs/specs/SPEC-10-REVIEW-QUEUES.md` §2 (review_bar), §6 AC#2, D4/D4b/D5
**Pre-flight (REV 2+, binding):** `docs/specs/phases/SPEC-10-PREFLIGHT-DECISIONS.md`
— D4, D4b, D5, D8, D9
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** `ui/views/review_bar.py` (queue view region) +
`ui/handlers/review_handler.py` (bar refresh wiring) + `ui/window.py`
(confirmation dialog) + `models/feed_card.py` metadata (D5 bridge) +
tests. **Sub-phase if needed** — see §5.

---

## 1. ReviewBar queue view (`ui/views/review_bar.py`)

Additive region — **the bar's existing states must not regress** (D4b):
`set_state_idle/reviewing/has_changes` unchanged; the queue region is a
separate `Gtk.Box` appended AFTER `_buttons_box`, visibility tied to
non-empty queues.

### 1a. Constructor additions

```python
        # SPEC-10 D4: per-agent queue view (additive region)
        self._queue_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._queue_box.set_visible(False)
        self._queue_box.add_css_class("review-bar-queues")
        # separator label before the region
        self._queue_sep = Gtk.Label(label="│")
        self._queue_sep.set_visible(False)
        self.append(self._queue_sep)
        self.append(self._queue_box)
        self._on_queue_accept_agent = None   # cb(agent_key)
        self._on_queue_accept_all = None     # cb()
        self._queue_agents: dict[str, Gtk.Widget] = {}  # agent_key -> per-agent row
```

### 1b. `set_queue_view(self, agents: list[tuple[str, int]]) -> None`

`agents` = `[(agent_key, pending_count), ...]` in first-enqueue order
(ReviewHandler supplies via `agents_with_pending` + `pending_count`).
Behavior:
- Empty list → hide region (`_queue_box` + `_queue_sep` invisible).
- Else rebuild the region: one chip per agent — a `Gtk.Button` labeled
  `f"{agent_key} ({n})"` with a subtle per-agent style class; clicking a
  chip = SELECT that agent's queue (highlight; Check Changes stays the
  session's — selection only affects which agent's entries the Accept
  targets — see 1c).
- Plus two batch buttons at the end: `Accept All (agent)` and
  `Accept All (everyone)` (agents only — D8: "pm" chips render read-only
  with a tooltip "PM queue — drained by your own /accept"; the batch
  buttons never target "pm").
- Re-entrancy: `set_queue_view` may be called from idle callbacks — clear
  children before rebuild (GTK4: `while box.get_first_child(): remove`).

### 1c. Callbacks (injected, pure view)

```python
    def set_queue_callbacks(self, on_accept_agent, on_accept_all) -> None:
        """SPEC-10 D4: batch accept wiring. on_accept_agent(agent_key) —
        accept one agent's queue; on_accept_all() — all agent queues."""
```

`Accept All (agent)` → `self._on_queue_accept_agent(selected_or_only_agent)`.
`Accept All (everyone)` → `self._on_queue_accept_all()`.

### 1d. Confirmation (D4: show the N)

The bar does NOT open dialogs itself (pure view). The window-level
callback opens the confirm (see §3). The bar's buttons call the injected
callbacks; the HANDLER side (§2) routes through window's confirm helper.

Actually — simpler and consistent with the codebase: **the bar fires the
callbacks; ReviewHandler intercepts nothing; window.py's wiring wraps the
callbacks with the confirm dialog** (same lazy-resolution pattern as
Stop All, window.py:1025). Buttons stay enabled during the dialog (modal
blocks anyway).

## 2. ReviewHandler bar-refresh wiring

### 2a. `_refresh_queue_view(project_name)` — after every queue mutation

Call sites: `enqueue_agent_checkpoint` (post-enqueue),
`_enqueue`'s cap path, `_dequeue`, `_drain_pm_queue`, and the accept
paths' summary-card emission. Implementation: gather
`[(k, pending_count(project, k)) for k in agents_with_pending(project)]`
on the mutation thread, then `idle_add(lambda: bar.set_queue_view(agents))`
— resolve `bar` via `self._mc.get_review_bar()`; None → skip (no bar yet).

### 2b. Wire the callbacks where the bar is created (`_show_review_bar`)

```python
        bar.set_queue_callbacks(
            on_accept_agent=lambda ak: self._confirm_and_accept_agent(project_name, ak),
            on_accept_all=lambda: self._confirm_and_accept_all(project_name),
        )
```

### 2c. Confirm helpers on ReviewHandler (dialog via window — but handlers
must not import GTK windows)

The confirm lives in **window.py** (§3); ReviewHandler exposes the direct
actions and the window wraps them. In `_show_review_bar` the wiring (2b)
calls window-provided confirmers injected at construction? — NO: keep the
existing pattern — ReviewHandler already has `_on_display_text` etc. from
window. Add an OPTIONAL constructor param:
`on_confirm_batch_accept=None` — `Callable[[str, int, Callable[[], None]],
None]` — `(project_name, n_items, on_confirm_callback)`. When unset (tests),
accept proceeds WITHOUT confirmation (disclose in docstring). window.py
passes its dialog helper.

```python
    def _confirm_and_accept_agent(self, project_name, agent_key):
        n = self.pending_count(project_name, agent_key)
        if self._on_confirm_batch_accept is not None:
            self._on_confirm_batch_accept(project_name, n, lambda: self.accept_agent_queue(agent_key, project_name))
        else:
            self.accept_agent_queue(agent_key, project_name)

    def _confirm_and_accept_all(self, project_name):
        n = sum(self.pending_count(project_name, k) for k in agents… ≠ pm)
        ...same shape...
```

## 3. window.py — the confirm dialog

Add `_confirm_batch_accept(project_name, n, on_confirm)` mirroring the
Stop-All dialog (window.py:1025 pattern: one-shot `_dispatched` guard,
WARNING type, explicit N):

- Agent: text `Accept N checkpoint(s) for <agent_key>?` secondary:
  worktree items are marked reviewed (no new commit); root items commit
  with the agent trailer. Cannot be undone by one click.
- Everyone: `Accept N pending agent checkpoint(s) (all agents)?` + the
  double-click note from Coder's round-2 disclosure: "A rapid double-click
  can span an enqueue boundary — a checkpoint arriving mid-batch is
  accepted in the next batch."

Wire in `_build`'s ReviewHandler construction:
`on_confirm_batch_accept=self._confirm_batch_accept`.

## 4. D5 bridge — feed-card metadata on queue emissions

`_emit_feed_card` for queue events: extend the card dicts with
`metadata["agent"] = agent_key`:
- The SP2 summary/stale/cap cards already emit — thread `agent_key` into
  their dicts' metadata (FeedCardData has `metadata: dict` —
  `_emit_feed_card` currently constructs FeedCardData WITHOUT metadata;
  extend `_emit_feed_card` to accept and pass `metadata=`).
- Checkpoint-creation cards: ARH's enqueue is silent today (D8c). Do NOT
  add agent-chat cards. But the PM-facing feed SHOULD see a checkpoint
  card: in `enqueue_agent_checkpoint` (ReviewHandler, post-validation),
  emit a feed card `title=f"Checkpoint queued: {agent_key}"`,
  `body=f"{sha[:7]} in {path}"`, `metadata={"agent": agent_key}`. This is
  the D5 "feed filter-by-agent comes free" surface.

## 5. Sub-phase discipline

If the build feels like >3 concurrent edits in review_bar + handler +
window, STOP and split: 5a bar view (pure widget + tests) → 5b handler
refresh + callbacks → 5c window confirm + wiring → 5d D5 metadata. Each
sub-phase lands with its tests green before the next begins. Disclose
which sub-phases you ran.

## 6. Tests

New file `tests/test_review_bar_queues.py` (GTK-free where possible —
the bar imports Gtk; follow `tests/test_window_stop_all_dialog.py`'s
approach for widget tests under xvfb; handler-side tests go in
`tests/test_review_queues.py`):

**View (xvfb):**
1. `test_set_queue_view_renders_chips` — 2 agents → 2 chips + 2 batch
   buttons visible; counts in labels.
2. `test_set_queue_view_empty_hides` — `[]` → region hidden.
3. `test_queue_callbacks_fire` — click chip → select; click Accept All
   (agent) → callback with right key.
4. `test_existing_states_unregressed` — idle/reviewing/has_changes calls
   still set the right visibility (D4b pin).

**Handler wiring (MockGLib):**
5. `test_enqueue_refreshes_bar` — enqueue → `bar.set_queue_view` called
   with the right list.
6. `test_confirm_path_without_window` — `_on_confirm_batch_accept=None` →
   accept runs directly (the tests' path).
7. `test_confirm_path_with_window` — injected confirmer receives
   (project, N, cb); accept runs only after cb().
8. `test_pm_chip_readonly` — pm entries render; batch buttons never
   target pm (callback-level assert).
9. `test_checkpoint_card_metadata_agent` — enqueue_agent_checkpoint
   emits a feed card with `metadata["agent"]` (D5).

## 7. Battery (paste all)

- `xvfb-run -a .venv/bin/python -m pytest tests/test_review_bar_queues.py tests/test_review_queues.py tests/test_review_handler_feed_card.py -q`
- `xvfb-run -a .venv/bin/python -m pytest tests/test_stop_all.py tests/test_window_stop_all_dialog.py -q`
- Full `tests/test_agent_runtime.py` (nohup split if over the cap)
- ruff multiset vs HEAD on all touched files (measure first)
- pyright vs baselines (0 on all)
- RED proofs for the 9 tests

## 8. COMPLETENESS (mandatory)

- [ ] Bar queue region + set_queue_view + callbacks — diff hunks
- [ ] Handler refresh wiring + confirm helpers — diff hunks
- [ ] window.py confirm dialog + wiring — diff hunks
- [ ] D5 metadata bridge (+_emit_feed_card extension) — diff hunks
- [ ] 9 tests RED-first — outputs + count
- [ ] Sub-phase disclosure (5a–5d or single pass)
- [ ] Battery + baselines pasted
- [ ] Related issues found, NOT fixed (flagged)

Invoke `prompts/steelFramedCodeWriter.md`. Please write and report when done.
