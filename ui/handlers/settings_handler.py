# ui/handlers/settings_handler.py
# Settings dialog logic — owns the provider list, save/delete/test operations,
# and the red-dot status check. Bridges the GTK view (Phase 6) and the data
# store (Phase 1). Pure logic — no GTK widgets, only GLib.idle_add for thread dispatch.
#
# Manifest:
#   - Reads: <config_dir>/providers.yaml
#   - Writes: <config_dir>/providers.yaml (via utils.providers_store)
#   - Network: yes (Test Connection, via utils.provider_test in a daemon thread;
#     Load Models, via utils.model_catalog.fetch_model_ids in a daemon thread)
#   - Imports: stdlib threading/datetime, utils.providers_store, utils.provider_test,
#              utils.provider_presets, utils.provider_errors, utils.model_catalog,
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
from utils.provider_errors import explain_test_error
from utils.provider_presets import get_preset, preset_for, preset_vendor_present
from utils.model_catalog import ModelListResult, fetch_model_ids

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

    def add_or_update(self, provider: ProviderConfig, *, allow_empty_caller: bool = False) -> None:
        """Validate fields non-empty, then save. Fires on_providers_changed.

        Raises ValueError on invalid input. Replaces existing entry with
        the same name (per providers_store.add_provider semantics).

        ``allow_empty_caller`` (SPEC-22 §2.8): the DecisionModel/JEV card keeps
        an empty caller — its adapter (Stage 2) sets its own; we do not invent
        caller detection for it. Every other card rejects an undetectable caller.
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
        #
        # SPEC-22 §2.8: the DecisionModel/JEV card skips caller handling
        # entirely (allow_empty_caller) — its Stage-2 adapter sets its own
        # caller, and we do not invent detection for a card that is not a
        # chat provider. Its model slug may carry a vendor prefix the general
        # taxonomy does not know.
        if not allow_empty_caller:
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
        # SPEC-62 fix-round 4 RULING 3: save_providers is the SINGLE OWNER of
        # global-default maintenance (adoption, stale healing, bare-model
        # refresh). This path passes ``prefer`` so a brand-new chat card is
        # adopted ahead of first-in-list ordering; the former item-4 /
        # fix-round-B block here was deleted — its logic lives in the choke
        # point now (including the fix-round-B same-card model refresh,
        # branch 4 of the choke block).
        save_providers(providers, prefer=provider.name)

        logger.info("Saved provider: %s", provider.name)
        if self._on_providers_changed:
            self._on_providers_changed(providers)
        if self._on_status_changed:
            self._on_status_changed(has_any_verified_provider(providers))

    def remove(self, name: str) -> None:
        """Remove by name. No-op if not found. Fires on_providers_changed
        and on_status_changed if the last verified provider was removed.

        SPEC-62 fix-round 4 RULING 3: no default-maintenance logic here —
        save_providers (allow_empty=True, the explicit-shrink path) retargets
        or clears the global default itself when the removed card was the
        default. The former item-4/fix-round-3 retarget block was deleted;
        its logic lives in the choke point's branch (2)."""
        providers = load_providers()
        if not any(p.name == name for p in providers):
            logger.debug("Remove: provider '%s' not found", name)
            return
        providers = [p for p in providers if p.name != name]
        # Explicit user removal — clearing the last card is intentional and
        # must pass the store's empty-overwrite guard (2026-10-11 hardening).
        save_providers(providers, allow_empty=True)

        logger.info("Removed provider: %s", name)
        if self._on_providers_changed:
            self._on_providers_changed(providers)
        if self._on_status_changed:
            self._on_status_changed(has_any_verified_provider(providers))

    # ── Agent settings (SPEC-53 §3) ────────────────────────────────────

    def get_room_fallback(self) -> str:
        """Read the JEV room-fallback setting, validated.

        Returns one of ``ROOM_FALLBACK_VALUES``. A missing, corrupt, or
        out-of-vocabulary stored value resolves to ``ROOM_FALLBACK_DEFAULT``
        (never raises). Mirrors the runtime fallback's tolerant read.
        """
        from agent.config import ROOM_FALLBACK_DEFAULT, ROOM_FALLBACK_VALUES
        from agent.config import get_agent_setting

        value = get_agent_setting("jev_room_fallback", ROOM_FALLBACK_DEFAULT)
        if value not in ROOM_FALLBACK_VALUES:
            value = ROOM_FALLBACK_DEFAULT
        return value

    def set_room_fallback(self, value: str) -> None:
        """Persist the JEV room-fallback setting to agent.json.

        Raises ValueError for values outside ``ROOM_FALLBACK_VALUES``.
        OSError propagates if the write fails.
        """
        from agent.config import ROOM_FALLBACK_VALUES, set_agent_setting

        if value not in ROOM_FALLBACK_VALUES:
            raise ValueError(
                "jev_room_fallback must be one of "
                f"{', '.join(ROOM_FALLBACK_VALUES)}; got {value!r}"
            )
        set_agent_setting("jev_room_fallback", value)
        logger.info("Saved JEV room-fallback setting: %s", value)

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

            # SPEC-63 §2.4a (fix-round 1 — Debugger #1/#3): ALL THREE exit
            # paths persist through the ONE handler-owned helper. The helper
            # is a no-op when no STORED card matches by name (unsaved card) —
            # it must never create or rewrite providers.yaml for a card the
            # user has not saved yet.
            self._persist_test_result(provider, result)

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

    def _persist_test_result(self, provider: ProviderConfig, result: TestResult) -> None:
        """Stamp the result on the STORED card and save. No-op when no stored
        card matches by name (unsaved card) — never creates an empty
        providers.yaml. Single owner of test-result persistence.

        SPEC-63 §2.4a (fix-round 1 — Debugger #1/#3/#7): the worker's THREE
        exit paths (valid-caller probe, empty-caller early return, invalid-
        caller early return) all route here. When the card IS stored, the
        stamping body is the verbatim move of the former :296-341 block —
        context-window pre-fill, the BUG #7 user_has_customized sentinel, the
        SPEC-38 dual write, last_verified_at/last_error stamping. ONE
        intentional change: a failed test stamps the EXPLAINED error
        (explain_test_error, §2.6c) — the persisted last_error must be the
        human string, never the raw probe output (Debugger #7, D#11).
        """
        providers = load_providers()
        for i, p in enumerate(providers):
            if p.name != provider.name:
                continue
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
                # SPEC-38: dual write — a discovered window lands in
                # BOTH max_tokens and context_window so the resolver's
                # context_window-first order sees the same number the
                # user sees. BUG #7 sentinel is honored above: when the
                # user has customized, neither field changes.
                new_context_window = (
                    result.context_window
                    if (result.context_window and not user_has_customized)
                    else p.context_window
                )
                providers[i] = replace(
                    p,
                    max_tokens=new_max_tokens,
                    context_window=new_context_window,
                    last_verified_at=datetime.now(timezone.utc).isoformat(),
                    last_error=None,
                )
            else:
                providers[i] = replace(
                    p,
                    # Debugger #7: persist the EXPLAINED string, resolved
                    # against the STORED card (base_url → preset join).
                    last_error=explain_test_error(
                        result.error, preset=preset_for(p)
                    ) or "unknown",
                )
            break
        else:
            return  # not stored: nothing to stamp, nothing to save
        save_providers(providers)

    def _dispatch_test_result(
        self,
        provider: ProviderConfig,
        result: TestResult,
        on_result: Callable[[TestResult], None],
    ) -> None:
        """Dispatch a failed result (short-circuit paths: invalid caller,
        empty caller gap) through the shared persistence helper, then fire
        on_result and on_status_changed via the main thread.

        SPEC-63 §2.4a: the stamping + save now lives in the ONE helper
        (`_persist_test_result`) shared by all three worker exit paths;
        this wrapper keeps only the dispatch half.

        Preserves `caller=p.caller` per the Bug #2/#3 fix — caller must
        round-trip through save unchanged (the helper replaces onto the
        STORED card, so untouched fields round-trip).
        """
        self._persist_test_result(provider, result)

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

    # ── SPEC-63 §2.4b — Load Models ─────────────────────────────────────

    def list_models(
        self,
        base_url: str,
        api_key: str,
        caller: str,
        on_result: Callable[[ModelListResult], None],
    ) -> None:
        """Fetch /models off the GTK main thread; dispatch via GLib.idle_add.
        Mirrors test_provider's thread + dispatch pattern. Never persists.

        The worker is defensive: fetch_model_ids is a total function, but a
        daemon thread must not swallow an unexpected exception silently.
        """
        def _worker():
            try:
                result = fetch_model_ids(base_url, api_key, caller)
            except Exception as e:  # noqa: BLE001 — a thread must not swallow
                result = ModelListResult(ok=False, model_ids=(),
                                         error=f"{type(e).__name__}: {e}")

            def _dispatch(result=result):  # fix-round 1 (Debugger #12): bind explicitly — never rely on late closure capture
                try:
                    on_result(result)
                except Exception:
                    logger.exception("list_models on_result callback raised")

            if self._GLib is not None and hasattr(self._GLib, "idle_add"):
                self._GLib.idle_add(_dispatch)
            else:
                _dispatch()  # test path: synchronous

        threading.Thread(target=_worker, daemon=True, name="list-models").start()

    # ── SPEC-63 §2.4c — env-var one-click card ─────────────────────────

    def create_from_env(self, preset_key: str, api_key: str) -> ProviderConfig | None:
        """Build + save a card from an environment-supplied key. Returns the
        saved ProviderConfig, or None if the preset is unknown / the vendor
        already has a card. All default maintenance stays in save_providers
        (prefer=...) via add_or_update — no new save_providers call site."""
        preset = get_preset(preset_key)
        if preset is None:
            return None
        providers = load_providers()
        # v2 (fix-round 1 — Debugger #18): guard on the VENDOR, not the name.
        # A renamed card ("my-openai") freed the name "openai" and a name-only
        # guard would let a second same-vendor card silently appear AND adopt
        # the global default. ONE helper (§2.4a): preset_vendor_present.
        if preset_vendor_present(providers, preset):
            return None  # never clobber an existing vendor card
        card = ProviderConfig(
            name=preset.name, base_url=preset.base_url, api_key=api_key.strip(),
            default_model=preset.default_model, caller=preset.caller,
            max_tokens=preset.context_window, context_window=preset.context_window,
        )
        try:
            self.add_or_update(card)  # validation + prefer=card.name + callbacks
        except ValueError:
            # fix-round 1 (Flagger #6): add_or_update's ValueError must not
            # escape into a GTK callback (chip click). Log and report failure
            # via None.
            logger.warning("create_from_env: add_or_update rejected preset %r", preset.key)
            return None
        return card

    def status_has_verified(self) -> bool:
        """Return True if any provider is verified (drives the red dot)."""
        return has_any_verified_provider(load_providers())
