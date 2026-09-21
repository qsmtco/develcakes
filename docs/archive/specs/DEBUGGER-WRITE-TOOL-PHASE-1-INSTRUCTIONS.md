# DEBUGGER-WRITE-TOOL Phase 1 — Instructions (Coder)

**Goal:** Debugger stops writing files via exec_command heredocs. Moves file
creation from opaque 22KB approval-card shell commands to named, small-card,
audit-logged write_file calls. Bonus: closes our last 2 unexplained baseline
failures (39 → 37).

**Root cause (verified by Supervisor, tree at 1be591c):**
1. `prompts/system/debugger.md:1` — "do NOT write files unless the PM
   explicitly asks"; Tool Strategy lists only exec/read/search/list/web.
2. `prompts/default_agents/debugger.yaml` — no write_file/edit_file in tools.
3. `llm_name: openai/gpt-4o` (line 6) — validate_agent_def
   (utils/agent_defs.py:375, skip at :247 "LOW-11") rejects it → Debugger
   silently absent on fresh installs → both test_special_agents failures.
4. tests/test_special_agents.py:162 `test_debugger_no_write_tools` pins the
   read-only design; :211 `test_debugger_si_context_only` fails only because
   the agent fails validation and get_special_agent returns None.
   (User config ~/.config/crabcakes/agents/debugger.yaml ALREADY has
   write_file/edit_file + llm_name: M3 — only the SHIPPED template is broken.)

## EDITS

**Edit 1 — prompts/system/debugger.md:**
- Line 1: replace "do NOT write files unless the PM explicitly asks" with
  text that keeps the investigate/diagnose/report mandate but directs:
  "When a file must be created or modified (test scaffolds, probe scripts,
  audit scratch), use write_file/edit_file — NEVER create files through
  exec_command heredocs (cat > file <<EOF): they emit oversized approval
  cards and bypass the audit trail's structure."
- Add "### write_file" and "### edit_file" subsections to Tool Strategy:
  when to use (scratch probes, audit artifacts, writing reproduction files),
  the heredoc ban, and the note that write_file needs no approval (small
  card) while exec_command always does.

**Edit 2 — prompts/default_agents/debugger.yaml:**
- tools: add edit_file + write_file (after search_files, before web_*).
- llm_name: remove the invalid `openai/gpt-4o` placeholder. Leave the line
  OUT entirely (fall through to provider default) — read
  utils/agent_defs.py validate_agent_def first to confirm a missing
  llm_name passes validation (it should: coder.yaml ships without one —
  verify and match coder.yaml's pattern exactly).
- Keep fallback_provider and self_improvement as-is.

**Edit 3 — tests/test_special_agents.py:**
- `test_debugger_no_write_tools` (:162) — INVERT deliberately, rename
  `test_debugger_has_write_tools`: assert "write_file" in tools,
  "edit_file" in tools, can_write is True. Docstring must state the design
  change and why (heredoc approval-card blowup; write_file is the audited,
  small-card path; the old read-only posture was nominal anyway since
  exec_command could always write).
- `test_debugger_si_context_only` (:211) — stays as-is; it should go green
  on its own once the agent passes validation.

## GATES

- RED-first: both tests fail at HEAD (they already do — capture output).
- GREEN: test_special_agents.py fully green; log shows NO "LOW-11: skipping
  invalid agent def debugger.yaml" warning during the test run.
- Simulated fresh-install check: with XDG_CONFIG_HOME pointed at an empty
  tmp dir, `get_special_agent("special:debugger")` returns a valid def with
  write tools (this is the actual regression being fixed).
- pyflakes clean on touched test file.
- Suites: test_special_agents green; quick smoke that
  tests/test_agent_defs.py (or the validate suite) stays green.
- One commit: `feat(agents): debugger write-tool posture — heredoc ban in prompt, tools+validation fix in template (closes 2 baseline failures)`
- Files: exactly 3 (prompt, yaml, test file). No python source changes.

## OUT OF SCOPE (do not touch)

- The user's live ~/.config/crabcakes/agents/debugger.yaml (already correct).
- audit-cleanup-3 work unit reassignment (Supervisor handles separately).
- supervision prompts for coder/supervisor (their heredoc habits are
  separate follow-ups if the PM wants them).
