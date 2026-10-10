# ui/handlers/settings_handler.py
# Settings dialog logic — owns the provider list, save/delete/test operations,
# and the red-dot status check. Bridges the GTK view (Phase 6) and the data
# store (Phase 1). Pure logic — no GTK widgets, only GLib.idle_add for thread dispatch.
#
# Manifest:
#   - Reads: <config_dir>/providers.yaml
#   - Writes: <config_dir>/providers.yaml (via utils.providers_store)
#   - Network: yes (Test Connection, via utils.provider_test in a daemon thread)
#   - Imports: stdlib threading/datetime, utils.providers_store, utils.provider_test,
#              models.providers
#   - Does NOT import gi.repository.Gtk — only optionally uses GLib for idle_add

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable

from models.providers import ProviderConfig
from agent.runtime import get_valid_callers
from utils.providers_store import (
    load_providers,
    save_providers,
    has_any_verified_provider,
)
from utils.provider_test import test_connection, TestResult

logger = logging.getLogger(__name__)


class SettingsHandler:
    """Settings dialog logic handler.

    Owns the list of providers, save/delete/test operations, and the red-dot
    status check. Wires Test Connection to utils/provider_test.

    Args:
        GLib_module: gi.repository.GLib — for idle_add dispatch of test results.
            If None, callbacks fire synchronously (test mode).
        parent_window: Gtk.Window — for transient_for on confirmations (future use).
        on_providers_changed: Called with the new list when providers are
            added/removed/edited. UI uses this to re-render.
        on_status_changed: Called with True if any provider is verified.
            Window uses this to update the toolbar red dot.
    """

    def __init__(
        self,
        *,
        GLib_module=None,
        parent_window=None,
        on_providers_changed: Callable[[list[ProviderConfig]], None] | None = None,
        on_status_changed: Callable[[bool], None] | None = None,
    ):
        self._GLib = GLib_module
        self._parent_window = parent_window
        self._on_providers_changed = on_providers_changed
        self._on_status_changed = on_status_changed

        # Ensure providers.yaml exists when the Settings UI is constructed.
        # This puts the file-creation side effect in the right place — the UI
        # that actually writes to it — rather than in agent.config.load_agent_config
        # (which is a read operation).
        try:
            from agent.config import ensure_providers_yaml_exists
            from utils.config import get_config_dir
            import os as _os
            _config_path = _os.path.join(get_config_dir(), "agent.json")
            ensure_providers_yaml_exists(_config_path)
        except Exception as e:
            logger.warning("Could not ensure providers.yaml exists: %s", e)

    def list_providers(self) -> list[ProviderConfig]:
        """Load from providers.yaml. Pure read — no I/O beyond yaml."""
        return load_providers()

    def add_or_update(self, provider: ProviderConfig) -> None:
        """Validate fields non-empty, then save. Fires on_providers_changed.

        Raises ValueError on invalid input. Replaces existing entry with
        the same name (per providers_store.add_provider semantics).
        """
        if not provider.name or not provider.name.strip():
            raise ValueError("Provider name is required")
        if not provider.base_url or not provider.base_url.strip():
            raise ValueError("Base URL is required")
        if not provider.api_key or not provider.api_key.strip():
            raise ValueError("API key is required")
        if not provider.default_model or not provider.default_model.strip():
            raise ValueError("Default model is required")

        # PHASE-10: auto-detect caller from default_model prefix when not set.
        # Strict caller validation: the prefix MUST lowercased and the result
        # MUST be in the valid-caller taxonomy. The same check applies to
        # explicitly-set callers (user may have hand-typed a bad value).
        # See .crabcakes/task-specs/caller-validation.md for the design.
        if not provider.caller and provider.default_model and "/" in provider.default_model:
            provider.caller = provider.default_model.split("/")[0].lower()
        elif not provider.caller:
            # Gap fix: default_model has no "/" (e.g. "gpt-4o") and caller
            # is empty — auto-detect cannot derive a caller. Surface a clear
            # error at save time rather than letting the empty string through
            # to runtime where it manifests as opaque "No streaming caller".
            raise ValueError(
                "Cannot auto-detect caller: default_model must be '<vendor>/<model>'. "
                "Set caller explicitly or use a prefixed model."
            )
        # PROV-CALLER-CONSISTENCY (regression for Sonnet 5: `caller=anthropic`
        # with `base_url=openrouter.ai` and `model=openrouter/claude-sonnet-5`):
        # when the model carries a `<vendor>/` prefix, the caller MUST match
        # it. A hand-set wrong caller (e.g. anthropic) silently slipped through
        # the taxonomy check above because anthropic IS a valid caller — but
        # it routes the request to /messages, which 404s on the OpenAI-compat
        # API. Prefer the prefix as the source of truth.
        if (
            provider.default_model
            and "/" in provider.default_model
            and provider.caller
        ):
            prefix = provider.default_model.split("/")[0].lower()
            if prefix in get_valid_callers() and prefix != provider.caller:
                logger.warning(
                    "Provider %r has caller=%r but default_model prefix=%r; "
                    "correcting caller to %r. To override, use a model whose "
                    "prefix matches the caller's taxonomy key.",
                    provider.name, provider.caller, prefix, prefix,
                )
                provider.caller = prefix
        if provider.caller and provider.caller not in get_valid_callers():
            valid = sorted(get_valid_callers())
            raise ValueError(
                f"Invalid caller {provider.caller!r}. "
                f"Valid callers: {', '.join(valid)}. "
                f"Set the caller field explicitly or use a model with a recognized prefix."
            )

        providers = load_providers()
        # Replace existing or append
        providers = [p for p in providers if p.name != provider.name]
        providers.append(provider)
        save_providers(providers)

        logger.info("Saved provider: %s", provider.name)
        if self._on_providers_changed:
            self._on_providers_changed(providers)
        if self._on_status_changed:
            self._on_status_changed(has_any_verified_provider(providers))

    def remove(self, name: str) -> None:
        """Remove by name. No-op if not found. Fires on_providers_changed
        and on_status_changed if the last verified provider was removed."""
        providers = load_providers()
        if not any(p.name == name for p in providers):
            logger.debug("Remove: provider '%s' not found", name)
            return
        providers = [p for p in providers if p.name != name]
        save_providers(providers)
        logger.info("Removed provider: %s", name)
        if self._on_providers_changed:
            self._on_providers_changed(providers)
        if self._on_status_changed:
            self._on_status_changed(has_any_verified_provider(providers))

    def test_provider(
        self,
        provider: ProviderConfig,
        on_result: Callable[[TestResult], None],
    ) -> None:
        """Run test_connection in a daemon thread; dispatch result to on_result
        via GLib.idle_add if available. On success: stamps last_verified_at and
        clears last_error, then saves. On failure: stamps last_error, saves.
        Always fires on_status_changed after.
        """
        def _worker():
            # PHASE-10: auto-detect caller from default_model prefix when not set.
            # Mirrors add_or_update (lines 93-95); lets us self-heal providers whose
            # YAML entry has an empty/absent caller (the post-regression state).
            # Caller validation (caller-validation spec): same lowercase + taxonomy
            # check as add_or_update. On invalid caller, return a failed TestResult
            # instead of raising (this is a daemon thread — exceptions get swallowed).
            if not provider.caller and provider.default_model and "/" in provider.default_model:
                provider.caller = provider.default_model.split("/")[0].lower()
            elif not provider.caller:
                # Gap fix: default_model has no "/" and caller is empty — auto-detect
                # cannot derive a caller. Return a failed TestResult (do NOT raise —
                # this is a daemon thread; exceptions get swallowed).
                self._dispatch_test_result(
                    provider,
                    TestResult(
                        ok=False,
                        latency_ms=0,
                        error=(
                            "Cannot auto-detect caller: default_model must be "
                            "'<vendor>/<model>'. Set caller explicitly or use a "
                            "prefixed model."
                        ),
                        model_used=provider.default_model,
                    ),
                    on_result=on_result,
                )
                return
            if provider.caller and provider.caller not in get_valid_callers():
                valid = sorted(get_valid_callers())
                result = TestResult(
                    ok=False,
                    latency_ms=0,
                    error=(
                        f"Invalid caller {provider.caller!r}. "
                        f"Valid callers: {', '.join(valid)}. "
                        f"Set the caller field explicitly or use a model with a recognized prefix."
                    ),
                    model_used=provider.default_model,
                )
                self._dispatch_test_result(provider, result, on_result)
                return  # do not call test_connection with an invalid caller

            try:
                result = test_connection(
                    base_url=provider.base_url,
                    api_key=provider.api_key,
                    model=provider.default_model,
                    caller=provider.caller or None,
                )
            except Exception as e:
                # test_connection itself raised (e.g. unknown provider)
                result = TestResult(
                    ok=False,
                    latency_ms=0,
                    error=f"test_connection raised: {e}",
                    model_used=provider.default_model,
                )

            # Stamp the result on the provider
            providers = load_providers()
            for i, p in enumerate(providers):
                if p.name == provider.name:
                    if result.ok:
                        # Pre-fill max_tokens from /v1/models probe ONLY if
                        # user hasn't customized. Sentinel conditions:
                        #   (a) max_tokens == 128_000 (dataclass default) AND
                        #       default_max_tokens == 0 (no wizard stamp).
                        # The wizard stamps default_max_tokens with the
                        # caller-specific value to mark "this is intentional",
                        # so we treat (default_max_tokens > 0) as a deliberate
                        # wizard-set choice that must not be overwritten even
                        # when max_tokens happens to equal 128K — audit BUG #7.
                        user_has_customized = (
                            p.max_tokens != 128_000
                            or (p.default_max_tokens or 0) > 0
                        )
                        new_max_tokens = (
                            result.context_window
                            if (result.context_window and not user_has_customized)
                            else p.max_tokens
                        )
                        providers[i] = replace(
                            p,
                            max_tokens=new_max_tokens,
                            last_verified_at=datetime.now(timezone.utc).isoformat(),
                            last_error=None,
                        )
                    else:
                        providers[i] = replace(
                            p,
                            last_error=result.error or "unknown",
                        )
                    break
            save_providers(providers)

            # Dispatch result back to the main thread
            def _dispatch():
                try:
                    on_result(result)
                except Exception:
                    logger.exception("test_provider on_result callback raised")
                if self._on_status_changed:
                    self._on_status_changed(has_any_verified_provider(load_providers()))

            if self._GLib is not None and hasattr(self._GLib, "idle_add"):
                self._GLib.idle_add(_dispatch)
            else:
                _dispatch()  # test path: synchronous

        t = threading.Thread(target=_worker, daemon=True, name=f"test-{provider.name}")
        t.start()

    def _dispatch_test_result(
        self,
        provider: ProviderConfig,
        result: TestResult,
        on_result: Callable[[TestResult], None],
    ) -> None:
        """Stamp the result on the provider, save, and dispatch the callback.

        Used by test_provider._worker for failure paths (invalid caller, empty
        caller gap, etc.) where we need to short-circuit BEFORE test_connection
        runs. Shared between branches to keep the dispatch logic in one place.

        Stamps `last_error` on the persisted provider (so the red-dot indicator
        reflects the bad state), saves, then dispatches on_result and
        on_status_changed via the main thread.

        Preserves `caller=p.caller` per the Bug #2/#3 fix — caller must
        round-trip through save unchanged.
        """
        providers = load_providers()
        for i, p in enumerate(providers):
            if p.name == provider.name:
                providers[i] = replace(
                    p,
                    last_error=result.error or "unknown",
                )
                break
        save_providers(providers)

        def _dispatch():
            try:
                on_result(result)
            except Exception:
                logger.exception("test_provider on_result callback raised")
            if self._on_status_changed:
                self._on_status_changed(has_any_verified_provider(load_providers()))

        if self._GLib is not None and hasattr(self._GLib, "idle_add"):
            self._GLib.idle_add(_dispatch)
        else:
            _dispatch()

    def status_has_verified(self) -> bool:
        """Return True if any provider is verified (drives the red dot)."""
        return has_any_verified_provider(load_providers())
