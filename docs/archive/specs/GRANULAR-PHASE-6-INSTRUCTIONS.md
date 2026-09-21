# GRANULAR Phase 6 of 8 — Exec Auto-Accept Integration + Window Wiring

**Spec:** `docs/specs/SPEC-AUTO-ACCEPT-GRANULAR-1.md` §2.5 + §2.6
**Files to change:**
1. `ui/handlers/agent_runtime_handler.py` (1180 lines)
2. `ui/window.py` (~1000+ lines)
3. `ui/handlers/feed_handler.py` (1639 lines) — add `get_exec_auto_accept_mode()` + `set_check_exec_auto_accept_callback_for_handler()` only

**Builder prompt:** `prompts/steelFramedCodeWriter.md`

## CRITICAL: Read ALL files before starting

Before writing ANY code, READ these files completely:
1. `ui/handlers/agent_runtime_handler.py` — focus on `__init__` (line 46) and `_do_approval_needed` (line 967)
2. `ui/window.py` — focus on the wiring section (around line 477) and `_show_auto_accept_warning` (around line 916)
3. `ui/handlers/feed_handler.py` — understand the Phase 4 state (prefs, refresh, etc.)
4. `docs/specs/SPEC-AUTO-ACCEPT-GRANULAR-1.md` §2.5 and §2.6 — the spec sections with exact code
5. `prompts/steelFramedCodeWriter.md` — your standing orders

## Task

Two sub-changes:

### Sub-change A: Exec Silent bypass in AgentRuntimeHandler (§2.5)

In `ui/handlers/agent_runtime_handler.py`:

#### A1: Add callback attribute to `__init__`

After line 80 (`self._on_agent_start_cb: Callable[[str], None] | None = None`), add:

```python
# V2 exec auto-accept callback: returns current exec mode ("off"|"show"|"silent")
# or None. Set by window.py wiring.  When the callback returns "silent",
# _do_approval_needed bypasses card creation and approves directly.
self._on_check_exec_auto_accept: Callable[[], str | None] | None = None
```

#### A2: Add callback setter

After `__init__`, add this method (near other setters):

```python
def set_check_exec_auto_accept_callback(self, callback: Callable[[], str | None] | None) -> None:
    """Install callback that returns the current exec auto-accept mode,
    or None if exec auto-accept is off. (Phase E + v2)
    """
    self._on_check_exec_auto_accept = callback
```

#### A3: Add Silent bypass in `_do_approval_needed`

At the TOP of `_do_approval_needed`, AFTER the `self._fh is None` check but BEFORE creating the card, add:

```python
# V2 Silent bypass: if exec auto-accept is in silent mode, approve
# directly without creating a feed card. The card is NOT stored
# in _cards or _pending_approvals (no double-action possible).
if (self._on_check_exec_auto_accept is not None
        and self._on_check_exec_auto_accept() == "silent"):
    agent_def = self._agents.get(session_key)
    if agent_def is None:
        return
    runtime = self._runtimes.get(agent_def.runtime_id)
    if runtime is None:
        return
    self._GLib.idle_add(
        lambda: runtime.approve_exec(session_key, tool_name, args, True)
    )
    return
```

IMPORTANT: The lambda captures `session_key`, `tool_name`, `args` by closure. Since these are parameters (not loop variables), this is safe.

### Sub-change B: FeedHandler additions (§2.5)

In `ui/handlers/feed_handler.py`:

#### B1: Add `get_exec_auto_accept_mode()` method

Add near the other auto-accept methods (after `_save_feed_prefs_idle` or near `_is_card_auto_acceptable`):

```python
def get_exec_auto_accept_mode(self) -> str | None:
    """Public API: return the current exec auto-accept mode.

    Used by AgentRuntimeHandler via the installed callback to decide
    whether to bypass card creation in Silent mode.

    Returns:
        The mode string ("off", "show", "silent"), or None if _prefs
        is not yet initialized.
    """
    if self._prefs is None:
        return None
    return self._prefs.exec_command.mode
```

#### B2: Add `set_check_exec_auto_accept_callback_for_handler()` method

This is a setter that Window.py calls to wire the callback. It receives the AgentRuntimeHandler's setter and connects it to FeedHandler's getter:

```python
def set_check_exec_auto_accept_callback_for_handler(self, handler_setter) -> None:
    """Wire the exec auto-accept callback to AgentRuntimeHandler.

    Per §8.6 R2 (no handler-to-handler imports), this indirection lets
    AgentRuntimeHandler query the exec mode without importing FeedHandler.
    Window.py calls this after both handlers are constructed.

    Args:
        handler_setter: AgentRuntimeHandler.set_check_exec_auto_accept_callback
    """
    handler_setter(self.get_exec_auto_accept_mode)
```

### Sub-change C: Window.py wiring (§2.6)

In `ui/window.py`:

#### C1: Add exec auto-accept wiring

After the existing `set_show_auto_accept_warning` wiring (around line 477-481), add:

```python
# V2: wire exec auto-accept callback (Phase 6)
self._feed_handler.set_check_exec_auto_accept_callback_for_handler(
    self._agent_runtime_handler.set_check_exec_auto_accept_callback
)
```

#### C2: Replace `set_show_auto_accept_warning` wiring with v2 signature

Find the existing wiring (around lines 477-481):

```python
self._feed_handler.set_show_auto_accept_warning(
    lambda agent_name, on_confirm, on_cancel: self._show_auto_accept_warning(
        agent_name, on_confirm, on_cancel
    )
)
```

Replace with:

```python
self._feed_handler.set_show_auto_accept_warning(
    lambda category, agent_name, on_confirm, on_cancel: self._show_auto_accept_warning_v2(
        category, agent_name, on_confirm, on_cancel
    )
)
```

#### C3: Add `_show_auto_accept_warning_v2` method

Add this method near the existing `_show_auto_accept_warning` (around line 916):

```python
def _show_auto_accept_warning_v2(
    self, category: str, agent_name: str, on_confirm: Callable, on_cancel: Callable
) -> None:
    """V2 warning dialog for per-type auto-accept activation.

    Args:
        category: "diffs", "files", or "exec"
        agent_name: human-readable agent identifier (resolved by handler)
        on_confirm: called if user confirms
        on_cancel: called if user cancels
    """
    titles = {
        "diffs": "Auto-accept diffs?",
        "files": "Auto-accept file changes?",
        "exec": "Auto-approve exec commands?",
    }
    bodies = {
        "diffs": f"{agent_name} will silently auto-accept every diff it writes. You will not see the diff before it is committed.",
        "files": f"{agent_name} will silently auto-accept every file_created/file_modified/file_deleted card it produces. You will not see the change before it is committed.",
        "exec":  f"{agent_name} will silently auto-approve every shell command it runs. This includes rm, git push, network calls, anything. There is no undo.",
    }
    title = titles.get(category, "Enable auto-accept?")
    body = bodies.get(category, f"Enable auto-accept for {category}?")

    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk

    dialog = Gtk.MessageDialog(
        transient_for=self,
        modal=True,
        message_type=Gtk.MessageType.WARNING,
        buttons=Gtk.ButtonsType.NONE,
        text=title,
        secondary_text=body,
    )
    dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
    dialog.add_button("Turn On", Gtk.ResponseType.OK)
    dialog.set_default_response(Gtk.ResponseType.CANCEL)

    _dispatched = [False]

    def _on_response(dialog, response):
        if _dispatched[0]:
            return
        _dispatched[0] = True
        if response == Gtk.ResponseType.OK:
            on_confirm()
        else:
            on_cancel()
        dialog.close()

    dialog.connect("response", _on_response)
    dialog.show()
```

#### C4: Keep the old `_show_auto_accept_warning` method

DO NOT remove the old `_show_auto_accept_warning` method. It's kept for legacy compat — tests and legacy v1 toggle paths may still reference it.

### DO NOT:
- Remove existing methods or wiring
- Modify tests
- Add Show mode auto-accept (that's Phase 7)
- Create any handler-to-handler imports (§8.6 R2)

## Verification

```bash
# Verify files parse
python3 -c "import ast; ast.parse(open('ui/handlers/agent_runtime_handler.py').read()); print('ARTH AST OK')"
python3 -c "import ast; ast.parse(open('ui/window.py').read()); print('Window AST OK')"
python3 -c "import ast; ast.parse(open('ui/handlers/feed_handler.py').read()); print('FH AST OK')"

# Verify new methods exist
grep -n "def set_check_exec_auto_accept_callback\|_on_check_exec_auto_accept" ui/handlers/agent_runtime_handler.py
grep -n "def get_exec_auto_accept_mode\|def set_check_exec_auto_accept_callback_for_handler" ui/handlers/feed_handler.py
grep -n "def _show_auto_accept_warning_v2" ui/window.py

# Verify wiring
grep -n "set_check_exec_auto_accept_callback_for_handler" ui/window.py
grep -n "_show_auto_accept_warning_v2" ui/window.py

# Run ALL tests
python3 -m pytest tests/test_feed_handler.py tests/test_feed_card.py tests/test_feed_store.py tests/test_low12_13_feed.py tests/test_crabcard_parser.py tests/test_crabwatch_handler.py -q

# Line counts
wc -l ui/handlers/agent_runtime_handler.py ui/window.py ui/handlers/feed_handler.py
```

## COMPLETENESS checklist

```
COMPLETENESS:
- [x/not done] A1: _on_check_exec_auto_accept attribute added to ARTH __init__
- [x/not done] A2: set_check_exec_auto_accept_callback setter added to ARTH
- [x/not done] A3: Silent bypass added to _do_approval_needed
- [x/not done] B1: get_exec_auto_accept_mode() added to FeedHandler
- [x/not done] B2: set_check_exec_auto_accept_callback_for_handler() added to FeedHandler
- [x/not done] C1: Window.py wires exec callback
- [x/not done] C2: Window.py wiring updated to v2 signature
- [x/not done] C3: _show_auto_accept_warning_v2 method added to Window
- [x/not done] C4: Old _show_auto_accept_warning kept (legacy compat)
- [x/not done] All existing tests pass — evidence (pytest output)
```
