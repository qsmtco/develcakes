# CrabCakes Threat Model

**Last updated:** 2026-06-19
**Status:** Living document — update when architecture or trust assumptions change.

---

## System Description

CrabCakes is a GTK4 desktop application that connects to an OpenClaw gateway via WebSocket (device-auth, operator role). It provides a chat interface for interacting with AI agents, managing projects, and executing development tasks. Agents can read/write files, execute shell commands (with user approval), and interact with external services via the gateway.

**Runtime context:**
- Runs as the local user on a Linux desktop
- GTK4 GUI (requires display server)
- Connects to OpenClaw gateway on localhost or remote host
- Agents execute tools (file I/O, shell) on the host machine

---

## Trust Boundaries

### Boundary 1: User ↔ CrabCakes

**Trust level: Fully trusted**

The user configures agents, approves shell commands, and controls the application. There is no adversarial relationship between user and application.

**Implications:**
- Config files (agent.json, agent YAML) are trusted input
- UI approval gates are UX features, not security boundaries
- The user is assumed to read and understand agent configurations before deploying them

### Boundary 2: CrabCakes ↔ OpenClaw Gateway

**Trust level: Trusted transport, authenticated**

Connection is authenticated via Ed25519 device keys. The gateway is assumed to be under the user's control (localhost or trusted remote).

**Implications:**
- Gateway events are trusted data
- Session keys and message content are not tampered with in transit
- Gateway is responsible for agent lifecycle, not CrabCakes

### Boundary 3: CrabCakes ↔ Agent Runtime

**Trust level: Varies — see Agent Trust Tiers below**

Agents run locally with tool access (file I/O, shell execution). The level of trust depends on the agent's origin and configuration.

### Boundary 4: CrabCakes ↔ Host System

**Trust level: Full access, user-level**

CrabCakes and its agents run as the local user. They have access to everything the user can access — filesystem, network, processes. This is by design.

**Implications:**
- No sandboxing, no namespace isolation, no containerization
- File writes go directly to the host filesystem
- Shell commands execute as the user via the host shell
- Network access is unrestricted

---

## Agent Trust Tiers

### Tier 1: User-Configured Agents (Current Default)

**Trust: High**

Agents the user creates via the agent builder, with known system prompts, known tools, and known provider/model configurations. These are extensions of the user's intent.

**Assumptions:**
- The user chose the system prompt and tools
- The user trusts the LLM provider not to be actively malicious
- The agent acts in the user's interest

**Protection needed:** Accidental damage only (approval gates, confirmation dialogs).

### Tier 2: Community/Shared Agents (Future)

**Trust: Medium**

Agents shared by other users or loaded from external sources. System prompts may not be fully audited. Tool configurations may grant more access than the user realizes.

**Assumptions:**
- The agent prompt may contain instructions the user hasn't fully read
- The agent may attempt actions the user wouldn't explicitly approve
- The LLM provider may interpret prompts in unexpected ways

**Protection needed (when this tier is implemented):**
- Agent prompt review UI (show full system prompt before first run)
- Tool permission summary at agent launch
- Write-then-execute detection (flag when agent writes a file then tries to exec it)
- Optional namespace isolation via `bwrap` or `podman`
- Rate limiting on shell execution and file writes

### Tier 3: Untrusted Agents (Not Currently Supported)

**Trust: Low**

Agents from untrusted sources, potentially adversarial. Not currently a supported use case.

**Protection needed (if ever supported):**
- Full sandboxing (namespace isolation, filesystem mounts, network restrictions)
- No direct shell access — all operations through sandboxed tool implementations
- Resource limits (CPU, memory, time, file size)
- Audit logging of all agent actions

---

## Known Attack Surfaces

### 1. Shell Command Execution

**Current state:** Commands run directly on the host as the user. UI approval gate prompts before execution.

**Known bypass:** Write-then-execute. An agent can write a script to disk (no approval needed for file writes) and then execute it (approval needed, but the user sees only the exec command, not the file contents).

**Risk today (Tier 1 agents):** Low. The agent is user-configured and trusted. Approval gate catches accidents.

**Risk with Tier 2 agents:** Medium. The user may not realize what the agent wrote to disk before executing it.

**Mitigation path:** When Tier 2 is implemented, add a pre-execution check that warns if the target file was written by an agent in the current session. Log all file writes with timestamps for audit trail.

### 2. File System Access

**Current state:** Agents can read and write files anywhere the user has access. No restrictions on paths.

**Risk:** An agent could overwrite important files (`~/.bashrc`, `~/.ssh/authorized_keys`, project source). The approval gate only applies to shell execution, not file writes.

**Mitigation path:** When Tier 2 is implemented, add configurable path restrictions per agent (whitelist of allowed directories). For Tier 1, the current open access is correct — developers need it.

### 3. API Key Exposure

**Current state:** API keys stored in `~/.config/crabcakes/agent.json` (plaintext, 0o600 file, 0o700 directory).

**Risk:** Any process running as the user can read the keys. LLM API keys are bearer tokens for billable cloud services — a leaked key can rack up significant charges.

**Acknowledged gap:** The "everyone does it this way" argument is weak for LLM keys specifically. SSH keys are used locally; LLM keys are billable remotely. This is a higher-value credential than a typical local secret.

**Mitigation path:** Integrate `keyring` library with graceful fallback to plaintext. Try system keyring (libsecret/GNOME Keyring) first; if unavailable, fall back to current plaintext storage. Zero disruption, strictly better when keyring is available. Tracked as backlog item.

### 4. Pickle Deserialization

**Current state:** `converge/model.pkl` and `vectorizer.pkl` loaded via `joblib` on startup.

**Risk:** Arbitrary code execution if a malicious `.pkl` is substituted.

**Mitigation:** Documented in `converge/converge.py` with remediation path for when repo goes public. Private repo, two contributors — risk is theoretical today.

### 5. Gateway WebSocket

**Current state:** Device-auth Ed25519, connection to localhost or trusted remote.

**Risk:** If gateway is compromised, attacker controls all agent communication. If connection is to a remote host, MITM could inject events.

**Mitigation:** Localhost connections are safe by definition. Remote connections should use WSS (TLS). Device-auth Ed25519 prevents impersonation. Gateway security is OpenClaw's responsibility.

---

## Defenses Implemented (as of 2026-06-19)

The following security findings from `docs/SECURITY_ARCHITECTURE_REVIEW.md` have been remediated across Phases 0–5 of the security-remediation spec. This section tracks the defenses currently in production so future architectural changes can reason about what's already protected.

### Image Viewer Path Hardening (LOW-7, Phase 5)

`ui/views/chat_bubble.py:_open_in_viewer` previously called `xdg-open` on any file path — including paths embedded in agent output. An LLM could suggest a path like `/etc/passwd` or `~/.ssh/id_rsa` and trick a user who clicks the link into opening sensitive files in their desktop's default handler.

**Defense:** Three helpers in `ui/views/chat_bubble.py` (`_ALLOWED_ROOTS_FALLBACK`, `_get_allowed_roots`, `_is_path_in_allowed_roots`) gate every call to `subprocess.Popen`. The validator:
- Resolves symlinks via `os.path.realpath` (symlink-to-`/etc/passwd` is rejected)
- Checks the resolved path is under an allowed root: active project path, `~`, or `/tmp`
- Logs a WARNING for rejected paths

**Active project path wiring:** `ui/window.py` project open/close callbacks call `set_active_project_path(p)` / `clear_active_project_path()` from `ui/wiring.py`. Without the wiring, the validator only has the `(home, /tmp)` fallback roots.

**Limitation:** env vars are process-global. If two projects are open simultaneously, only the most recently opened is in scope. The fallback `(home, /tmp)` is always present.

### Feed Card Atomic Write (LOW-13, Phase 4)

`utils/feed_store.py` previously wrote `feed.json` non-atomically. A power loss, OOM kill, or crash mid-write truncated the entire feed (all cards lost).

**Defense:** Two helpers `_atomic_write_json` (0o600) and `_atomic_write_text` (0o644) use `tmp + os.replace` for atomicity. All three write functions (`save_feed`, `append_feed_card`, `update_feed_card`) use these helpers.

### Project Gitignore Auto-Update (LOW-12, Phase 4)

`.crabcakes/feed.json` may contain pasted secrets (code snippets, file paths, API keys). It was not auto-gitignored — developers had to remember to add it manually.

**Defense:** `_ensure_gitignore_entry` in `utils/feed_store.py` creates/updates the project `.gitignore` with `.crabcakes/feed.json` on first save. Whole-line match prevents duplicate entries; commented entries are correctly treated as absent.

### Agent Definition Validation on Load (LOW-11, Phase 3)

`utils/agent_defs.py:load_agent_defs` previously loaded agent YAMLs without running `validate_agent_def`. Invalid defs (unknown tools, missing fields) could propagate into the runtime.

**Defense:** `load_agent_defs` now calls `validate_agent_def` per def. Role-aware exemptions: helper role allows empty `llm_name` (because `ensure_kb_provider` patches it at startup). Invalid defs are logged as WARNING and skipped.

### Diff Path Parsing (LOW-10, Phase 3)

`utils/diff_parser.py` used `lstrip("a/")` which strips a *character set*, mangling paths like `a/app.py` to `pp.py`.

**Defense:** Replaced with `re.sub(r'^a/', '', p)` (prefix removal). `a/app.py` correctly becomes `app.py`.

### Other Defenses

- **LOW-4 (Secrets in Logs)** — gateway client now logs type and length, never raw message content
- **LOW-5 (Event Type Validation)** — gateway client requires `isinstance(payload, dict)` plus event-name allowlist
- **LOW-6 (STT Supply Chain)** — model size allowlist, pinned `download_root`, `local_files_only=True`
- **LOW-8 (SVG Injection)** — `xml.sax.saxutils.escape` for text, `^#[0-9A-Fa-f]{6}$` validation for colors
- **LOW-9 (Git Error Info Disclosure)** — `str(e)` from GitPython no longer surfaced to UI; logged full, shown generic
- **A-10 (Dead Code)** — `utils/image_utils.py` deleted; `utils/review_log.py:19` comment corrected
- **MED-3 (SSRF)** — `web_fetch` resolves host, rejects private/loopback/link-local, restricted to http/https
- **HIGH-3 (API Key in Conversation Files)** — `api_key` removed from serialization; re-resolved on load from `providers.yaml`; conversations dir 0700, per-file 0600
- **HIGH-6 (Link Scheme Allowlist)** — non-allowlisted schemes (`file://`, `smb://`, etc.) render with red warning prefix U+26A0 in Pango bold; still clickable (user agency)

For the full list of 43 shipped findings, see `docs/SECURITY_ARCHITECTURE_REVIEW.md` §4.4 and the Phase 0–3 post-mortem (`docs/post-mortems/2026-06-19-SECURITY-REMEDIATION-PHASE-0-3-POST-MORTEM.md`).

---

## Threats NOT in Scope

- **Physical access to the machine:** If someone has physical access, game over regardless of software security.
- **Malware on the host:** If the host is compromised, CrabCakes security measures are irrelevant.
- **LLM provider compromise:** If the provider is malicious or compromised, it can inject arbitrary responses. CrabCakes cannot defend against this.
- **Supply chain attacks on dependencies:** `PyGObject`, `websockets`, `cryptography` — if these are compromised, CrabCakes is compromised. Standard hygiene (pinning versions, monitoring advisories) applies.

---

## When to Revisit This Document

- Adding support for community/shared agents (Tier 2)
- Adding support for untrusted agents (Tier 3)
- Adding multi-user support
- Making the repo public
- Adding network-accessible API endpoints to CrabCakes
- Integrating third-party plugins or extensions
- Significant changes to the gateway protocol
- **LOW-7 follow-up:** If the env-var pattern (process-global active project path) becomes a limitation (e.g. per-window projects are introduced), refactor `_is_path_in_allowed_roots` to take an explicit `project_path` argument instead of reading from `os.environ`

---

## References

- `docs/SECURITY_REVIEW_2026-05-29.md` — Original audit findings and disposition
- `agent/config.py` — API key storage and permission enforcement
- `converge/converge.py` — Pickle deserialization security note
- `ui/views/agent_builder.py` — Agent configuration UI
