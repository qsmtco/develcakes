# OpenClaw Gateway in DevelCakes

The OpenClaw gateway is the bridge between DevelCakes and remote AI agents. While special agents (Coder, Debugger, Auxilium) run locally inside DevelCakes, **gateway agents** run on an OpenClaw gateway server and communicate with DevelCakes over WebSocket.

---

## What Is the OpenClaw Gateway?

The gateway is a separate service (part of the OpenClaw ecosystem) that hosts AI agents, manages their sessions, and routes messages. DevelCakes connects to it as a client — receiving agent messages, sending user input, and monitoring agent health.

When connected, the gateway provides:
- **Agent discovery** — A snapshot of available agents and their recent sessions
- **Message routing** — Send/receive chat messages with remote agents
- **Event streaming** — Real-time events for agent activity, tool calls, and responses
- **Session management** — Multiple concurrent conversations per agent

---

## How DevelCakes Connects

### Connection Protocol

DevelCakes uses a **WebSocket** connection with **Ed25519 device authentication** (protocol v3/v4):

1. **WebSocket connect** — DevelCakes opens a WebSocket to the gateway URL (default `ws://localhost:18789`)
2. **Receive nonce** — Gateway sends a nonce challenge
3. **Sign payload** — DevelCakes signs a v3 auth payload with the device's Ed25519 private key
4. **Send connect request** — Includes device ID, public key, signature, device token, scopes, and client metadata
5. **Receive hello-ok** — Gateway validates the signature and returns a snapshot of agent health
6. **Listen** — DevelCakes pumps the WebSocket for events and responses

### Auth Payload Format

The signed payload contains:

```
v3|{device_id}|openclaw-control-ui|ui|operator|{scopes}|{timestamp}|{device_token}|{nonce}|linux|linux
```

Signed with the device's Ed25519 private key, then base64-encoded.

### Device Identity Files

DevelCakes loads device credentials from `~/.openclaw/identity/`:

- **`device-auth.json`** — Contains `deviceId` and `tokens` (operator token used for auth)
- **`device.json`** — Contains `privateKeyPem` (Ed25519 private key in PEM format)

These are created by running `openclaw login` or `openclaw device register` in a terminal. If either file is missing or corrupted, the connection fails with a clear error message.

### Connection Lifecycle

The `GatewayHandler` manages the full lifecycle:

1. **`connect()`** — Creates `GatewayClient` + `AgentManager`, sets toolbar to "connecting", starts the background thread
2. **`on_connected()`** — Called from the gateway thread on success. Dispatches via `GLib.idle_add()`:
   - Updates toolbar to "connected"
   - Loads agent snapshot into `AgentManager`
   - Syncs live `GatewayClient` to `ChatHandler`
   - Populates the sidebar agent list
3. **`on_error(msg)`** — Called on connection failure or disconnect. Sets toolbar to "disconnected".
4. **`disconnect()`** — Stops the client, clears agents, sets "disconnected"

### Reconnection

The `GatewayClient._connect_loop()` implements automatic reconnection with exponential backoff:
- Initial retry delay: 1 second
- Maximum delay: 30 seconds
- Doubles on each failure: 1s → 2s → 4s → 8s → 16s → 30s
- Resets to 1s on successful connection

The connection keeps running until `stop()` is called or the app closes.

---

## Configuring the Gateway Connection

### Gateway URL

The gateway WebSocket URL is controlled by the `CRABCAKES_GATEWAY_URL` environment variable:

```bash
# Default
export CRABCAKES_GATEWAY_URL=ws://localhost:18789

# Remote gateway
export CRABCAKES_GATEWAY_URL=ws://my-server.local:18789
```

If not set, defaults to `ws://localhost:18789`.

### Identity Directory

Device identity lives at `~/.openclaw/identity/` by default. This path is not configurable via DevelCakes — it's owned by OpenClaw.

To set up identity:
```bash
openclaw login
# or
openclaw device register
```

### Debug Mode

Enable raw WebSocket message logging:

```bash
export CRABCAKES_GATEWAY_DEBUG=1
```

This sets the gateway client logger to DEBUG level, showing raw message frames. Independent of the general `DEVELCAKES_DEBUG` flag.

### Settings File

DevelCakes configuration lives at `~/.config/develcakes/` (respects `$XDG_CONFIG_HOME`):

- `config.json` — General settings
- `agent.json` — LLM provider configuration
- `providers.yaml` — Provider definitions (API keys, base URLs, models)
- `agents/*.yaml` — Special agent definitions
- `mcp-servers.json` — MCP server configuration
- `projects/` — Per-project legacy config

---

## Gateway Agents vs Special Agents

| Aspect | Special Agents | Gateway Agents |
|--------|---------------|----------------|
| **Where they run** | Locally, inside DevelCakes (via `AgentRuntime`) | Remotely, on the OpenClaw gateway server |
| **How they authenticate** | No auth needed — they're local processes | Ed25519 device authentication over WebSocket |
| **Tool execution** | Direct: `subprocess.run()`, file I/O, httpx | Proxied through the gateway server |
| **LLM calls** | Direct from DevelCakes to the LLM provider | Handled by the gateway server |
| **System prompts** | `prompts/system/{role}.md` files | Managed in gateway agent configuration |
| **Conversation storage** | In-memory + JSON files (`<config_dir>/conversations/`) | Managed by the gateway server |
| **Session keys** | `special:{role}` (e.g. `special:coder`) | `agent:{name}:{platform}:{channel}:{id}` |
| **Availability** | Always available (works offline) | Requires active gateway connection |
| **Tool set** | 8 local tools (read, write, edit, exec, list, search, web) | Gateway-managed, varies per agent |
| **Cost tracking** | Local — via `on_token_usage` callback | Gateway-side — usage APIs |
| **MCP integration** | Direct subprocess connection | Via gateway MCP configuration |

### When to Use Which

- **Special agents** — Local development, offline work, direct file manipulation, running tests, custom tool configurations
- **Gateway agents** — Remote work, multi-device access, agents managed by OpenClaw, integrations with external services

Both types can coexist in the same DevelCakes session. Project teams can include both special and gateway agents.

---

## Session Management

### Snapshot

On connect, the gateway sends a health snapshot containing agent information:

```json
{
  "health": {
    "agents": [
      {
        "agentId": "qtr",
        "name": "QTR",
        "sessions": {
          "recent": [
            {"key": "agent:qtr:telegram:direct:12345"},
            {"key": "agent:qtr:discord:guild:67890"}
          ]
        }
      }
    ]
  }
}
```

This snapshot is validated by `_validate_snapshot()` — it must contain `health.agents` with each agent having `agentId` and `sessions` fields. Invalid snapshots raise `SnapshotValidationError` and prevent connection.

### Agent Registration

After receiving the snapshot, the `AgentManager` registers:
- Each recent session key under the agent's name
- A synthetic `agent:{id}:main` key if no sessions exist (so the agent always appears in the sidebar)

### Sending Messages

Messages are sent via `chat.send` RPC:

```python
gateway_client.send_message(
    session_key="agent:qtr:telegram:direct:12345",
    text="Hello!",
    on_sent=callback
)
```

The gateway responds with a `runId` on success or an error on failure. Pending requests time out after 30 seconds.

### Keepalive

A tick loop fires `on_tick()` every 15 seconds while connected. This drives heartbeat-driven tasks in the main window (like periodic checks).

---

## What Happens When the Gateway Goes Offline

### Immediate Effects

1. **Toolbar** — Changes to "disconnected" state
2. **WebSocket events stop** — No new agent messages arrive
3. **Pending RPCs drain** — All pending requests fire with error callbacks ("connection closed")
4. **Agent list persists** — The sidebar still shows known agents, but they're unreachable

### Special Agents Still Work

Special agents (Coder, Debugger, Auxilium) are **completely independent of the gateway**. They:
- Run via local `AgentRuntime`
- Make LLM API calls directly to configured providers
- Execute tools locally
- Persist conversations locally

So even with the gateway offline, you can continue working with local agents on your projects.

### Automatic Reconnection

The `GatewayClient` retries with exponential backoff (1s → 30s max). Once the gateway comes back online:
- Connection re-establishes
- A fresh snapshot loads
- The toolbar returns to "connected"
- Messages flow again

---

## Troubleshooting Connection Issues

### "OpenClaw identity not found"

The device identity files at `~/.openclaw/identity/` are missing. Fix:

```bash
openclaw login
# or
openclaw device register
```

This creates `device-auth.json` and `device.json` with valid credentials.

### "OpenClaw identity file is corrupted (invalid JSON)"

The identity file exists but is unreadable. Re-run `openclaw login` to regenerate.

### "Missing 'deviceId' in device-auth.json"

The auth file is incomplete. Re-run `openclaw login`.

### "No valid operator token found"

The `tokens` section of `device-auth.json` is empty or missing the operator token. Re-run `openclaw login`.

### "OpenClaw device key not found"

Missing `device.json`. Run `openclaw device register` to generate a new device key pair.

### "connect failed [code]: message"

The gateway rejected the connection. Common causes:
- **Device not paired** — The gateway doesn't recognize this device. Pair it via `openclaw device pair` or the gateway admin.
- **Expired token** — The device token has expired. Re-run `openclaw login`.
- **Insufficient scopes** — The device doesn't have operator permissions.

### Connection keeps dropping

Check if:
- The gateway service is running: `openclaw gateway status`
- The gateway URL is correct: `echo $CRABCAKES_GATEWAY_URL`
- No firewall is blocking the WebSocket port
- The gateway logs show why the connection was closed

Enable debug logging for more detail:

```bash
export CRABCAKES_GATEWAY_DEBUG=1
```

### Agents appear but can't send messages

This usually means the `GatewayClient` reference wasn't synced to `ChatHandler`. This is set via `set_sync_callback()` in `GatewayHandler.on_connected()`. If it fails, check the error logs — it may indicate a bug in the connection sequence.

### Snapshot validation failure

The `SnapshotValidationError` is raised when the gateway's hello-ok response doesn't match the expected schema. This indicates a protocol version mismatch between DevelCakes and the gateway. Update both to the latest version.

---

## Architecture Details

### GatewayHandler (`ui/handlers/gateway_handler.py`)

Owns the `GatewayClient` and `AgentManager` instances. All GTK operations in callbacks are dispatched via `GLib.idle_add()` because gateway callbacks fire from a background thread.

Key responsibilities:
- Connection lifecycle (connect, disconnect, reconnect)
- Agent discovery and sidebar population
- Event forwarding to `ChatHandler` via `on_event` callback
- Syncing the live `GatewayClient` reference to other handlers

### GatewayClient (`gateway/client.py`)

Threaded async WebSocket client. Runs its own asyncio event loop on a daemon thread. Key internals:

- **`_connect_loop()`** — Reconnecting WebSocket loop with exponential backoff
- **`_handshake()`** — v3 device-auth protocol (nonce → sign → connect → hello-ok)
- **`_listen()`** — Pumps WebSocket messages, dispatches events and responses to GTK main thread
- **`_tick_loop()`** — Fires `on_tick()` every 15 seconds
- **`_pending` dict** — Tracks outstanding RPC requests with 30s timeout
- **`_expire_pending()`** — Fires timeout callbacks for expired requests

### Event Flow

```
Gateway WebSocket
    ↓ raw JSON message
GatewayClient._listen()
    ↓ parse → event/res
    ↓ GLib.idle_add()
GatewayHandler._on_event_stub()
    ↓ forwards to window._on_ws_event()
    ↓ routes to ChatHandler
    ↓ renders in chat box
```

### Thread Safety

All gateway callbacks (`on_connect`, `on_error`, `on_event`) are called from the gateway's background thread. **Every GTK operation must go through `GLib.idle_add()`**. The `GatewayHandler._dispatch()` helper wraps this pattern:

```python
def _dispatch(self, fn):
    if self._GLib is not None:
        self._GLib.idle_add(lambda: (fn(), False))
    else:
        fn()
```

### Identity Caching

Device identity is loaded once at module import time (`_load_identity()`) and cached in `_IDENTITY_CACHE`. The `_reload_identity()` function forces a fresh load from disk, used during reconnection attempts in case the identity files were regenerated.
