# Task: Per-agent thinking effort (Off / Low / High)

> **SUPERSEDED (2026-10-09):** This design was never implemented and is re-homed to
> `docs/specs/SPEC-REASONING-EFFORT.md` — the control moves from the agent builder to the
> **provider card** (PM direction), level set becomes off/low/medium/high, and the per-agent
> override is now a Phase-2 candidate there. Carried forward into the new spec: off = omit
> entirely, the hardcoded Anthropic budget table, backwards compat, and the "don't bundle
> effort with visibility" rule. **Do not implement THIS document.**

**Status:** ❌ NOT IMPLEMENTED — SUPERSEDED by `SPEC-REASONING-EFFORT.md` (2026-10-09)

## Context

Crabcakes has three LLM caller families (`_call_openai`, `_call_minimax`,
`_call_anthropic` in `agent/runtime.py:195,238,363`). Each accepts a fixed
kwarg set: `base_url, api_key, model, messages, tools, timeout, x_title`. None
of them accept a `reasoning`/`thinking`/`reasoning_effort` parameter, so the
runtime cannot request more or less model reasoning on a per-agent basis.

The user is building an agent edit dialog (`ui/views/agent_builder.py`) and
wants a `Thinking` toggle so each agent (Coder, Debugger, etc.) can opt into
deeper reasoning. After researching OpenClaw's `agents.list[].thinkingLevel`
pattern (openclaw/openclaw#21624, docs.openclaw.ai/tools/thinking), the clean
shape is **one abstraction level, three provider translations**:

- **Agent level:** `SpecialAgentDef.thinking_effort` — `"" | "off" | "low" | "high"`
- **Provider level:** `ProviderConfig.supports_thinking: bool = True`
- **Runtime level:** three per-caller payload assembly branches, keying off
  the abstract level

Constraints gathered from research:
1. **Three different provider APIs, same user intent.** OpenAI uses
   `reasoning_effort: "low"|"medium"|"high"`, Anthropic uses
   `thinking: {type: "enabled", budget_tokens: <int>}`,
   MiniMax-M3 uses `reasoning: "off"|"low"|"high"`. The user picks one
   abstract level; the runtime picks the right wire format.
2. **Capability gate is mandatory.** OpenClaw learned this the hard way:
   `compat.supportedReasoningEfforts` per provider/model declares which
   levels are honored, and unsupported values are silently dropped. Crabcakes
   needs an equivalent — `supports_thinking` on `ProviderConfig` defaults True
   so all current providers remain unchanged, but it can be flipped to False
   in YAML for providers like GLM that ignore the field.
3. **Anthropic has no levels, only budgets.** Map abstract level to a
   hardcoded budget table (off → omit field, low → 1024, high → 4096).
4. **Don't bundle effort with visibility.** `/think` (effort) and `/reasoning`
   (show thinking blocks in chat) are independent concerns. This task ships
   only the effort side; visibility is already wired via
   `agent.thinking` events in `ui/handlers/chat_handler.py:711`.
5. **YAML backward compat.** Existing `~/.config/crabcakes/agents/*.yaml`
   files have no `thinking_effort` field. Loader must default to `""`
   (inherit provider default / off-equivalent). No warning, no crash.

## Scope

**IN:**
- Add `thinking_effort: str = ""` to `SpecialAgentDef`
- Add `supports_thinking: bool = True` to `ProviderConfig`
- Wire `thinking_effort` through `Conversation` so it persists across session
  reset (`/new`, daily reset) the same way `fallback_provider` does
- Three runtime branches in the three callers — one per provider family
- One `Thinking` dropdown in the agent builder dialog, gated on
  `supports_thinking` of the selected provider
- Validation in `utils/agent_defs.validate_agent_def`
- 4 targeted tests (see §Acceptance Criteria)

**OUT (deferred to follow-up tasks):**
- Chat-directive `/think <level>` runtime parsing
- The provider settings dialog exposing `supports_thinking` toggle (mark
  it via YAML only for v1)
- Per-session override UI (`/think <level>` mid-conversation)
- Anthropic budget customization (hardcoded table only)
- OpenClaw-style `adaptive` / `xhigh` / `max` levels

## Files Changed

### 1. `models/providers.py` — add `supports_thinking` field

**Location:** `ProviderConfig` dataclass, after `supports_streaming` (line 49).

**What:** Add one field with default True so all existing providers keep
working without any YAML change.

**Verified signature** (from `models/providers.py:40-56`):

```python
class ProviderConfig:
    """Configuration for a single LLM API provider."""
    name: str
    base_url: str
    api_key: str
    default_model: str
    caller: str = ""                    # API caller key (openai|minimax|anthropic|openrouter|zai)
    enabled: bool = True
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_thinking: bool = True      # NEW — set False for providers that ignore thinking fields
    max_tokens: int = 128_000
    default_max_tokens: int = 0
    compaction_threshold: float = 0.80  # fraction of max_tokens that triggers compaction
    last_verified_at: str | None = None
    last_error: str | None = None
    context_mode: str = "auto"          # "auto" | "preload" | "jit" | "hybrid"
```

**Imports required:** None (same file).

### 2. `utils/providers_store.py` — round-trip the new field

**Location:** `_to_dict` (line 55-69) and `_from_dict` (line 73-95).

**Why:** Verified by reading the file. `_to_dict` enumerates every field of
`ProviderConfig`; `_from_dict` calls `d.get(...)` with defaults. **If the new
field is not added to BOTH, existing YAML loads will silently strip it and
save will lose it.** This is the bug pattern from the project bug journal
("field-strip-on-save", Bug #2/Bug #3). The `_from_dict` block also has a
custom comment about caller validation — read it before editing, do not
regress that logic.

```python
# In _to_dict (add one line after supports_streaming):
return {
    "name": p.name,
    "base_url": p.base_url,
    "api_key": p.api_key,
    "default_model": p.default_model,
    "caller": p.caller,
    "enabled": p.enabled,
    "supports_tools": p.supports_tools,
    "supports_streaming": p.supports_streaming,
    "supports_thinking": p.supports_thinking,    # NEW
    "max_tokens": p.max_tokens,
    "default_max_tokens": p.default_max_tokens,
    "compaction_threshold": p.compaction_threshold,
    "last_verified_at": p.last_verified_at,
    "last_error": p.last_error,
}

# In _from_dict (add one kwarg to the ProviderConfig(...) constructor):
return ProviderConfig(
    name=d.get("name", ""),
    base_url=d.get("base_url", ""),
    api_key=d.get("api_key", ""),
    default_model=d.get("default_model", ""),
    caller=caller,
    enabled=d.get("enabled", True),
    supports_tools=d.get("supports_tools", True),
    supports_streaming=d.get("supports_streaming", True),
    supports_thinking=d.get("supports_thinking", True),    # NEW
    max_tokens=d.get("max_tokens", 128_000),
    default_max_tokens=d.get("default_max_tokens", 0),
    compaction_threshold=d.get("compaction_threshold", 0.80),
    last_verified_at=d.get("last_verified_at"),
    last_error=d.get("last_error"),
)
```

### 3. `agent/special_agents.py` — add `thinking_effort` field

**Location:** `SpecialAgentDef` dataclass (line 24-65). Add the field and
its loader line.

```python
@dataclass
class SpecialAgentDef:
    conv_id_prefix: str
    display_name: str
    role: str
    emoji: str
    tools: list[str]
    can_write: bool
    llm_name: str | None = None
    fallback_provider: str | None = None
    fallback_model: str | None = None
    api_key: str | None = None
    app_title: str | None = None
    self_improvement: dict = field(default_factory=dict)
    mcp_servers: list[str] = field(default_factory=list)
    auto_open: bool = False
    auto_add_to_projects: bool = False
    thinking_effort: str = ""      # NEW — "" | "off" | "low" | "high"

    # ... existing methods unchanged
```

In `_load_registry()` (around line 80-100), add the loader line:

```python
registry[session_key] = SpecialAgentDef(
    conv_id_prefix=session_key,
    display_name=name,
    role=role,
    emoji=agent_def.get("emoji", "🤖"),
    tools=tools,
    can_write="write_file" in tools or "edit_file" in tools,
    llm_name=agent_def.get("llm_name"),
    fallback_provider=agent_def.get("fallback_provider"),
    fallback_model=agent_def.get("fallback_model"),
    api_key=***
    app_title=agent_def.get("app_title"),
    self_improvement=agent_def.get("self_improvement", {}),
    mcp_servers=raw_mcp,
    auto_open=agent_def.get("auto_open", False),
    auto_add_to_projects=agent_def.get("auto_add_to_projects", False),
    thinking_effort=agent_def.get("thinking_effort", ""),    # NEW
)
```

### 4. `models/conversation.py` — per-conversation `thinking_effort`

**Location:** `Conversation` dataclass (line 141-190). Add field next to
`fallback_provider`/`fallback_model`.

**Why per-conversation:** matches the `fallback_provider` pattern (line 174-175).
When a session is created from an agent def, copy `thinking_effort` to the
conversation so it survives `/new` and daily reset. The agent def stays the
default for new conversations only.

```python
fallback_provider: str | None = None   # KB fallback provider (from agent def)
fallback_model: str | None = None      # KB fallback model (from agent def)
thinking_effort: str = ""              # NEW — copied from agent def on conv create
```

### 5. `agent/runtime.py` — three per-caller branches

**Locations:** `_call_openai` (line 195), `_call_minimax` (line 238),
`_call_anthropic` (line 363).

**The key question** I verified before writing this: how does the runtime
know what thinking effort to apply for a given call? Answer: it has
`provider_cfg` (already passed into the call site at line 2564) and
`conv.thinking_effort` (new field). Add a `thinking_effort` kwarg to each
caller, with default `""` (no-op). The call site resolves which level to use.

**Pattern for each caller — `thinking_effort: str = ""` keyword argument,
conditional payload block:**

```python
# _call_openai (line 195) — OpenAI-compatible family
def _call_openai(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    timeout: float,
    x_title: str = "",
    *,                                         # NEW: keyword-only after here
    thinking_effort: str = "",                  # NEW
) -> dict:
    """Call OpenAI Chat Completions API (also used by OpenRouter, ZAI)."""
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    payload = {
        "model": _model_id(model),
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if thinking_effort in ("low", "high"):      # NEW
        payload["reasoning_effort"] = thinking_effort
    # "off" and "" both mean "don't send the field" — matches OpenClaw behavior
    # for providers that don't honor reasoning.effort: "none"
    # ... rest unchanged
```

```python
# _call_minimax (line 238) — M2/M3 family uses `reasoning` not `reasoning_effort`
def _call_minimax(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    timeout: float,
    x_title: str = "",
    *,                                         # NEW
    thinking_effort: str = "",                  # NEW
) -> dict:
    """Call MiniMax ChatCompletion v2 API."""
    endpoint = f"{base_url.rstrip('/')}/text/chatcompletion_v2"
    payload = {
        "model": _model_id(model),
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
    if thinking_effort in ("off", "low", "high"):    # NEW — MiniMax uses `reasoning`
        payload["reasoning"] = thinking_effort
    # ... rest unchanged
```

```python
# _call_anthropic (line 363) — Anthropic uses `thinking` with budget_tokens
_ANTHROPIC_THINKING_BUDGETS = {"low": 1024, "high": 4096}    # NEW module constant

def _call_anthropic(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    timeout: float,
    x_title: str = "",
    *,                                         # NEW
    thinking_effort: str = "",                  # NEW
) -> dict:
    """Call Anthropic Messages API."""
    # ... existing system-prompt extraction unchanged ...
    payload: dict[str, Any] = {
        "model": _model_id(model),
        "messages": api_messages,
        "max_tokens": 4096,
    }
    if system_msg:
        payload["system"] = system_msg
    if tools:
        payload["tools"] = _convert_tools_for_anthropic(tools)
    # NEW: Anthropic thinking field with budget lookup
    budget = _ANTHROPIC_THINKING_BUDGETS.get(thinking_effort)
    if budget is not None:
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
    # ... rest unchanged
```

**Call site at `agent/runtime.py:2641`** — pass the resolved effort through.
Read that block first; the existing `caller(...)` call needs `thinking_effort=`
added, with the value coming from `conv.thinking_effort` (after capability
gate — see step 6 below).

```python
# Around line 2641, in the existing call:
return caller(
    base_url=provider_cfg.base_url,
    api_key=effect…key,
    model=model,
    messages=messages,
    tools=tools if tools else None,
    timeout=float(self._config.tool_timeout_seconds),
    x_title=x_title,
    thinking_effort=_resolved_thinking_effort,    # NEW
)
```

`_resolved_thinking_effort` is computed in the same function block:
```python
_resolved_thinking_effort = (
    conv.thinking_effort if provider_cfg.supports_thinking else ""
)
```
If the provider doesn't support thinking, pass `""` so the caller's `if` block
never fires. This is the single capability gate.

### 6. `utils/agent_defs.py` — validate `thinking_effort`

**Location:** `validate_agent_def()` (line 377-450). Add validation right
after the `llm_name` validation.

```python
# Validate thinking_effort (only if present — field is optional)
effort = agent_def.get("thinking_effort")
if effort is not None:
    if not isinstance(effort, str):
        errors.append(f"Field 'thinking_effort' must be a string, got {type(effort).__name__}")
    elif effort not in ("", "off", "low", "high"):
        errors.append(
            f"Invalid thinking_effort: {effort!r}. "
            f"Must be one of: '', 'off', 'low', 'high'."
        )
```

**Why this exact set:** verified against the runtime callers' `if` blocks
above — `""` (inherit/inherit-off), `"off"` (MiniMax explicit), `"low"`,
`"high"`. Anything else is rejected.

### 7. `ui/views/agent_builder.py` — Thinking dropdown

**Location:** form layout (line 91-132). Insert a new row between the
Provider row and the Fallback provider row.

**Verified pattern** for visibility-toggling rows: `_on_provider_changed`
(line 345) already toggles fallback row visibility. Mirror it for the new
Thinking dropdown.

**Insertion in `__init__`** (after line 116, before fallback row):

```python
# Thinking effort row — gated on supports_thinking of selected provider
self._thinking_row = self._build_thinking_row()
form_box.append(self._thinking_row)
```

**New helper method** (place near `_build_fallback_provider_row`):

```python
def _build_thinking_row(self) -> Gtk.Box:
    """Build the Thinking effort dropdown row.

    Three states: Off, Low, High. Always visible; sensitive (interactive)
    is toggled based on the selected provider's supports_thinking flag.
    Mirrors the Fallback row pattern (ui/views/agent_builder.py:355).
    """
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    row.set_hexpand(True)

    self._thinking_dropdown = Gtk.DropDown(
        model=Gtk.StringList.new(["Off", "Low", "High"])
    )
    self._thinking_labeled = self._labeled_box("Thinking", self._thinking_dropdown)
    row.append(self._thinking_labeled)
    return row
```

**Modify `_on_provider_changed`** (line 345-348) — add the gate call:

```python
def _on_provider_changed(self, dropdown, _param) -> None:
    """When provider changes, refresh save button state and toggle fallback row."""
    self._update_save_button()
    self._update_fallback_visibility()
    self._update_thinking_visibility()    # NEW
```

**New helper `_update_thinking_visibility`:**

```python
def _update_thinking_visibility(self) -> None:
    """Enable Thinking dropdown only when the selected provider supports it.

    Disabled state = visible but not interactable (not hidden) so the user
    sees the field exists. Matches the user-research pattern from openclaw's
    provider-profile-driven menus.
    """
    idx = self._provider_dropdown.get_selected()
    supports = True    # default
    if 0 <= idx < len(self._providers):
        supports = getattr(self._providers[idx], "supports_thinking", True)
    self._thinking_dropdown.set_sensitive(supports)
    if not supports:
        # Reset to "Off" since the value can't be honored
        self._thinking_dropdown.set_selected(0)
```

**Modify `get_values()`** (line 167-185) — extract the field:

```python
return {
    "name": name,
    "emoji": emoji,
    "role": role,
    "prompts": prompts,
    "tools": tools,
    "llm_name": llm_name,
    "mcp_servers": self._get_selected_mcp_servers(),
    "self_improvement": self._get_si_config(tools),
    "fallback_provider": self._get_selected_fallback_provider() or None,
    "thinking_effort": self._get_selected_thinking_effort(),    # NEW
}
```

**New helper `_get_selected_thinking_effort`:**

```python
def _get_selected_thinking_effort(self) -> str:
    """Map the dropdown index to the canonical effort string.
    0 → "off", 1 → "low", 2 → "high".
    """
    idx = self._thinking_dropdown.get_selected()
    return ("off", "low", "high")[idx]
```

**Modify `_fill_form`** (line 689-728) — pre-fill on edit:

```python
# After the existing fallback provider restore block (around line 723):
effort = agent_def.get("thinking_effort", "")
effort_idx = {"off": 0, "low": 1, "high": 2}.get(effort, 0)
if 0 <= self._thinking_dropdown.get_model().get_n_items() > effort_idx:
    self._thinking_dropdown.set_selected(effort_idx)
self._update_thinking_visibility()    # gate based on the (already-selected) provider
```

**CSS classes:** none new — reuse `agent-builder-*` classes already loaded.

### 8. `agent/runtime.py` — copy `thinking_effort` to conversation on create

**Location:** `create_conversation()` block at line 1427 (where other
fields like `fallback_provider` are copied from `data`).

**Pattern verified** by reading `agent/runtime.py:1427-1448`. `data` is the
agent-def dict; `conv` is the new Conversation. Add one line:

```python
# After the existing fallback_provider / fallback_model lines:
thinking_effort=data.get("thinking_effort", ""),
```

**Files NOT changed** (already correct):
- `ui/handlers/chat_handler.py:711` — `thinking` event rendering already
  wired; this task is about effort, not visibility
- `ui/handlers/settings_handler.py` — provider settings dialog; out of scope
  for v1 (user can edit `supports_thinking` via YAML if needed)
- `agent/runtime.py:_PROVIDER_CALLERS` — no new caller family needed;
  translation happens inside the existing three callers
- `docs/ARCHITECTURE.md` — **NOT changed in this task** (see §ARCHITECTURE Updates)

## Data Flow

End-to-end trace for a user turning on "High" thinking for Coder:

```
1. User edits Coder agent → opens agent_builder dialog
2. dialog: form shows Thinking dropdown next to Provider dropdown
3. dialog: Provider is "M3" (MiniMax-M3), supports_thinking=True (default)
   → Thinking dropdown is sensitive (interactive)
4. User picks "High" → dropdown index 2
5. User clicks Save → get_values() returns {"thinking_effort": "high", ...}
6. agent_builder_handler persists to ~/.config/crabcakes/agents/coder.yaml
7. On next app launch / agent def reload → special_agents._load_registry()
   reads "thinking_effort: high" → SpecialAgentDef.thinking_effort = "high"
8. User sends a message in Coder tab
9. runtime.create_conversation(data) → conv.thinking_effort = "high"
10. runtime._call_llm() → resolves _resolved_thinking_effort
11. caller=_call_minimax, conv.thinking_effort="high", provider_cfg.supports_thinking=True
    → thinking_effort kwarg = "high"
12. _call_minimax payload: payload["reasoning"] = "high"
13. HTTP POST to MiniMax API with reasoning="high"
14. MiniMax-M3 returns reasoning-amplified response
```

Verified against actual function signatures:
- `special_agents._load_registry()` line 80-100 (real loader loop)
- `runtime.create_conversation()` line 1427-1448 (real kwargs)
- `runtime._call_llm()` line 2600-2650 (real call site)
- `_call_minimax` line 238-285 (real payload assembly)

## File Change Summary

| File | Change | Lines | Risk |
|---|---|---|---|
| `models/providers.py` | Add `supports_thinking` field | +1 | Low (default True, no behavior change) |
| `utils/providers_store.py` | Round-trip `supports_thinking` | +2 | Low (mirror existing pattern) |
| `agent/special_agents.py` | Add `thinking_effort` field + loader | +3 | Low (default "", loader tolerant) |
| `models/conversation.py` | Add `thinking_effort` field | +1 | Low (default "") |
| `agent/runtime.py` | 3 caller signatures + payload blocks + call site + conv copy | ~30 | **Medium** (touches the LLM call path — all 3 callers) |
| `utils/agent_defs.py` | Validate `thinking_effort` in `validate_agent_def` | +7 | Low (validator only) |
| `ui/views/agent_builder.py` | Thinking row + helpers + fill_form | ~45 | Medium (UI, but isolated) |
| `tests/test_thinking_effort.py` | NEW — 4 tests | ~120 | Low (new file) |

Total: ~210 lines added across 8 files, 0 removed.

## Implementation Order

Each step ends with `pytest tests/test_thinking_effort.py` (after step 4) and
`pytest tests/` (after each step).

1. **`models/providers.py` + `utils/providers_store.py` + new test**
   - Add `supports_thinking: bool = True` to `ProviderConfig`
   - Round-trip in `_to_dict`/`_from_dict`
   - Write test_providers_store_supports_thinking_roundtrip — must FAIL before
     this step (revert `_to_dict`/`_from_dict` and confirm)
2. **`agent/special_agents.py`** — add field + loader
   - Write test_special_agents_thinking_effort_default_empty
3. **`models/conversation.py` + `agent/runtime.py` (conv create)**
   - Add field, copy on create
   - Write test_conversation_thinking_effort_persists (after /new)
4. **`agent/runtime.py` (3 callers + call site)**
   - Add kwarg to all 3 callers + conditional payload blocks
   - Add `_resolved_thinking_effort` resolution at call site
   - Write test_runtime_thinking_effort_translation — the BIG test that proves
     each provider family maps the abstract level to the right wire field
5. **`utils/agent_defs.py`** — validation
   - Add validator block
   - Write test_validate_agent_def_rejects_bad_thinking_effort
6. **`ui/views/agent_builder.py`** — Thinking dropdown
   - Add row, helpers, fill_form entry, get_values entry
   - Verify manually by opening dialog in dev mode
7. **Final sweep**: `pytest tests/`, `ruff check .`, `pyright .` (or whatever
   the project uses — verify with `cat pyproject.toml`)

## Acceptance Criteria

Each is testable. The 4 tests live in a new file `tests/test_thinking_effort.py`.

### Test 1: `test_providers_store_supports_thinking_roundtrip`
Round-trips `ProviderConfig(supports_thinking=False)` through `_to_dict` →
`_from_dict` and asserts the False value is preserved. **Proves the YAML
field survives load/save.** (Catches the field-strip-on-save bug pattern.)

```python
def test_providers_store_supports_thinking_roundtrip(tmp_path):
    from models.providers import ProviderConfig
    from utils.providers_store import _to_dict, _from_dict
    p = ProviderConfig(name="test", base_url="https://x", api_key="k",
                       default_model="m", supports_thinking=False)
    d = _to_dict(p)
    assert d["supports_thinking"] is False
    p2 = _from_dict(d)
    assert p2.supports_thinking is False
```

### Test 2: `test_special_agents_thinking_effort_default_empty`
Loads an agent YAML with no `thinking_effort` field and asserts
`SpecialAgentDef.thinking_effort == ""`. **Proves backward compat** —
existing user YAMLs don't break.

```python
def test_special_agents_thinking_effort_default_empty(tmp_path):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "test-agent.yaml").write_text(
        "name: Test\nrole: test\ntools: [read_file]\nprompts: [system/coder.md]\nllm_name: x\n"
    )
    # patch utils.config.get_agents_dir() to return tmp_path/agents
    # then call _load_registry() and check the def
    ...
    assert defs["special:test"].thinking_effort == ""
```

### Test 3: `test_runtime_thinking_effort_translation` (the big one)
For each provider family, asserts the right wire field is sent at each
abstract level. Uses `unittest.mock` to patch `_urlopen_with_ssl_retry`
and inspect the request body.

```python
def test_runtime_thinking_effort_translation(monkeypatch):
    import json
    from agent import runtime

    cases = [
        # (caller_name, abstract_effort, expected_field, expected_value)
        ("_call_openai",   "low",  "reasoning_effort", "low"),
        ("_call_openai",   "high", "reasoning_effort", "high"),
        ("_call_openai",   "",     None, None),                # "" → field omitted
        ("_call_openai",   "off",  None, None),                # "off" → field omitted (OpenAI ignores)
        ("_call_minimax",  "low",  "reasoning", "low"),
        ("_call_minimax",  "high", "reasoning", "high"),
        ("_call_minimax",  "off",  "reasoning", "off"),        # MiniMax uses explicit "off"
        ("_call_anthropic","low",  "thinking", {"type": "enabled", "budget_tokens": 1024}),
        ("_call_anthropic","high", "thinking", {"type": "enabled", "budget_tokens": 4096}),
        ("_call_anthropic","off",  None, None),                # "off" → field omitted
    ]
    for caller_name, effort, field, expected in cases:
        captured = {}
        def fake_urlopen(req, timeout):
            captured["body"] = json.loads(req.data)
            class Resp:
                def read(self): return b'{}'
                def __enter__(self): return self
                def __exit__(self, *a): pass
            return Resp()
        monkeypatch.setattr(runtime, "_urlopen_with_ssl_retry", fake_urlopen)
        caller = getattr(runtime, caller_name)
        caller(base_url="https://x", api_key="k", model="m",
               messages=[], tools=None, timeout=30, thinking_effort=effort)
        if field is None:
            assert field not in captured["body"], \
                f"{caller_name}({effort!r}) should omit {field}"
        else:
            assert captured["body"].get(field) == expected, \
                f"{caller_name}({effort!r}) should send {field}={expected}"
```

**This test is the proof that the spec works.** If you revert any of the
three callers' payload blocks, this test FAILS at the corresponding case.

### Test 4: `test_validate_agent_def_rejects_bad_thinking_effort`
Asserts `validate_agent_def({"thinking_effort": "ultra", ...})` returns
an error. Also asserts `""`, `"off"`, `"low"`, `"high"` all pass.

```python
def test_validate_agent_def_rejects_bad_thinking_effort():
    from utils.agent_defs import validate_agent_def
    base = {"name": "x", "prompts": ["a"], "tools": ["t"],
            "llm_name": "x", "fallback_provider": "x"}
    # Valid
    for effort in ("", "off", "low", "high"):
        errs = validate_agent_def({**base, "thinking_effort": effort})
        assert not any("thinking_effort" in e for e in errs), f"rejected {effort!r}"
    # Invalid
    for bad in ("ultra", "MEDIUM", "on", 42, None):
        errs = validate_agent_def({**base, "thinking_effort": bad})
        assert any("thinking_effort" in e for e in errs), f"accepted {bad!r}"
```

## Edge Cases

| Case | Expected behavior | Verified |
|---|---|---|
| `thinking_effort=""` on agent | Runtime passes `""` to caller; caller omits the field entirely | Test 3 (`_call_openai`, `""` case) |
| `thinking_effort="off"` on agent, OpenAI provider | Runtime passes `"off"`; `_call_openai` omits the field (no `"reasoning_effort": "off"` sent — OpenAI doesn't honor it) | Test 3 (`_call_openai`, `"off"` case) |
| `thinking_effort="off"` on agent, MiniMax provider | Runtime passes `"off"`; `_call_minimax` sends `payload["reasoning"] = "off"` (MiniMax uses explicit value) | Test 3 (`_call_minimax`, `"off"` case) |
| `thinking_effort="high"`, Anthropic provider | `_call_anthropic` sends `payload["thinking"] = {"type": "enabled", "budget_tokens": 4096}` | Test 3 (`_call_anthropic`, `"high"` case) |
| Provider has `supports_thinking=False`, agent has `thinking_effort="high"` | Runtime resolves to `""`; no thinking field sent | Manual test (capability gate at call site) |
| User toggles Provider dropdown to a non-thinking provider | Dialog resets Thinking dropdown to "Off" and disables it | `_update_thinking_visibility` |
| Existing YAML with no `thinking_effort` field | Loader returns `""`; runtime sends no field | Test 2 |
| `thinking_effort` is `None` (YAML null) | Validator rejects with "must be a string" error | Test 4 |
| `thinking_effort` is `42` (int) | Validator rejects with "must be a string" error | Test 4 |
| `thinking_effort="ultra"` | Validator rejects with valid-options list | Test 4 |

## ARCHITECTURE.md Updates Required

**Defer to a follow-up task** — this spec deliberately does NOT include
ARCHITECTURE.md changes because (a) the change is contained to the LLM call
abstraction and doesn't change a public interface, (b) the agent-def schema
extension (`thinking_effort`) is a YAML field, not a code contract.

The follow-up spec should add to ARCHITECTURE.md §3.4 (`agent/runtime.py`)
a paragraph:

> Per-agent thinking effort is configured via `SpecialAgentDef.thinking_effort`
> with values `""`, `"off"`, `"low"`, `"high"`. The runtime resolves the
> abstract level into the provider's native field at call time:
> `reasoning_effort` (OpenAI family), `reasoning` (MiniMax family),
> `thinking: {type, budget_tokens}` (Anthropic). Providers with
> `ProviderConfig.supports_thinking=False` have the field stripped entirely.

The implementer should file the ARCHITECTURE.md update as a separate task
on completion.

## Self-Audit Checklist (Rule 9)

- [x] Every code sample in this spec traced against actual source files
      (line numbers cited: 195, 238, 363, 40, 73, 24, 141, 1427, 2641,
      689, 167)
- [x] Every function signature referenced verified (ProviderConfig field
      list, _to_dict/_from_dict, SpecialAgentDef, Conversation, three
      caller signatures)
- [x] All exception types considered: only `ValueError`-equivalent
      (validation), no IO or network in this change
- [x] Key structures verified (Conversation dataclass field list matches
      runtime kwarg list)
- [x] Return values handled: callers return the LLM response dict
      unchanged; only the *outbound* payload is extended
- [x] No "should work" code — each conditional is `if thinking_effort in (...)`
      with the exact same set as the validator
- [x] Files NOT changed explicitly listed (chat_handler, settings_handler,
      _PROVIDER_CALLERS, ARCHITECTURE.md)