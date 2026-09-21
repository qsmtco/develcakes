# UIRESP3-PHASE-2 — Instructions (Coder)

**Origin:** external live profiles (Qrusher, 2026-09-12 and 2026-09-14) of the
running app. Contract: `docs/specs/SPEC-UI-RESPONSIVENESS-3.md` **§3 (Phase 2)**
plus **§7 (measured acceptance gate)**.

**Why this unit exists.** Phase 1 of that spec (rendered-body cap) shipped
earlier as UIRESP2-T2 `dccce6c` and is done — this unit is Phase 2 only. The
profile shows the GTK main thread pinned at **94–100% of a core** with the
process making **0 voluntary context switches and 0 minor faults** in one
15 s window — i.e. a pure userspace loop that never blocks and never
allocates. Two unbounded mechanisms match that signature exactly, and both are
still in the tree.

**Supervisor-verified anchors (tree at `bdfc246`):**

- `ui/handlers/activity_handler.py` — `_set_state` (the `timeout_add(250, self._status_tick)` arm). A 250 ms repeating source, started on **every** state transition.
- `ui/handlers/activity_handler.py` — `_status_tick()` — returns `True` for `reasoning/streaming/tool_use` **and for `idle`**. A `True` return re-arms the source, so in the idle state **the timer never dies**.
- `ui/handlers/activity_handler.py` — `_idle_pulse()` — docstring says *"ANIMATION: never skip-gated"*; calls `self._feedbar.pulse_progress()` and returns `True` unconditionally.
- `ui/handlers/activity_handler.py` — `_live_update()` — signature-gated (`_last_tick_signature`), cheap, **correct, do not touch**.
- `ui/views/feedbar.py` — `set_progress_hidden(hidden)` — sets **`set_opacity(0)`**, not `set_visible(False)`. The "hidden" bar stays in the widget tree and is still traversed on every layout/render pass.
- `ui/views/feedbar.py` — `pulse_progress()` → `self._progress_bar.pulse()`.

**Amplifier worth knowing (flag, don't fix here):** `_set_state()` opens with
`if not self._is_ui_active(session_key): return`. State events for a session
that is not the displayed tab are **ignored**, so the feedbar can stay in
`idle` while background agents work — meaning the pulse can be running during
an active agent run, not only when everything is quiet.

## EDITS

**Edit A — bound the idle pulse (`ui/handlers/activity_handler.py`).**
PM direction: **bound it, do not remove it** (the idle pulse is wanted).
- Initialise `self._idle_ticks = 0` alongside the other ticker state.
- Reset `self._idle_ticks = 0` in `_set_state()` on every transition, so each
  entry into `idle` gets a full pulse budget.
- In `_status_tick()`'s idle branch, count and return a bounded keep-alive:
  ```python
  if self._state == "idle":
      self._idle_pulse()
      self._idle_ticks += 1
      return self._idle_ticks < 20      # ~5 s of pulse at 250 ms, then the source dies
  ```
- Keep the active-state branch and `_live_update` exactly as they are.
- The progress bar must not be left mid-pulse when the ticker stops — set the
  hidden/idle state explicitly on the final tick.

**Edit B — take the hidden bar out of traversal (`ui/views/feedbar.py:76`).**
- `set_progress_hidden(hidden)` must also call `self._progress_bar.set_visible(not hidden)`.
  Keep the opacity call for the fade, but visibility is what removes it from
  layout/render.
- **Check every caller first** (`grep -rn 'set_progress_hidden' ui/`) and confirm
  none depends on the bar being visible-but-transparent. Report what you found.
- Re-check `set_progress_opacity` / `set_progress_pulse` for the same issue:
  anything that sets opacity without visibility is a traversal cost.

**Edit C — the performance probe (`scripts/crab_perf_probe.py`, new) — spec §7.**
Without this the phase cannot be accepted, because its whole claim is a number.
- Read-only. Samples the **main thread** via `/proc/<pid>/task/<tid>/stat`.
- ⚠️ **Do NOT use `/proc/<pid>/stat`** — that file is **process-wide**, and
  using it produced a real mis-measurement during this investigation (it was
  labelled "main thread" and read 100–124% when the main thread was at 3%).
  Put that warning in the module docstring.
- Report over a 60 s window: main-thread **mean / median / p90 / max**, plus
  feed card arrivals, so idle and storm conditions are distinguishable.
- Exit non-zero when over budget so it can gate CI or a cron check.

**Edit D — tests (RED-FIRST).** Spec §5 Phase-2 rows:
- `test_idle_tick_terminates` — the idle branch returns `False` within the tick budget.
- `test_idle_ticks_reset_on_state_change` — counter resets; the next active state ticks normally.
- `test_active_states_unaffected` — `reasoning/streaming/tool_use` still return `True` and still update the feedbar.
- `test_hidden_progress_bar_not_visible` — `set_progress_hidden(True)` → widget not visible.
- `test_main_thread_idle_budget` — with a ≥3,500-card fixture and the app idle,
  main-thread CPU stays under budget over a 60 s sample. If this cannot run
  headless in CI, mark it explicitly (slow/manual) and say so in the report
  rather than dropping it silently.

## GATES

- **RED-first:** paste the failing output for every Edit-D row before implementing.
- **GREEN:** the new suite ×2. Existing `tests/test_activity_*` and feedbar
  suites must stay green — the state machine is exercised by them.
- pyflakes on touched files: **0 undefined**.
- **Measured evidence (§7) — required, not optional.** Run the probe before and
  after, on the same machine, and paste both numbers:
  - idle, ≥3,500 cards: main-thread CPU **< 10 %** sustained over 60 s
  - under an agent storm: **< 25 %** sustained over 60 s
  - `pango_layout_get_size` share materially below the recorded **60.1 %** baseline
    (native profile: `py-spy record --native`; `--native` is incompatible with `--nonblocking`)
  - a 30-minute idle soak showing no return to the pinned state
- **If the gate is not met, report the numbers and STOP.** Do not escalate to
  §4 (bound rendered cards / virtualize) on your own — that is a separate,
  much larger decision for the PM.
- Full suite: report the count against the current baseline (**0F as of `bdfc246`**).
- One commit:
  `perf(ui): bound idle pulse + drop hidden progress bar from traversal (UIRESP3 Phase 2, measured)`
- Flag (don't fix) anything adjacent you notice.

Then STOP — audit next.
