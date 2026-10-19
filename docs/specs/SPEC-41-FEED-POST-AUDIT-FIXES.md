# SPEC: Feed surface post-audit fixes (WU 10.1 / 10.2) + wire adjacency

**Date:** 2026-10-19
**Author:** Supervisor (from Debugger's adversarial audit of WU 00000010)
**Status:** Draft — for implementation
**Implements:** WU 00000010 follow-up; audit probes in `.debug/audit-scratch/wu10_probe*.py` (gitignored)
**Depends on:** SPEC-FEED-HTML-SURFACE (HTML feed surface), SPEC-40 (wire-aware read guard)
**Target branch:** main

> Architecture: the feed is one WebKit document (`ui/views/feed_surface.py`);
> card data is `models/feed_card.py`; decisions stay in Python
> (`ui/handlers/feed_handler.py`). The compiled wire is owned by
> `agent/runtime.py`; serialization is `models/conversation.py`
> (`serialize_messages`). No new modules.

---

## DISCOVERY

- Read `ui/views/feed_tab.py`: `FeedTab` (Gtk.Box) owns the scroll and toolbar.
  The GTK card-list API was deleted in `da7913f6` — AST check confirms
  `remove_card`, `append_card`, `prepend_card`, `replace_card`,
  `get_card_container` are ABSENT, while `remove_card_html` (:262) and the
  document API remain. `hide_card_buttons` (:868) still reads
  `self._cards_by_id`; `is_above_viewport` (:544) still reads
  `self._card_container`. The class docstring (:15) already promises
  `remove_card` is a "compat no-op (E2/E3 re-route callers)" — the promise
  was never delivered.
- Read `ui/handlers/feed_handler.py`: two LIVE callers of the deleted
  `remove_card` — `:586` (idle `_remove` in the review path) and `:762`
  (`clear_project`). `hide_card_buttons` is called at `:975` inside a
  `try/except AttributeError` — the AttributeError is swallowed, so
  auto-approved exec cards KEEP their Approve/Deny buttons (double-action
  risk). `_card_widgets` (:102) is now always empty on the HTML path.
- Read `ui/feed/load.py`: `_evict_surplus_card_widgets` (:334) branches to
  `_evict_surplus_html_rows` (:434) when `self._html_feed` — the GTK branch
  (:365-397, which calls `is_above_viewport` and pops `_card_widgets`) is
  dead on the HTML path. `on_project_opened` → `clear_project(prev)` (:25)
  and `on_project_closed` (:211) reach the crash. `_load_more` (:256-283)
  iterates `reversed(page)` → `prepend_html_card` → `FeedSurface.upsert(prepend=True)`
  → `_order.insert(0, cid)`: each insert lands at the FRONT, so the last
  insert wins the front — the page renders newest-to-oldest while
  `render_feed_document` documents "fragments are oldest first".
- Read `ui/views/feed_surface.py`: `set_cards` (:118) resets `_order`,
  `_cards`, `_html`, `_body`, `_revealed`, `_context_open` — but NOT
  `_doc_near_bottom` or `_doc_y`. `upsert(prepend=True)` inserts at index 0.
  `_on_script_message` (:275) dispatches `scroll_state` BEFORE the
  `card_id` check, so the scroll payload (no card_id) cannot be misrouted.
- Read `agent/runtime.py`: `_compiled_messages` (:3492) serializes
  `turn` and `tail` INDEPENDENTLY — `serialize_messages(turn)` then
  `serialize_messages(tail)`. `turn = conv.messages[wire_from:after]`
  where `wire_from` = (index of last USER message) + 1 (:3779-3782).
  `_maybe_compile` (:3716) snapshots hot windows at :3769-3777 copying
  ONLY `path`/`offset`/`limit`/`body` — the `line` key is dropped, while
  `_capture_hot_window` (:4138) stores it (:4164) and `_compiled_messages`
  reads it (:3532) — so the `line=` branch is dead.
- Read `models/conversation.py` `serialize_messages` (:144): it collects
  `claimed_ids` from assistant `tool_calls` and demotes any unmatched
  `role:"tool"` to a user row `[tool result {id}] — …`, logging a warning.
  Its docstring cites MiniMax 2013 and Anthropic strict ordering as the
  reason. VERIFIED BY REPRODUCTION (Supervisor, live): with messages
  `[user, assistant(call_write+call_read), tool(call_write),
  user-nudge, tool(call_read)]` and `wire_from=4`, the SPLIT serialization
  emits `['user','user','assistant','tool(call_write)','user(<demoted>)']` —
  `call_read` is declared but has NO tool row, i.e. the exact 2013 shape.
  Single-pass serialization of the same range emits all five rows correctly.
- Read `agent/tools.py`: the byte-window continuation hint (:515) computes
  `line + content.count("\n")`. When the window ends mid-line the partial
  line's newline is uncounted, so the hint points at a line already read.
- Architecture owner: `FeedTab` owns the view seam, `FeedHandler` owns
  lifecycle, `FeedSurface` owns the document, `AgentRuntime` owns the wire.
- Existing patterns to copy: the document API on `FeedTab` (`remove_card_html`,
  `upsert_card`, `prepend_html_card`, `set_html_cards`) is the surface the
  GTK API used to front; `serialize_messages` is the single serializer the
  wire must go through.

---

## 1. Overview

### Problem

The WU 10 purge removed the GTK card-list seam but left two live callers
pointing at it, so **every project switch or close raises AttributeError** —
and the raise aborts the window's project-opened callback tuple, so CrabWatch,
prompt handlers, the activity drawer and the feed bar never update. Our
gates stayed green because the test mock still had `remove_card`: a mock
mirroring a world that no longer exists. Three more defects are smaller but
real, and one of them is on the LLM wire.

### Solution

- **Phase 1 (10.1)** — restore the feed's view seam as document-backed
  no-ops (or re-route the callers), fix the two methods that read deleted
  state, and add a regression test that drives the REAL `FeedTab`.
- **Phase 2 (10.2)** — serialize the compiled wire as ONE list so an
  assistant's tool calls can never be separated from their results; carry
  the `line` coordinate through the hot-window snapshot.
- **Phase 3 (10.3)** — scroll-state reset on project switch, Load More
  ordering, and the read_file continuation hint.

### Scope

| In | Out |
|---|---|
| `ui/views/feed_tab.py` compat seam + two stale-state methods | New feed features, card visual design |
| `ui/handlers/feed_handler.py` caller re-route | `models/feed_card.py` data model |
| `agent/runtime.py` wire adjacency + hot-window `line` | `agent/llm/convert.py` (normalize_tool_history) |
| `ui/views/feed_surface.py` scroll-state reset | Provider layer, tool schemas |
| `ui/feed/load.py` Load More ordering | The review/approval decision flow |
| `agent/tools.py` continuation hint | (new) `read_file` features |

---

## 2. Changes by File

### Phase 1 — 10.1: the feed view seam (BLOCKER)

**(a) `ui/views/feed_tab.py` — re-add the removed API as document-backed
no-ops.** The docstring already promises this shape; implement it.

```python
def remove_card(self, card_id: str) -> None:
    """Compat no-op: the feed is a document. Removes the row if attached.

    The GTK card list was removed in WU 10; callers that still speak the
    old API are re-pointed here so project switch/close cannot raise
    AttributeError.
    """
    self.remove_card_html(card_id)

def append_card(self, widget, card_id: str) -> None:
    """Compat no-op: widgets are not part of the document feed."""
    return None

def prepend_card(self, widget, card_id: str) -> None:
    """Compat no-op: widgets are not part of the document feed."""
    return None

def replace_card(self, card_id: str, widget) -> None:
    """Compat no-op: widgets are not part of the document feed."""
    return None

def get_card_container(self) -> Gtk.Box:
    """Compat no-op: the document has no GTK container. Returns None."""
    return None
```

`remove_card` MUST forward to `remove_card_html` (the document row must
actually disappear on project switch/close). The other four take the
legacy signature so no caller breaks.

**(b) `ui/views/feed_tab.py` — `hide_card_buttons` (:868) and
`is_above_viewport` (:544) must not read deleted state.**
- `hide_card_buttons`: the document path has no per-card widget. Replace
  the body with a no-op that returns immediately (the HTML card renders its
  own approved badge via `update_card`/`upsert`); keep the signature.
- `is_above_viewport`: `self._card_container` no longer exists. Return
  `False` immediately (the GTK eviction branch that calls it is dead on the
  HTML path; `False` is the fail-safe: "not above the viewport" → never
  evict).

**(c) `ui/handlers/feed_handler.py` — remove the legacy call sites.**
- `:586` — the idle `_remove` closure calling `self._feed_tab.remove_card(card_id)`.
  On the document path this must remove the ROW: call
  `self._feed_tab.remove_card_html(card_id)` (the surface API), or drop the
  closure if the caller only ever ran on the GTK path. Grep the enclosing
  function and choose: if it is review-toggle code, the HTML equivalent is
  `surface.toggle_context(card_id)`.
- `:762` — `clear_project` iterates card ids calling `remove_card`. With
  (a) in place this is safe (it forwards), but prefer calling
  `remove_card_html` directly so the intent is explicit.

**(d) Regression test — REAL `FeedTab`, not a mock.**
`tests/test_feed_switch_regression.py` (new):
1. Build a real `FeedTab` (follow the pattern in the existing feed tests —
   grep `tests/` for a file that constructs `FeedTab`; read ONE).
2. Attach a fake surface via `attach_html_surface`, then call
   `clear_project`/`on_project_opened` on a real `FeedHandler` (or the
   minimal handler surface the existing tests use) — assert NO exception
   and that the surface's card list is empty afterwards.
3. Assert `hasattr(FeedTab, "remove_card")` is True and that
   `remove_card` forwards to `remove_card_html` (monkeypatch the surface
   and assert the call).
4. Assert `hide_card_buttons(...)` and `is_above_viewport(...)` return
   without raising on a tab with no widget map.

**Why the mock must go:** `tests/test_feed_handler.py`'s `MockFeedTab` still
defines `remove_card`/`get_card_container` — the mock is why the suite was
green against a crash. The new test MUST use the real class.

### Phase 2 — 10.2: wire adjacency + hot-window `line`

**(a) `agent/runtime.py` `_compiled_messages` (:3492) — serialize ONE list.**

Current (broken):
```python
messages.extend(serialize_messages(turn, index_base=len(messages)))
messages.extend(serialize_messages(tail, index_base=len(messages)))
```

Fix: serialize the concatenation so `serialize_messages` sees the whole
range and can match each tool result to its declaring assistant:
```python
messages.extend(
    serialize_messages(turn + tail, index_base=len(messages))
)
```
`turn` and `tail` are contiguous slices of `conv.messages`
(`turn = msgs[wire_from:after]`, `tail = msgs[after:]`), so the
concatenation is exactly `msgs[wire_from:]` — same content, one pass, no
split. `index_base` semantics are unchanged (the serializer only uses it
for warning indices).

**(b) `agent/runtime.py` `_maybe_compile` (:3769-3777) — carry `line`.**
Add the key to the snapshot dict:
```python
{
    "path": w["path"],
    "offset": w["offset"],
    "limit": w["limit"],
    "line": w.get("line"),
    "body": w["body"],
}
```
`_capture_hot_window` already stores `line` (:4164) and
`_compiled_messages` already reads it (:3532) — the snapshot was the only
place it was lost. `_compiled_messages`'s `where` branch must also treat
`line=0`/falsy correctly: use `w.get("line") is not None` (a 1-based line
is never 0, but an explicit None check keeps the byte branch intact).

### Phase 3 — 10.3: scroll reset, Load More order, read hint

**(a) `ui/views/feed_surface.py` `set_cards` (:118)** — reset the scroll
state with the document:
```python
self._doc_near_bottom = True
self._doc_y = None
```
A new project starts at the top and follows. Without this, project B
inherits project A's offset AND A's follow-off state, so B's new cards land
below the fold permanently.

**(b) `ui/feed/load.py` `_load_more` (:275)** — the HTML branch iterates
`reversed(page)` and calls `prepend_html_card`, which inserts at index 0 —
the page lands reversed. Fix: iterate the page in the order the document
expects (oldest first at the top) — i.e. iterate `page` such that the
OLDEST card ends up first in `_order`. With `prepend` inserting at 0, the
call order must be NEWEST → OLDEST so the oldest lands last-inserted =
index 0. `page` is newest-first, so iterating `page` DIRECTLY (not
`reversed(page)`) gives newest→oldest insertion = oldest at the front.
Verify against `render_feed_document`'s "fragments are oldest first" and
`set_cards`'s "cards is oldest-first".

**(c) `agent/tools.py` (:515)** — the continuation hint must not point at a
line already consumed. Count the newlines in the body, and when the body
does NOT end with a newline the last (partial) line is incomplete, so the
next unread line is `line + newlines + 1`; when it DOES end with a
newline, the next line is `line + newlines`.

**Files NOT changed (Rule 8):**
- `models/conversation.py` — `serialize_messages` is correct; the runtime
  was calling it on split slices.
- `render/feed_html.py` — the document builder's oldest-first contract is
  the reference, not the thing to change.
- `ui/handlers/feed_handler.py` decision flow (accept/reject/persist) —
  only the two legacy call sites are touched.

---

## 3. Data Flow

**Bug #1 today:** project switch → `ui/feed/load.py:25`
`on_project_opened` → `clear_project(prev)` → `feed_handler.py:762` →
`self._feed_tab.remove_card(cid)` → `AttributeError` (method deleted) →
the exception propagates out of the project-opened callback tuple
(`ui/window.py:604-631`), so every later callback in that tuple never
runs: CrabWatch never starts watching, the chat surface keeps the old
project name, the activity drawer keeps the old events.

**After 10.1:** `clear_project` → `remove_card` (compat no-op) →
`remove_card_html(card_id)` → `FeedSurface.remove` → `_order` filtered →
document re-rendered. No raise; the callback tuple completes.

**Bug #2 today:** enforced write → `_maybe_compile` stores
`wire_from`/`after` → next model call → `_compiled_messages` →
`serialize_messages(turn)` and `serialize_messages(tail)` separately → the
assistant that declared `call_read` is in the first slice while its result
is in the second (or vice versa) → the serializer demotes the orphan to a
user row → the wire carries a tool call with no tool result → MiniMax
2013, Anthropic strict ordering.

**After 10.2:** one `serialize_messages(turn + tail)` → the serializer
sees the whole range → every result is matched to its declaring assistant
→ the wire is well-formed again.

---

## 4. File Change Summary

| File | Change | Lines | Risk |
|---|---|---|---|
| ui/views/feed_tab.py | 5 compat no-ops; two stale-state methods | ~45 | Med |
| ui/handlers/feed_handler.py | re-route 2 call sites | ~10 | Low |
| agent/runtime.py | one-pass wire serialize; `line` in snapshot | ~6 | Med |
| ui/views/feed_surface.py | scroll-state reset in set_cards | ~3 | Low |
| ui/feed/load.py | Load More ordering | ~2 | Low |
| agent/tools.py | continuation hint arithmetic | ~4 | Low |
| tests/test_feed_switch_regression.py | new (real FeedTab) | ~120 | — |

## 5. Implementation Order

Phases are independent; each ends with its own gate.

1. **10.1** — compat no-ops + stale-state fixes + re-route + the real-FeedTab
   regression test. Gate: the new test + `tests/test_feed_handler.py` +
   `tests/test_feed_retention.py` green; full suite.
2. **10.2** — one-pass serialize + `line` snapshot. Gate:
   `tests/test_spec24_compiled_turn.py` + `tests/test_spec40_wire_aware_guard.py`
   + a new test that serializes a split-shape conversation and asserts every
   declared tool_call id has a tool row. Full suite.
3. **10.3** — scroll reset + Load More order + hint. Gate: new tests for
   each; `tests/test_feed_html.py` + `tests/test_feed_retention.py` green.
   Full suite.

**Gate protocol (established 2026-10-19):** targeted `xvfb-run -a pytest`
runs pasted in the report; the full suite launched BACKGROUNDED with the pid
reported; the Supervisor polls and gates. Never poll from inside a turn.

**Audit after 10.3:** a Debugger re-audit of the three phases, read-only,
confirming each finding is closed.

## 6. Acceptance Criteria

- [ ] `FeedTab` has `remove_card`/`append_card`/`prepend_card`/`replace_card`/
      `get_card_container` again; `remove_card` forwards to `remove_card_html`.
- [ ] Project switch and project close raise NOTHING (real FeedTab test).
- [ ] The project-opened callback tuple completes (assert the later
      callbacks ran, or that no exception escaped the handler).
- [ ] `hide_card_buttons` and `is_above_viewport` return without raising on
      a widget-less tab; `is_above_viewport` returns False.
- [ ] No production reference to `_cards_by_id` / `_card_container` remains
      in `feed_tab.py` (grep asserts).
- [ ] The compiled wire contains a tool row for EVERY declared tool_call id
      in the split-shape conversation (regression test).
- [ ] A hot window captured with `line=42` replays as `line=42`, not
      `offset=0` (regression test).
- [ ] `set_cards` resets `_doc_near_bottom` to True and `_doc_y` to None.
- [ ] Load More renders the page oldest-first (seq numbers ascending in the
      document order).
- [ ] The continuation hint after a mid-line window points at the next
      UNREAD line.
- [ ] Full suite: failure set identical to the 47F baseline; the new tests
      are the only delta.

## 7. Edge Cases

| Case | Behavior |
|---|---|
| `remove_card` on a card not in the document | no-op (surface.remove already returns early) |
| `append_card`/`prepend_card` called by legacy code | no-op, no raise |
| `get_card_container()` on the document path | returns None — callers must tolerate (grep for users) |
| `hide_card_buttons` for a card mid-document | no-op (HTML card shows its own badge) |
| `is_above_viewport` with no container | False → never evict |
| Wire: assistant declares 2 calls, one result missing | serializer demotes the orphan to a user row (existing, correct) |
| Wire: no interleaved nudge | one-pass output identical to today's split output |
| Hot window captured by byte offset | `line` absent → byte branch (unchanged) |
| Hot window captured by line | `line` present → `line=N limit=M` |
| `set_cards` with an empty list | scroll state still reset |
| Load More with a 1-card page | ordering trivially correct |
| `read_file` window ending exactly on a newline | hint = `line + newlines` (next line) |
| `read_file` window ending mid-line | hint = `line + newlines + 1` (next unread) |

## 8. ARCHITECTURE.md Updates Required

- Feed section: the document is the only card surface; the legacy widget
  API survives as no-ops for one release.
- Runtime/compiled-packet section: the wire is serialized in ONE pass so a
  tool call can never be separated from its result (MiniMax 2013 /
  Anthropic strict ordering).

---

## Self-Audit (Rule 9)

- Every anchor verified by direct read/grep this session: `feed_tab.py`
  :262/:544/:868, `feed_handler.py` :102/:586/:762/:975, `load.py`
  :25/:211/:256-283/:334/:434, `feed_surface.py` :118/:135/:275,
  `runtime.py` :3492/:3532/:3716/:3769-3777/:3779-3782/:4138/:4164,
  `conversation.py` :144/:174, `tools.py` :515, `render/feed_html.py` :298.
- BUG #2 reproduced live by the Supervisor before writing (split
  serialization dropped `call_read`'s tool row; single-pass kept it).
- The compat no-op signatures were copied from the DELETED methods'
  pre-purge signatures (SPEC-FEED-HTML-SURFACE DISCOVERY block records
  `append_card(widget, card_id)`, `prepend_card`, `remove_card`,
  `replace_card`, `get_card_container`).
- `serialize_messages(turn + tail)` is equivalent to the current
  concatenation of results (both slices are contiguous and ordered), so
  the only behavioral delta is the serializer seeing the whole range.
- Exception types: the compat methods must not raise; `remove_card_html`
  already guards on missing ids.
- Scope table lists every file; the audit's clean areas (purge
  completeness, click bridge, eviction invariants, NO_WEBKIT) are
  explicitly out of scope.
