# SPEC-47: The Acceptance Gate — make "done" mechanically re-provable

**Date:** 2026-10-21
**Author:** Supervisor (PM direction 2026-10-21)
**Status:** Draft — for implementation
**Implements:** `docs/proposals/PROPOSAL-META.md` §2 (Program 1 — Acceptance gate), which adopts `docs/proposals/PROPOSAL-acceptance-gate.md` (2026-10-09) as the design source
**Depends on:** nothing to land first — SP1 is self-contained and additive
**Target branch:** main

> Architecture compliance: `scripts/acceptance.py` is a standalone tool — stdlib plus
> pytest, no framework, no GTK import at module scope. It never imports `ui/` or
> `agent/`; it drives pytest as a subprocess and reads YAML with the repo's own
> `utils/yaml_load.safe_load_yaml` (Rule 3: verified signature below). New
> directories `docs/acceptance/` and `tests/mutations/` are additions to the tree.
> No existing module's behavior changes except where §2.7 lists it explicitly.

---

## DISCOVERY

- Read `docs/proposals/PROPOSAL-acceptance-gate.md` (§2.1 rows, §2.2 runner, §2.3 first batch, §2.4 wiring): the design source. Its §2.3 row table names two anchors that **do not exist in the current tree** — the R-1 mutation target `delete_session_rows` (no such symbol anywhere; `grep -rn "delete_session_rows" --include="*.py"` returns zero) and `tests/known_failures.txt` (`ls tests/known_failures.txt` → "No such file or directory"). §2.5 of this spec pins the real anchors instead.
- Read `ui/agent_runtime/session.py` (`RuntimeSessionMixin.clear_conversation`, `:14`): the R-1 claim's real implementation. Guard is `session_key.startswith("special:")`; refuses when `rt.is_loop_active(session_key)` is true; resets `conv.messages = []`, `conv.step_count = 0`, `conv.total_tokens = 0`, `conv.total_cost = 0.0`, `conv.task_card = ""`, `conv._token_estimate_cache = None`; calls `rt.drop_read_cache`, `rt.drop_compile`, `rt.drop_repo_map`; then deletes the persisted JSON at `os.remove(conv_path)` (`:118`) inside `try/except FileNotFoundError: pass` / `except OSError` (logged, not raised). Returns `True` on success, `False` for non-special key, unknown agent, active loop, or reset failure. **This is the R-1 mutation anchor** — the proposal's `delete_session_rows` is stale.
- Read `tests/test_live_bridge.py` (`test_clear_conversation_deletes_json_only`, `:279`): the R-1 node. Builds `AgentRuntimeHandler.__new__` (no constructor), fakes `_agents`/`_turn_attr`/`_fh`/`_runtimes`, monkeypatches `utils.config.get_config_dir` to `tmp_path`, asserts the JSON is deleted, the in-memory conv is cleared, the drops happened, and that an active loop refuses the clear and leaves both the file and the conversation intact. It is a plain function, not a class method — the node id is `tests/test_live_bridge.py::test_clear_conversation_deletes_json_only`.
- Read `ui/views/chat_surface.py` (`_scroll_to_latest_js` `:168` and `_inject_script` `:195`): both build a JS string containing the literal `el.scrollTop = el.scrollHeight;`. `_inject_script` returns `'held'` when the reader was not at bottom, and pins only when it was. A third occurrence is at `:631` (a comment). **The R-2 mutation anchor is `_inject_script`'s pin line at `:195`** — deleting it breaks follow-after-append while leaving the "held" path intact.
- Read `tests/test_live_guard.py` (`class TestInjectionScroll`, `:405`): the R-2 node set — `test_injected_rows_follow_at_bottom` (`:449`), `test_injected_rows_preserve_reading_position` (`:471`), `test_scroll_to_latest_then_append_follows` (`:497`). Verified live: `pytest tests/test_live_guard.py::TestInjectionScroll --collect-only -q` → 3 tests collected. The class is a plain `pytest` class (no `unittest` base) — it must be addressable as `tests/test_live_guard.py::TestInjectionScroll`.
- Read `ui/window.py` (`MainWindow._build`, `:137`): `self._agent_runtime_handler = AgentRuntimeHandler(...)` is at `:223`; the dependent wiring `set_agent_runtime_handler` calls are at `:240-242`; the live-bridge approver/resolver wiring follows with a comment stating it MUST run AFTER the ARH is constructed. `__init__` sets `self._agent_runtime_handler = None` at `:86`. **The R-3 mutation anchor is the ordering of `:223` relative to the dependents' wiring** — moving the ARH construction above the point where the dependents are given it reproduces the 2026-10-08 half-wired `_build` crash class.
- Read `ui/handlers/feed_handler.py` (`update_card(self, card_id: str, card_data: FeedCardData) -> None`, `:614`) and `utils/feed_store.py` (`append_card_update(project_path, card_id, updates) -> bool`, `:281`; `load_feed(project_path) -> list[FeedCardData]`, `:437`; `save_feed(project_path, cards) -> None`, `:495`; `FEED_WINDOW_DEFAULT = 2000` at `:67`). These are the R-7 perf-row anchors. `models/feed_card.py` `class FeedCardData` (`:100`) is a plain dataclass with fields `card_type`, `source`, `title`, `body`, `author`, `timestamp`, `project_name` and optional `file_path` — no GTK, no git, no network.
- Read `utils/config.py` (`get_config_dir() -> str`, `:15`): returns `$XDG_CONFIG_HOME/develcakes` when that env var is set, else `~/.config/develcakes`. It does **not** create the directory. This is the R-6 tripwire's snapshot root and the reason the gate must set `XDG_CONFIG_HOME` to a temp dir for its own runs.
- Read `utils/yaml_load.py` (`safe_load_yaml(stream)`): the repo's own YAML loader. It is used instead of adding a PyYAML dependency.
- Read `tests/conftest.py`: `sys.path.insert(0, <repo root>)` at import time; the widget-construction detection is **textual and transitive** — it parses the test module and follows intra-project imports to a bounded depth to decide whether to skip. Consequence for the gate: a GTK-touching node under `tests/` is skipped without a display, and a *skip* is not a pass. The gate must treat "skipped" as a failure for a registered row unless the row declares otherwise (see §3, row field `skip_ok`).
- Read `pytest.ini`: `addopts = -v --tb=short`; markers `slow`, `manual` are declared. `testpaths = tests`. The `slow` marker exists specifically for "long-running measurement rows (excluded from fast suites)" — the perf row uses it.
- Read `ui/views/chat_surface.py:66` (`from utils.live_bridge import READ_ONLY_METHODS, LiveBridge`): the live bridge is imported by the chat surface, so a clean-checkout import smoke that imports `main` transitively reaches it — R-5's untracked-file class is live in this tree.
- Read `main.py` (header comment `:5-8`): **"importing this module has a deliberate side effect … Do NOT import main from tests."** It registers a token-redaction filter and configures logging at import. The R-5 smoke must therefore import `main` in a **subprocess** (fresh interpreter), never in-process, and must not import it from any `tests/` module.
- Checked `xvfb-run`: present at `/usr/bin/xvfb-run`. GTK4 suites must run under it.
- Checked `ls .github`: **does not exist**. CI is a Program-1 deliverable per `PROPOSAL-META.md` §2, and the acceptance-gate proposal §2.5 says CI is out of scope for *that* proposal — the two documents conflict. `PROPOSAL-META.md` is the later, ACTIVE document and explicitly says "CI that runs this gate" is part of Program 1. **This spec resolves the conflict in favour of META: CI lands in this unit.** It is listed in §5 non-goals as a Program-1 item to be built *after* the runner is green, in its own step, so the gate is never blocked on a workflow file.
- Checked `git log` for the commit ids the proposal cites: `5a17aaf2` (clear tests), `600dfd42` (scroll tests). The proposal's row table cites them as provenance; this spec cites the live files instead, because the proposal's own §2.3 anchors are stale in two places (R-1's `delete_session_rows`, `known_failures.txt`).
- Architecture owner: `ui/agent_runtime/session.py` owns the R-1 clear behavior; `ui/views/chat_surface.py` owns the R-2 scroll behavior; `ui/window.py` owns the R-3 wiring order; `utils/config.py` owns the config root the R-6 tripwire snapshots. The new `scripts/acceptance.py` owns row execution; `docs/acceptance/rows.yaml` owns the row registry.
- Existing patterns to copy: the repo already runs throwaway copies for verification (`utils/worktree_manager.py` for git worktrees; `scripts/crab_perf_probe.py` for measurement harnesses) — the gate's temp-copy mechanism follows the worktree pattern rather than inventing one. `scripts/crab_status.py` is the precedent for a committed `scripts/` tool with a `__main__` entry and argparse.

---

## 1. Overview

**Problem.** Load-bearing claims live as prose checkboxes in specs, and nothing re-runs them. Two failure modes, both observed live in the 2026-10-08/09 week: **vacuity** (a test asserts against a degenerate object and cannot fail — the smart-scroll-follow tests stayed green while follow was broken in the shipped default) and **claim drift** (a fix shipped "129 passed" as evidence while being un-importable from a clean checkout). A lesson is not a mechanism until something can fail on it.

**Solution.** One command in the repo can disagree with a claim of "done."

- An **acceptance row** binds one behavioral claim to two runnable things: the node that proves it, and a mutation that proves the node can fail.
- The gate runs each row **twice** — **mutated** (the node MUST fail; if it passes, the row is vacuous and the gate refuses) and **clean** (the node MUST pass).
- The live tree is never mutated. Mutations run in a throwaway copy.

**Scope**

| In | Out |
|---|---|
| `scripts/acceptance.py` with `rows`, `suite`, `smokes`, `all` | A coverage target; a "test everything" gate |
| `docs/acceptance/rows.yaml` — the row registry | Retrofitting every historical spec (rows register lazily) |
| `tests/mutations/*.diff` — one diff per row | A CI service (CI lands in Program 1 per META, as a separate step) |
| First rows R-1, R-2, R-3, R-5, R-6, R-7 (R-4 deferred, R-3/R-5 ship as smokes) | Coverage targets, whole-UI typing, burning the historical failure baseline |
| `tests/known_failures.txt` — the shrink-only baseline | A detector that ends an audit by itself |
| The perf bound row (R-7) from the September P0.1 item | A perf dashboard |

**Architecture principles that apply**

1. **No red, no green.** Every row runs mutated and clean. A row that cannot fail is refused, not passed.
2. **The live tree is never mutated.** Mutations apply to a throwaway copy only.
3. **Loud on unappliable diffs.** A diff that does not apply fails the gate with the patch output, naming the file and line it expected. This is the "mutation diffs need upkeep when files move" cost the proposal names, paid loudly instead of silently.
4. **A skip is not a pass.** A registered node that is skipped (no display for a GTK node) fails its row unless the row declares `skip_ok: true`.
5. **Row economy.** Rows prefer small sub-second nodes. `rows` should stay in the minutes. `suite` is for phase boundaries.
6. **The gate is stdlib + pytest.** No new dependency, no framework, no GTK import at module scope.

---

## 2. Changes by File

### 2.1 `docs/acceptance/rows.yaml` (new)

The row registry. Read by `utils/yaml_load.safe_load_yaml` — no PyYAML dependency.

```yaml
# docs/acceptance/rows.yaml
# Acceptance rows — one behavioral claim bound to a node and a mutation.
# The gate runs each row twice: mutated (node MUST fail) and clean (node MUST pass).
# A row whose mutation still passes is refused: the node cannot see the behavior.
#
# Format per row:
#   id         R-<n>                       unique row id
#   claim      <one sentence>              the behavioral claim, in plain words
#   source     <spec / audit path>         where the claim came from
#   node       <pytest node id>            the node that proves the claim
#   mutation   tests/mutations/<id>.diff   the diff that must make the node fail
#   added      YYYY-MM-DD
#   runner     plain | xvfb | xvfb+aa-exec per-row runner wrap
#   skip_ok    true only when a skip is an acceptable outcome
version: 1
rows:
  - id: R-1
    claim: "A /clear on a special-agent session durably clears: the persisted JSON is deleted, the in-memory conversation is reset, and a refused clear (active tool loop) leaves both the file and the conversation intact."
    source: "docs/proposals/PROPOSAL-acceptance-gate.md §2.3 R-1 (anchor corrected 2026-10-21: the proposal's delete_session_rows does not exist; the real anchor is ui/agent_runtime/session.py:118 os.remove(conv_path))"
    node: "tests/test_live_bridge.py::test_clear_conversation_deletes_json_only"
    mutation: "tests/mutations/R-1.diff"
    added: "2026-10-21"
    runner: plain
  - id: R-2
    claim: "Injected rows follow the bottom; reading position holds when the reader is scrolled up; button-then-append follows."
    source: "docs/proposals/PROPOSAL-acceptance-gate.md §2.3 R-2; SPEC-19 SP1/SP2 (landed 600dfd42)"
    node: "tests/test_live_guard.py::TestInjectionScroll"
    mutation: "tests/mutations/R-2.diff"
    added: "2026-10-21"
    runner: plain
  - id: R-3
    claim: "MainWindow constructs with the agent runtime handler wired into its dependents."
    source: "docs/proposals/PROPOSAL-acceptance-gate.md §2.3 R-3 (2026-10-08 half-wired _build crash class)"
    node: "scripts/window-smoke.py"
    mutation: "tests/mutations/R-3.diff"
    added: "2026-10-21"
    runner: xvfb
  - id: R-4
    claim: "PLACEHOLDER — a superseded or cancelled turn executes nothing further. Registers when the stale-turn fix lands."
    source: "docs/proposals/PROPOSAL-acceptance-gate.md §2.3 R-4"
    node: null
    mutation: "tests/mutations/R-4.diff"
    added: "2026-10-21"
    runner: plain
    skip_ok: true
  - id: R-5
    claim: "HEAD is self-contained: the app imports from a clean archive of the committed tree."
    source: "docs/proposals/PROPOSAL-acceptance-gate.md §2.3 R-5 (the /clear fix shipped '129 passed' while un-importable from a clean checkout)"
    node: "scripts/clean-checkout-smoke.py"
    mutation: "tests/mutations/R-5.diff"
    added: "2026-10-21"
    runner: plain
  - id: R-6
    claim: "No gate run writes into live state."
    source: "docs/proposals/PROPOSAL-acceptance-gate.md §2.3 R-6 (~93% of 61k turns were fixture sessions in the live store)"
    node: "scripts/live-state-tripwire.py"
    mutation: "tests/mutations/R-6.diff"
    added: "2026-10-21"
    runner: plain
  - id: R-7
    claim: "Feed card update and feed load stay inside a hard time bound, so the 0.62s main-thread stall class cannot return unnoticed."
    source: "docs/proposals/PROPOSAL-META.md §2 (perf bound); PROPOSAL-post-responsiveness-priorities.md P0.1"
    node: "tests/test_perf_feed.py"
    mutation: "tests/mutations/R-7.diff"
    added: "2026-10-21"
    runner: plain
```

**Row-field rules (normative):**

- `node` is a pytest node id (`path::Class::test` or `path::test`) **or** a committed script path under `scripts/` or `tests/`. A path under `.debug/` does not qualify — acceptance evidence must live in the tree.
- `mutation` is a unified diff, appliable with `git apply` at the repo root, relative to the repo root.
- `runner` is one of `plain`, `xvfb`, `xvfb+aa-exec`. `plain` = no wrapper. `xvfb` = `xvfb-run -a <cmd>`.
- `skip_ok: true` is required for a row whose node may legitimately be skipped (R-4 is a placeholder with no node). A row with `node: null` is **not registered** — the gate reports it as a placeholder and does not run it.
- `id` must be unique. `R-<n>` is the convention; the gate does not require the numbers to be contiguous.

### 2.2 `tests/mutations/*.diff` (new)

One unified diff per registered row. Each diff **removes or breaks** the behavior the row claims, so the row's node fails when the diff is applied.

**R-1.diff** — breaks the durable delete in `ui/agent_runtime/session.py`. The node asserts `not json_path.exists()`; deleting the `os.remove(conv_path)` line makes the file survive and the node fail.

```diff
--- a/ui/agent_runtime/session.py
+++ b/ui/agent_runtime/session.py
@@ -115,7 +115,6 @@
             conv_dir = os.path.join(get_config_dir(), "conversations")
             conv_path = os.path.join(conv_dir, f"{session_key}.json")
-            os.remove(conv_path)
             _mod().logger.info(
                 "clear_conversation: deleted persisted conversation %s",
                 conv_path,
```

**R-2.diff** — breaks the follow-after-append pin in `ui/views/chat_surface.py::_inject_script` (the `:195` occurrence, *not* the `:168` one in `_scroll_to_latest_js`; the diff's context lines disambiguate them). The node asserts the script pins the scroll position.

```diff
--- a/ui/views/chat_surface.py
+++ b/ui/views/chat_surface.py
@@ -192,7 +192,6 @@
         "  if (!wasBottom) { return 'held'; }"
-        "  el.scrollTop = el.scrollHeight;"
         + _settle_tail_js()
         "  return 'followed';"
         "})();"
```

> **Implementation note for the implementer:** the two `el.scrollTop = el.scrollHeight;` occurrences are in *different* functions and the diff context must select only `_inject_script`'s. The implementer must generate these diffs from the live files with `git diff` after the edit (not hand-write the hunk headers) and verify each applies with `git apply --check`.

**R-3.diff** — breaks the ARH wiring order in `ui/window.py::_build`. Moving the `AgentRuntimeHandler(...)` construction above the dependents' wiring reproduces the 2026-10-08 crash. Because the mutation must be a *reordering*, the diff swaps the two statements' positions; the context lines must include the `set_agent_runtime_handler` calls.

```diff
--- a/ui/window.py
+++ b/ui/window.py
@@ -237,6 +237,17 @@
         for agent_def in get_special_agents():
             self._agent_runtime_handler.add_special_agent(agent_def)
 
+        self._agent_runtime_handler = AgentRuntimeHandler(
+            main_content=self._main_content,
+            chat_render_handler=self._chat_render_handler,
+            GLib_module=GLib,
+            review_handler=None,
+        )
+
         # Inject into dependents after _agent_runtime_handler is assigned
         self._chat_handler.set_agent_runtime_handler(self._agent_runtime_handler)
```

> **Implementation note:** a mutation that *duplicates* the construction is the simplest form that is guaranteed to apply: the second construction overwrites `self._agent_runtime_handler` with a new instance **after** the dependents were handed the first one, so the window's dependents hold a handler the window no longer owns. The implementer must confirm the smoke actually fails under this mutation (that is the row's whole point) and choose the mutation form that does.

**R-5.diff** — breaks self-containment. The mutation removes a required module from the copied tree (not from the live tree): the gate applies the diff inside the temp copy, where the file is removed from the archive.

```diff
--- a/utils/yaml_load.py
+++ /dev/null
@@ -1,3 +0,0 @@
-"""YAML loading helpers."""
-
```

> **Implementation note:** the R-5 mutation is applied to the **temp copy** after `git archive` extraction, not to the live tree. The diff removes a file that the import chain requires. The implementer picks a file that is genuinely on the import path of `main` (verified by the clean-checkout smoke passing before the mutation and failing after it) — `utils/live_bridge.py` is a candidate (imported by `ui/views/chat_surface.py:66`), but the implementer must verify reachability by running the smoke, not by assumption.

**R-6.diff** — the tripwire's own mutation. The tripwire is self-checking: with isolation *disabled* it must detect a change in the live state and fail. The diff disables the isolation (the `XDG_CONFIG_HOME` override) in the tripwire script, so the run writes into the real config dir and the snapshot comparison fires.

```diff
--- a/scripts/live-state-tripwire.py
+++ b/scripts/live-state-tripwire.py
@@ -20,7 +20,7 @@
 # The gate runs with XDG_CONFIG_HOME pointed at a temp dir; the tripwire
 # compares the LIVE config dir before and after.
-LIVE_CONFIG = Path(os.environ.get("DEVELCAKES_LIVE_CONFIG", Path.home() / ".config" / "develcakes"))
+LIVE_CONFIG = Path(os.environ.get("DEVELCAKES_LIVE_CONFIG", Path.home() / ".config" / "develcakes"))
+os.environ["XDG_CONFIG_HOME"] = str(LIVE_CONFIG.parent)
```

> **Implementation note:** the R-6 mutation is deliberately degenerate-looking — it makes the run write into live state. The gate applies it **only inside the temp copy** and the tripwire must fail there. The implementer finalizes the exact diff against the real script once §2.4 defines that script.

**R-7.diff** — breaks the perf bound. The mutation makes the feed path artificially slow (e.g. adds a deliberate delay in the update path), so the perf node's time assertion fails. The mutation must not hang the suite: a sleep of a few hundred milliseconds in the measured function is enough to blow the bound.

> **Implementation note:** R-7 is the row with the least pinned anchor. The implementer writes `tests/test_perf_feed.py` first, measures the real numbers, then writes the diff against the function the test measures (`ui/handlers/feed_handler.py::update_card` or `utils/feed_store.py::load_feed`). If neither can be mutated cleanly, the row is registered with `skip_ok: true` and a `known_gap` note, and R-7 does not block the gate.

### 2.3 `scripts/acceptance.py` (new)

The runner. One command, four subcommands. Stdlib + pytest, no framework.

**Subcommand table**

| Subcommand | Runs | Exit code |
|---|---|---|
| `rows` | every registered row (mutated-fail, then clean-pass); per-row evidence lines | 0 = all rows pass; 1 = any row failed, was vacuous, or had an unappliable diff |
| `suite` | full pytest, fail-set diffed against `tests/known_failures.txt` — the set may only **shrink**; a new failure refuses | 0 = fail-set ⊆ baseline; 1 = new failure or a baseline entry that disappeared (a shrink is reported, not an error) |
| `smokes` | R-3 window smoke + R-5 clean-checkout import smoke | 0 = both pass; 1 = either fails |
| `all` | `rows` + `suite` + `smokes`, writes `.acceptance/last-run.json`, prints a stdout summary table | 0 = everything passed; 1 = anything failed |

**CLI surface**

```
scripts/acceptance.py rows [--ids R-1,R-2] [--json]
scripts/acceptance.py suite [--json]
scripts/acceptance.py smokes [--json]
scripts/acceptance.py all [--json]
```

**Exact function signatures** (implementer follows these):

```python
ROWS_PATH = Path("docs/acceptance/rows.yaml")
KNOWN_FAILURES_PATH = Path("tests/known_failures.txt")
MUTATIONS_DIR = Path("tests/mutations")
LAST_RUN_PATH = Path(".acceptance/last-run.json")

def load_rows(path: Path = ROWS_PATH) -> list[dict]:
    """Parse rows.yaml via utils.yaml_load.safe_load_yaml.

    Returns the list of row dicts. Raises SystemExit(2) with a clear message
    when the file is missing, is not a mapping, or has no 'rows' list.
    A row missing 'id', 'claim', or 'mutation' is a hard error naming the row.
    """

def registered_rows(rows: list[dict]) -> list[dict]:
    """Rows that have a non-null 'node'. Placeholder rows (node: null) are excluded."""

def row_node_id(row: dict) -> str:
    """The pytest node id, or the script path for a script-backed row."""

def run_node(node: str, runner: str, cwd: Path, env: dict) -> tuple[int, str]:
    """Run one node. Returns (exit_code, combined_output).

    'plain':  python -m pytest <node> -q
    'xvfb':   xvfb-run -a python -m pytest <node> -q
    Script-backed rows (node ends in .py) run the script directly under the
    same wrapper rules; the script's exit code is the result.
    """

def apply_mutation(diff_path: Path, tree: Path) -> tuple[bool, str]:
    """Apply a unified diff inside `tree` with `git apply`. Returns (ok, output).

    Never touches the live tree: `tree` is always a temp copy.
    A failure returns (False, git's own stderr) so the gate can print it.
    """

def make_temp_copy(include_uncommitted: bool) -> Path:
    """Copy the repo into a temp dir.

    include_uncommitted=True: `git archive HEAD` plus an overlay of the working
    tree's uncommitted and untracked files (excluding .git, .venv, __pycache__,
    .pytest_cache, .acceptance, .debug).
    include_uncommitted=False: `git archive HEAD` only.
    """

def check_row(row: dict) -> dict:
    """Run one row mutated-then-clean. Returns a result dict.

    Keys: id, claim, node, mutated_failed (bool), clean_passed (bool),
    vacuous (bool), mutation_error (str), status ("pass"|"fail"),
    mutated_output (str), clean_output (str).
    """

def run_suite(cwd: Path) -> dict:
    """Run the full pytest suite; return {returncode, failed_ids, output}."""

def diff_fail_set(failed: set[str], baseline: set[str]) -> dict:
    """Compare a run's fail set to the baseline.

    Returns {new (sorted list), fixed (sorted list), ok (bool)}.
    ok is False when there is any NEW failure. A shrink (fixed entries) is
    reported but keeps ok True.
    """
```

**Run semantics (normative, from the source proposal §2.1):**

1. For each registered row, in file order:
   a. Make a temp copy of the tree (including uncommitted files).
   b. Apply the row's mutation **inside the copy**. If the diff does not apply → the row **fails loudly** with git's output, naming the expected file and line. The gate does not silently skip.
   c. Run the row's node in the copy. The node **MUST fail**. If it passes → the row is **vacuous** and the gate fails with that verdict. (This is the structural fix for vacuity: a row that cannot fail is refused, not passed.)
   d. Run the row's node in the clean tree. The node **MUST pass**. If it fails → the row fails.
   e. Print one evidence line per row.
2. A node that is **skipped** (pytest exit code 5 with only skips, or the node reports as skipped) counts as a failure unless the row sets `skip_ok: true`. A skip is not a pass.
3. The live tree is never mutated. Only the temp copy is.

**Exception handling (Rule 4 — enumerated):**

- `utils.yaml_load.safe_load_yaml` — the repo's loader; it does not raise on malformed YAML by default (it returns a fallback). The runner must check the parsed shape itself and `SystemExit(2)` on a non-mapping or missing `rows`.
- `subprocess.run` — catches `FileNotFoundError` when `xvfb-run` or `python` is missing → the row fails with a clear message ("xvfb-run not found"), not a traceback.
- `git apply` — returns a non-zero exit code with output on stderr; never raises.
- `git archive` / `tar` — non-zero exit → the row fails with the command output.
- `tempfile.TemporaryDirectory` — cleanup errors are caught and ignored (the directory is under the OS temp root).

**Isolation for the gate's own runs:** every subprocess the gate spawns gets `XDG_CONFIG_HOME` pointed at a fresh temp dir, so a gate run cannot write into `~/.config/develcakes`. This is the R-6 mechanism and it must be in place for *every* subprocess, not just the tripwire row.

**`.acceptance/last-run.json` shape:**

```json
{
  "started": "2026-10-21T12:00:00Z",
  "finished": "2026-10-21T12:03:11Z",
  "subcommands": ["rows", "suite", "smokes"],
  "rows": [{"id": "R-1", "status": "pass", "vacuous": false}],
  "suite": {"ok": true, "new": [], "fixed": []},
  "ok": true
}
```

### 2.4 `scripts/window-smoke.py`, `scripts/clean-checkout-smoke.py`, `scripts/live-state-tripwire.py` (new)

Three committed scripts, each a row's node.

**`scripts/window-smoke.py`** (R-3 node) — constructs `MainWindow` headlessly.

- Runs under `xvfb` (the row's `runner: xvfb`).
- Sets `XDG_CONFIG_HOME` to a temp dir before importing the app.
- Imports `main` **in a subprocess** (see §2.5 — importing `main` has a deliberate side effect and must not happen inside a pytest process) OR imports only `ui.window` and constructs `MainWindow` without touching `main`. The implementer must pick the option that does not import `main` in-process; `ui.window.MainWindow` is the target, and `main`'s import-time side effects are explicitly documented as "do NOT import main from tests".
- The smoke asserts: the window constructs without raising, and `window._agent_runtime_handler is not None` and each dependent received it (`window._chat_handler._agent_runtime_handler is window._agent_runtime_handler`).
- Exit code 0 = pass, non-zero = fail, with the traceback on stderr.

**`scripts/clean-checkout-smoke.py`** (R-5 node) — proves HEAD is self-contained.

- `git archive HEAD | tar -x -C <tmp>` (subprocess, `shell=False` with a pipe, or `subprocess.run` with `stdout=` to a tar file — the implementer picks one and must handle the exit codes of both commands).
- In the extracted tree, run a **subprocess** that imports the app's entry modules: `import ui.window` and `import agent.special_agents` and calls `utils.agent_defs.load_agent_defs()`. The subprocess must run with `cwd=<tmp>` and `PYTHONPATH=<tmp>` so it imports the *extracted* tree, not the live one.
- Fails if any import target or test dependency is untracked in the live tree — the F-1 class. It compares the imported module paths against the archive's file list.
- Exit code 0 = pass.

**`scripts/live-state-tripwire.py`** (R-6 node) — proves the gate run does not write live state.

- Snapshots the live config dir (`utils.config.get_config_dir()`) — path, mtime, size, and a content hash per file — before and after the measured action.
- The measured action is the gate's own row run (a representative subset: the R-1 and R-2 nodes, which touch config-dir state).
- ANY change to the live config dir fails the tripwire.
- Under the R-6 mutation (isolation disabled), the run writes into the live dir and the comparison fires → the row's mutated run fails, as required.
- Must not fail on unrelated machine activity: the snapshot covers only `<config_dir>` and only its immediate files and `conversations/` subdir. A missing dir is not a change.

### 2.5 `tests/known_failures.txt` (new)

The shrink-only baseline. One pytest node id per line, `#` for comments, blank lines ignored.

- The file does not exist today (`ls tests/known_failures.txt` → no such file). The September records cited a 40-failure baseline; the current suite's actual fail set is what the file must contain. The implementer generates it from one full suite run and **reviews each entry** before committing it — a baseline is a record of known-broken things, not a dumping ground. Any entry that no longer fails must be removed (the gate reports a shrink; the file is then updated).
- `suite` compares the run's fail set to this file: a **new** failure refuses; a **fixed** entry is reported as a shrink (ok stays True).
- The file is committed. It is not regenerated automatically by the gate.

### 2.6 `tests/test_perf_feed.py` (new) — R-7

The perf bound, from `PROPOSAL-post-responsiveness-priorities.md` P0.1.

| Assertion | Bound | Source |
|---|---|---|
| `feed_handler.update_card()` with a large feed | < 50 ms | was ~620 ms on 2026-09-11 |
| `feed_store.load_feed()` at `FEED_WINDOW_DEFAULT` | < 100 ms | measured 0.24 s pre-pruning |
| `append_card_update()` cost vs. an empty feed | within 3× | O(1) append invariant |
| main-thread stall while a card update is enqueued | < 5 ms | the user-visible metric |

- The test is marked `slow` (the marker exists in `pytest.ini` for exactly this) so it is excluded from fast suites.
- It builds a realistic feed fixture: `FeedCardData` instances (`models/feed_card.py:100`) at the `FEED_WINDOW_DEFAULT` scale (`utils/feed_store.py:67` = 2000).
- Timing assertions are generous enough to be stable on a loaded machine but tight enough to catch the 0.62 s class. The implementer measures the real numbers first and sets the bounds from the measurement plus headroom.
- A perf row that cannot be made stable is registered with `skip_ok: true` and a `known_gap` note rather than left flaky.

### 2.7 `docs/ARCHITECTURE.md` — one paragraph

Add a short paragraph in the `scripts/` or tooling area describing `scripts/acceptance.py`: what an acceptance row is, that the gate runs each row mutated-then-clean, that mutations apply to a temp copy, and that the live tree is never mutated. Roughly 10 lines. This is the only change to an existing product file besides the CI workflow in §2.8 and the spec-template change in §2.9.

### 2.8 `.github/workflows/acceptance.yml` (new)

CI that runs the gate. `.github` does not exist today.

- Triggers: `push` on `main`, and `pull_request`.
- Installs Python 3.12, runs `pip install -e .` (or the repo's documented install path — the implementer must follow `knowledge/install.md`).
- Runs `python scripts/acceptance.py rows` and `python scripts/acceptance.py smokes`.
- The `suite` subcommand is **not** run in CI on the first landing: the baseline in §2.5 is a known-failing set, and a permanently-red CI is the P1.3 problem the September doc names. CI runs the rows and smokes first; wiring `suite` into CI waits until the baseline is small enough to be meaningful — recorded as a follow-up in the workflow file's header comment, not as a hidden TODO.

### 2.9 Spec template — "Acceptance rows" section

Per the source proposal §2.4: the spec template gains an "Acceptance rows" table, and rows are registered **at spec-write time** (node + mutation), not after implementation. A spec that cannot name its mutation has not stated its claim.

- Locate the spec template in the repo (the implementer finds the file that holds the spec skeleton; `prompts/cc-spec-planning.md` is the current spec-planning prompt and is the likely home — the implementer must confirm the actual template file and edit that one, not a new file).
- Add the section to the template with the table format:

```markdown
## Acceptance rows

| Row | Claim | Node | Mutation | Registered |
|---|---|---|---|---|
| R-n | <one sentence> | `tests/...::test_name` | `tests/mutations/R-n.diff` | YYYY-MM-DD |
```

- Also add a one-line rule to the template: "A spec that cannot name its mutation has not defined its claim."

**Files NOT changed** (already correct):

- `ui/agent_runtime/session.py` — the R-1 behavior is correct; the row's mutation *breaks* it in a temp copy. Only the diff touches it, and only in the copy.
- `ui/views/chat_surface.py` — same for R-2.
- `ui/window.py` — same for R-3.
- `utils/config.py` — `get_config_dir()` already respects `XDG_CONFIG_HOME`; the gate uses that, it does not change it.
- `utils/feed_store.py`, `ui/handlers/feed_handler.py` — the R-7 measurement targets are correct as they are.
- `utils/yaml_load.py` — `safe_load_yaml` already exists and is used as-is.
- `agent/runtime.py`, `agent/tools.py`, `ui/handlers/chat_handler.py` — not involved; the gate is a test-harness tool, not a runtime change.
- `docs/specs/SPEC-19-LIVE-CHAT-SURFACE.md` and other existing specs — rows register lazily; retrofits are a non-goal.
- `tests/conftest.py` — the display-skip behavior is correct and the gate *reads* it (a skip is a failure for a registered row) rather than changing it.
- `pyproject.toml`, `pytest.ini` — no new dependency, no new marker (`slow` already exists).

---

## 3. Data Flow

**`scripts/acceptance.py rows`**

1. Parse argv → subcommand + options. `--ids R-1,R-2` filters the row list.
2. `load_rows()` → `utils.yaml_load.safe_load_yaml(ROWS_PATH.read_text())` → shape check → `SystemExit(2)` on a malformed file.
3. `registered_rows(rows)` drops placeholder rows (`node: null`) and reports them as placeholders.
4. For each row (file order):
   a. `make_temp_copy(include_uncommitted=True)` → a temp dir with the tree.
   b. `apply_mutation(row["mutation"], temp_dir)` → `git apply` with `cwd=temp_dir`. On failure → row status "fail" with `mutation_error` = git's stderr. **The live tree is untouched.**
   c. `run_node(row["node"], row["runner"], temp_dir, env)` with `XDG_CONFIG_HOME` pointed at a second temp dir → `(code, output)`. The node MUST fail (non-zero, or a pytest failure). If it passes → row status "fail", `vacuous: true`.
   d. `run_node(row["node"], row["runner"], repo_root, env)` on the clean tree → the node MUST pass. If it fails → row status "fail".
   e. Print one evidence line: `[PASS] R-1  mutated: FAIL(as required)  clean: PASS  node: tests/...::test_x`.
5. Exit 0 only if every row's status is "pass". Write `.acceptance/last-run.json`.

**`scripts/acceptance.py suite`**

1. `run_suite(cwd)` → `python -m pytest -q` with `XDG_CONFIG_HOME` at a temp dir. Returns the return code and the set of failed node ids (parsed from the pytest output; the implementer must use a machine-readable form — `--tb=no -q` plus a report plugin, or `--junit-xml` into the temp dir, which pytest supports natively and is the more robust parse).
2. Read `tests/known_failures.txt` → baseline set (comments and blanks stripped).
3. `diff_fail_set(failed, baseline)` → `new` (must be empty), `fixed` (reported).
4. Exit 0 if `new` is empty. A `fixed` entry is printed as a shrink and invites updating the baseline file — it does not fail the gate.

**`scripts/acceptance.py smokes`**

1. R-3: `run_node("scripts/window-smoke.py", "xvfb", ...)` → must exit 0.
2. R-5: `run_node("scripts/clean-checkout-smoke.py", "plain", ...)` → must exit 0.
3. Exit 0 only if both pass.

**`scripts/acceptance.py all`**

Runs `rows`, then `suite`, then `smokes`, aggregates, writes `.acceptance/last-run.json`, prints a summary table, exits 0 only if all three succeeded.

---

## 4. File Change Summary

| File | Change | Lines | Risk |
|---|---|---|---|
| `scripts/acceptance.py` | New — the runner | ~400 | Medium. New code, no product impact; the temp-copy + git-apply path is the risky part |
| `docs/acceptance/rows.yaml` | New — the row registry | ~90 | Low. Data only |
| `tests/mutations/R-1.diff` … `R-7.diff` | New — one per row | ~10 each | Low, but they rot when the target files move (the gate fails loudly by design) |
| `scripts/window-smoke.py` | New — R-3 node | ~60 | Medium. GTK construction; must run under xvfb |
| `scripts/clean-checkout-smoke.py` | New — R-5 node | ~80 | Medium. `git archive` + subprocess import |
| `scripts/live-state-tripwire.py` | New — R-6 node | ~90 | Medium. Must not write into live state itself |
| `tests/test_perf_feed.py` | New — R-7 node | ~120 | Medium. Timing assertions can be flaky |
| `tests/known_failures.txt` | New — the shrink-only baseline | ~50 | Low. Must be reviewed, not auto-generated |
| `.github/workflows/acceptance.yml` | New — CI that runs the gate | ~40 | Low |
| `docs/ARCHITECTURE.md` | One paragraph on the gate | ~10 | Low |
| spec template (locate: `prompts/cc-spec-planning.md` or the actual skeleton) | Add the "Acceptance rows" section | ~20 | Low |

**No product code changes.** Every existing module listed in §2.7 is untouched; the mutations break behavior only inside a temp copy.

---

## 5. Implementation Order

1. **`scripts/acceptance.py` core** — `load_rows`, `registered_rows`, `run_node`, `make_temp_copy`, `apply_mutation`, `check_row`, and the `rows` subcommand. Verification: `python scripts/acceptance.py rows` with **zero registered rows** exits 0 and prints "no rows registered" (the registry does not exist yet). Then add `docs/acceptance/rows.yaml` with R-1 and R-2 only and their two diffs; verification: `python scripts/acceptance.py rows --ids R-1,R-2` — both must print `[PASS]`, and a deliberately corrupted diff must make R-1 print `[FAIL] … mutation did not apply` with git's output.
2. **R-1 and R-2 end to end.** Generate each diff from the live file with `git diff` (not hand-written hunks) and verify each applies with `git apply --check`. Verification: the gate's mutated run of R-1 fails on `test_clear_conversation_deletes_json_only` and the mutated run of R-2 fails on `TestInjectionScroll`; the clean runs of both pass.
3. **The three smoke scripts** — `window-smoke.py` (R-3), `clean-checkout-smoke.py` (R-5), `live-state-tripwire.py` (R-6) — plus their diffs. Verification: `python scripts/acceptance.py smokes` passes; then apply each diff by hand into a temp copy and confirm the smoke fails.
4. **`suite` subcommand + `tests/known_failures.txt`.** Run the full suite once, extract the fail set, review each entry, write the baseline. Verification: `python scripts/acceptance.py suite` exits 0 with the current tree; a deliberately renamed baseline entry makes it exit 1 with the new failure named.
5. **R-7 perf row.** Write `tests/test_perf_feed.py`, measure, set bounds, write the diff, register the row. Verification: the row's mutated run fails the time assertion; the clean run passes.
6. **CI** — `.github/workflows/acceptance.yml`. Verification: the workflow file parses as YAML and the commands it runs are the same ones verified above.
7. **Wiring** — the spec-template "Acceptance rows" section, and the `docs/ARCHITECTURE.md` paragraph. Verification: the template contains the table and the one-line rule; ARCHITECTURE.md names the tool, the mutated-then-clean rule, and the temp-copy rule.
8. **Full verification** — `python scripts/acceptance.py all` from a clean checkout of the branch. Everything green; `.acceptance/last-run.json` written.

---

## 6. Acceptance Criteria

- [ ] `python scripts/acceptance.py rows` exits non-zero if any registered mutation still passes (vacuous row), and exits non-zero if any clean node fails.
- [ ] A mutation diff that does not apply fails the gate loudly, naming the expected file and the git output.
- [ ] The live working tree is byte-identical before and after a gate run (no product file is ever mutated by the gate).
- [ ] R-1's mutated run fails `tests/test_live_bridge.py::test_clear_conversation_deletes_json_only`; its clean run passes.
- [ ] R-2's mutated run fails `tests/test_live_guard.py::TestInjectionScroll`; its clean run passes.
- [ ] R-3's mutated run fails the window smoke (the ARH-wiring crash class); its clean run passes.
- [ ] R-5's mutated run fails the clean-checkout smoke; its clean run passes.
- [ ] R-6's mutated run fails the live-state tripwire; its clean run passes.
- [ ] A pytest node that is **skipped** fails its row unless the row sets `skip_ok: true`.
- [ ] `suite` exits non-zero on a failure that is not in `tests/known_failures.txt`, and exits zero when the fail set is a subset of the baseline.
- [ ] Every subprocess the gate spawns runs with `XDG_CONFIG_HOME` pointed at a temp dir.
- [ ] `python scripts/acceptance.py all` writes `.acceptance/last-run.json` and prints a summary table.
- [ ] The spec template contains the "Acceptance rows" table and the rule that a spec which cannot name its mutation has not defined its claim.
- [ ] `.github/workflows/acceptance.yml` exists and runs `rows` and `smokes`.

---

## 7. Edge Cases

| Case | Expected behavior |
|---|---|
| `rows.yaml` missing or malformed | `SystemExit(2)` with a clear message; not a traceback |
| A row has `node: null` (placeholder, e.g. R-4) | Reported as a placeholder; not run; does not fail the gate |
| A row's node id does not exist | The row fails with pytest's "not found" output. The gate never guesses a node |
| A row's node is skipped (no display) | Row fails unless `skip_ok: true` |
| Mutation diff does not apply | Row fails loudly with git's stderr; the live tree is untouched |
| Mutation applies but the node still passes | Row is **vacuous** → gate fails. This is the vacuity fix, and it is a refusal, not a warning |
| Mutation makes the node error (not fail) | Treated as a failure of the mutated run — the node failing is what the row needs |
| `xvfb-run` not installed | The row fails with "xvfb-run not found", not a traceback |
| `git` not available | The gate exits 2 with a clear message; it cannot run without git |
| The working tree has uncommitted changes | The temp copy includes them (uncommitted + untracked, excluding `.git`, `.venv`, `__pycache__`, `.pytest_cache`, `.acceptance`, `.debug`) |
| A mutation target file moved | The diff stops applying → the gate fails loudly and names the file. This is the designed maintenance cost |
| The suite's fail set is smaller than the baseline | Reported as a shrink; `ok` stays True; the baseline file should be updated |
| The suite's fail set has a new entry | `suite` exits 1 and names the new failure |
| `tests/known_failures.txt` missing | `suite` treats the baseline as empty → every failure is new → exits 1. It does not auto-create the file |
| A row's claim has no node and no mutation | Refused at load time (a row missing `id`, `claim`, or `mutation` is a hard error) |
| The gate runs concurrently with itself | Each run uses its own temp dirs; `.acceptance/last-run.json` is written last-writer-wins |

---

## 8. ARCHITECTURE.md Updates Required

One paragraph, in the tooling/`scripts/` area:

> `scripts/acceptance.py` is the acceptance gate. An acceptance row (`docs/acceptance/rows.yaml`) binds one behavioral claim to a pytest node and a mutation diff. The gate runs each row twice — the mutated run must fail, the clean run must pass — so a row that cannot fail is refused rather than trusted. Mutations apply to a throwaway copy of the tree; the live working tree is never mutated. `rows` runs the row set, `suite` runs the full suite and refuses any failure outside `tests/known_failures.txt`, `smokes` runs the window and clean-checkout smokes, and `all` runs everything and writes `.acceptance/last-run.json`. A spec that cannot name its mutation has not defined its claim.

---

## 9. Self-audit

1. Every code sample's function signature was read from source, not assumed: `safe_load_yaml(stream)` (`utils/yaml_load.py`), `get_config_dir() -> str` (`utils/config.py:15`), `update_card(self, card_id: str, card_data: FeedCardData) -> None` (`ui/handlers/feed_handler.py:614`), `append_card_update(project_path, card_id, updates) -> bool` (`utils/feed_store.py:281`), `load_feed(project_path) -> list[FeedCardData]` (`utils/feed_store.py:437`), `FeedCardData` (`models/feed_card.py:100`), `FEED_WINDOW_DEFAULT = 2000` (`utils/feed_store.py:67`).
2. The row registry's node ids were verified to exist by collection: `tests/test_live_guard.py::TestInjectionScroll` → 3 tests collected; `tests/test_live_bridge.py::test_clear_conversation_deletes_json_only` → present at `:279`.
3. Exception types enumerated: `subprocess.run` → `FileNotFoundError`; `git apply`/`git archive`/`tar` → non-zero exit codes, no raise; `safe_load_yaml` → shape-checked by the caller; `tempfile` cleanup → caught and ignored.
4. Key structures verified, not assumed: the row dict keys (`id`, `claim`, `node`, `mutation`, `runner`, `skip_ok`) are the gate's own; the fail set is a `set[str]` of node ids; the baseline file is one node id per line.
5. The two stale anchors in the source proposal were found and replaced: `delete_session_rows` (no such symbol) → `ui/agent_runtime/session.py:118` `os.remove(conv_path)`; `tests/known_failures.txt` (no such file) → created by this spec.
6. The `main.py` import side effect is respected: the R-5 smoke imports the app in a **subprocess**; no `tests/` module imports `main`.
7. The conflict between the acceptance-gate proposal (CI out of scope) and `PROPOSAL-META.md` (CI in Program 1) is resolved in favour of META, in §2.8, with `suite` deliberately not wired into CI on the first landing.
8. R-7 is explicitly allowed to land as `skip_ok: true` with a `known_gap` note, so a flaky perf measurement cannot block the gate. R-3 and R-5 depend on GTK/`git archive` behavior that is verified at implementation time; if either cannot be made to fail under its mutation, the row is registered with `skip_ok: true` rather than left vacuously passing.
