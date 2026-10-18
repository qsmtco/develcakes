# SPEC: Chat — render prose and HTML fences in the same message

**Date:** 2026-10-17
**Author:** Micah
**Status:** Implemented (2026-10-17)
**Implements:** investigation 2026-10-17 (mixed text + HTML renders as source)
**Depends on:** SPEC-13 (agent-native HTML), SPEC-19 (live fences stay whole-message), SPEC-15 / SPEC-15c (Telegram text flatten)
**Target branch:** main

> Architecture: `render/html.py` owns chat composition. `ChatRenderHandler._compose_text` is the only production caller of `render_message`. The surface (`ui/views/chat_surface.py`) already appends a sanitized fragment; it does not decide what is a card. This spec changes the composer, not the WebView.

---

## DISCOVERY

- Read `render/html.py`: `render_message(text: str) -> str` promotes only when `_whole_message_html_fence` matches `^```html[ \t]*\r?\n(.*?)\r?\n?```[ \t]*$` (DOTALL, IGNORECASE) against `text.strip()`. Any other shape calls `render_document(text)`, which is `sanitize_with_local_images(markdown_to_html(text))`. Non-str returns `""` before either branch. `_emit_block` turns a `code` segment into `highlight_html` inside `<pre><code>`, except `lang == "image"`, which becomes a `data:` image or `""`.
- Read `utils/block_parser.py`: `extract_blocks(text) -> list[dict]`. Fence regex is `` (```(\w*)\n)(.*?)(```) `` (DOTALL). It requires `\n` after the language tag, so a CRLF fence is not a code segment. Closing fence is the first `` ``` ``. Empty text returns `[{"type": "text", "content": ""}]`.
- Read `render/sanitize.py`: `sanitize_agent_html(html: str) -> str` runs `nh3.clean` with the author tag set and CSS allowlist, `except BaseException: return ""`. `sanitize_with_local_images` is the markdown policy plus local-image `data:` URIs. Passing an author fragment through `sanitize_with_local_images` would strip `div` and `style`. The two outputs must be concatenated, never re-sanitized as one string.
- Read `ui/handlers/chat_render_handler.py`: `_compose_text(text: str) -> tuple[str | None, str]` checks `live_fence` first, else `render_message(text)`. Call sites are `_append_to_surface` (has `role`) and `render_async`'s `_compose_off_thread` (closes over `role`). `render_sync` and `end_streaming` both land in `_append_to_surface`. Roles used in production and tests are `"You"`, `"Agent"`, and `"System"`. `start_streaming(..., role: str = "Agent")` stores `_stream_role`.
- Read `ui/handlers/chat_handler.py`: user sends call `render_async("You", text, ...)`. Agent replies are a different role string, `"Agent"`.
- Read `ui/handlers/telegram_bridge_handler.py`: `_telegram_text` matches its own copy of the whole-message regex (`_WHOLE_MESSAGE_HTML_FENCE_RE`, lines 50–53) and flattens the payload with `_html_fragment_to_text`. A miss returns the original string, so prose plus a fence is sent to the phone as source. `_html_fragment_to_text` already `.strip()`s. The regex is not imported from `render/html.py`.
- Read `tests/test_render_html.py` `TestRenderMessage`: whole-message fences must stay byte-identical (`out == '<div>x</div>'` for the simple case). `test_prose_plus_fence_stays_code_block` and `test_prose_plus_fence_byte_identical_to_render_document` pin today's source-block behavior. `test_html_guard_sites.py` asserts `render_message` is called only inside `_compose_text`, and `render_document` only inside `render_welcome`.
- Read `prompts/chat-surface-authoring.md` and `prompts/system/crabcakes-context.md`: both tell the model that one line of prose before or after a fence drops the whole message to markdown. No other prompt file repeats that rule.
- Architecture owner: `render/html.py` for composition; `ChatRenderHandler` for which role reaches it; `TelegramBridgeHandler._telegram_text` for the phone mirror.
- Existing pattern to copy: the whole-message branch (detect, then `sanitize_agent_html`, never the markdown sanitizer). Segment promotion is that branch applied per fence, with `render_document` on the slices that are not an `html` fence.

---

## 1. Overview

**Problem.** An agent reply that is prose plus a `` ```html `` fence never matches `_whole_message_html_fence`. `render_message` sends the whole string through `render_document`. The fence becomes a highlighted code block, so the chat shows HTML source. The same miss makes Telegram send that source to the phone. SPEC-13 locked this on purpose so "here is how you write a div" stayed visible source, and the prompts were updated to forbid prose around a card. Agents still write prose around the fence.

**Solution.** For role `"Agent"` only, each `` ```html `` fence in a message is sanitized with `sanitize_agent_html` and the slices between fences stay on `render_document`. A message that is still one whole-message fence keeps today's fast path, including the CRLF regex. `` ```live `` stays whole-message only (`live_fence` runs first in `_compose_text`). Unfenced `<div>` stays escaped. Showing HTML as source is a fence whose language is not `html`.

**Scope**

| In | Out |
|---|---|
| Agent rows: prose and `` ```html `` fences in one message | Unfenced HTML sniffing (`a < b`, tool-result echo) |
| Telegram flatten of those same fences to visible text | Promoting `` ```live `` unless it is the whole message |
| Prompt text that currently forbids prose around an `html` fence | Changing `ChatSurface.append_message` |
| Tests for both the new agent path and the unchanged user/system path | Inner triple-backtick splitting (still ends the fence) |
| | `utils/block_parser.py` (CRLF stays a render-time normalize, not a parser change) |
| | Feed cards (separate spec) |

**Principles.** Escape-first remains the markdown path. Author HTML is only the inside of an `html` fence, and only after `sanitize_agent_html`. User and System rows do not opt in. Fail-closed: non-str is `""`; a sanitizer panic on one fence contributes `""` for that fence and does not drop the prose.

---

## 2. Changes by File

### 2a. `render/html.py`

Add a keyword-only flag. Default `False` so every current direct caller, including the tests that pin "mixed stays a code block", keeps today's behavior until it opts in. Production opts in from the handler, for `"Agent"` only.

```python
def render_message(text: str, *, promote_html_segments: bool = False) -> str:
```

Body, in this order:

1. `if not isinstance(text, str): return ""` — unchanged. Do not call `extract_blocks` on a non-str (`extract_blocks` is untyped and would iterate the wrong object).
2. `payload = _whole_message_html_fence(text)`. If `payload is not None`: `return sanitize_agent_html(payload)`. This runs even when the flag is false, so a user who sends a whole-message card still gets a card. CRLF whole-message tests stay on this regex, not on the splitter.
3. If `not promote_html_segments`: `return render_document(text)`.
4. Otherwise splice. `split_html_fences` returning `None` means `return render_document(text)` (no `html` fence, or an unclosed one). That keeps plain markdown, python fences, and image fences byte-identical to today.

New helper, same module:

```python
_HTML_FENCE_RE = re.compile(
    r"```html[ \t]*\n(.*?)\n?```",
    re.DOTALL | re.IGNORECASE,
)

def split_html_fences(text: str) -> list[tuple[str, str]] | None:
    """Ordered ("text", raw_slice) / ("html", payload) parts.

    None when `text` is not a str, is empty, or contains no closed
    ```html fence. Newlines are normalized to \\n before the scan.
    Slices are the raw characters between fences, not re-emitted markdown.
    """
    if not isinstance(text, str) or not text:
        return None
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(_HTML_FENCE_RE.finditer(normalized))
    if not matches:
        return None
    parts: list[tuple[str, str]] = []
    cursor = 0
    for m in matches:
        if m.start() > cursor:
            parts.append(("text", normalized[cursor:m.start()]))
        parts.append(("html", m.group(1)))
        cursor = m.end()
    if cursor < len(normalized):
        parts.append(("text", normalized[cursor:]))
    return parts
```

Trace of the regex against `` ```html\n<div>x</div>\n``` ``: `[ \t]*` matches nothing, `\n` matches the opener newline, `(.*?)` is non-greedy, `\n?` consumes the newline before the closer, group 1 is `<div>x</div>`. Same payload the whole-message regex captures. `` ```HTML `` matches because of `IGNORECASE`. A fence with no closing `` ``` `` produces no match, so the helper returns `None`.

Splice:

```python
def _render_html_segments(text: str) -> str:
    parts = split_html_fences(text)
    if parts is None:
        return render_document(text)
    out: list[str] = []
    for kind, chunk in parts:
        if kind == "html":
            # Author policy ONLY. Do not pass this through render_document.
            out.append(sanitize_agent_html(chunk))
        elif chunk.strip():
            # Markdown policy, including ```image and other fences.
            out.append(render_document(chunk))
    return "".join(out)
```

`sanitize_agent_html` is imported inside the function today (`from render.sanitize import sanitize_agent_html` in the whole-message branch). Do the same here, or hoist one import. Do not import it at module top if that creates a cycle — `render/sanitize.py` does not import `render/html.py` today, so a top-level import is safe. Either style is fine; one import site is enough.

`render_document` already returns sanitized HTML (`sanitize_with_local_images`). Do not sanitize the joined string again.

Whitespace-only text slices are skipped (`chunk.strip()`), so the newlines around a fence do not become empty paragraphs. A slice that is real prose, or prose plus a non-html fence, goes through `render_document` unchanged in policy.

**Return value.** The joined string. Callers already treat it as the fragment to append. No error dict.

**Exceptions.** `sanitize_agent_html` and `sanitize_with_local_images` / `sanitize_html` catch `BaseException` and return `""`. `render_message` must not add a new raise. `split_html_fences` only uses `str.replace` and `re.finditer`.

### 2b. `ui/handlers/chat_render_handler.py`

```python
def _compose_text(text: str, *, role: str = "") -> tuple[str | None, str]:
    payload = live_fence(text)
    if payload is not None:
        return payload, sanitize_agent_html(payload)
    return None, render_message(
        text, promote_html_segments=(role == "Agent"),
    )
```

`live_fence` stays first. A whole-message `` ```live `` fence never reaches segment promotion. A `` ```live `` fence that is not the whole message returns `None` from `live_fence` (anchored the same way as the html detector). With the flag on, that fence's language is `live`, not `html`, so it stays inside a text slice and `render_document` draws it as a code block. It does not execute.

Both call sites pass the role they already have:

- `_append_to_surface`: `_compose_text(text, role=role)`
- `_compose_off_thread`: `_compose_text(text, role=role)`

Default `role=""` keeps any forgotten caller on the old protocol (no segment promotion). The comparison is exact `"Agent"`. `"You"` and `"System"` do not promote. `_surface_role` is unrelated; do not reuse it here (it maps `"You"` → `"user"` for the surface, after compose).

Update the comments on `_compose_text`, `_append_to_surface`, and the module docstring that say the only HTML promotion is a whole-message fence.

### 2c. `ui/handlers/telegram_bridge_handler.py`

`_telegram_text` keeps the whole-message branch exactly, so `test_html_card_reply_flattened_to_text` still hits `_html_fragment_to_text` on the regex group and nothing else changes for that shape.

When that regex misses, ask `split_html_fences`. `None` → return `text` (plain replies stay byte-identical, including markdown markers). Otherwise:

```python
from render.html import split_html_fences

parts = split_html_fences(text)
if not parts:
    return text
chunks: list[str] = []
for kind, chunk in parts:
    if kind == "html":
        chunks.append(_html_fragment_to_text(chunk))
    else:
        chunks.append(chunk)
return "".join(chunks)
```

Do not `.strip()` the join. `_html_fragment_to_text` already strips each payload. Stripping the join would eat leading or trailing prose.

Leave the handler's `_WHOLE_MESSAGE_HTML_FENCE_RE` in place for the fast path. Do not point it at `render.html._WHOLE_MESSAGE_HTML_FENCE_RE` in this change (private, and the fast path is what the existing test locks).

The `except` around `_telegram_text` stays: any failure returns the original text. `split_html_fences` does not raise on str input. A non-str is already returned before the try's useful work (`if not isinstance(text, str) or not text`).

### 2d. Prompts

`prompts/chat-surface-authoring.md` section 2 and `prompts/system/crabcakes-context.md` the fence-discipline bullet. Split the rule:

- An `html` fence renders wherever it sits. Prose before and after it is normal markdown. Commentary does not have to be a second message.
- A `live` fence is still the entire message. One line of prose and the live section is not live.
- To show HTML as source, fence it as `` ``` ``, `` ```text ``, or `` ```xml ``. `` ```html `` always renders.
- A run of three backticks inside a card still ends the fence early. That sentence stays.

### 2e. `docs/specs/SPEC-13-AGENT-NATIVE-HTML-CHAT.md`

One status line under the mixed-content rule: agent rows no longer use that rule; SPEC-CHAT-MIXED-HTML does. Do not rewrite SPEC-13's historical implementation notes.

**Files NOT changed**

- `render/sanitize.py` — both policies stay as they are.
- `utils/block_parser.py` — the segment scanner normalizes newlines itself. Widening the block parser would change every markdown caller, including non-chat ones.
- `ui/views/chat_surface.py` — it appends the fragment it is given.
- `ui/handlers/chat_handler.py` — it already passes `role="You"` into `render_async`.
- Feed files — the other spec. Feed bodies are tool output and must not start calling `render_message`.

---

## 3. Data Flow

Agent reply, mixed:

`render_async("Agent", text, ...)` or `end_streaming` → `_append_to_surface(role="Agent", text)` → `_compose_text(text, role="Agent")` → `live_fence` is `None` → `render_message(..., promote_html_segments=True)` → whole-message regex misses → `split_html_fences` → each `html` part `sanitize_agent_html`, each non-empty text part `render_document` → fragment → `ChatSurface.append_message`.

User reply, mixed:

`render_async("You", text, ...)` → `_compose_text(text, role="You")` → `promote_html_segments=False` → `render_document(text)` → the fence is a code block. A whole-message `` ```html `` message still promotes at step 2, before the flag is consulted.

Supervisor reply to Telegram:

`on_supervisor_reply` → `_telegram_text`. Whole-message fence: existing flatten. Mixed: `split_html_fences`, html parts flattened, prose slices kept.

---

## 4. File Change Summary

| File | Change | ~Lines | Risk |
|---|---|---|---|
| `render/html.py` | `split_html_fences`, flag on `render_message` | +70 | med |
| `ui/handlers/chat_render_handler.py` | pass `role` into `_compose_text` | +15 | low |
| `ui/handlers/telegram_bridge_handler.py` | flatten mixed fences | +20 | low |
| `prompts/chat-surface-authoring.md` | html vs live fence rule | ~15 | low |
| `prompts/system/crabcakes-context.md` | same | ~10 | low |
| `tests/test_render_html.py` | both modes | +80 | low |
| `tests/test_chat_render_handler.py` | Agent promotes, You does not | +40 | low |
| `tests/test_telegram_bridge_handler.py` | mixed flatten | +25 | low |
| `docs/specs/SPEC-13-AGENT-NATIVE-HTML-CHAT.md` | status line | +3 | low |
| `docs/ARCHITECTURE.md` | one paragraph on the agent segment rule | +8 | low |

---

## 5. Implementation Order

1. `split_html_fences` and the flag, with unit tests, flag default still false. Existing `TestRenderMessage` stays green.
2. New tests with `promote_html_segments=True`.
3. Handler role gate plus a render-handler test that an Agent mixed message contains a live `<div` and a You mixed message does not.
4. Telegram branch plus a test that prose survives and the card's tags do not.
5. Prompts, SPEC-13 status line, ARCHITECTURE paragraph.
6. `pytest tests/test_render_html.py tests/test_chat_render_handler.py tests/test_telegram_bridge_handler.py tests/test_html_guard_sites.py -q` and ruff/pyright on the touched Python files.

The guard test `test_render_message_is_the_compose_entry` stays valid: `render_message` is still called only from `_compose_text`. A keyword argument does not move the call.

---

## 6. Acceptance Criteria

- [ ] Agent message `Here:\n```html\n<div style="color:red">hi</div>\n```\nDone.` renders a `<div` with that style, the words Here and Done as text, and no `&lt;div`.
- [ ] The same string with role `"You"` or `"System"` is still a code block (`&lt;` present, no live `<div`).
- [ ] A whole-message `` ```html `` fence is unchanged for every role, including CRLF and the empty fence.
- [ ] Plain markdown and a `` ```python `` fence with the flag on are byte-identical to `render_document`.
- [ ] `` ```text `` / bare `` ``` `` around `<div>x</div>` stays source when the flag is on.
- [ ] `<script>` inside a promoted fence is stripped; surrounding prose remains.
- [ ] An unclosed `` ```html `` fence stays on `render_document`.
- [ ] A `` ```image `` fence beside an html fence still emits a `data:image` `<img` when the path is valid, and the html fence still renders.
- [ ] A mixed `` ```live `` fence does not call `append_live`.
- [ ] Telegram: mixed Supervisor reply sends the prose and the card's visible text, and does not send `<div`.
- [ ] Telegram: whole-message card and plain text tests stay as they are.
- [ ] `render_message(123)` is still `""`.
- [ ] Prompts no longer say that prose around an `html` fence shows the card as source. They still say that for `live`.

---

## 7. Edge Cases

| Case | Behavior |
|---|---|
| Flag false, prose + html fence | Today's code block. Default. |
| Flag true, no html fence | `render_document` of the original string, not the normalized one. |
| Flag true, CRLF around an html fence | Normalize to `\n`, then split. The card renders. |
| Whole-message CRLF fence | Fast path regex. Splitter not used. |
| Empty html fence between prose | `sanitize_agent_html("")` contributes `""`. Prose remains. |
| Sanitizer panic on one fence | That fence is `""`. Other parts remain. |
| Two html fences | Both render, in order, prose between them kept. |
| `` ```html `` inside a python fence's body | The scanner is not nest-aware. The inner opener can start a fence. Same class of miss as `extract_blocks`. Not fixed here. |
| Three backticks inside the card | Non-greedy closer ends the fence. Rest is a text slice. Prompt still forbids this. |
| Unfenced `<div>` | Escaped by `_inline`. Not a fence. |
| User whole-message card | Still promoted. The flag is not consulted. |
| Role missing (`""`) | No segment promotion. |

---

## 8. ARCHITECTURE.md Updates Required

The directory blurb for `chat_surface.py` is not the composer. Add a short paragraph next to the chat-render description (the handler section that owns `_compose_text`, §3.14d if that is still the chat render orchestrator): agent rows promote every closed `` ```html `` fence; user and system rows promote only a whole-message fence; `` ```live `` is whole-message only. Note that Telegram's Supervisor mirror flattens those same fences to text.

Also the SPEC-15 sentence that says a whole-message html card is flattened for the phone (around the Telegram bridge blurb): add that a card with surrounding prose is flattened the same way, prose kept.
