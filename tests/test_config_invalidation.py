# tests/test_config_invalidation.py
# SPEC-01 Phase 1 — provider-config refresh path (fix round 2).
#
# Covers:
#   1. refresh swaps each cached runtime's _config.providers to its OWN clone
#      of the fresh values — no shared dict object and no shared value
#      objects between runtimes (BUG 6: Phase 2 mutates provider values in
#      place during live base_url/caller resolution; a shared object leaks
#      one agent's resolution into every other agent)
#   2. missing or corrupt providers.yaml keeps cached snapshots (SPEC-01 §7),
#      while an EXISTING file containing an empty list applies (BUG 2:
#      removing the last provider in Settings must actually reach runtimes —
#      load_providers() maps missing/corrupt/empty to the same [], so the
#      refresh path probes the file itself)
#   3. no-op with empty _runtimes; per-runtime try/except isolates one bad
#      runtime so the rest of the batch still updates (BUG 5)
#   4. wire_settings_handler routes on_providers_changed → on_runtimes_refresh,
#     tolerating both a missing and a raising refresh hook (BUG 5 boundary)
#   5. SettingsHandler.add_or_update/remove fires on_providers_changed
#   6. end-to-end chain with all real code: SettingsHandler.add_or_update →
#      wire_settings_handler closure → MainWindow._on_providers_changed
#      (bound to a stub window) → refresh_provider_config → runtime swap
#      (BUG 3: the chain was previously "tested" by a bare lambda spy that
#      could never fail on a broken link)
#
# No GTK — handler/wiring logic only, matching tests/test_window_settings_wiring.py.

import inspect
import logging
import os
from types import SimpleNamespace
from typing import Any

from models.providers import ProviderConfig
from ui.handlers.agent_runtime_handler import AgentRuntimeHandler
from ui.handlers.settings_handler import SettingsHandler
from ui.wiring import wire_settings_handler
from utils.providers_store import (
    get_providers_path,
    load_providers,
    save_providers,
)

_LOGGER_NAME = "ui.handlers.agent_runtime_handler"


def _make_provider(name: str = "test", **overrides) -> ProviderConfig:
    """Same shape as tests/test_settings_handler.py::_make_provider."""
    defaults: dict[str, Any] = {
        "name": name,
        "base_url": f"https://api.{name}.example.com/v1",
        "api_key": f"sk-{name}-key",
        "default_model": f"openai/{name}-model-v1",
    }
    defaults.update(overrides)
    return ProviderConfig(**defaults)


def _make_handler() -> AgentRuntimeHandler:
    """AgentRuntimeHandler constructs with None deps (headless-testable)."""
    return AgentRuntimeHandler(main_content=None, chat_render_handler=None)


def _stub_runtime(providers: dict, default_provider: str = "p1") -> SimpleNamespace:
    """Minimal stand-in for AgentRuntime: only `_config` is read by refresh."""
    return SimpleNamespace(
        _config=SimpleNamespace(providers=providers, default_provider=default_provider)
    )


def _write_yaml_raw(text: str) -> None:
    """Overwrite providers.yaml with raw text (for corrupt-file cases)."""
    with open(get_providers_path(), "w", encoding="utf-8") as f:
        f.write(text)


class TestRefreshProviderConfig:
    def test_refresh_gives_each_runtime_its_own_providers_copy(self, tmp_config_dir):
        # BUG 6: two runtimes refreshed in the same pass must NOT share the
        # fresh providers dict, and their per-key values must not share
        # objects either. Phase 2's live base_url/caller resolution mutates
        # the runtime's provider values in place; with the old
        # `rt._config.providers = fresh.providers` one agent's resolution
        # leaked into every other agent. Stub runtimes are sufficient —
        # refresh only touches rt._config.providers — and avoid spawning
        # real AgentRuntime threads in a unit test.
        save_providers([_make_provider("p1")])
        h = _make_handler()
        rt1 = _stub_runtime(providers={"p1": _make_provider("p1")})
        rt2 = _stub_runtime(providers={"p1": _make_provider("p1")})
        h._runtimes["Alpha"] = rt1
        h._runtimes["Beta"] = rt2

        save_providers([_make_provider("p1", base_url="https://new.example.com/v1")])
        h.refresh_provider_config()

        p1, p2 = rt1._config.providers, rt2._config.providers
        # The refresh actually happened (guards against a vacuous pass)…
        assert p1["p1"].base_url == "https://new.example.com/v1"
        assert p2["p1"].base_url == "https://new.example.com/v1"
        # …the dicts are distinct objects…
        assert p1 is not p2
        # …and every value is an independent clone — never an alias of the
        # fresh source or of the sibling runtime's value.
        for key in p1:
            assert p1[key] is not p2[key]

        # In-place mutation of agent A's values must not reach agent B.
        snap2 = dict(p2)
        for value in p1.values():
            value.api_key = "leaked-from-agent-a"
        for key, value in snap2.items():
            assert p2[key].api_key == value.api_key

    def test_refresh_updates_cached_runtime_providers(self, tmp_config_dir):
        # Seed providers.yaml with the OLD card, build the stub runtime off it.
        save_providers([_make_provider("p1", base_url="https://old.example.com/v1")])
        h = _make_handler()
        h._runtimes["Coder"] = _stub_runtime(
            providers={
                "p1": _make_provider("p1", base_url="https://old.example.com/v1")
            },
            default_provider="p1",
        )

        # The edit that previously required an app restart: change base_url on disk.
        save_providers([_make_provider("p1", base_url="https://new.example.com/v1")])
        h.refresh_provider_config()

        providers = h._runtimes["Coder"]._config.providers
        # Every value object in the runtime's dict must carry the new card
        # (keys repeat per card — by derived id and by display name — see
        # agent/config.py _load_providers_from_yaml).
        for value in providers.values():
            if value.name == "p1":
                assert value.base_url == "https://new.example.com/v1"
        assert providers["p1"].base_url == "https://new.example.com/v1"
        # default_provider preserved (resolvable in the fresh set)
        assert h._runtimes["Coder"]._config.default_provider == "p1"

    def test_per_agent_default_provider_override_survives_refresh(self, tmp_config_dir):
        # default_provider points at a provider that no longer exists. The
        # refresh must leave the field untouched for EVERY runtime (keep
        # semantics, no crash) — the old tautology branch that "restored" it
        # only ever assigned the field back to itself and is now deleted.
        save_providers([_make_provider("p1")])
        h = _make_handler()
        h._runtimes["Coder"] = _stub_runtime(
            providers={"p1": _make_provider("p1")}, default_provider="gone"
        )
        save_providers([_make_provider("p9")])  # 'gone' no longer resolvable
        h.refresh_provider_config()  # must not raise

        cfg = h._runtimes["Coder"]._config
        assert cfg.default_provider == "gone"  # field left as-is
        assert cfg.providers["p9"].name == "p9"
        assert cfg.providers["p9"].base_url == "https://api.p9.example.com/v1"

    def test_refresh_pulls_newly_added_provider(self, tmp_config_dir):
        save_providers([_make_provider("p1")])
        h = _make_handler()
        h._runtimes["Coder"] = _stub_runtime(providers={"p1": _make_provider("p1")})
        save_providers([_make_provider("p1"), _make_provider("p2")])
        h.refresh_provider_config()
        assert "p2" in h._runtimes["Coder"]._config.providers

    def test_refresh_reads_live_disk_state(self, tmp_config_dir):
        # Prove the refresh path re-reads disk (not a stale in-process cache):
        # write yaml, refresh, rewrite yaml with different key, refresh again.
        save_providers([_make_provider("p1", api_key="key-1")])
        h = _make_handler()
        h._runtimes["Coder"] = _stub_runtime(providers={})
        h.refresh_provider_config()
        assert h._runtimes["Coder"]._config.providers["p1"].api_key == "key-1"

        save_providers([_make_provider("p1", api_key="key-2")])
        h.refresh_provider_config()
        assert h._runtimes["Coder"]._config.providers["p1"].api_key == "key-2"

    def test_refresh_with_no_runtimes(self, tmp_config_dir):
        save_providers([_make_provider("p1")])
        h = _make_handler()
        h.refresh_provider_config()  # no-op, no exception
        assert h._runtimes == {}

    # ── BUG 2: missing/corrupt keep, existing-empty applies ─────────────────

    def test_missing_providers_yaml_keeps_cached_snapshot(self, tmp_config_dir, caplog):
        # SPEC-01 §7: a missing file must NOT wipe cached snapshots. This is
        # the keep case the empty-guard originally protected.
        save_providers([_make_provider("p1", base_url="https://old.example.com/v1")])
        h = _make_handler()
        old = _make_provider("p1", base_url="https://old.example.com/v1")
        rt = _stub_runtime(providers={"p1": old})
        h._runtimes["Coder"] = rt
        pre_values = dict(rt._config.providers)
        assert load_providers() != []  # precondition: file exists with content

        os.unlink(get_providers_path())
        assert load_providers() == []  # precondition: file is gone

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            h.refresh_provider_config()

        # Cached snapshot intact — same keys, equal values, same objects.
        providers = rt._config.providers
        assert set(providers) == set(pre_values)
        for key, old_value in pre_values.items():
            assert providers[key] == old_value
            assert providers[key] is old_value
        # The user was told why nothing changed.
        assert any(
            "missing or corrupt" in record.getMessage() for record in caplog.records
        )

    def test_corrupt_providers_yaml_keeps_cached_snapshot(self, tmp_config_dir, caplog):
        # A corrupt file parses to [] through load_providers() (the store
        # swallows the parse error), but must behave like missing, not like
        # a valid empty file.
        save_providers([_make_provider("p1", base_url="https://old.example.com/v1")])
        h = _make_handler()
        old = _make_provider("p1", base_url="https://old.example.com/v1")
        rt = _stub_runtime(providers={"p1": old})
        h._runtimes["Coder"] = rt
        pre_values = dict(rt._config.providers)
        assert load_providers() != []  # precondition

        _write_yaml_raw("{unclosed: [")  # verified live: yaml.safe_load raises
        assert load_providers() == []  # store swallows the corruption

        with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
            h.refresh_provider_config()

        providers = rt._config.providers
        assert set(providers) == set(pre_values)
        for key, old_value in pre_values.items():
            assert providers[key] is old_value
        assert any(
            "missing or corrupt" in record.getMessage() for record in caplog.records
        )

    def test_existing_empty_providers_yaml_applies(self, tmp_config_dir):
        # BUG 2: removing the last provider in Settings writes a valid,
        # parseable `[]`. The old guard treated that exactly like a missing
        # file and silently ignored the save; the cached runtime kept stale
        # providers forever. An existing parseable file must apply.
        save_providers([_make_provider("p1")])
        h = _make_handler()
        rt = _stub_runtime(
            providers={"p1": _make_provider("p1")}, default_provider="p1"
        )
        h._runtimes["Coder"] = rt
        providers = rt._config.providers
        assert providers != {}  # precondition

        save_providers([])  # disk now reports no providers
        assert load_providers() == []  # precondition: the wipe is really there
        h.refresh_provider_config()

        # In-place clear: same dict object, now empty — nothing stale left.
        assert providers == {}
        assert rt._config.providers is providers

    def test_refresh_survives_one_bad_runtime(self, tmp_config_dir, caplog):
        # BUG 5: one malformed runtime must not abort the batch — the good
        # runtime still gets the fresh config and the failure is logged.
        save_providers([_make_provider("p1", api_key="key-new")])
        h = _make_handler()
        good = _stub_runtime(providers={})
        bad = SimpleNamespace(_config=None)  # reading .providers raises
        h._runtimes["Bad"] = bad
        h._runtimes["Good"] = good

        with caplog.at_level(logging.ERROR, logger=_LOGGER_NAME):
            h.refresh_provider_config()

        assert good._config.providers["p1"].api_key == "key-new"
        assert any("Bad" in record.getMessage() for record in caplog.records)

    def test_refresh_tautology_branch_is_gone(self):
        # Fix 3 (audit BUG 1): the dead branch
        #   `if default_provider in fresh.providers or default_provider == "local-kb":`
        #   `    rt._config.default_provider = default_provider`
        # only ever assigned the field back to itself (a tautology), and the
        # direct `rt._config.providers = fresh.providers` shared-object form
        # is what BUG 6 replaced. Neither may return.
        source = inspect.getsource(AgentRuntimeHandler.refresh_provider_config)
        assert 'or default_provider == "local-kb"' not in source
        assert "rt._config.providers = fresh.providers" not in source
        assert "_parse_providers_file_strict" in source


class TestWiring:
    def test_on_providers_changed_routes_to_runtime_refresh(self, tmp_config_dir):
        # MainWindow is too heavy to construct; unit-test the wiring seam the
        # way tests/test_window_settings_wiring.py does — stubs + the real
        # wire_settings_handler. The window passes self._on_providers_changed
        # (which guards on the runtime handler) as on_runtimes_refresh.
        calls: list[int] = []
        win = SimpleNamespace(
            _on_providers_changed=lambda: calls.append(1),
            _agent_runtime_handler=None,  # wizard-ordering guard: None → no call
        )
        h = SettingsHandler()
        t = type("T", (), {"set_settings_status": lambda self, v: None})()
        wired = wire_settings_handler(
            h, t, on_runtimes_refresh=win._on_providers_changed
        )
        assert wired is h

        h.add_or_update(_make_provider("p1"))
        assert calls == [1]  # fired exactly once per save

        # The window method itself must exist and guard on a missing handler.
        from ui.window import MainWindow

        assert callable(MainWindow._on_providers_changed)

    def test_wiring_tolerates_missing_refresh_method(self, tmp_config_dir):
        # A runtime-handler stub lacking refresh_provider_config must not break
        # provider saves: the window guard AttributeError is caught by
        # wire_settings_handler's closure try/except (logged, not raised).
        from ui.window import MainWindow

        old_style = SimpleNamespace()  # no refresh_provider_config attr
        win_guard = SimpleNamespace(_agent_runtime_handler=old_style)
        guard = MainWindow._on_providers_changed.__get__(win_guard)

        h = SettingsHandler()
        t = type("T", (), {"set_settings_status": lambda self, v: None})()
        wire_settings_handler(h, t, on_runtimes_refresh=guard)
        h.add_or_update(_make_provider("p1"))  # must not raise
        assert len(h.list_providers()) == 1

    def test_wiring_tolerates_raising_refresh_hook(self, tmp_config_dir):
        # BUG 5 boundary side: a refresh hook that raises must not break the
        # provider save — wire_settings_handler logs and continues.
        from ui.window import MainWindow

        def boom() -> None:
            raise RuntimeError("refresh exploded")

        win_guard = SimpleNamespace(_agent_runtime_handler=SimpleNamespace())
        guard = MainWindow._on_providers_changed.__get__(win_guard)
        # Replace the guarded call with a raising one at the handler seam:
        # bind the guard to a stub whose refresh_provider_config raises.
        bad_arh = SimpleNamespace(refresh_provider_config=boom)
        win_guard = SimpleNamespace(_agent_runtime_handler=bad_arh)
        guard = MainWindow._on_providers_changed.__get__(win_guard)

        h = SettingsHandler()
        t = type("T", (), {"set_settings_status": lambda self, v: None})()
        wire_settings_handler(h, t, on_runtimes_refresh=guard)
        h.add_or_update(_make_provider("p1"))  # must not raise
        assert len(h.list_providers()) == 1


class TestSettingsSaveFiresCallback:
    def test_settings_save_fires_callback(self, tmp_config_dir):
        fired: list[list] = []
        h = SettingsHandler(on_providers_changed=lambda plist: fired.append(plist))
        h.add_or_update(_make_provider("p1"))
        assert len(fired) == 1
        assert [p.name for p in fired[0]] == ["p1"]

    def test_settings_remove_fires_callback(self, tmp_config_dir):
        fired: list[list] = []
        h = SettingsHandler(on_providers_changed=lambda plist: fired.append(plist))
        h.add_or_update(_make_provider("p1"))
        h.remove("p1")
        assert len(fired) == 2
        assert fired[-1] == []


class TestEndToEndChain:
    """Full production chain, all real code (BUG 3 fix).

    The old e2e test asserted only that a bare lambda spy appended a token —
    a broken link anywhere between the wiring closure and the runtime config
    swap could never fail it. These tests bind the REAL unbound
    MainWindow._on_providers_changed to a stub window holding a REAL
    AgentRuntimeHandler with a stub runtime, so every link is real code:

        SettingsHandler.add_or_update/remove
          → wire_settings_handler closure
            → MainWindow._on_providers_changed (bound)
              → AgentRuntimeHandler.refresh_provider_config
                → runtime _config.providers swap
    """

    @staticmethod
    def _make_toolbar() -> Any:
        class StubToolbar:
            def __init__(self) -> None:
                self.status_calls: list[bool] = []

            def set_settings_status(self, has_verified: bool) -> None:
                self.status_calls.append(has_verified)

        return StubToolbar()

    def _build_chain(self, tmp_config_dir):
        """Seed p1, build the real handler with one stub runtime, wire the
        real window guard through the real wiring. Returns (settings, handler,
        runtime, runtime_providers_dict)."""
        from ui.window import MainWindow

        save_providers([_make_provider("p1")])
        h = _make_handler()
        runtime = _stub_runtime(
            providers={"p1": _make_provider("p1")}, default_provider="p1"
        )
        h._runtimes["Stub"] = runtime
        providers_dict = runtime._config.providers

        window = SimpleNamespace(_agent_runtime_handler=h)
        guard = MainWindow._on_providers_changed.__get__(window)
        settings = SettingsHandler()
        wire_settings_handler(
            settings,
            self._make_toolbar(),
            settings_dialog_factory=None,
            agent_builder_factory=None,
            on_runtimes_refresh=guard,
        )
        return settings, h, runtime, providers_dict

    def test_settings_save_reaches_runtime_config_swap(self, tmp_config_dir):
        settings, h, runtime, _providers = self._build_chain(tmp_config_dir)

        # The save that previously required an app restart.
        settings.add_or_update(
            _make_provider("p1", base_url="https://new.example.com/v1")
        )

        providers = runtime._config.providers
        assert providers["p1"].base_url == "https://new.example.com/v1"
        assert providers["p1"].api_key == "sk-p1-key"
        # The runtime's dict was swapped in place (same object identity) —
        # and the cloned values are BUG 6-safe (no aliasing into the fresh
        # load_agent_config() result, which this run owns).
        assert runtime._config.providers is _providers
        assert h._runtimes["Stub"] is runtime

    def test_settings_remove_last_provider_reaches_runtimes(self, tmp_config_dir):
        # BUG 2 end-to-end: removing the last real provider through Settings
        # (whatever exists in the tmp providers.yaml) must clear the cached
        # runtime's providers.
        settings, _h, runtime, providers_dict = self._build_chain(tmp_config_dir)
        assert runtime._config.providers != {}  # precondition
        assert settings.list_providers() != []  # precondition: p1 present

        for name in [p.name for p in settings.list_providers()]:
            settings.remove(name)

        assert load_providers() == []  # the file really is an empty list
        assert providers_dict == {}
        assert runtime._config.providers is providers_dict  # cleared in place


# ── SPEC-01 Phase 2 — _call_llm live base_url/caller refresh ─────────────────
#
# The live-lookup block used to be gated on `not effective_api_key`, so an
# agent with a per-agent key never saw base_url/caller fixes (the 2026-09-20
# z.ai incident). Phase 2 decouples: the live CARD is resolved unconditionally,
# base_url/caller mutate the runtime's OWN clone in place before the
# streaming/non-streaming branch split, and api_key precedence is unchanged
# (per-agent key > live card key > frozen snapshot key).
#
# Test seams (no network, no GTK):
#   - leave caller="" on the frozen card → _call_llm raises ValueError("No
#     caller…") AFTER the mutation → assert on config.providers afterwards;
#   - set a valid caller + monkeypatch agent.runtime._get_provider to capture
#     the kwargs actually handed to the provider (api_key precedence evidence);
#   - streaming path: monkeypatch rt._call_llm_streaming (instance attribute
#     shadows the bound method) to capture its kwargs.

import pytest

from agent.config import AgentConfig, LLMProviderConfig
from agent.runtime import AgentRuntime

_RUNTIME_LOGGER = "agent.runtime"
_LIVE_URL = "https://live.example.com/v1"
_OLD_URL = "https://old.example.com/v1"


class _CapturingProvider:
    """Stands in for the real provider behind _get_provider; records kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        return {"content": "captured"}


def _make_runtime(providers: dict[str, LLMProviderConfig]) -> AgentRuntime:
    """Headless AgentRuntime: no threads start until send_message is called;
    migrate_conversation_files() failure is caught non-fatal (runtime :503)."""
    return AgentRuntime(config=AgentConfig(providers=providers, default_provider="p1"))


def _inject_conversation(rt: AgentRuntime, api_key: str | None = None) -> None:
    """_call_llm needs self._conversations[session_key] with .api_key, .model,
    .app_title — inject directly (Phase 2 instructions, verified facts)."""
    rt._conversations["s1"] = SimpleNamespace(
        api_key=api_key, model="p1/m1", app_title="t"
    )


def _patch_provider(monkeypatch) -> _CapturingProvider:
    cap = _CapturingProvider()
    monkeypatch.setattr("agent.runtime._get_provider", lambda _key: cap)
    return cap


class TestCallLlmLiveRefresh:
    def test_stale_base_url_refreshed_next_call(self, tmp_config_dir):
        # The original incident: a corrected base_url never reached a running
        # runtime. Mutation must land BEFORE the branch split — proven by the
        # no-caller ValueError firing after provider_cfg.base_url is live.
        frozen = _make_provider("p1", base_url=_OLD_URL)  # caller="" (default)
        rt = _make_runtime(providers={"p1": frozen})
        _inject_conversation(rt)
        save_providers([_make_provider("p1", base_url=_LIVE_URL)])

        with pytest.raises(ValueError, match="No caller"):
            rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])

        assert rt._config.providers["p1"].base_url == _LIVE_URL

    def test_per_agent_key_does_not_skip_base_url_refresh(
        self, tmp_config_dir, monkeypatch
    ):
        # THE Phase 2 defect: the old `if not effective_api_key:` gate meant a
        # per-agent key skipped live resolution entirely. The refresh must run
        # AND the per-agent key must still beat the live card's key.
        frozen = _make_provider("p1", base_url=_OLD_URL, caller="zai")
        rt = _make_runtime(providers={"p1": frozen})
        _inject_conversation(rt, api_key="sk-override")
        save_providers(
            [_make_provider("p1", base_url=_LIVE_URL, api_key="sk-live-key")]
        )
        cap = _patch_provider(monkeypatch)

        result = rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])

        assert result == {"content": "captured"}
        assert cap.calls, "provider.call never reached — refresh untestable"
        assert cap.calls[0]["base_url"] == _LIVE_URL  # refresh ran despite override
        assert cap.calls[0]["api_key"] == "sk-override"  # precedence unchanged

    def test_caller_refreshed_lowercased(self, tmp_config_dir, monkeypatch):
        # Caller assignment lowercases (matches _resolve_caller_key at :2053).
        # _from_dict keeps an invalid-but-present caller as-is (warning only),
        # so "ZAI" survives the yaml round-trip and reaches the mutator.
        frozen = _make_provider("p1", base_url=_LIVE_URL)  # caller="" (default)
        rt = _make_runtime(providers={"p1": frozen})
        _inject_conversation(rt)
        save_providers([_make_provider("p1", base_url=_LIVE_URL, caller="ZAI")])
        _patch_provider(monkeypatch)  # caller becomes valid mid-call; no network

        rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])

        assert rt._config.providers["p1"].caller == "zai"

    def test_no_live_match_leaves_snapshot_untouched(self, tmp_config_dir):
        # Unrelated live provider (name and default_model prefix both miss)
        # must not mutate the frozen card.
        frozen = _make_provider("p1", base_url=_OLD_URL)
        rt = _make_runtime(providers={"p1": frozen})
        _inject_conversation(rt)
        save_providers([_make_provider("other", default_model="openai/other-model")])

        with pytest.raises(ValueError, match="No caller"):
            rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])

        assert rt._config.providers["p1"].base_url == _OLD_URL
        assert rt._config.providers["p1"].caller == ""

    def test_load_providers_failure_uses_frozen_values(
        self, tmp_config_dir, monkeypatch, caplog
    ):
        # Store failure → warning logged, frozen values kept, and the call
        # proceeds to its NORMAL failure (ValueError), not a new exception type.
        frozen = _make_provider("p1", base_url=_OLD_URL)
        rt = _make_runtime(providers={"p1": frozen})
        _inject_conversation(rt)

        def boom():
            raise RuntimeError("disk on fire")

        monkeypatch.setattr("utils.providers_store.load_providers", boom)

        with (
            caplog.at_level(logging.WARNING, logger=_RUNTIME_LOGGER),
            pytest.raises(ValueError, match="No caller"),
        ):
            rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])

        assert rt._config.providers["p1"].base_url == _OLD_URL
        assert rt._config.providers["p1"].caller == ""
        assert any(
            "Cannot load providers.yaml" in r.getMessage() for r in caplog.records
        )

    def test_streaming_path_uses_refreshed_base_url(self, tmp_config_dir):
        # The mutation happens before the branch split, so the STREAMING branch
        # must also see the refreshed base_url (and lowercased caller).
        frozen = _make_provider("p1", base_url=_OLD_URL, caller="")
        frozen.supports_streaming = True
        rt = _make_runtime(providers={"p1": frozen})
        rt._on_text_delta = lambda *a, **k: None
        _inject_conversation(rt)
        save_providers([_make_provider("p1", base_url=_LIVE_URL, caller="zai")])

        captured: list[dict] = []
        rt._call_llm_streaming = lambda **kwargs: captured.append(kwargs)

        rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])

        assert captured, "streaming branch never invoked"
        assert captured[0]["base_url"] == _LIVE_URL
        assert captured[0]["caller_key"] == "zai"

    def test_live_card_empty_api_key_falls_back_to_frozen_key(
        self, tmp_config_dir, monkeypatch
    ):
        # A keyless live card still refreshes base_url, but must NOT zero out
        # the key — the frozen snapshot remains the last-resort api_key source.
        frozen = _make_provider("p1", base_url=_OLD_URL, caller="zai")
        rt = _make_runtime(providers={"p1": frozen})
        _inject_conversation(rt)
        save_providers([_make_provider("p1", base_url=_LIVE_URL, api_key="")])
        cap = _patch_provider(monkeypatch)

        rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])

        assert cap.calls[0]["base_url"] == _LIVE_URL  # card WAS matched…
        assert cap.calls[0]["api_key"] == "sk-p1-key"  # …but frozen key survived

    def test_invalid_live_caller_keeps_frozen_caller(
        self, tmp_config_dir, monkeypatch, caplog
    ):
        # A truthy-but-invalid live caller (unknown key) must NOT poison the
        # frozen snapshot: warn and keep the last known-good caller so
        # _PROVIDER_CALLERS.get still resolves downstream.
        frozen = _make_provider("p1", base_url=_OLD_URL, caller="zai")
        rt = _make_runtime(providers={"p1": frozen})
        _inject_conversation(rt)
        save_providers(
            [_make_provider("p1", base_url=_LIVE_URL, caller="NotARealCaller")]
        )
        cap = _patch_provider(monkeypatch)

        with caplog.at_level(logging.WARNING, logger=_RUNTIME_LOGGER):
            result = rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])

        assert rt._config.providers["p1"].caller == "zai"
        assert cap.calls[0]["base_url"] == _LIVE_URL  # base_url refresh unaffected
        # Downstream caller resolution still good: the call completed through
        # the patched provider (no "No caller" ValueError) — _resolve_caller_key
        # returned the KEPT frozen caller, not the invalid live value.
        assert result == {"content": "captured"}
        assert any("not a valid caller" in r.getMessage() for r in caplog.records)

    def test_whitespace_only_live_caller_keeps_frozen_caller(
        self, tmp_config_dir, monkeypatch, caplog
    ):
        # Whitespace-only is truthy-but-invalid: warn and KEEP the frozen
        # caller (strip().lower() collapses it to "" — cannot validate, so it
        # must not clobber the snapshot's last known-good caller).
        frozen = _make_provider("p1", base_url=_OLD_URL, caller="zai")
        rt = _make_runtime(providers={"p1": frozen})
        _inject_conversation(rt)
        save_providers([_make_provider("p1", base_url=_LIVE_URL, caller="   ")])
        cap = _patch_provider(monkeypatch)

        with caplog.at_level(logging.WARNING, logger=_RUNTIME_LOGGER):
            rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])

        assert rt._config.providers["p1"].caller == "zai"
        assert cap.calls[0]["base_url"] == _LIVE_URL  # refresh still ran
        assert any("not a valid caller" in r.getMessage() for r in caplog.records)

    def test_valid_live_caller_with_whitespace_is_stripped_and_applied(
        self, tmp_config_dir, monkeypatch
    ):
        # " ZAI " strips+lowers to "zai" — valid — so the override APPLIES.
        frozen = _make_provider("p1", base_url=_OLD_URL)  # caller="" (default)
        rt = _make_runtime(providers={"p1": frozen})
        _inject_conversation(rt)
        save_providers([_make_provider("p1", base_url=_LIVE_URL, caller=" ZAI ")])
        cap = _patch_provider(monkeypatch)

        result = rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])

        assert result == {"content": "captured"}  # resolved caller was valid
        assert rt._config.providers["p1"].caller == "zai"
        assert cap.calls[0]["base_url"] == _LIVE_URL

    def test_whitespace_padded_live_base_url_is_stripped_before_compare(
        self, tmp_config_dir, monkeypatch
    ):
        # Same truthy-but-whitespace class as the caller field: strip before
        # comparing so padded-but-equal URLs don't churn the snapshot, and
        # store the stripped form so downstream comparisons are consistent.
        frozen = _make_provider("p1", base_url=_OLD_URL, caller="zai")
        rt = _make_runtime(providers={"p1": frozen})
        _inject_conversation(rt)
        save_providers(
            [_make_provider("p1", base_url=f"  {_LIVE_URL}  ", caller="zai")]
        )
        cap = _patch_provider(monkeypatch)

        rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])

        assert rt._config.providers["p1"].base_url == _LIVE_URL
        assert cap.calls[0]["base_url"] == _LIVE_URL  # sent to the wire stripped

    def test_name_match_wins_regardless_of_live_list_order(
        self, tmp_config_dir, monkeypatch
    ):
        # Pins the two-pass scan order (adjudicated audit behavior): pass 1
        # matches by display name, pass 2 by default_model prefix — so the
        # name-match card wins EVEN when a prefix-matching card appears
        # FIRST in the live list. (Debugger finding #4 was REJECTED — the
        # supervisor probe proved the scan already guarantees this; this
        # test guards the property against future restructuring.)
        prefix_card = _make_provider(
            "other",
            default_model="p1/other-model",  # prefix "p1" — would match pass 2
            base_url="https://prefix-match.example.com/v1",
        )
        name_card = _make_provider(
            "p1", default_model="openai/x", base_url="https://name-match.example.com/v1"
        )
        frozen = _make_provider("p1", base_url=_OLD_URL, caller="zai")
        rt = _make_runtime(providers={"p1": frozen})
        _inject_conversation(rt)  # model="p1/m1" → provider_name="p1"
        save_providers([prefix_card, name_card])  # prefix-match card FIRST
        cap = _patch_provider(monkeypatch)

        rt._call_llm("s1", [{"role": "user", "content": "hi"}], [])

        # A naive first-match scan would have taken prefix_card (it appears
        # first AND its default_model prefix is "p1"); the two-pass scan
        # must resolve to the name-match card instead.
        assert (
            rt._config.providers["p1"].base_url == "https://name-match.example.com/v1"
        )
        assert cap.calls[0]["base_url"] == "https://name-match.example.com/v1"
