# Phase 2 Follow-up — Test isolation + docstring + cleanup hardening

Debugger's Phase-2 audit found 3 fixable issues, all in `tests/test_special_agents.py`. Fix them in this turn. Do NOT change `utils/agent_defs.py` further — Phase 2's production code is signed off.

## BUG #1 (HIGH) — autouse `fresh_registry` pollutes real config dir

**Problem:** The module-level autouse `fresh_registry` fixture (near the top of `tests/test_special_agents.py`) calls `reload_registry()`. That triggers `load_agent_defs()` → `_seed_defaults()`, which uses the **real** `_get_agents_dir()` / `_get_default_agents_src()` — so the 4 built-in YAMLs get copied into the real user config dir (or `XDG_CONFIG_HOME`) on every pytest run, BEFORE any test's `monkeypatch` fixture is applied.

**Fix:** Make the `fresh_registry` autouse fixture itself redirect seeding to an isolated temp dir. Concretely, add `monkeypatch` to the autouse fixture's signature and redirect `_get_agents_dir` + `_get_default_agents_src` (in the `utils.agent_defs` module) to temp dirs before calling `reload_registry()`. The source dir should still contain the REAL built-in YAMLs (copy them from `prompts/default_agents/` into the temp src dir) so registry loading reflects production. Pattern:

```python
@pytest.fixture(autouse=True)
def fresh_registry(tmp_path, monkeypatch):
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
```

This redirects ALL tests in the file (including the pre-existing TestRegistry / TestSupervisorDef / TestSpecialAgentColorStability) to isolated temp dirs. Preserve the existing `reload_registry()` before+after behavior. Note: this means `TestSeedDefaultsPerFile`'s own `iso_agents_dir` fixture may now be partially redundant in its redirection — that's fine, keep it (double-redirection is harmless and the per-file tests still need their own controlled src-dir contents). Just ensure nothing breaks.

**Important:** `os` and `shutil` must be imported at the top of the test file (check — they may already be). The autouse fixture must take `monkeypatch` and `tmp_path` as params (pytest provides both).

## BUG #2 (MEDIUM) — stale `supervisor_def_present` docstring

The `TestSupervisorDef.supervisor_def_present` fixture's docstring (around line 218-219) says: "The all-or-nothing default seeding won't copy Supervisor into a non-empty agents dir, so place the real built-in YAML directly so the normal load path is exercised."

This justification is now FALSE after Phase 2 (per-file seeding DOES copy supervisor.yaml). Update the docstring to reflect reality, e.g.:

```
"Reload_registry may seed supervisor.yaml depending on test ordering;
copy the real built-in YAML directly to guarantee the registry sees it
and to refresh any stale user copy to the current built-in (Phase 2
changed auxilium.yaml's auto_add_to_projects flag)."
```

## BUG #4 (LOW) — missing try/finally in fixture cleanup

The `TestSupervisorDef.supervisor_def_present` fixture does its cleanup after `yield` without a try/finally. If the test body raises (assertion, interrupt), the user's auxilium.yaml is left modified. Wrap the cleanup in try/finally:

```python
    yield
    try:
        for path in (dst, aux_dst):
            if os.path.exists(path):
                os.remove(path)
    finally:
        reload_registry()
```

## Verification

```bash
# No pollution: run with a fresh HOME/XDG_CONFIG_HOME and confirm zero files written there
rm -rf /tmp/iso_check && mkdir -p /tmp/iso_check
HOME=/tmp/iso_check XDG_CONFIG_HOME=/tmp/iso_check/.config \
  python3 -m pytest tests/test_special_agents.py -q 2>&1 | tail -8
find /tmp/iso_check -type f
# The find output must be EMPTY (or only contain pytest cache, NOT agents/*.yaml)

# All tests still pass
XDG_CONFIG_HOME=/tmp/cctest_home/.config python3 -m pytest tests/test_special_agents.py tests/test_agent_defs.py -q 2>&1 | tail -8
```

## COMPLETENESS (mandatory)

```
COMPLETENESS:
- [ ] BUG #1: fresh_registry autouse redirects _get_agents_dir/_get_default_agents_src to temp — evidence: find /tmp/iso_check output is empty after a fresh-HOME run
- [ ] BUG #2: supervisor_def_present docstring updated — evidence: grep/awk showing new docstring text
- [ ] BUG #4: supervisor_def_present cleanup wrapped in try/finally — evidence: sed/awk showing the try/finally block
- [ ] All tests pass: test_special_agents.py + test_agent_defs.py — evidence: pytest -q output
- [ ] Any related issue found, not silently fixed (report here)
```
