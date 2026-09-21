# PHASE A — Context UI Surface

**Spec:** `docs/specs/SPEC-CONTEXT-UI-COMPACT-LLM-2026-07-10.md` §3.1
**Files to change:** `ui/handlers/agent_runtime_handler.py`, `ui/views/main_content.py`, `ui/views/settings_dialog.py`, `ui/window.py`

---

## EDIT 1 — AgentRuntimeHandler: init state + _on_token_breakdown + new methods

**File:** `ui/handlers/agent_runtime_handler.py`

### Step A: Add init state (around line 108, after `self._last_error_exception`)

```python
        # Phase A — Context UI state.
        self._last_breakdown: dict[str, dict] = {}
        self._last_warning_pct: dict[str, float] = {}
        self._first_compaction_seen: dict[str, bool] = {}
        self._on_token_breakdown_extra: Callable | None = None
```

### Step B: Replace _on_token_breakdown (lines 1238-1249)

Read the current method first. Replace the entire body with the expanded version that: logs (preserved), caches breakdown, fires compaction bubble on first trim, fires threshold warnings at 80%/95% with hysteresis, and forwards to the extra listener.

Key logic:
- `if breakdown.get("trimmed_this_turn", False)` and not already seen → fire `_do_compaction_bubble` via GLib.idle_add
- `usage_pct >= 95.0 and last_pct < 95.0` → fire `_do_usage_warning` with "auto-compact-imminent"
- `usage_pct >= 80.0 and last_pct < 80.0` → fire `_do_usage_warning` with "approaching-limit"
- `usage_pct < 75.0 and last_pct >= 80.0` → reset hysteresis
- Forward to `self._on_token_breakdown_extra` if set

### Step C: Add 3 new methods after _on_token_breakdown

1. `_do_compaction_bubble(self, session_key, ev)` — resolves chat_box, renders "🧹 Context reset. Removed N messages, freed ~N tokens." bubble, appends to chat_box, scrolls.
2. `_do_usage_warning(self, session_key, level, usage_pct)` — renders "⚠️ Context at X%." or "🔴 Context at X%." bubble.
3. `get_last_breakdown(self, session_key)` — returns cached breakdown dict or None.
4. `set_on_token_breakdown_extra(self, cb)` — stores the extra listener callback.

Read the spec §3.1.1 for exact code. Mirror the `_do_error` pattern at line 1286 for bubble rendering.

---

## EDIT 2 — MainContent: context meter widget + setter

**File:** `ui/views/main_content.py`

### Step A: Create meter widgets as instance attributes

In the `__init__` area (around line 160, before `button_bar` construction), add:

```python
        self._context_meter = Gtk.ProgressBar()
        self._context_meter.set_size_request(80, 6)
        self._context_meter.set_show_text(True)
        self._context_meter.set_fraction(0.0)
        self._context_meter.add_css_class("context-meter")
        self._context_meter_label = Gtk.Label(label="")
        self._context_meter_label.add_css_class("context-meter-label")
        self._meter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self._meter_box.append(self._context_meter)
        self._meter_box.append(self._context_meter_label)
```

Then after `button_bar.append(self._send_button)` (around line 190), add:
```python
        button_bar.append(self._meter_box)
```

### Step B: Add set_context_meter method (after existing button helpers, ~line 200)

Takes `(self, session_key, usage_percent)`. Updates the ProgressBar fraction, swaps CSS classes (context-meter-low < 70%, context-meter-medium 70-90%, context-meter-high >= 90%), updates label text. Negative values reset to idle.

Read spec §3.1.2 for exact code.

---

## EDIT 3 — SettingsDialog: compaction_threshold SpinButton

**File:** `ui/views/settings_dialog.py`

### Step A: Add SpinButton after max_tokens row (around line 108)

```python
        self._compaction_threshold_spin = Gtk.SpinButton.new_with_range(0.50, 0.95, 0.05)
        self._compaction_threshold_spin.set_value(self._provider.compaction_threshold or 0.80)
        self._compaction_threshold_spin.set_hexpand(True)
        threshold_row = self._labeled("Compaction threshold", self._compaction_threshold_spin)
        vbox.append(threshold_row)
```

### Step B: Add compaction_threshold to _collect_from_form (around line 190)

Add this kwarg to the ProviderConfig constructor call:
```python
            compaction_threshold=float(self._compaction_threshold_spin.get_value()),
```

---

## EDIT 4 — Window: wire context meter callback

**File:** `ui/window.py`

After the `/clear` wiring block (around line 628), add:

```python
        # Phase A — Wire context meter.
        def _on_context_meter(sk: str, breakdown: dict) -> None:
            usage_pct = breakdown.get("usage_percent", 0.0)
            self._main_content.set_context_meter(sk, usage_pct)
        self._agent_runtime_handler.set_on_token_breakdown_extra(_on_context_meter)
```

---

## Rules

- Use the `steelFramedCodeWriter` prompt at `prompts/steelFramedCodeWriter.md`.
- Read each file before editing. Line numbers may have drifted.
- The spec at `docs/specs/SPEC-CONTEXT-UI-COMPACT-LLM-2026-07-10.md` §3.1 has the exact code for each edit.
- Do NOT touch `agent/runtime.py`, `agent/context_strategy.py`, or `models/conversation.py`.

## Verification commands

```bash
cd /path/to/projects/crabcakes

# 1. Syntax check
python3 -c "import ast; [ast.parse(open(f).read()) for f in ['ui/handlers/agent_runtime_handler.py', 'ui/views/main_content.py', 'ui/views/settings_dialog.py', 'ui/window.py']]; print('SYNTAX OK')"

# 2. AgentRuntimeHandler has new methods
grep -n "_do_compaction_bubble\|_do_usage_warning\|get_last_breakdown\|set_on_token_breakdown_extra\|_last_breakdown\|_first_compaction_seen" ui/handlers/agent_runtime_handler.py

# 3. MainContent has meter
grep -n "_context_meter\|set_context_meter" ui/views/main_content.py

# 4. SettingsDialog has threshold spin
grep -n "compaction_threshold" ui/views/settings_dialog.py

# 5. Window has meter wiring
grep -n "set_on_token_breakdown_extra\|_on_context_meter" ui/window.py

# 6. Existing tests pass
python3 -m pytest tests/test_agent_runtime.py tests/test_chat_render_handler.py -q -x
```

## Deliverables

```
COMPLETENESS:
- [x/not done] Edit 1: _on_token_breakdown expanded + 4 new methods + init state — evidence: (command 2)
- [x/not done] Edit 2: context meter widget + set_context_meter — evidence: (command 3)
- [x/not done] Edit 3: compaction_threshold SpinButton + save — evidence: (command 4)
- [x/not done] Edit 4: window wiring — evidence: (command 5)
- [x/not done] Existing tests pass — evidence: (command 6)
```
