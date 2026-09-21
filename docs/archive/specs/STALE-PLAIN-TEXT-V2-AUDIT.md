# Final Audit Request: Stale plain_text fix v2 (unconditional + crabcard-safe)

## What changed from v1

v1 used a length guard (`len(text) > len(streaming_text)`) which Debugger
found unsafe (BUG #1: overwrites crabcard-cleaned text; BUG #2: length isn't
provenance). 

v2 restructures the ordering: set authoritative text FIRST, then run crabcard
extraction on it. No length guard.

## The fix (in _do_response_complete)

```python
# Step 1: Set sb.plain_text to the authoritative full text unconditionally
if was_streaming and text:
    self._crh.set_streaming_text(session_key, text)

# Step 2: Crabcard extraction runs on the authoritative text
if was_streaming and project_name and self._fh is not None:
    full_text = self._crh.get_streaming_text(session_key) or ""
    if full_text:
        cleaned, cards = extract_crabcards(full_text, ...)
        if cards:
            self._fh.add_cards_batch(cards)
            self._crh.set_streaming_text(session_key, cleaned)

# Step 3: end_streaming reads sb.plain_text (now authoritative + cleaned)
streaming_text = self._crh.get_streaming_text(session_key) or ""
self._crh.end_streaming(session_key, ..., render=bool(streaming_text.strip()))
```

## Why this fixes both v1 bugs

- **BUG #1 (crabcard overwrite):** Crabcard extraction runs AFTER the
  authoritative text is set. If cards are found, `cleaned` overwrites the raw
  text. The raw text is never re-applied after cleaning.
- **BUG #2 (length guard):** No length comparison. The authoritative `text`
  is always used when `was_streaming and text`.

## Questions for the auditor

1. Is the ordering correct? Authoritative text → crabcard extraction → end_streaming.
2. Could `text` ever be empty/None when `was_streaming=True`? If so, the
   `if was_streaming and text:` guard skips the overwrite, leaving stale
   `sb.plain_text`. Is this a problem?
3. Does `set_streaming_text` actually update `sb.plain_text` on the
   StreamingBubble object? Or does it update something else?
4. Is there any case where the `text` argument differs from what was streamed
   (e.g. tool-loop multi-iteration where `text_content` is only the last
   call's text)?

## File
`ui/handlers/agent_runtime_handler.py` — `_do_response_complete` (1466+)

Write findings to `docs/specs/STALE-PLAIN-TEXT-V2-FINDINGS.md`.
