# Phase 2: Config Migration — compaction_threshold Field

**Spec:** `docs/specs/SPEC-CONTEXT-MANAGEMENT-ROADMAP.md` §2.3
**Goal:** Add `compaction_threshold: float = 0.80` to the config round-trip chain.
**Files to change (4 files, ~6 one-line edits):**
1. `agent/config.py` — `LLMProviderConfig` dataclass + `_to_llm_provider()` function
2. `models/providers.py` — `ProviderConfig` dataclass
3. `utils/providers_store.py` — `_to_dict()` + `_from_dict()`

**WARNING:** This is the highest correctness risk in the spec. The value must survive the full round-trip chain: `providers.yaml` → `_to_dict()` → `_from_dict()` → `ProviderConfig` → `_to_llm_provider()` → `LLMProviderConfig` → runtime. Forgetting any link silently drops the value to the default.

---

## Step 1: `agent/config.py` — `LLMProviderConfig` (line 37)

Current code:
```python
max_tokens: int = 128_000          # context window size
enabled: bool = True
```

Add `compaction_threshold` between `max_tokens` and `enabled`:
```python
max_tokens: int = 128_000          # context window size
compaction_threshold: float = 0.80  # fraction of max_tokens that triggers compaction
enabled: bool = True
```

## Step 2: `agent/config.py` — `_to_llm_provider()` (line 131)

Current code:
```python
def _to_llm_provider(p) -> LLMProviderConfig:
    """Convert a models.providers.ProviderConfig to agent.config.LLMProviderConfig."""
    return LLMProviderConfig(
        name=p.name,
        base_url=p.base_url,
        api_key=p.api_key,
        default_model=p.default_model,
        caller=p.caller,
        supports_tools=p.supports_tools,
        supports_streaming=p.supports_streaming,
        max_tokens=p.max_tokens,
        enabled=p.enabled,
        last_verified_at=p.last_verified_at,
        last_error=p.last_error,
    )
```

Add `compaction_threshold` using `getattr` for backward-compat:
```python
def _to_llm_provider(p) -> LLMProviderConfig:
    """Convert a models.providers.ProviderConfig to agent.config.LLMProviderConfig."""
    return LLMProviderConfig(
        name=p.name,
        base_url=p.base_url,
        api_key=p.api_key,
        default_model=p.default_model,
        caller=p.caller,
        supports_tools=p.supports_tools,
        supports_streaming=p.supports_streaming,
        max_tokens=p.max_tokens,
        compaction_threshold=getattr(p, "compaction_threshold", 0.80),
        enabled=p.enabled,
        last_verified_at=p.last_verified_at,
        last_error=p.last_error,
    )
```

## Step 3: `models/providers.py` — `ProviderConfig` (line 49)

Current code:
```python
max_tokens: int = 128_000
default_max_tokens: int = 0
last_verified_at: str | None = None
```

Add `compaction_threshold` after `default_max_tokens`:
```python
max_tokens: int = 128_000
default_max_tokens: int = 0
compaction_threshold: float = 0.80  # fraction of max_tokens that triggers compaction
last_verified_at: str | None = None
```

## Step 4: `utils/providers_store.py` — `_to_dict()` (line 35)

Current code:
```python
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
        "last_verified_at": p.last_verified_at,
        "last_error": p.last_error,
    }
```

Add `compaction_threshold` after `default_max_tokens`:
```python
        "default_max_tokens": p.default_max_tokens,
        "compaction_threshold": p.compaction_threshold,
        "last_verified_at": p.last_verified_at,
```

## Step 5: `utils/providers_store.py` — `_from_dict()` (line 55)

Current code:
```python
def _from_dict(d: dict[str, Any]) -> ProviderConfig:
    """Convert a plain dict to a ProviderConfig. Tolerates missing optional fields."""
    return ProviderConfig(
        name=d.get("name", ""),
        base_url=d.get("base_url", ""),
        api_key=d.get("api_key", ""),
        default_model=d.get("default_model", ""),
        caller=d.get("caller", ""),
        enabled=d.get("enabled", True),
        supports_tools=d.get("supports_tools", True),
        supports_streaming=d.get("supports_streaming", True),
        max_tokens=d.get("max_tokens", 128_000),
        default_max_tokens=d.get("default_max_tokens", 0),
        last_verified_at=d.get("last_verified_at"),
        last_error=d.get("last_error"),
    )
```

Add `compaction_threshold` after `default_max_tokens`:
```python
        default_max_tokens=d.get("default_max_tokens", 0),
        compaction_threshold=d.get("compaction_threshold", 0.80),
        last_verified_at=d.get("last_verified_at"),
```

---

## Verification

Run these exact commands after making the changes:

```bash
cd /path/to/projects/crabcakes

# 1. Round-trip test: ProviderConfig → dict → ProviderConfig → LLMProviderConfig
python3 -c "
from models.providers import ProviderConfig
from utils.providers_store import _to_dict, _from_dict
from agent.config import _to_llm_provider

# Create with custom threshold
p = ProviderConfig(name='x', base_url='x', api_key='***', default_model='x',
                   compaction_threshold=0.90)
d = _to_dict(p)
assert d['compaction_threshold'] == 0.90, f'to_dict failed: {d}'
p2 = _from_dict(d)
assert p2.compaction_threshold == 0.90, f'from_dict failed: {p2.compaction_threshold}'
llm = _to_llm_provider(p2)
assert llm.compaction_threshold == 0.90, f'to_llm_provider failed: {llm.compaction_threshold}'
print('Round-trip test PASSED')

# Default test
p3 = ProviderConfig(name='y', base_url='y', api_key='***', default_model='y')
d3 = _to_dict(p3)
assert d3['compaction_threshold'] == 0.80, f'default to_dict failed: {d3}'
p4 = _from_dict({'name': 'z', 'base_url': 'z', 'api_key': '***', 'default_model': 'z'})
assert p4.compaction_threshold == 0.80, f'default from_dict failed'
llm2 = _to_llm_provider(p4)
assert llm2.compaction_threshold == 0.80, f'default to_llm_provider failed'
print('Default test PASSED')

# Backward-compat: old dict without compaction_threshold
old_dict = {'name': 'old', 'base_url': 'x', 'api_key': '***', 'default_model': 'x'}
p5 = _from_dict(old_dict)
assert p5.compaction_threshold == 0.80
print('Backward-compat test PASSED')
"

# 2. Full test suite has no regressions
python3 -m pytest tests/ -q --tb=short 2>&1 | tail -20
```

---

## COMPLETENESS Checklist

After implementation, report:

```
COMPLETENESS:
- [x/not done] Added compaction_threshold to LLMProviderConfig — evidence (line N)
- [x/not done] Added compaction_threshold to _to_llm_provider() — evidence (line N)
- [x/not done] Added compaction_threshold to ProviderConfig — evidence (line N)
- [x/not done] Added compaction_threshold to _to_dict() — evidence (line N)
- [x/not done] Added compaction_threshold to _from_dict() — evidence (line N)
- [x/not done] Round-trip test passes (custom value 0.90 survives full chain) — evidence
- [x/not done] Default value test passes (0.80 when not set) — evidence
- [x/not done] Backward-compat test passes (old dict without field gets 0.80) — evidence
- [x/not done] Full test suite has no regressions — evidence
```
