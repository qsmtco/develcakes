# Verification Request: Pango Markup Failure Modes in Chat Rendering

**Date:** 2026-07-30
**From:** Supervisor
**Request:** Verify the 4 failure modes below by independently reproducing each, then report whether the analysis is correct, incomplete, or wrong. Add any failure modes I missed.

## Background

The Pango `<a>`-tag fix (2026-07-30) removed `<a href>` emission from
`format_markdown`. That fixed one failure mode. But the user still sees
`Gtk-WARNING **: Failed to set text` warnings when opening a project
(re-rendering persisted conversation history). I ran all 328 messages from
`~/.config/crabcakes/conversations/special:supervisor.json` through the render
pipeline (`process_segments` → `format_markdown` → `Pango.parse_markup`) and
found **41 rejected segments** across 4 distinct failure modes.

## How to reproduce

```python
import json, gi
gi.require_require_version('Pango', '1.0')
from gi.repository import Pango
from ui.views.chat_bubble import process_segments

with open('~/.config/crabcakes/conversations/special:supervisor.json') as f:
    conv = json.load(f)
messages = conv if isinstance(conv, list) else conv.get('messages', [])

for i, msg in enumerate(messages):
    text = msg.get('content', msg.get('text', '')) if isinstance(msg, dict) else str(msg)
    for seg in process_segments(text):
        if seg.get('type') == 'text':
            markup = seg.get('markup', '')
            if markup.strip():
                try:
                    Pango.parse_markup(markup, -1, '\x00')
                except Exception as e:
                    print(f"MSG {i}: {e}")
                    print(f"  {markup[:200]!r}")
```

## The 4 claimed failure modes

### Mode 1: Unclosed `<u>` tags — auto-linker bare-hostname bug

**Pango error:** `Element "markup" was closed, but the currently open element is "u"`

**Claim:** The Step 4 auto-linker (`_AUTO_LINK_RE` group 2: bare-hostname
alternative) matches dotted identifiers like `ARCHITECTURE.md`,
`implementationLoop.md`, `steelFramedCodeWriter.md` as "bare hostnames" and
wraps them in `<u>...</u>`. In some cases the closing `</u>` is lost — likely
when the match spans a line boundary, a code-span placeholder, or intersects
with a `<b>` tag boundary.

**Example (MSG 2):**
```
Markup: '...<u>\x00CODE0\x00</u> instruction</b>...'
Error: Element "markup" was closed, but the currently open element is "u"
```

**Claimed impact:** HIGH — affects new messages containing any dotted
identifier (filenames, module paths).

**Verify:** (a) Does the auto-linker actually match `ARCHITECTURE.md`? (b) Does
it produce unclosed `<u>` tags? (c) What is the exact mechanism by which the
closing tag is lost? (d) Is the code-placeholder interaction the real cause?

### Mode 2: Step 3b double-href mangling

**Pango error:** `"..."" is not a valid name: """` / `Odd character "=", expected an open quote mark`

**Claim:** Text containing a literal `<a href="...">` (e.g., displaying source
code or the GTK warning). Step 3b's href-protection wraps the URL in
`href="..."` AGAIN, producing `href="href="URL""` — a malformed attribute.

**Example (MSG 232):**
```
anchor_html = f'<a href="href="{safe_url}""><u>{label}</u></a>'
```

**Claimed impact:** MEDIUM — affects messages quoting source code or warnings
containing `<a href`.

**Verify:** Reproduce `format_markdown('<a href="https://x.com">link</a>')` and
check whether Step 3b produces the double-href.

### Mode 3: Literal `<a>` in code/text not escaped

**Pango error:** `Unknown tag 'a'`

**Claim:** Messages containing grep output or source-code snippets with literal
`<a href` — the code highlighting or text segment doesn't escape `<a` before it
reaches Pango. Mostly old messages from before the fix.

**Claimed impact:** LOW for new messages; HIGH for re-rendering old history.

**Verify:** Check whether `escape_for_pango` and the code-segment highlighting
path actually escape `<a` tags inside code blocks.

### Mode 4: Attribute-value termination in quoted code

**Pango error:** `Document ended unexpectedly while inside an attribute value` / `Odd character ">", expected a "=" after attribute name`

**Claim:** Messages containing Python source code with string literals like
`'<a href="...">link</a>'` — quotes and angle brackets inside code interact with
Pango's XML parser despite escaping.

**Claimed impact:** LOW — only messages containing source code mentioning HTML.

**Verify:** Reproduce with a code block containing `'<a href="x">y</a>'`.

## What I need from you

1. **Reproduce all 4 modes** independently. Confirm or refute each.
2. **Find any modes I missed.** The 41 rejections may cluster into more than 4
   categories.
3. **For Mode 1 specifically:** determine the EXACT mechanism of the unclosed
   `<u>` tag. Is it the code-placeholder interaction? A regex boundary issue?
   A `<b>` nesting conflict? This determines the fix.
4. **Assess the proposed fixes:**
   - (A) Remove bare-hostname auto-linking entirely (only link `scheme://`)
   - (B) Fix the unclosed-`<u>` mechanism
   - (C) Disable all auto-linking for now
   Which is correct? Which is minimal? Which is overkill?
5. **Check whether `escape_for_pango` is the right layer to fix Modes 3+4,**
   or whether the code-segment highlighting path needs its own fix.
