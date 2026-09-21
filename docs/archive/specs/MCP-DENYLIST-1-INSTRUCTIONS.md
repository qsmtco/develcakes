# MCP-DENYLIST-1 — Instructions (Coder)

**Goal (PM-approved):** replace MED-12's env allowlist with a DENYLIST of
genuinely dangerous variables, so credential vars (`${GITHUB_TOKEN}` etc.)
can be forwarded to MCP servers again while the code-injection threat stays
blocked.

## Verified anchors (Supervisor, tree at db2cbe7)

- `utils/mcp_config.py:30` — `_MCP_FORWARDABLE_ENV_VARS: frozenset[str] = frozenset({"PATH", "HOME", "LANG", "VIRTUAL_ENV", "PYTHONPATH"})`
- `utils/mcp_config.py:78` — `if var_name not in _MCP_FORWARDABLE_ENV_VARS:` → warn + `continue` (the refusal)
- `tests/test_mcp_config.py` — `test_env_var_substitution_allowlisted_var_is_substituted`
  (uses `${PATH}`) and `test_env_var_refused_when_not_in_med12_allowlist`
  (uses `${TEST_MCP_TOKEN}` — currently asserts refusal; that assertion INVERTS
  under this change).

## EDITS

**Edit A — `utils/mcp_config.py`:** replace `_MCP_FORWARDABLE_ENV_VARS` with
`_MCP_DANGEROUS_ENV_VARS` (a denylist). Keep `frozenset`, keep the name style.
Rename the check to `if var_name in _MCP_DANGEROUS_ENV_VARS:` → warn + skip
with a MED-12 message (keep the "MED-12" token so the audit trail stays
greppable). Include at minimum:

  Code-injection / loader hijack:
  LD_PRELOAD, LD_LIBRARY_PATH, LD_AUDIT,
  DYLD_INSERT_LIBRARIES, DYLD_LIBRARY_PATH, DYLD_FRAMEWORK_PATH,
  BASH_ENV, ENV, PROMPT_COMMAND,
  PYTHONSTARTUP, PYTHONHOME, PYTHONPATH,
  NODE_OPTIONS, PERL5LIB, PERL5OPT, RUBYOPT,
  JAVA_TOOL_OPTIONS, _JAVA_OPTIONS,
  GIT_SSH_COMMAND, GIT_EXTERNAL_DIFF, GIT_CONFIG, GIT_CONFIG_SYSTEM,
  GIT_CONFIG_GLOBAL, GIT_ASKPASS, SSH_ASKPASS

  Add a short module comment stating the rationale (denylist targets vars that
  can hijack code execution or intercept credentials; credential vars the user
  deliberately names in their own config ARE forwarded — that is the field's
  purpose) and the residual risk (a user naming a secret they shouldn't expose
  to a third-party server is their call, same trust boundary as their own shell).

**Edit B — `tests/test_mcp_config.py`:** rewrite the two tests as a pair:
1. `test_env_var_substitution_forwards_credential_var` — `${TEST_MCP_TOKEN}`
   (a non-denylisted var) → substituted into `params.env["TOKEN"]`, no warning.
   This is the behaviour the PM restored. RED on current code.
2. `test_env_var_refused_when_denylisted` — `${LD_PRELOAD}` (denylisted, set in
   os.environ to a dummy value) → key omitted, MED-12 warning logged via caplog,
   and the security invariant asserted as `"LD_PRELOAD" not in (params.env or {})`
   (decoupled from the env=None degrade choice — see the existing pattern in the
   file, audit suggestion from TEST-DEBT-3). RED on current code (it currently
   forwards nothing, so the first test fails and this one's warning text differs).
3. Keep/extend a test that a mix (one denylisted + one allowed) forwards only
   the allowed one.

## GATES

- RED: both rewritten tests fail on current code (paste output).
- GREEN: `tests/test_mcp_config.py` fully green ×2 consecutive runs.
- Hermetic: `try/finally` env restore (existing pattern); no real config writes.
- pyflakes /tmp/pf-venv3: 0 undefined on both files.
- Full suite: baseline is **2F/3844P** (the 2 architecture product items — this
  unit must not change them). Report the count.
- One commit: `fix(mcp): denylist dangerous env vars instead of allowlisting (MED-12 rework, PM-approved)`
- Flag (don't fix) anything adjacent.

Then STOP — audit next.
