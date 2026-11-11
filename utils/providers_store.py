# utils/providers_store.py
# Provider YAML persistence — load/save/add/remove/update for providers.yaml.
# Pure functions — no GTK, no state, no network.
#
# Manifest:
#   - Reads: <config_dir>/providers.yaml (or .json fallback)
#   - Writes: <config_dir>/providers.yaml (atomic, chmod 0o600)
#   - Network: none
#   - Imports: stdlib only (yaml if available, else json); models.providers;
#              agent.config (get/set_agent_setting, SPEC-62 global default)
#
# Architecture: mirrors utils/feed_store.py and utils/agent_defs.py patterns.
# Path resolution uses utils.config.get_config_dir() — never hardcoded paths.

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import shutil
from typing import Any

from models.providers import (
    _VALID_REASONING_LEVELS,
    ProviderConfig,
    coerce_provider_context_mode,
    validate_provider_context_mode,
    validate_provider_reasoning_effort,
)

_logger = logging.getLogger(__name__)


# DUPLICATION INVARIANT: this set must match agent.runtime._PROVIDER_CALLERS.keys().
# Layer rule: utils/* cannot import from agent/*, so the set is duplicated here.
# Enforced by a regression test in tests/test_providers_store.py
# (TestValidCallersDuplicationInvariant). If you add a new adapter to
# agent.runtime._PROVIDER_CALLERS, you MUST also add the key here and update
# the regression test. See .crabcakes/task-specs/caller-validation.md.
_VALID_CALLERS: frozenset[str] = frozenset({
    "anthropic", "minimax", "openai", "openrouter", "zai",
})

_FILENAME = "providers.yaml"


def _effective_caller(p: ProviderConfig) -> str:
    """The caller a card effectively presents, derived exactly like
    SettingsHandler.add_or_update's auto-detect (settings_handler.py:111):
    a stored caller wins (VALIDATED — invalid or whitespace-only is treated
    as empty, fix-round 4 RULING 1a), otherwise a ``<prefix>/<model>``
    default_model with a RECOGNIZED prefix supplies it.

    SPEC-62 fix-round 2: the write choke point needs this because only
    add_or_update populates ``caller`` — direct save_providers calls, the
    agent-builder saves, and migrate_from_agent_json all produce chat cards
    with caller="", which a bare ``if p.caller`` test silently misses.

    RULING 1 (fix-round 4): derivation decides WHICH caller, never WHETHER
    the card is chat — whether-gating lives at the adoption sites via
    _NON_CHAT_NAMES (deterministic name exclusion for the two fixed
    non-chat cards). Residual risk: a hypothetical future non-chat card
    with a recognized prefixed model would be misjudged as chat here —
    accepted, post-mortem note.

    Uses THIS module's _VALID_CALLERS — the duplication-invariant twin of
    agent.runtime's taxonomy (TestValidCallersDuplicationInvariant) — NOT an
    agent.runtime import: agent/runtime.py imports providers_store
    (:3345/:3478), so importing back would be circular, and the utils/*
    layer rule forbids it anyway.

    Returns "" when nothing is derivable (caller absent/invalid/whitespace +
    unprefixed model, or an unrecognized prefix) — the silent choke-point
    analogue of add_or_update's "Cannot auto-detect caller" ValueError.
    """
    caller = (p.caller or "").strip()
    if caller:
        # RULING 1a: a stored caller counts only when it is in the taxonomy
        # ("not-a-real-caller", "   ", None → the card has NO effective
        # caller — it must never be adopted as the global default).
        return caller if caller in _VALID_CALLERS else ""
    default_model = p.default_model or ""
    if "/" in default_model:
        prefix = default_model.split("/", 1)[0].strip().lower()
        if prefix in _VALID_CALLERS:
            return prefix
    return ""


# SPEC-62 fix-round 4 RULING 1b: the two FIXED non-chat cards are excluded by
# NAME — a DecisionModel card whose model carries a recognized prefix
# ("openrouter/decision-x") derives a caller, so name exclusion is the
# deterministic whether-guard for exactly these cards. The constants live in
# agent/llm/jev_client.py and utils/improve.py (agent.config imports them
# function-locally, so they are NOT re-exported there). providers_store cannot
# import agent.llm.jev_client / utils.improve directly at module scope
# (utils/* layer rule + agent-side import of this module), so mirror the same
# function-local pattern agent.config uses — and degrade gracefully when the
# sub-modules are absent (fresh-install probe paths).
def _fixed_non_chat_names() -> frozenset[str]:
    names: set[str] = set()
    with contextlib.suppress(ImportError):
        from agent.llm.jev_client import DECISION_MODEL_PROVIDER
        names.add(DECISION_MODEL_PROVIDER)
    with contextlib.suppress(ImportError):
        from utils.improve import IMPROVE_MODEL_PROVIDER
        names.add(IMPROVE_MODEL_PROVIDER)
    return frozenset(names)


_NON_CHAT_NAMES: frozenset[str] = _fixed_non_chat_names()


def _is_chat_card(p: ProviderConfig) -> bool:
    """The RULING 1 adoption predicate: a derivable/valid caller AND the card
    is not one of the fixed non-chat cards (DecisionModel / ImproveModel)."""
    return _effective_caller(p) != "" and p.name not in _NON_CHAT_NAMES


def _bare_model(default_model: str) -> str:
    """RULING 2: agent.json default_model is consumed only by agent/runtime's
    f"{default_provider}/{default_model}" composition, so it must be the BARE
    name — a stored full "openai/gpt-4o" would compose "mycard/openai/gpt-4o"
    and brick the wire call. Unprefixed models pass through unchanged."""
    return (default_model or "").split("/", 1)[-1]


def get_providers_path() -> str:
    """Return absolute path to providers.yaml under config dir.
    Does NOT create the file."""
    from utils.config import get_config_dir
    return os.path.join(get_config_dir(), _FILENAME)


def _to_dict(p: ProviderConfig) -> dict[str, Any]:
    """Convert a ProviderConfig to a plain dict for serialization."""
    return {
        "name": p.name,
        "base_url": p.base_url,
        "api_key": p.api_key,
        "default_model": p.default_model,
        "caller": p.caller,
        "enabled": p.enabled,
        "supports_tools": p.supports_tools,
        "supports_streaming": p.supports_streaming,
        "max_tokens": p.max_tokens,
        "default_max_tokens": p.default_max_tokens,
        "context_window": p.context_window,
        "compaction_threshold": p.compaction_threshold,
        "tool_output_offload_chars": p.tool_output_offload_chars,
        "last_verified_at": p.last_verified_at,
        "last_error": p.last_error,
        "context_mode": _normalize_context_mode_value(getattr(p, "context_mode", "auto")),
        "reasoning_effort": validate_provider_reasoning_effort(
            getattr(p, "reasoning_effort", "off")
        ),
        "supports_reasoning": bool(getattr(p, "supports_reasoning", False)),
    }


def _normalize_reasoning_effort(d: dict[str, Any]) -> str:
    """Load-path coerce: missing/empty → off; unknown → off + warning."""
    raw = d.get("reasoning_effort", "off")
    if raw in (None, ""):
        return "off"
    level = validate_provider_reasoning_effort(raw)
    already_off = isinstance(raw, str) and raw.strip().lower() == "off"
    if level == "off" and not already_off:
        _logger.warning(
            "providers.yaml: provider %r has invalid reasoning_effort %r; "
            "coercing to 'off'. Valid levels: %s.",
            d.get("name", "<unnamed>"), raw, sorted(_VALID_REASONING_LEVELS),
        )
    return level


def _normalize_supports_reasoning(d: dict[str, Any]) -> bool:
    """Load-path: only an actual bool True is True. Missing → False."""
    raw = d.get("supports_reasoning", False)
    if isinstance(raw, bool):
        return raw
    if raw not in (None, ""):
        _logger.warning(
            "providers.yaml: provider %r has invalid supports_reasoning %r; "
            "coercing to False.",
            d.get("name", "<unnamed>"), raw,
        )
    return False


def _normalize_context_mode_value(raw: object) -> str:
    """Load-path coerce: missing, empty, or invalid → auto. Never raises."""
    if raw in (None, ""):
        return "auto"
    mode = coerce_provider_context_mode(raw)
    if isinstance(raw, str):
        try:
            validate_provider_context_mode(raw)
        except ValueError:
            _logger.warning(
                "providers.yaml: invalid context_mode %r; coercing to 'auto'.",
                raw,
            )
    else:
        _logger.warning(
            "providers.yaml: invalid context_mode %r; coercing to 'auto'.",
            raw,
        )
    return mode


def _context_window_value(raw: object) -> int:
    """Optional YAML int. A bool, a float, or any non-int is 0."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw


def _from_dict(d: dict[str, Any]) -> ProviderConfig:
    """Convert a plain dict to a ProviderConfig. Tolerates missing optional fields.

    Caller validation (caller-validation spec): if a non-empty caller field
    is present but is NOT in _VALID_CALLERS, log a warning and keep the value
    unchanged. Do NOT mutate — the load-time path is for surfacing legacy
    bad data, not for silent rewrites. Save-time validation in
    settings_handler.add_or_update prevents NEW bad data.
    """
    # None-normalize: dict.get's default only fires on MISSING keys, not
    # on YAML-null values (which deserialize to Python None). Without this,
    # `caller: null` in YAML leaks None into ProviderConfig.caller and
    # silently bypasses the `not caller` truthy checks downstream.
    caller = d.get("caller") or ""
    if caller and caller not in _VALID_CALLERS:
        _logger.warning(
            "providers.yaml: provider %r has invalid caller %r. "
            "Valid callers: %s. Auto-detect at save time will lower-case and "
            "validate, but load-time keeps the value unchanged. Fix the YAML "
            "or set caller explicitly via settings dialog.",
            d.get("name", "<unnamed>"), caller, sorted(_VALID_CALLERS),
        )
    return ProviderConfig(
        name=d.get("name", ""),
        base_url=d.get("base_url", ""),
        api_key=d.get("api_key", ""),
        default_model=d.get("default_model", ""),
        caller=caller,
        enabled=d.get("enabled", True),
        supports_tools=d.get("supports_tools", True),
        supports_streaming=d.get("supports_streaming", True),
        max_tokens=d.get("max_tokens", 128_000),
        default_max_tokens=d.get("default_max_tokens", 0),
        context_window=_context_window_value(d.get("context_window", 0)),
        compaction_threshold=d.get("compaction_threshold", 0.80),
        tool_output_offload_chars=int(d.get("tool_output_offload_chars", 0) or 0),
        last_verified_at=d.get("last_verified_at"),
        last_error=d.get("last_error"),
        context_mode=_normalize_context_mode_value(d.get("context_mode", "auto")),
        reasoning_effort=_normalize_reasoning_effort(d),
        supports_reasoning=_normalize_supports_reasoning(d),
    )


def _serialize(providers: list[ProviderConfig]) -> str:
    """Serialize providers list to YAML or JSON string."""
    items = [_to_dict(p) for p in providers]
    try:
        import yaml
        return yaml.dump(items, default_flow_style=False, allow_unicode=True)
    except ImportError:
        return json.dumps(items, indent=2, ensure_ascii=False)


def _parse(text: str) -> list[ProviderConfig]:
    """Parse YAML or JSON text into a list of ProviderConfig.
    Tolerates malformed content — returns [] with a warning."""
    raw: Any = None
    try:
        from utils.yaml_load import safe_load_yaml
        raw = safe_load_yaml(text)
    except ImportError:
        try:
            raw = json.loads(text)
        except (json.JSONDecodeError, ValueError) as e:
            _logger.warning("providers_store: failed to parse: %s", e)
            return []
    except Exception as e:
        _logger.warning("providers_store: YAML parse error: %s", e)
        return []

    if raw is None:
        return []
    if not isinstance(raw, list):
        _logger.warning("providers_store: expected list, got %s", type(raw).__name__)
        return []

    providers: list[ProviderConfig] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            _logger.warning("providers_store: skipping non-dict entry %d", i)
            continue
        try:
            providers.append(_from_dict(item))
        except (KeyError, TypeError) as e:
            _logger.warning("providers_store: skipping malformed entry %d: %s", i, e)
    return providers


def _existing_card_count() -> int:
    """Count cards currently on disk (0 when absent/unreadable/malformed)."""
    path = get_providers_path()
    if not os.path.isfile(path):
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return 0
    return len(_parse(text))


def _backup_existing(path: str) -> None:
    """Keep a single-generation .bak of `path` before an overwrite.

    Best-effort — a backup failure must never block a legitimate save; it
    is logged instead. Recovery net for the 2026-10-11 store-truncation
    incident class.
    """
    if not os.path.isfile(path):
        return
    try:
        shutil.copy2(path, path + ".bak")
    except OSError as e:
        _logger.warning("providers_store: could not write %s.bak: %s", path, e)


# (path, text, parsed). Content-keyed so an edit is always seen; callers get copies.
_PARSE_CACHE: tuple[str, str, list[ProviderConfig]] | None = None


def load_providers() -> list[ProviderConfig]:
    """Read providers.yaml → list[ProviderConfig]. Empty list if missing.
    Tolerates malformed lines (logs warning, skips)."""
    global _PARSE_CACHE
    path = get_providers_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        _logger.warning("providers_store: failed to read %s: %s", path, e)
        return []
    cached = _PARSE_CACHE
    if cached is not None and cached[0] == path and cached[1] == text:
        parsed = cached[2]
    else:
        parsed = _parse(text)
        _PARSE_CACHE = (path, text, parsed)
    return [dataclasses.replace(p) for p in parsed]


def save_providers(providers: list[ProviderConfig], *,
                   allow_empty: bool = False,
                   prefer: str | None = None) -> None:
    """Write list[ProviderConfig] → providers.yaml. Creates parent dir.
    File is written with mode 0o600 (owner-only — contains API keys).
    Parent dir is chmod 0o700 if created. The previous file is preserved
    as providers.yaml.bak (one generation) before every overwrite.
    Atomic: writes to .tmp then renames.

    Safety guard (2026-10-11 incident): refuses to overwrite a non-empty
    store with an empty list unless the caller passes ``allow_empty=True``
    (the explicit remove-the-last-card path). A degraded load must not
    silently truncate the store; oversized intentional shrinks log a
    warning naming the .bak recovery file.

    SPEC-62 fix-round 4 RULING 3: this function is the SINGLE OWNER of
    global-default maintenance — every provider write path funnels through
    here, so all branches live in one place (the former settings_handler
    adoption/retarget blocks were deleted; ``add_or_update`` passes
    ``prefer=provider.name``):
      * ``prefer``: when the stored default is EMPTY, adopt the named card
        if it is a chat card (RULING 1 predicate), else the first chat card.
    """
    path = get_providers_path()
    existing = _existing_card_count()
    if not providers and existing and not allow_empty:
        raise ValueError(
            f"refusing to overwrite providers.yaml ({existing} card(s)) with an "
            f"empty list — pass allow_empty=True if clearing is intentional"
        )
    if existing and len(providers) < existing:
        _logger.warning(
            "providers_store: saving %d card(s) over %d on disk — if this drop "
            "was not intentional, the previous file is at %s.bak",
            len(providers), existing, path,
        )
    _backup_existing(path)
    parent = os.path.dirname(path)

    # Create parent dir with restricted permissions if it doesn't exist
    parent_existed = os.path.isdir(parent)
    if not parent_existed:
        os.makedirs(parent, exist_ok=True)

    # Atomic write: .tmp → rename
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(_serialize(providers))
        os.rename(tmp_path, path)
    except Exception:
        # Clean up tmp on failure
        if os.path.isfile(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise

    # Set file permissions — owner only (contains API keys)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # non-POSIX filesystem

    # Set parent dir permissions if we just created it
    if not parent_existed:
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass

    # SPEC-62 fix-round 4 RULING 3: single-owner default maintenance. All
    # branches live here; settings_handler's two blocks were DELETED and
    # add_or_update now passes prefer=provider.name. Branches:
    #   (1) allow_empty + empty list            → clear both (explicit clear)
    #   (2) allow_empty + non-empty stale list  → retarget/clear  (fixes #6/#7)
    #   (3) default empty OR stale              → adopt prefer-or-first chat
    #   (4) default live                        → refresh BARE model (fixes #4)
    # Chat-card gating: _is_chat_card (RULING 1 predicate — derived/validated
    # caller via _effective_caller + fixed-name exclusion). Stored model is
    # always the BARE name (RULING 2) so agent/runtime's
    # f"{default_provider}/{default_model}" composes the full model.
    # Layer rule respected: agent.config getters only, never ui/*.
    try:
        from agent.config import get_agent_setting, set_agent_setting

        current_default = get_agent_setting("default_provider", "")
        saved_names = {p.name for p in providers}
        default_is_live = current_default in saved_names

        if allow_empty and not providers:
            # (1) Explicit remove-the-last-card: reset both settings.
            if current_default:
                set_agent_setting("default_provider", "")
                set_agent_setting("default_model", "")
                _logger.info(
                    "save_providers: all cards cleared — "
                    "default_provider/default_model reset"
                )
        elif allow_empty:
            # (2) Explicit shrink that still leaves cards (fixes #6/#7): the
            # retarget/clear decision is the choke point's, not the caller's.
            if current_default and not default_is_live:
                chat_cards = [p for p in providers if _is_chat_card(p)]
                if chat_cards:
                    first = chat_cards[0]
                    set_agent_setting("default_provider", first.name)
                    set_agent_setting("default_model", _bare_model(first.default_model))
                    _logger.info(
                        "save_providers: default card removed via shrink — "
                        "retargeted to %s (%s)",
                        first.name, first.default_model,
                    )
                else:
                    set_agent_setting("default_provider", "")
                    set_agent_setting("default_model", "")
                    _logger.info(
                        "save_providers: default card removed via shrink — "
                        "no chat cards left, defaults cleared"
                    )
        elif not current_default or not default_is_live:
            # (3) Empty or stale default → adopt. Prefer the named card when
            # it is a chat card (add_or_update's just-saved card), else the
            # first chat card (any other write path).
            prefer_card = None
            if prefer:
                prefer_card = next(
                    (p for p in providers if p.name == prefer), None
                )
            chosen = (
                prefer_card if prefer_card is not None and _is_chat_card(prefer_card)
                else (chat_cards[0] if (chat_cards := [p for p in providers if _is_chat_card(p)]) else None)
            )
            if chosen is not None:
                set_agent_setting("default_provider", chosen.name)
                set_agent_setting("default_model", _bare_model(chosen.default_model))
                _logger.info(
                    "save_providers: set global default provider to %s (%s)",
                    chosen.name, chosen.default_model,
                )
        else:
            # (4) Default is LIVE: refresh default_model from THE DEFAULT
            # CARD — BARE (RULING 2) — on EVERY live path, with or without
            # ``prefer`` (fixes #4): a re-save of the default card with an
            # edited model (raw save, test_provider stamp, builder save)
            # must reach agent.json without an app restart. ``prefer`` is a
            # no-op here by design (a live default is never re-pointed).
            default_card = next(
                (p for p in providers if p.name == current_default), None
            )
            if default_card is not None and _is_chat_card(default_card):
                set_agent_setting("default_model", _bare_model(default_card.default_model))
                _logger.info(
                    "save_providers: refreshed bare default_model for %s → %s",
                    default_card.name, default_card.default_model,
                )

    except Exception as e:
        # The settings persistence must never break a provider save.
        _logger.warning("save_providers: could not persist default_provider: %s", e)


def add_provider(providers: list[ProviderConfig], p: ProviderConfig) -> None:
    """Append-and-save. Replaces existing entry with the same name."""
    # Remove existing with same name, then append
    filtered = [x for x in providers if x.name != p.name]
    filtered.append(p)
    save_providers(filtered)


def remove_provider(providers: list[ProviderConfig], name: str) -> None:
    """Remove-by-name-and-save. No-op if not found.

    Removing the last card is an intentional clear — pass through
    ``allow_empty=True`` to the save guard.
    """
    filtered = [x for x in providers if x.name != name]
    save_providers(filtered, allow_empty=True)


def update_provider(providers: list[ProviderConfig], p: ProviderConfig) -> None:
    """Replace existing entry with same name. Adds if new."""
    add_provider(providers, p)


def has_any_verified_provider(providers: list[ProviderConfig]) -> bool:
    """True if at least one provider has last_verified_at set. Drives the red dot."""
    return any(p.last_verified_at is not None for p in providers)


def remove_providers_from_agent_json() -> bool:
    """One-shot: remove legacy providers key from agent.json.

    Idempotent — returns True if removed, False if key was not present.
    Uses atomic .tmp + chmod 0o600 write, matching save_provider pattern.
    """
    from utils.config import get_config_dir
    agent_json_path = os.path.join(get_config_dir(), "agent.json")

    if not os.path.isfile(agent_json_path):
        return False

    try:
        with open(agent_json_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _logger.debug("remove_providers_from_agent_json: cannot read agent.json: %s", e)
        return False

    if "providers" not in raw:
        return False

    del raw["providers"]

    # Atomic write: .tmp → rename → chmod 0o600
    tmp_path = agent_json_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, indent=4, ensure_ascii=False)
        os.rename(tmp_path, agent_json_path)
        os.chmod(agent_json_path, 0o600)
        _logger.info("removed legacy providers key from agent.json")
        return True
    except Exception:
        if os.path.isfile(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise


def migrate_from_agent_json() -> int:
    """One-time migration: copy agent.json providers → providers.yaml.

    Reads the legacy `providers` key from agent.json. For each entry,
    converts to a ProviderConfig and merges into providers.yaml. YAML wins
    on name conflict (it's the current source of truth). Safe to call
    multiple times — idempotent after first migration.

    After migrating, removes the `providers` key from agent.json so the
    legacy store is fully consolidated into providers.yaml.

    Returns the count of providers migrated.
    """
    from utils.config import get_config_dir
    agent_json_path = os.path.join(get_config_dir(), "agent.json")

    if not os.path.isfile(agent_json_path):
        return 0

    try:
        with open(agent_json_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        _logger.debug("migrate_from_agent_json: cannot read agent.json: %s", e)
        return 0

    providers_dict = raw.get("providers")
    has_providers_key = "providers" in raw
    if not providers_dict or not isinstance(providers_dict, dict) or len(providers_dict) == 0:
        # No providers to migrate, but strip the empty/missing key so agent.json is clean
        if has_providers_key:
            remove_providers_from_agent_json()
        return 0

    # Convert legacy agent.json entries to ProviderConfig
    migrated = []
    for name, cfg in providers_dict.items():
        if not isinstance(cfg, dict):
            continue
        try:
            migrated.append(ProviderConfig(
                name=name,
                base_url=cfg.get("base_url", ""),
                api_key=cfg.get("api_key", ""),
                default_model=cfg.get("default_model", ""),
                caller=cfg.get("caller", ""),
                supports_tools=cfg.get("supports_tools", True),
                supports_streaming=cfg.get("supports_streaming", True),
                max_tokens=cfg.get("max_tokens", 128_000),
            ))
        except (KeyError, TypeError) as e:
            _logger.warning("migrate_from_agent_json: skipping malformed entry %s: %s", name, e)
            continue

    if not migrated:
        return 0

    # Merge with existing YAML providers (YAML wins on name conflict)
    yaml_providers = load_providers()
    yaml_names = {p.name for p in yaml_providers}
    added_count = 0
    for p in migrated:
        if p.name not in yaml_names:
            yaml_providers.append(p)
            added_count += 1
            _logger.debug("migrate_from_agent_json: added %s from agent.json", p.name)

    save_providers(yaml_providers)
    # 1b: strip the providers key from agent.json after successful migration
    remove_providers_from_agent_json()
    _logger.info("migrated %d providers from agent.json to providers.yaml", added_count)
    return added_count
