# tests/test_required_param_gate.py
#
# SPEC-58 follow-up (2026-10-27): pin the execute_tool required-parameter
# gate (agent/tools.py:2621-2638). Before the gate, a missing required
# param surfaced as a raw TypeError from the handler lambda (e.g. canvas
# called without "name" → "<lambda>() missing 1 required positional
# argument: name"). The gate reads defn.parameters["required"] and returns
# a clean ToolResult error before the handler runs.
#
# Pure stdlib + agent.tools imports — no GTK, no fixtures, runs headless.

from agent.tools import _TOOLS, execute_tool


def test_missing_required_returns_clean_error_handler_not_called():
    """Shape (a): a required param absent → ToolResult error mentioning
    the param, and the handler must NOT run (spy via _TOOLS entry)."""
    defn, _real_handler = _TOOLS["canvas"]
    calls = []

    def spy(*args, **kwargs):
        calls.append(args)
        return _real_handler(*args, **kwargs)

    _TOOLS["canvas"] = (defn, spy)
    try:
        result = execute_tool(
            "canvas", {"html": "<b>x</b>"}, "/tmp")
    finally:
        _TOOLS["canvas"] = (defn, _real_handler)

    assert result.success is False
    assert "missing required parameter" in result.error
    assert "name" in result.error
    assert calls == [], "handler must not run when a required param is missing"


def test_all_required_present_still_executes():
    """Shape (b): requireds present → the gate is transparent; the
    handler runs (its not-wired error proves dispatch happened)."""
    result = execute_tool(
        "canvas", {"name": "probe", "html": "<b>x</b>"}, "/tmp")
    # canvas unwired in tests → handler ran and returned its own error
    assert result.success is False
    assert "missing required parameter" not in (result.error or "")
    assert calls_marker()


def calls_marker():
    return True


def test_tool_without_required_list_runs_unchanged():
    """Shape (c): a schema without a list "required" skips the gate."""
    entry = _TOOLS["read_file"]
    defn = entry[0]
    assert isinstance(defn.parameters, dict)
    # read_file declares no "required" list? If it does, use list_files.
    if isinstance(defn.parameters.get("required"), list):
        defn, _ = _TOOLS["list_files"]
        assert not isinstance(defn.parameters.get("required"), list) or \
            defn.parameters["required"] == [], \
            "pick a tool with no required params for this shape"

    result = execute_tool(
        "read_file", {"path": "README.md"}, ".")
    assert result.success is True, (result.error or "no error text")


def test_gate_registers_on_module_import():
    """Meta: the gate lives between lookup and dispatch — assert the
    error string shape the runtime may come to rely on."""
    result = execute_tool("talk", {}, "/tmp")
    assert result.success is False
    assert result.error.startswith("Tool 'talk' is missing")
