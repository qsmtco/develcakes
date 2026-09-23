# tests/test_special_agents.py
# Tests for agent/special_agents.py — agent definition registry.
#
# Tests the YAML-loaded registry, self-improvement config resolution,
# and backward compatibility with programmatic SpecialAgentDef creation.

import os

import pytest

from agent.special_agents import (
    SpecialAgentDef,
    get_special_agent,
    get_special_agents,
    reload_registry,
    _ensure_loaded,
)


@pytest.fixture(autouse=True)
def fresh_registry(tmp_path, monkeypatch):
    """Ensure a fresh, isolated registry for each test.

    Redirects agent dirs to a temp dir BEFORE reload_registry() triggers
    _seed_defaults(), so the built-in YAMLs are never copied into the real
    user config dir on every pytest run. The temp source dir holds copies of
    the real built-in defaults so registry loading reflects production.
    """
    import shutil
    import utils.agent_defs as ad

    # Redirect agent dirs to temp BEFORE reload_registry() triggers _seed_defaults()
    agents_dir = str(tmp_path / "agents")
    src_dir = str(tmp_path / "default_agents")
    os.makedirs(src_dir, exist_ok=True)
    # Copy the real built-in defaults so registry loading reflects production
    real_src = ad._get_default_agents_src()
    for fname in os.listdir(real_src):
        if fname.endswith((".yaml", ".yml", ".json")):
            shutil.copy2(os.path.join(real_src, fname), os.path.join(src_dir, fname))
    monkeypatch.setattr(ad, "_get_agents_dir", lambda: agents_dir)
    monkeypatch.setattr(ad, "_get_default_agents_src", lambda: src_dir)
    reload_registry()
    yield
    reload_registry()


class TestSpecialAgentDef:
    def test_create_with_minimal_fields(self):
        """Creating with the required fields works; llm_name defaults to None."""
        agent = SpecialAgentDef(
            conv_id_prefix="special:test",
            display_name="Test",
            role="test",
            emoji="🔬",
            tools=["read_file"],
            can_write=False,
        )
        assert agent.llm_name is None
        assert agent.self_improvement == {}

    def test_create_with_llm_name(self):
        agent = SpecialAgentDef(
            conv_id_prefix="special:custom",
            display_name="Custom",
            role="custom",
            emoji="🤖",
            tools=["read_file", "write_file"],
            can_write=True,
            llm_name="minimax",
            self_improvement={"bug_journal": False},
        )
        assert agent.llm_name == "minimax"
        assert agent.self_improvement["bug_journal"] is False


class TestGetSelfImprovementConfig:
    def test_writer_defaults(self):
        agent = SpecialAgentDef(
            conv_id_prefix="special:w",
            display_name="Writer",
            role="writer",
            emoji="✏️",
            tools=["read_file", "write_file"],
            can_write=True,
        )
        cfg = agent.get_self_improvement_config()
        assert cfg["enforcement"] is True  # can_write=True
        assert cfg["bug_journal"] is True

    def test_reader_defaults(self):
        agent = SpecialAgentDef(
            conv_id_prefix="special:r",
            display_name="Reader",
            role="reader",
            emoji="📖",
            tools=["read_file"],
            can_write=False,
        )
        cfg = agent.get_self_improvement_config()
        assert cfg["enforcement"] is False  # can_write=False

    def test_override_applies(self):
        agent = SpecialAgentDef(
            conv_id_prefix="special:o",
            display_name="Override",
            role="override",
            emoji="🔧",
            tools=["read_file", "write_file"],
            can_write=True,
            self_improvement={"enforcement": False, "dream_consolidation": True},
        )
        cfg = agent.get_self_improvement_config()
        assert cfg["enforcement"] is False       # overridden
        assert cfg["dream_consolidation"] is True  # overridden
        assert cfg["bug_journal"] is True         # default


class TestRegistry:
    def test_loads_coder_and_debugger(self):
        """Coder and Debugger load from the default agents fixtures (mocked)."""
        from unittest.mock import patch
        # Use the project's own prompts/default_agents/ fixtures, patched to
        # match the new "all agents need a fallback" contract.
        defaults_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "prompts", "default_agents",
        )
        coder_path = os.path.join(defaults_dir, "coder.yaml")
        debugger_path = os.path.join(defaults_dir, "debugger.yaml")
        # Read the project's fixtures directly; they already include fallback_provider.
        import yaml as _yaml
        with open(coder_path) as f:
            coder = _yaml.safe_load(f)
        with open(debugger_path) as f:
            debugger = _yaml.safe_load(f)
        with patch("utils.agent_defs.load_agent_defs", return_value=[coder, debugger]):
            reload_registry()
            agents = get_special_agents()
            names = [a.display_name for a in agents]
            assert "Coder" in names
            assert "Debugger" in names

    def test_coder_has_write_tools(self):
        from unittest.mock import patch
        coder_def = {
            "role": "coder",
            "name": "Coder",
            "emoji": "🛠️",
            "tools": ["read_file", "write_file", "edit_file", "exec_command",
                      "list_files", "search_files", "web_search", "web_fetch"],
            "provider": "minimax",
            "model": "MiniMax-M2.7",
        }
        with patch("utils.agent_defs.load_agent_defs", return_value=[coder_def]):
            reload_registry()
            coder = get_special_agent("special:coder")
            assert coder is not None
            assert "write_file" in coder.tools
            assert coder.can_write is True

    def test_debugger_has_write_tools(self):
        """Debugger is write-capable as of DEBUGGER-WRITE-TOOL Phase 1.

        DESIGN CHANGE (deliberate inversion of `test_debugger_no_write_tools`):
        the shipped debugger.yaml used to list only read tools, but that
        read-only posture was nominal — `exec_command` could always write, and
        agents used `cat > file <<EOF` heredocs for probe scripts and audit
        scratch. Those heredocs surface as single approval cards containing the
        ENTIRE file (22,714 chars measured live on 2026-09-12), which is both an
        approval-UX problem (the PM must read a 22 KB command to approve a
        probe) and a pango cost problem (T2: text measurement was 60.1% of
        main-thread burn).

        Granting write_file/edit_file is the audited path: a named tool call
        with a small card, a structured audit-log record, and no approval prompt
        (exec_command always requires one). The investigate/diagnose/report
        mandate is unchanged in prompts/system/debugger.md, which now states the
        heredoc ban explicitly.
        """
        debugger = get_special_agent("special:debugger")
        assert debugger is not None
        assert "write_file" in debugger.tools
        assert "edit_file" in debugger.tools
        assert debugger.can_write is True

    def test_get_nonexistent_returns_none(self):
        assert get_special_agent("special:nonexistent") is None

    def test_coder_has_llm_name(self):
        from unittest.mock import patch
        coder_def = {
            "role": "coder",
            "name": "Coder",
            "emoji": "🛠️",
            "tools": ["read_file", "write_file"],
            "llm_name": "minimax",
        }
        with patch("utils.agent_defs.load_agent_defs", return_value=[coder_def]):
            reload_registry()
            coder = get_special_agent("special:coder")
            assert coder.llm_name == "minimax"

    def test_coder_si_full_stack(self):
        from unittest.mock import patch
        coder_def = {
            "role": "coder",
            "name": "Coder",
            "emoji": "🛠️",
            "tools": ["read_file", "write_file"],
            "provider": "minimax",
            "model": "MiniMax-M2.7",
            "self_improvement": {
                "bug_journal": True,
                "project_rules": True,
                "enforcement": True,
                "structured_feedback": True,
                "dream_consolidation": True,
            },
        }
        with patch("utils.agent_defs.load_agent_defs", return_value=[coder_def]):
            reload_registry()
            coder = get_special_agent("special:coder")
            si = coder.get_self_improvement_config()
            assert si["bug_journal"] is True
            assert si["enforcement"] is True
            assert si["structured_feedback"] is True
            assert si["dream_consolidation"] is True

    def test_debugger_si_context_only(self):
        debugger = get_special_agent("special:debugger")
        si = debugger.get_self_improvement_config()
        assert si["enforcement"] is False
        assert si["structured_feedback"] is False
        assert si["dream_consolidation"] is False

    def test_reload_clears_and_reloads(self):
        agents1 = get_special_agents()
        reload_registry()
        agents2 = get_special_agents()
        # Should reload same agents
        assert len(agents1) == len(agents2)
        assert [a.display_name for a in agents1] == [a.display_name for a in agents2]


class TestSupervisorDef:
    """Tests for the built-in Supervisor agent definition (SOR Phase 1).

    Supervisor is manually added by the user (auto_add_to_projects: false)
    and is write-capable (write_file/edit_file in tools).
    """

    @pytest.fixture
    def supervisor_def_present(self):
        """Copy the built-in supervisor.yaml into the agent dir and reload.

        Reload_registry may seed supervisor.yaml depending on test ordering;
        copy the real built-in YAML directly to guarantee the registry sees it
        and to refresh any stale user copy to the current built-in (Phase 2
        built-in set).
        """
        import shutil
        from utils.agent_defs import _get_agents_dir

        agents_dir = _get_agents_dir()
        os.makedirs(agents_dir, exist_ok=True)
        defaults = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "prompts", "default_agents",
        )
        src = os.path.join(defaults, "supervisor.yaml")
        dst = os.path.join(agents_dir, "supervisor.yaml")
        shutil.copy2(src, dst)
        reload_registry()
        yield
        try:
            if os.path.exists(dst):
                os.remove(dst)
        finally:
            reload_registry()

    def test_supervisor_loads(self, supervisor_def_present):
        sup = get_special_agent("special:supervisor")
        assert sup is not None
        assert sup.role == "supervisor"
        assert sup.display_name == "Supervisor"
        assert sup.auto_add_to_projects is False

    def test_supervisor_can_write_derived_from_tools(self, supervisor_def_present):
        sup = get_special_agent("special:supervisor")
        assert sup is not None
        assert "write_file" in sup.tools
        assert "edit_file" in sup.tools
        assert sup.can_write is True

    def test_supervisor_prompt_exists(self, supervisor_def_present):
        prompt_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "prompts", "system", "supervisor.md",
        )
        assert os.path.isfile(prompt_path)
        with open(prompt_path, encoding="utf-8") as f:
            content = f.read()
        assert content.strip() != ""


class TestSpecialAgentColorStability:
    """Tests for models/colors.color_for_special_agent() — the stable
    per-role color cache introduced in Phase 1 of SPEC-AGENT-COLOR-STABILITY.

    These tests verify:
      - Same role returns the same color across reload_registry() calls.
      - Empty role returns the deterministic default.
      - Gateway reconnect (reset_color_indices) does not reset special-agent
        colors.
    """

    @pytest.fixture(autouse=True)
    def reset_color_cache(self):
        """Reset the module-level _SPECIAL_AGENT_COLORS cache and the
        _agent_color_next counter before each test in this class, so test
        order does not affect results."""
        from models.colors import _SPECIAL_AGENT_COLORS, _agent_color_next
        _SPECIAL_AGENT_COLORS.clear()
        # Save and restore _agent_color_next so other tests aren't disturbed.
        import models.colors as colors_mod
        saved_next = colors_mod._agent_color_next
        colors_mod._agent_color_next = 0
        yield
        _SPECIAL_AGENT_COLORS.clear()
        colors_mod._agent_color_next = saved_next

    def test_color_stable_across_reload(self):
        """Reload registry; same roles get the same color."""
        from models.colors import color_for_special_agent
        reload_registry()
        first_colors = {a.role: color_for_special_agent(a.role) for a in get_special_agents()}
        reload_registry()
        second_colors = {a.role: color_for_special_agent(a.role) for a in get_special_agents()}
        assert first_colors == second_colors
        # At least one role should be assigned a real color
        assert all(c.startswith("#") for c in first_colors.values())
        # At least one role must have been assigned (sanity check that
        # the YAML registry is not empty).
        assert len(first_colors) >= 1

    def test_color_deterministic_for_empty_role(self):
        """Empty role returns the deterministic default without touching the cache."""
        from models.colors import color_for_special_agent, _SPECIAL_AGENT_COLORS
        assert color_for_special_agent("") == "#6366f1"
        assert color_for_special_agent("") == "#6366f1"  # idempotent
        # Empty role must NOT pollute the cache
        assert "" not in _SPECIAL_AGENT_COLORS

    def test_color_persists_across_reset_color_indices(self):
        """Gateway reconnect (reset_color_indices) does not reset special-agent colors."""
        from models.colors import color_for_special_agent, reset_color_indices
        # First call assigns from palette
        c1 = color_for_special_agent("test_role_x")
        # Simulate gateway reconnect
        reset_color_indices()
        # Same role returns the same color
        c2 = color_for_special_agent("test_role_x")
        assert c1 == c2
        # Color is a real palette entry, not the empty-role default
        assert c1.startswith("#")
        assert len(c1) == 7  # "#RRGGBB"


class TestSeedDefaultsPerFile:
    """Tests for per-file default seeding in utils.agent_defs._seed_defaults.

    _seed_defaults() must copy each missing built-in agent file into the user
    agents dir independently — an unrelated existing user agent must not
    suppress seeding of a missing built-in (e.g. supervisor.yaml reaching
    existing users), and existing user files must never be overwritten.
    """

    @pytest.fixture
    def iso_agents_dir(self, monkeypatch, tmp_path):
        """Redirect the agent defs dir and default source to temp dirs."""
        import utils.agent_defs as ad

        agents_dir = str(tmp_path / "agents")
        src_dir = str(tmp_path / "default_agents")
        os.makedirs(src_dir, exist_ok=True)

        def _write_default(fname, name):
            with open(os.path.join(src_dir, fname), "w", encoding="utf-8") as f:
                f.write(
                    "# built-in\n"
                    f"name: {name}\n"
                    f"role: {name.lower()}\n"
                    "prompts: [system/coder.md]\n"
                    "tools: [read_file]\n"
                    "llm_name: openrouter\n"
                    "fallback_provider: openrouter\n"
                )

        # Populate the default source with the built-in set.
        _write_default("coder.yaml", "Coder")
        _write_default("debugger.yaml", "Debugger")
        _write_default("supervisor.yaml", "Supervisor")

        monkeypatch.setattr(ad, "_get_agents_dir", lambda: agents_dir)
        monkeypatch.setattr(ad, "_get_default_agents_src", lambda: src_dir)
        return agents_dir

    def _write_user(self, agents_dir, fname, name):
        os.makedirs(agents_dir, exist_ok=True)
        with open(os.path.join(agents_dir, fname), "w", encoding="utf-8") as f:
            f.write(
                f"name: {name}\n"
                f"role: {name.lower()}\n"
                "prompts: [system/coder.md]\n"
                "tools: [read_file]\n"
                "llm_name: openrouter\n"
                "fallback_provider: openrouter\n"
            )

    def test_seed_with_unrelated_user_agent_copies_supervisor(self, iso_agents_dir):
        import utils.agent_defs as ad

        # One unrelated user agent exists; supervisor.yaml is missing.
        self._write_user(iso_agents_dir, "custom.yaml", "Custom")
        ad._seed_defaults()
        super_path = os.path.join(iso_agents_dir, "supervisor.yaml")
        assert os.path.isfile(super_path)
        # The unrelated file is preserved.
        assert os.path.isfile(os.path.join(iso_agents_dir, "custom.yaml"))

    def test_seed_does_not_overwrite_existing_user_supervisor(self, iso_agents_dir):
        import utils.agent_defs as ad

        # User has a customized supervisor.yaml with a distinctive name.
        self._write_user(iso_agents_dir, "supervisor.yaml", "MySupervisor")
        ad._seed_defaults()
        with open(os.path.join(iso_agents_dir, "supervisor.yaml"), encoding="utf-8") as f:
            content = f.read()
        # The built-in did NOT overwrite the user's custom file.
        assert "MySupervisor" in content
        assert "name: Supervisor" not in content

    def test_seed_preserves_unrelated_user_file(self, iso_agents_dir):
        import utils.agent_defs as ad

        # Unrelated user file with custom content.
        self._write_user(iso_agents_dir, "custom.yaml", "Custom")
        ad._seed_defaults()
        with open(os.path.join(iso_agents_dir, "custom.yaml"), encoding="utf-8") as f:
            content = f.read()
        assert "Custom" in content
        # Other built-ins (coder/debugger) seeded alongside it.
        for fname in ("coder.yaml", "debugger.yaml"):
            assert os.path.isfile(os.path.join(iso_agents_dir, fname)), fname

