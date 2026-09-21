# Phase 3 of 4 — Architectural & Low Severity Findings

**Master spec:** `docs/specs/SPEC-SECURITY-REMEDIATION.md`

**Phase 0+1+2 shipped.** Hotfix `d5cb6c9` (CSS fix) also done. **26 findings remain. This is the final phase.**

**Scope:** LOW-1 through LOW-13 + A-4, A-6, A-8, A-9, A-10 = **16 findings.**

---

## MANDATORY: Read every file in full before writing code

**Files to read (ALL of each, not snippets):**
1. `agent/runtime.py`
2. `agent/tools.py`
3. `agent/kb_server.py`
4. `gateway/client.py`
5. `gateway/server.py`
6. `ui/window.py`
7. `ui/toolbar.py`
8. `ui/handlers/gateway_handler.py`
9. `utils/file_security.py` (Phase 2)
10. `utils/agent_defs.py`
11. `utils/mcp_config.py`
12. `utils/feedback_processor.py`
13. `utils/markdown.py`
14. `tests/test_*.py` (existing patterns)

---

## The 16 Findings

All edits anchor to **identifiers, not line numbers**. Current HEAD is `d5cb6c9`.

### LOW-1 — `agent/runtime.py` — Set `user` field on `Task`
Find `Task(` constructor calls. Add `user=self._config.user_id` (or similar field from config). Pure traceability addition.

### LOW-2 — `agent/kb_server.py` — Add `DELETE /agents/{id}` endpoint
New route handler. Loads agent def, removes from KB store, returns 204. Logs deletion.

### LOW-3 — `agent/kb_server.py` — Add `GET /agents` listing endpoint
Returns JSON list of all registered agent ids. `[]` if none.

### LOW-4 — `utils/agent_defs.py` — Log on `save_provider` overwrite
Before writing, log INFO if provider already exists ("overwriting existing provider").

### LOW-5 — `utils/mcp_config.py` — Detect legacy `~/.mcp.json`
At module load, if `Path.home() / ".mcp.json"` exists, log one-time warning suggesting migration.

### LOW-6 — `gateway/client.py` — Schema validation in `_load_identity`
After `json.load(f)`, validate the dict has required keys. If malformed, delete file and regenerate. **HIGH RISK: this is the same function as hotfix `851ca12`. Do NOT drop the file read again.**

### LOW-7 — `agent/kb_server.py` — Log bound address on startup
After bind, log actual bound address and port.

### LOW-8 — `ui/handlers/gateway_handler.py` — DEBUG log for agent_id
Add `logger.debug("Connecting with agent_id=%s", self._agent_id)` before `self._gw.start()`.

### LOW-9 — `ui/window.py` — Tooltip on connect button
Set tooltip text and `has_tooltip = True` on connect button.

### LOW-10 — `gateway/client.py` — WebSocket retry logging
Log initial connection failures at INFO (not WARNING) with retry counter.

### LOW-11 — `gateway/client.py` — `uuid.uuid4().hex` for device_id
Ensure device_id generation uses `uuid.uuid4().hex` (32 hex chars).

### LOW-12 — `utils/feedback_processor.py` — `get_session_id()` helper
Add stable session identifier helper. Use in feedback entries.

### LOW-13 — `agent/tools.py` — `requires_approval` decorator support
Add `@tool(requires_approval=True)` flag. No existing tools flagged.

### A-4 — `agent/runtime.py` — Audit log for tool execution
Add structured audit logging: tool name, args hash, approval decision, user, timestamp, result hash. Wired into tool execution loop. In-memory default.

### A-6 — `agent/kb_server.py` — Subprocess refactor
If current code uses `subprocess.run(..., shell=True)` with user-controlled args, refactor to: argv list, `shell=False`, stdin via FD, timeout, no shell metacharacter interpretation. **If shell=True with attacker input exists, flag as CRITICAL regression.**

### A-8 — `gateway/server.py` — Agent-id authorization on registration
Verify agent_id against known list from agent_defs. Reject unknown with 401.

### A-9 — `ui/window.py` — Display agent_id in UI
Add read-only label showing registered agent_id.

### A-10 — `agent/kb_server.py` — Bind failure error message
Clear error on port conflict: address tried, OS error, hint about env var.

---

## Rules
- Read every file in full before editing
- Anchor to identifiers, not line numbers
- Scope = exactly 16 findings. No scope creep.
- Do NOT touch Phase 0/1/2 work
- No new dependencies
- **HIGH RISK edits: LOW-6, A-4, A-6, A-8** — write integration tests with real code paths (no monkeypatch)
- Test suite must pass before committing

## Verification (all must pass)
```bash
cd /path/to/projects/crabcakes
python3 -m pytest tests/ -q --no-header 2>&1 | tail -5
python3 -c "
import sys; sys.path.insert(0, '.')
import utils.markdown, agent.runtime, agent.tools, agent.kb_server, gateway.client, ui.window
from gateway.client import _load_identity
identity = _load_identity()
assert isinstance(identity, dict) and len(identity) > 0
print('ALL IMPORTS + _load_identity PASS')
"
grep -rn "text-align" --include="*.py" . 2>/dev/null
echo "text-align check: $?"
```

## Report format
Send completion report with: files changed, verification output, COMPLETENESS checklist (all 16 findings with `[x]` or `[NOT DONE] WHY`), any issues found.

**Required word marker for /ask acknowledgment: "please write"**
