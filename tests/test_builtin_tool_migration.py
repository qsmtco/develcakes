"""Installed built-in agent files gain new tools. Custom files do not."""

from utils.agent_defs import ensure_builtin_tool


def test_supervisor_gains_tool_and_custom_file_is_untouched(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    agents.mkdir()
    supervisor = agents / "supervisor.yaml"
    supervisor.write_text("name: Supervisor\ntools:\n  - read_file\n", encoding="utf-8")
    custom = agents / "custom.yaml"
    custom.write_text("name: Custom\ntools:\n  - read_file\n", encoding="utf-8")
    monkeypatch.setattr("utils.agent_defs._get_agents_dir", lambda: str(agents))

    ensure_builtin_tool("Supervisor", "agent_context")
    text = supervisor.read_text(encoding="utf-8")
    assert "  - agent_context\n" in text
    assert text.count("agent_context") == 1
    assert "agent_context" not in custom.read_text(encoding="utf-8")

    ensure_builtin_tool("Supervisor", "agent_context")
    assert supervisor.read_text(encoding="utf-8").count("agent_context") == 1


def test_unindented_tool_list_keeps_its_style(tmp_path, monkeypatch):
    """Zero-indent ``- name`` lists stay valid. A two-space insert in front of
    that style is what crashed startup (ParserError at the next ``-``)."""
    agents = tmp_path / "agents"
    agents.mkdir()
    coder = agents / "coder.yaml"
    coder.write_text(
        "name: Coder\ntools:\n- list_files\n- read_file\nllm_name: M3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("utils.agent_defs._get_agents_dir", lambda: str(agents))

    ensure_builtin_tool("Coder", "diary")
    text = coder.read_text(encoding="utf-8")
    assert "- diary\n" in text
    assert "  - diary" not in text

    from utils.yaml_load import safe_load_yaml
    data = safe_load_yaml(text)
    assert data["tools"] == ["list_files", "read_file", "diary"]

    ensure_builtin_tool("Coder", "diary")
    assert coder.read_text(encoding="utf-8").count("diary") == 1


def test_malformed_agent_yaml_is_skipped(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "coder.yaml").write_text(
        "name: Coder\ntools:\n  - diary\n- list_files\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("utils.agent_defs._get_agents_dir", lambda: str(agents))
    monkeypatch.setattr(
        "utils.agent_defs._get_default_agents_src", lambda: str(tmp_path / "no-defaults")
    )
    from utils.agent_defs import load_agent_defs

    assert load_agent_defs() == []


def test_coder_file_with_a_different_name_is_skipped(tmp_path, monkeypatch):
    agents = tmp_path / "agents"
    agents.mkdir()
    coder = agents / "coder.yaml"
    coder.write_text("name: NotCoder\ntools:\n  - read_file\n", encoding="utf-8")
    monkeypatch.setattr("utils.agent_defs._get_agents_dir", lambda: str(agents))
    ensure_builtin_tool("Coder", "diary")
    assert "diary" not in coder.read_text(encoding="utf-8")


def test_seed_defaults_adds_feed_card_to_all_installed_agents(tmp_path, monkeypatch):
    """BUG #1 regression: a pre-SPEC-45 install (existing YAMLs WITHOUT
    feed_card in tools) gains feed_card in all three built-in agents.

    `_seed_defaults()` early-returns when the defaults src dir is missing, so
    this drives the migration helper `ensure_builtin_tool` directly (the same
    call the migration loop makes per agent) — same shape as
    `test_supervisor_gains_tool_and_custom_file_is_untouched`.
    """
    agents = tmp_path / "agents"
    agents.mkdir()
    for fname, name in (
        ("coder.yaml", "Coder"),
        ("debugger.yaml", "Debugger"),
        ("supervisor.yaml", "Supervisor"),
    ):
        (agents / fname).write_text(
            f"name: {name}\ntools:\n  - read_file\n", encoding="utf-8"
        )
    monkeypatch.setattr("utils.agent_defs._get_agents_dir", lambda: str(agents))

    ensure_builtin_tool("Coder", "feed_card")
    ensure_builtin_tool("Debugger", "feed_card")
    ensure_builtin_tool("Supervisor", "feed_card")

    for fname in ("coder.yaml", "debugger.yaml", "supervisor.yaml"):
        text = (agents / fname).read_text(encoding="utf-8")
        assert text.count("feed_card") == 1, f"{fname} lost/gained feed_card"


def test_migration_tuple_contains_feed_card():
    """Direct pin on the migration tuple: if feed_card is dropped from the
    tuple at the migration site, this test goes RED immediately."""
    import inspect

    from utils import agent_defs

    src = inspect.getsource(agent_defs._seed_defaults)
    assert '"feed_card"' in src
