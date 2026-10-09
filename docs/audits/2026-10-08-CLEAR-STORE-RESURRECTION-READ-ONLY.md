# Investigation Report — /clear vs the Transcript Store: cleared conversations resurrect (READ-ONLY)

**Date:** 2026-10-08 ~22:30 PDT
**Scope:** develcakes working tree @ HEAD `15e9a020` + uncommitted SPEC-19 SP4 changes (dirty: `ui/window.py`, `ui/handlers/agent_runtime_handler.py`, `ui/handlers/chat_render_handler.py`, `ui/views/chat_surface.py`, `prompts/system/*.md`, `utils/live_bridge.py`, `tests/test_live_bridge.py`). Live install state inspected read-only (`~/.config/develcakes/`, `~/.config/crabcakes/`).
**Status:** DIAGNOSTIC ONLY — no code changed, no fixes applied, nothing in the repo or live stores was modified.
**Severity:** HIGH — context can never be durably cleared by `/clear` (token cost + accuracy tax on every turn), plus a live "ghost" state that silently undoes manual cleanup.
**Related:** SPEC-08 (transcript store), FIX-CLEAR-ASK-RACE, STEP-COUNT-RESET-FIX; independent verification of the Supervisor's diagnosis of 2026-10-08 21:13.
**Files modified:** none (this report is the only artifact; it is untracked until committed).

---

## TL;DR — VERDICT

`/clear` still executes, but it was broken by a **design collision shipped with SPEC-08**, and it now fails in **two independent layers**, both probe-verified against the real code:

1. **Data plane (root cause).** `clear_conversation` resets the in-memory conversation and deletes the JSON file, but the SPEC-08 SP4A "store-mode load" (`agent/persistence.py:513-544`, commit `3f61ba6e`, **2026-10-02**, default ON) re-hydrates any session whose JSON is absent **from `transcript.db` rows** — and `/clear` never deletes those rows (`delete_session`/`bump_epoch` have zero production callers). The next *load* after a clear resurrects the full history. "JSON gone = history gone" died silently.

2. **UI plane.** Under the SPEC-06 WebKit chat surface, `/clear`'s UI side effect (`window._clear_chat_box`) only un-mounts the chat surface widget; the surface object and its full transcript survive in the render handler, and the very next render — the "Cleared…" confirmation itself — re-mounts the SAME surface with every old bubble still in it. Visually the conversation never leaves.

**Live-state warning (as of this audit):** the Supervisor's "Coder and Debugger both at 0 at rest — just restart" is **stale for the Coder**. Its stopped SP4 turn auto-saved at **21:45:08** and re-persisted the entire **2,087-message** history back into **both** the JSON and the DB. A restart alone will NOT give a fresh coder. (Debugger is genuinely 0/0.) A verified manual remediation procedure is in §6.

---

## Timeline of the regression

| When | What |
|---|---|
| ≤ 2026-10-02 | `/clear` = in-memory reset + JSON delete. Sufficient: JSON absent → load returns `None` → fresh session. |
| 2026-10-01 | SPEC-08 SP2 `e818b7de` (dual-write JSON→store), SP3 `43f59845` (migration sweep renames JSONs `.migrated`). `persistence.py:47-50` explicitly defers: *"this release NEVER calls bump_epoch — the wrapper has no /clear trigger"*. |
| **2026-10-02 01:02** | **SPEC-08 SP4A `3f61ba6e` — store-mode load lands: "JSON present → JSON wins; JSON ABSENT → hydrate from store rows." Default ON (`main.py:44-55`). This is the regression for `/clear`.** |
| 2026-10-08 ~09:08-10:02 | App (re)started (Supervisor: "up since 10:02") — a load boundary. With the newly-enforced per-round clearing protocol (user, 20:16), the resurrection stopped being intermittent and became a hard, repeatable failure. |
| 2026-10-08 21:02-21:13 | Supervisor investigates: finds JSON "fully intact (1,897 msgs)"; develops the durable clear (delete DB rows + sessions row); deletes Coder's 1,897 rows + Debugger's 494 rows via `delete_session`. Reports "both at 0". |
| 2026-10-08 21:45:08 | Coder's stopped SP4 turn terminates → `_terminate_turn` auto-save (`agent/runtime.py:915-927`) rewrites the JSON (full 2,087 msgs) and **re-appends all 2,087 rows** to the DB (wm was -1 → full write). The Supervisor's "0" state is undone ~30 minutes before it is reported as final. |

---

## 1. Root cause (data plane)

### 1.1 What `/clear` does

`ui/handlers/agent_runtime_handler.py:713-824` (`clear_conversation`):

- refuses while a turn is genuinely in flight (`FIX-CLEAR-ASK-RACE`, `:773-778`) — by design;
- resets `conv.messages / step_count / total_tokens / total_cost` in place (`:783-790`);
- deletes `<config_dir>/conversations/<session_key>.json` (`:806-822`, `os.remove` at `:811`) — best-effort;
- **never touches `transcript.db`.**

### 1.2 What the store fallback does

`agent/persistence.py:513-544` (`load_conversation_from_disk`):

```
path = .../<session_key>.json
if not os.path.isfile(path):        # :527
    ...
    return _load_conversation_from_store(session_key, store)   # :544
```

`_load_conversation_from_store` (`:399-452`) rebuilds the full `Conversation` from `turns` rows; **empty rows → `None`** (`:434-435`) — i.e. the store is now the sole arbiter of "history exists" whenever the JSON is absent.

### 1.3 When the resurrection fires

The only production load site is `ui/handlers/agent_runtime_handler.py:1461-1463`:

```
if rt.get_conversation(session_key) is None:
    loaded = rt.load_conversation(session_key)
```

So the reload happens on the **first send after a process start** for that session (or any moment no in-memory conversation exists) — not on every send. Net effect: **clear → app restart (without an intervening saved turn) → next send → full history back.**

### 1.4 The store's own docs already flagged the gap (before SP4A made it live)

- `agent/persistence.py:47-50` — *"D4 epoch note: this release NEVER calls bump_epoch — the wrapper has no /clear trigger (no runtime delete API exists pre-group-chat)."* The premise was stale: `/clear` and its JSON deletion already existed (STEP-COUNT-RESET-FIX).
- `utils/transcript_store.py:282-284` — `bump_epoch` docstring: *"Post-MVP trigger (/clear); exercised by tests now."* Never wired.
- `docs/specs/SPEC-08-TRANSCRIPT-STORE.md:186` — decision table: *"Session cleared (/clear) | Post-MVP … Store keeps rows (audit trail); `delete_session()` (D2) is the manual surface."* SP4A's read-fallback inverted this: kept rows became resurrection material.

`delete_session` (`utils/transcript_store.py:262-280`) exists and does exactly the right thing (all `turns` rows, every epoch, + the `sessions` row). Its only callers are the Supervisor's manual repair **and tests** — zero production callers:

```
$ grep -rn "delete_session" ui/ agent/ main.py utils/ (current tree)
utils/transcript_store.py:262   # the definition
agent/persistence.py:49         # a comment, not a call
```

### 1.5 Probe proof (real code, isolated sandbox)

Ran the actual `agent/persistence.py` + `TranscriptStore` in an isolated `XDG_CONFIG_HOME` sandbox (scripts were throwaway `/tmp` probes; key logic in Appendix A):

- **Probe A:** save 30 messages → apply `/clear`'s data plane verbatim (`messages=[]` + `os.remove(json)`) → simulate restart → `load_conversation_from_disk` → **"RESURRECTED 30 messages from transcript.db"** (store rows present, `diverged=0` at load time).
- **Probe B:** `/clear` → send a new task → save (JSON small; store flags `diverged`; delta suspended at `agent/persistence.py:261-270`) → JSON loss again → load → **resurrects the OLD 30; the new-task messages are gone** (they never reached the store). Repeated clears revert to ever-more-stale history; a turn-loss hazard rides along.

### 1.6 Why this matches the observed coder history

The coder's persisted conversation spans **2026-10-02 15:38 → 2026-10-08 21:45** (2,087 messages) — i.e. the persisted history has been accumulating for six days: no clear during that window ever got ahead of the resurrection loop. (Contrast: the supervisor tab's history starts 2026-10-07 ~22:00 — clears were still sticking for that session as recently as ~24h ago.)

---

## 2. UI layer (why it also LOOKS like nothing happened)

- `ui/window.py:1007-1031` (`_clear_chat_box`) removes **every GTK child** of the session's chat box (`while … get_first_child() … chat_box.remove(child)`). This code predates the SPEC-06 surface migration (its handoff comment references `clear-ui-fix`). Under the current architecture the box's child **is** the WebKit chat surface; removing it only **un-mounts** the widget. The surface object — and its full transcript — survives in `ChatRenderHandler._surfaces` (cache keyed by display key, `ui/handlers/chat_render_handler.py:336-396`).
- The "Cleared coder's conversation…" confirmation renders via `command_handler._dispatch_result` (`ui/handlers/command_handler.py:616-636`) → `window._on_command_text` (`ui/window.py:933-944`) → `render_sync` → `_surface_for` → `_mount_surface` (`chat_render_handler.py:255-296`), which sees the surface is parentless and **re-appends the same surface** (`chat_box.append(surface)`), then appends the confirmation row.
- There is **no clear/reset API anywhere**: `ui/views/chat_surface.py` exposes only `append_message` / `append_live` / `destroy`; the render handler has no `clear`/`reset`. Nothing can actually empty a surface today.

**Handler-level replay probe** (real `ChatRenderHandler`, fake surface/box stand-ins; GTK/WebKit not exercised — see Appendix A for the honesty note):

```
1) after render:           box.children=1  surface mounted=True  surface rows=1
2) after _clear_chat_box:  box.children=0  surface parent=None (unmounted)
3) after confirm render:   box.children=1  same surface object=True  surface rows=2
   row 0: system | <p>some earlier conversation content</p>
   row 1: system | <p>Cleared coder's conversation. Step count reset to 0.</p>
```

Verdict: after a "successful" clear the user sees the **entire old transcript** again, with the cleared-notice appended.

---

## 3. Live state at audit time (and the 21:45:08 re-persist)

Read-only queries against the live store (URI `mode=ro`):

| Session | `turns` rows | sessions row | JSON | Note |
|---|---|---|---|---|
| `special:coder` | **2087** (seq 0..2086; ids 61408..63494 contiguous) | wm=2086, diverged=0, updated `2026-10-09T04:45:08.770Z` | **2087 msgs**, step=2, mtime 21:45:08 | FULL history back in BOTH stores |
| `special:debugger` | 0 | (none) | 0 msgs, mtime 21:51:06 | genuinely clear |
| `special:supervisor` | 816 (frozen; bulk stamps Oct 2→Oct 5) | wm=815, diverged=1 | 1114 msgs | JSON-authoritative (suspended delta) |

- All 2,087 coder rows carry a **single append window** (`04:45:08.740Z → .770Z` insert stamps) and form one **contiguous id block right at the table's end** — the signature of one full-history `append_delta` (wm=-1 → base_idx 0) at 21:45:08, timed to the stopped-turn auto-save (`agent/runtime.py:915-927`: COMPLETED/FAILED always persist; CANCELLED when flagged).
- Total db: 61,103 turns; **2,391 ids permanently missing** = exactly 1,897 (coder) + 494 (debugger) — i.e. 100% attributable to the two `delete_session` repairs. Cross-check: an id-set diff against the pre-rename copy `~/.config/crabcakes/transcript.db` shows the 485 old-era rows (coder 322 + debugger 163) among the missing.
- **Implication for the "restart to flush" advice:** the next restart's first coder send reloads the full 2,087 messages from the DB (and the JSON). The manual cleanup must be redone (or code-fixed) — §6.

---

## 4. Verification of the Supervisor's diagnosis (claim-by-claim)

Held up (verified against source):

- `/clear` = memory reset + JSON delete; the store fallback defeats it; **"delete_session is the correct remedy"**; **"bump_epoch alone is insufficient — `load_all` reads ALL epochs"** (`utils/transcript_store.py:420-428`) — correct and well-reasoned.
- The running app's in-memory copy persists after file surgery (that is why the meter read 66%): correct — nothing the Supervisor ran touches the live `Conversation` object.
- Backups exist and match: `/tmp/coder_turns_backup_sp3.json` (1,817,400 B ≈ 1,897 rows), `/tmp/debugger_turns_backup.json` (1,072,743 B ≈ 494 rows), `special:coder.json.bak-spec19-sp3` (1,897 msgs).

Corrections (report the drift as drift):

1. **"On the next send, `send_to_special_agent` reloads this file."** → The reload is conditional and happens on the next **load**, not every send: the only load site is `_prepare_turn_conversation` under `if rt.get_conversation(...) is None` (`agent_runtime_handler.py:1461-1463`). In-session next sends do NOT reload; the bite comes at process start / first send after restart.
2. **"SPEC-08's store-mode fallback went active ~12h ago."** → It shipped with SP4A on **2026-10-02** (5 days earlier; `3f61ba6e`, default ON). No code or config change was found at the 12-hour mark. What changed in that window is load timing (today's app start at ~10:02) plus the newly-mandated per-round clearing protocol.
3. **"Coder and Debugger both at 0 (DB + JSON cleared) … restart the app before their next sends."** → **No longer true for the Coder** as of 21:45:08 (see §3): the stopped turn's auto-save re-persisted the full 2,087-message history into both stores. Debugger is still genuinely clear. Any file/db surgery on a live session is undone by the next terminal turn event (stop included).

---

## 5. Fix direction (NOT applied)

### 5.1 Data plane — wire the store into `/clear`

In `clear_conversation`, after the JSON-delete block (`ui/handlers/agent_runtime_handler.py:806-822`), add best-effort, in the same tolerance shape:

```python
# SPEC-08: the store-mode load (SP4A) hydrates from transcript.db when the
# JSON is absent — deleting only the JSON leaves the store to resurrect the
# cleared history on the next load. Delete the session's rows + meta too
# (D2 delete_session; bump_epoch alone is insufficient — load_all reads all
# epochs). Best-effort, mirrors the JSON-delete contract.
try:
    from agent.persistence import _get_store
    _get_store().delete_session(session_key)
except Exception as exc:
    logger.warning(
        "clear_conversation: store delete failed for %s: %s", session_key, exc
    )
```

Notes for the implementer:

- Prefer adding a small public wrapper in `agent/persistence.py` over reaching for `_get_store` from the UI layer (house style).
- Update the stale notes when landing: `agent/persistence.py:47-50` and the SPEC-08 decision table row (`docs/specs/SPEC-08-TRANSCRIPT-STORE.md:186`).
- Trade-off to state in the spec: `delete_session` removes the store-side audit ledger for that session (SPEC-08 deliberately wanted to keep rows). Acceptable for `/clear` semantics; a separate archived copy is possible if the ledger matters.
- Tests: `/clear` → `store.load_all(sk) == []`; restart-load (no JSON, no rows) → `None` → fresh conversation; plus the existing `test_clear_no_payload_required` suite stays green.

### 5.2 UI plane — a real surface clear

- Add a clear/reset path on the surface (reset `_rows`, `_live_sections`, rebuild the empty document, reset the welcome flag so the re-welcome policy stays coherent) exposed through `ChatRenderHandler`, and have `window`'s clear callback call **that** instead of stripping GTK children (`ui/window.py:1007-1031`).
- Do **not** naively reuse `close_session` (`ui/handlers/chat_render_handler.py:398-475`): it tombstones the key (`_closed_sessions`), which DROPS subsequent renders until a tab re-open — `/clear` must remain a live, renderable tab.
- Test: after clear + the confirmation render, the surface shows **only** the confirmation row.

---

## 6. Manual remediation (NOT performed by this audit — read-only)

To get a genuinely fresh coder **now** (verified state: app is closed — this is the safe window; *any* terminal turn after reopening re-persists only the new round, which is desired):

```
# 0) optional backup of the current 2,087-message file:
cp ~/.config/develcakes/conversations/special:coder.json /tmp/coder_full_backup_oct8.json

# 1) delete the JSON:
rm ~/.config/develcakes/conversations/special:coder.json

# 2) delete the store rows + sessions row (run from the repo root, app CLOSED):
cd /home/mushy/projects/develcakes && PYTHONPATH=. .venv/bin/python3 -c \
  "from utils.transcript_store import TranscriptStore; s=TranscriptStore(); \
   print('deleted:', s.delete_session('special:coder')); \
   print('remaining:', len(s.load_all('special:coder'))); s.close()"
# expect: deleted: 2087 / remaining: 0

# 3) reopen develcakes, confirm the coder context reads empty, then send the brief.
```

Caveats:

- Keep the app closed through step 2; a turn (including a stop) re-persists the in-memory conversation.
- `.migrated` / `.bak-*` files are inert: the loader reads only `<sk>.json` (`agent/persistence.py:527`), and the sweep selects only `*.json` (`agent/persistence.py:684-686`). Leave the backups in place.
- This remains a **manual workaround** until §5.1 lands.

---

## 7. Ruled out (checked, clean — do not re-chase)

- **Command parsing / registration.** `/clear` is registered payload-free (`ui/handlers/command_handler.py:159-161`; payload-free check `:390-394`); `process_input` path intact; the clear/payload unit tests pass (19 passed: `tests/test_command_handler.py`, `tests/test_project_handler.py`, `tests/test_compact_command.py -k "clear or Clear or payload"`).
- **Config-dir / path mismatch.** The clear deletes from the same dir every save writes to (`get_config_dir()/conversations/<sk>.json`); the live files' mtimes confirm the app resolves the same paths.
- **Migration sweep destroying data.** The sweep only appends + renames; it never deletes rows (docstring contract). All observed row deletions are 100% attributable to the Supervisor's two `delete_session` calls.
- **`.migrated` / `.bak` files.** Inert to both the loader and the sweep (see §6 caveats).
- **FIX-CLEAR-ASK-RACE guard misfiring.** The refusal path is by design (only while a turn is genuinely in flight; `_active_loops` add/discard at `agent/runtime.py:1465` / `:2159` is exception-safe). Whether one specific user attempt hit a refusal or a stale context-meter reading could not be determined from disk state (no UI event logs) — flagged as honestly unproven rather than asserted.
- **The store's `bump_epoch` as an alternative fix.** Insufficient: `load_all` returns rows across **all epochs** (`utils/transcript_store.py:420-428`), so a bump alone would not empty the reload.

---

## 8. Side findings (registered, separate from the `/clear` bug)

1. **Production store polluted by test/probe fixture sessions.** The live `transcript.db` (108 MB + 7 MB WAL, 61,103 turns) contains ~56.7k turns / ~10.1k sessions under `rt*` keys plus `sa*` (887 turns / 328 sessions), `test-*` (398 / 100), and ~90 `special:prep-*` rows — fixture-shaped data spanning Oct 2→8. Some test/probe path still writes into the real store despite `tests/conftest.py`'s autouse isolation + `CRABCAKES_MIGRATE_STORE=0` pin (`tests/conftest.py:33-56`); likely standalone probe scripts. Worth a separate isolation audit — it also makes any spot-check of "what's in the DB" misleading for operators.
2. **No clear/reset API on the chat surface or render handler** (fix groundwork; §5.2).
3. **Refusal visibility.** `Could not clear …` results surface only as `[Action result]` injections into the *issuing* agent's conversation (`ui/handlers/agent_command_handler.py:336-340`, injected at `:395`); a user clearing from a non-agent tab can miss the reason entirely. UX nit, not a bug.
4. **Terminal-turn persistence is total.** Any stop/fail rewrites JSON + appends to the store (`agent/runtime.py:915-927`) — anything doing manual store surgery must account for it (this is exactly how the Supervisor's clean state was undone).

---

## Appendix A — Probe evidence (throwaway scripts; logic inlined for durability)

**A.1 Persistence probe** (real `agent/persistence.py` + `TranscriptStore`; isolated `XDG_CONFIG_HOME=/tmp/hermes_clear_probe_cfg`; run via `PYTHONPATH=<repo> .venv/bin/python3`):

```
save 30 msgs via save_conversation_to_disk   → json written; store rows=30 wm=29
/clear data-plane: messages=[] ; os.remove(json) ; drop object (restart)
load_conversation_from_disk(sk)              → (Conversation, meta)   ← NOT None
  → RESURRECTED 30 messages from transcript.db   [diverged=0 at this point]
--- second shape ---
/clear ; append new task msgs ; save          → json=2 msgs ; store diverged=True wm=29 rows=30
os.remove(json) ; load                        → RESURRECTED the OLD 30; new-task msgs absent
```

Honesty note: the probes exercise the real persistence code end-to-end; they do not run the GTK app.

**A.2 UI probe** (real `ChatRenderHandler`; fake surface/box stand-ins implementing only the GTK methods the handler calls — `append`/`remove`/`get_first_child`/`get_parent`; the child-strip loop that `/clear` runs, copied verbatim from `ui/window.py:1027-1031`):

```
1) after render:           box.children=1  surface mounted=True   rows=1
2) after _clear_chat_box:  box.children=0  surface parent=None (unmounted)
3) after confirm render:   box.children=1  SAME surface object    rows=2  [old row + "Cleared…"]
```

Honesty note: this is a handler-level replay, not a live-WebKit UI test. The remount itself is standard GTK behavior (un-parenting preserves the widget and its state; `_mount_surface` re-appends the cached surface). A live-app confirmation would need a GTK/WebKit session with the real window.

**A.3 Store forensics** (read-only SQL via `sqlite3` URI `mode=ro`): coder's contiguous 2,087-row block (ids 61408–63494, single insert window), the exact 2,391 missing ids (= 1,897 + 494), the old-db id-set diff (485 old-era rows among the missing), and the fixture census above.

---

*End of report. Everything above was gathered read-only; nothing in the repo, the config dir, or the stores was modified during the investigation.*
