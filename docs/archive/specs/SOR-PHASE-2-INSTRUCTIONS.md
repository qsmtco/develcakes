# Phase 2 of 8 — Per-file default seeding

**Master spec:** `docs/specs/SPEC-SUPERVISOR-ONBOARDING-REFINEMENTS.md` §2.4 + §2.10.
**Closes:** Debugger Phase-1 audit BUG #2 (the all-or-nothing seeding guard prevents supervisor.yaml from reaching existing users).

**Goal:** Change `_seed_defaults()` in `utils/agent_defs.py` from all-or-nothing ("if any user files exist, seed nothing") to per-file seeding ("copy each missing built-in file; never overwrite an existing user file").

## Rules
- Use the `prompts/steelFramedCodeWriter.md` prompt. Read `utils/agent_defs.py` in FULL before editing.
- Anchor edits to identifiers, not line numbers.
- Verify every claim with evidence (paste command output).

## Edit 1 — `utils/agent_defs.py`: change `_seed_defaults()`

The current code (function `_seed_defaults`) has an all-or-nothing guard: it builds `existing = [f for f in os.listdir(agents_dir) ...]` and does `if existing: return`. Per master spec §2.4, replace this with per-file seeding:

**Required behavior (spec §2.4):**
- Enumerate built-in YAML/JSON files in `prompts/default_agents/`.
- For each built-in file, copy it **only when the same destination filename is absent** in the user agents directory.
- Do **not** return early merely because unrelated user agent files exist.
- **Never overwrite** an existing user file, including an existing customized `supervisor.yaml`.

**Concrete change:** Remove the block that computes `existing` and returns early when non-empty. Keep the directory-creation guard. Keep the per-file loop, but the loop body already checks `if not os.path.isfile(dst):` — that guard becomes the actual seeding decision now that the early-return is gone. Confirm the per-file loop still only copies when `dst` does not exist (so it never overwrites).

Read the surrounding code (the `os.makedirs(agents_dir, exist_ok=True)` + the per-file loop) and rewrite the function so:
1. It still returns early if the source dir (`prompts/default_agents/`) doesn't exist (existing behavior — preserve it).
2. It still creates the agents dir if missing.
3. It removes the `existing` / `if existing: return` block.
4. The existing per-file loop then seeds each missing built-in file.

## Edit 2 — Tests in `tests/test_special_agents.py`

Add a test class `TestSeedDefaultsPerFile` (match the file's existing fixture style). The tests must set `XDG_CONFIG_HOME`/`HOME` to a temp dir, create the agents dir, call `_seed_defaults()`, and assert. Use `monkeypatch` (pytest) or `tempfile.TemporaryDirectory()` to isolate the config dir — look at how other tests in the file isolate config state. Required tests:

1. **`test_seed_with_unrelated_user_agent_copies_supervisor`** — Pre-create a user agents dir containing ONE unrelated agent file (e.g. `custom.yaml` with valid minimal content). Call `_seed_defaults()`. Assert `supervisor.yaml` now exists in the user agents dir (it was missing → seeded). Assert the unrelated file is still present.
2. **`test_seed_does_not_overwrite_existing_user_supervisor`** — Pre-create `supervisor.yaml` in the user agents dir with custom content (e.g. a distinctive `name:` value). Call `_seed_defaults()`. Assert the file is **unchanged** (the built-in did NOT overwrite it). Read the file back and confirm the custom name is still there.
3. **`test_seed_preserves_unrelated_user_file`** — Pre-create an unrelated agent file with custom content. Call `_seed_defaults()`. Assert the unrelated file is **unchanged** (read it back, confirm content preserved) AND that `coder.yaml`/`debugger.yaml`/`auxilium.yaml` were seeded alongside it.

Use the real `_get_agents_dir()` / `_get_default_agents_src()` helpers (import them from `utils.agent_defs`). Each test must clean up / use a fresh temp config dir so tests don't bleed into each other or into `~/.config`. The existing tests in the file may use a module-level config-dir pattern — match it.

## Verification (run and paste output)

```bash
# The all-or-nothing early-return is gone
grep -n "if existing:" utils/agent_defs.py   # MUST return 0 matches
grep -n "isfile(dst)" utils/agent_defs.py    # must show the per-file guard still present

# Functional proof: supervisor.yaml seeds into a dir with an unrelated agent
python3 -c "
import os, sys, tempfile, shutil
tmp = tempfile.mkdtemp()
os.environ['HOME'] = tmp
os.environ['XDG_CONFIG_HOME'] = os.path.join(tmp, '.config')
sys.path.insert(0, '.')
import importlib
import utils.config, utils.agent_defs
importlib.reload(utils.config)
importlib.reload(utils.agent_defs)
from utils.agent_defs import _seed_defaults, _get_agents_dir
ad = _get_agents_dir(); os.makedirs(ad, exist_ok=True)
open(os.path.join(ad, 'custom.yaml'),'w').write('name: Custom\nrole: custom\nprompts: [system/auxilium.md]\ntools: [read_file]\nllm_name: local-kb\nfallback_provider: openrouter\n')
_seed_defaults()
print('supervisor seeded?', os.path.isfile(os.path.join(ad,'supervisor.yaml')))
print('custom preserved?', os.path.isfile(os.path.join(ad,'custom.yaml')))
shutil.rmtree(tmp)
"

# Tests
XDG_CONFIG_HOME=/tmp/cctest_home/.config python3 -m pytest tests/test_special_agents.py -q 2>&1 | tail -15
```

## COMPLETENESS checklist (mandatory)

```
COMPLETENESS:
- [ ] Edit 1: _seed_defaults all-or-nothing guard removed, per-file loop preserved — evidence: grep "if existing:" returns 0; grep "isfile(dst)" returns the per-file guard
- [ ] Edit 2: TestSeedDefaultsPerFile tests added — evidence: pytest -q output (the 2 pre-existing Debugger env failures must still be present, not newly fixed)
- [ ] Verification: functional proof shows supervisor seeded alongside unrelated file
- [ ] Any related issue found, not silently fixed (report here)
```
