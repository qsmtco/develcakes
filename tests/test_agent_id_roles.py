# tests/test_agent_id_roles.py
# SPEC-AGENT-ID-AND-ROLES §2.3 + §2.10.
#
# Covers:
#   - is_local_agent_key (table-driven)
#   - SpecialAgentDef.prompts default + registry loading from a YAML with
#     non-string entries filtered in _load_registry
#   - AgentRuntimeHandler._worktree_for_turn _ROOT_KEYS behavior
#     (special:coder -> None, special:coder-2 -> worktree, agent:alphacoder
#     -> worktree), following the SP2 patterns in test_agent_runtime.py.

import os
from types import SimpleNamespace
from unittest import mock

import pytest

from agent.special_agents import (
    SpecialAgentDef,
    get_special_agent,
    is_local_agent_key,
    reload_registry,
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

    agents_dir = str(tmp_path / "agents")
    src_dir = str(tmp_path / "default_agents")
    os.makedirs(src_dir, exist_ok=True)
    real_src = ad._get_default_agents_src()
    for fname in os.listdir(real_src):
        if fname.endswith((".yaml", ".yml", ".json")):
            shutil.copy2(
                os.path.join(real_src, fname), os.path.join(src_dir, fname)
            )
    monkeypatch.setattr(ad, "_get_agents_dir", lambda: agents_dir)
    monkeypatch.setattr(ad, "_get_default_agents_src", lambda: src_dir)
    reload_registry()
    yield
    reload_registry()


class TestIsLocalAgentKey:
    @pytest.mark.parametrize("key,expected", [
        ("special:coder", True),
        ("agent:alphacoder", True),
        ("", False),
        (None, False),
        ("gateway:x", False),
    ])
    def test_is_local_agent_key(self, key, expected):
        assert is_local_agent_key(key) is expected


class TestSpecialAgentDefPrompts:
    def test_prompts_defaults_to_empty_list(self):
        agent = SpecialAgentDef(
            conv_id_prefix="special:p",
            display_name="P",
            role="p",
            emoji="🤖",
            tools=["read_file"],
            can_write=False,
        )
        assert agent.prompts == []

    def test_prompts_can_be_set_explicitly(self):
        agent = SpecialAgentDef(
            conv_id_prefix="agent:alpha",
            display_name="Alpha",
            role="coder",
            emoji="🤖",
            tools=["read_file", "write_file"],
            can_write=True,
            prompts=["codeWriter.md"],
        )
        assert agent.prompts == ["codeWriter.md"]


class TestRegistryPromptsLoading:
    def test_valid_yaml_prompts_loaded(self, tmp_path):
        """A fully valid YAML def's prompts list survives validation and
        registry loading end-to-end."""
        import yaml

        import utils.agent_defs as ad

        agents_dir = ad._get_agents_dir()  # redirected temp dir by fixture
        data = {
            "name": "BetaCoder",
            "role": "coder",
            "session_key": "special:coder-beta",
            "emoji": "🤖",
            "llm_name": "local-kb",
            "fallback_provider": "openrouter",
            "tools": ["read_file", "write_file"],
            "prompts": ["system/coder.md"],
        }
        with open(os.path.join(agents_dir, "beta_coder.yaml"), "w") as f:
            yaml.safe_dump(data, f)

        reload_registry()

        agent = get_special_agent("special:coder-beta")
        assert agent is not None
        assert agent.prompts == ["system/coder.md"]

    def test_nonstring_prompts_filtered_in_load_registry(self):
        """_load_registry (§2.3) coerces and filters: non-list → [], and only
        str entries survive. Patched past validate_agent_def so the filter is
        exercised directly (YAML defs with non-string prompts are rejected by
        validation upstream and can never reach the registry)."""
        from unittest.mock import patch

        coder_def = {
            "role": "coder",
            "name": "MixedPrompts",
            "session_key": "special:mixed-prompts",
            "emoji": "🤖",
            "tools": ["read_file"],
            "prompts": ["codeWriter.md", 42, None, "steelFramedCodeWriter.md"],
        }
        with patch("utils.agent_defs.load_agent_defs", return_value=[coder_def]):
            reload_registry()
            agent = get_special_agent("special:mixed-prompts")
            assert agent is not None
            assert agent.prompts == [
                "codeWriter.md",
                "steelFramedCodeWriter.md",
            ]

    def test_nonlist_prompts_coerced_to_empty(self):
        """A non-list prompts value (e.g. a string) is coerced to []."""
        from unittest.mock import patch

        coder_def = {
            "role": "coder",
            "name": "StrPrompts",
            "session_key": "special:str-prompts",
            "emoji": "🤖",
            "tools": ["read_file"],
            "prompts": "codeWriter.md",
        }
        with patch("utils.agent_defs.load_agent_defs", return_value=[coder_def]):
            reload_registry()
            agent = get_special_agent("special:str-prompts")
            assert agent is not None
            assert agent.prompts == []

    def test_missing_prompts_key_yields_empty_list(self):
        """A definition with no prompts key loads with an empty list."""
        from unittest.mock import patch

        coder_def = {
            "role": "coder",
            "name": "NoPrompts",
            "session_key": "special:no-prompts",
            "emoji": "🤖",
            "tools": ["read_file"],
        }
        with patch("utils.agent_defs.load_agent_defs", return_value=[coder_def]):
            reload_registry()
            agent = get_special_agent("special:no-prompts")
            assert agent is not None
            assert agent.prompts == []


def _current_agents_dir():
    import utils.agent_defs as ad

    return ad._get_agents_dir()


def utils_agent_defs_path():
    import utils.agent_defs as ad

    return ad.__file__


# ── _worktree_for_turn / _ROOT_KEYS (SPEC-AGENT-ID-AND-ROLES §2.10) ──
# Patterns mirror the SP2 pins in tests/test_agent_runtime.py.

def _make_handler():
    from ui.handlers.agent_runtime_handler import AgentRuntimeHandler

    mc = mock.MagicMock()
    crh = mock.MagicMock()
    return AgentRuntimeHandler(
        main_content=mc, chat_render_handler=crh, GLib_module=None
    )


def _git_repo(tmp_path):
    import git as gitpython

    repo = gitpython.Repo.init(str(tmp_path))
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "t")
        cw.set_value("user", "email", "t@t")
    (tmp_path / "seed.txt").write_text("seed\n")
    repo.index.add(["seed.txt"])
    repo.index.commit("init")
    return repo


def _writer_def():
    return SimpleNamespace(
        display_name="Coder", role="coder", can_write=True, tools=["write_file"],
        mcp_servers=None, api_key=None, app_title=None, fallback_provider=None,
    )


def _reader_def():
    return SimpleNamespace(
        display_name="Reader", role="reviewer", can_write=False,
        tools=["read_file"],
        mcp_servers=None, api_key=None, app_title=None, fallback_provider=None,
    )


class TestRootKeysWorktree:
    def test_builtin_root_keys_stay_on_project_root(self, tmp_path):
        handler = _make_handler()
        project = tmp_path / "proj"
        project.mkdir()
        _git_repo(project)
        for sk in ("special:coder", "special:debugger", "special:supervisor"):
            assert handler._worktree_for_turn(
                sk, _writer_def(), str(project)
            ) is None

    def test_suffixed_legacy_key_gets_worktree(self, tmp_path):
        handler = _make_handler()
        project = tmp_path / "proj"
        project.mkdir()
        _git_repo(project)

        got = handler._worktree_for_turn(
            "special:coder-2", _writer_def(), str(project)
        )
        assert got is not None
        assert os.path.basename(got) == "special-coder-2"
        assert os.path.isdir(got)

    def test_agent_key_gets_worktree(self, tmp_path):
        handler = _make_handler()
        project = tmp_path / "proj"
        project.mkdir()
        _git_repo(project)

        got = handler._worktree_for_turn(
            "agent:alphacoder", _writer_def(), str(project)
        )
        assert got is not None
        assert os.path.basename(got) == "agent-alphacoder"
        assert os.path.isdir(got)

    def test_non_writer_still_no_worktree(self, tmp_path):
        handler = _make_handler()
        project = tmp_path / "proj"
        project.mkdir()
        _git_repo(project)

        assert handler._worktree_for_turn(
            "agent:alphacoder", _reader_def(), str(project)
        ) is None
