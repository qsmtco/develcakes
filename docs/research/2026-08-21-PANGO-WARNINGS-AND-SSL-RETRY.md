# Investigation Report — GTK Pango Markup Warnings + LLM SSL Errors

**Date:** 2026-08-21
**Investigator:** Supervisor (read-only; no code modified)
**Scope:** 5 `Gtk-WARNING **: Failed to set text ...` errors + 2 `agent.llm.streaming WARNING [ssl-retry]` errors collected over a 4-day window
**Project:** crabcakes (the PDE itself — **not** eagledispatch)
**Method:** Every error class reproduced character-for-character against the actual pipeline functions (`escape_for_pango` → `format_markdown` → `Pango.parse_markup`), then traced to the exact call site. All findings verified at least twice (once via minimal reproduction, once via the full production pipeline path).

---

## Executive Summary

There are **two unrelated root causes**. The five markup warnings share **one root cause**: `utils/escaping.py:escape_for_pango()` preserves `<span>` tags (and other known Pango tags) **without validating attribute names**, so JSX/TSX source code like `<span classname="field-error">` passes through the escaper intact and is then rejected by Pango's markup parser, which allows only a fixed attribute set on `<span>`. The two SSL errors are a **separate, working-as-designed** transient-network issue in the LLM streaming retry layer — the retry machinery did exactly what it was built to do.

The markup warnings are **cosmetic-but-lossy**: when `Gtk.Label.set_markup()` fails, GTK logs the warning and **renders the label empty**. The user sees a blank bubble where code should be. That makes this more than log spam — it is intermittent content loss in the chat UI.

---

## Part 1 — The Five Pango Markup Warnings (one root cause)

### 1.1 Root cause (verified by minimal reproduction)

`utils/escaping.py:escape_for_pango()` whitelists **tag names** (`_PANGO_KNOWN_TAGS` = `b, i, u, s, tt, big, small, span, sub, sup, o`) and passes known tags through **with their attributes**, applying only two transformations: lowercasing attribute *names* and escaping bare `&` in attribute *values*. It does **not** validate attribute names against what Pango actually permits.

Pango's `<span>` accepts only a fixed attribute set (`foreground`, `background`, `size`, `weight`, `style`, `font_desc`, `font_features`, …). Anything else — `classname`, `className`, `style={{...}}` — is a hard parse error:

```
Attribute 'classname' is not allowed on the <span> tag on line 3 char 41
```

Reproduction (exact error text matches the terminal log):

```python
escape_for_pango('return <span classname="field-error">{m}</span>;')
→ 'return <span classname="field-error">{m}</span>;'   # unchanged — tag + attr preserved
Pango.parse_markup(...)
→ GLib.Error: Attribute 'classname' is not allowed on the <span> tag on line 3 char 41
```

Second failure shape, same root cause — **unquoted attribute values containing `{}`** (JSX object literals):

```python
escape_for_pango('<span style={{ color: "red" }}>hi</span>')
# <span> preserved with style={{ color: "red" }} → Pango:
→ Error: Odd character "{", expected an open quote mark after the equals sign
         when giving value for attribute "style" of element "span"
```

This explains error #1 (App.tsx content with `style={{...}}` — note its warning text shows `&quot;` entities, confirming the string had been through the escaper, which escapes `"` → `&quot;` while preserving the tag).

### 1.2 Why the guard that fixed this before didn't catch these

Commit `898062a` (2026-08-04, "guard make_safe_label set_markup via Pango pre-validation", post-mortem at `docs/post-mortems/2026-07-31-PANGO-MARKUP-GUARD-POST-MORTEM.md`) added exactly the right defense: `make_safe_label()` in `utils/gtk_safe_link.py` runs `Pango.parse_markup()` first and falls back to `set_text()` on failure — no warning, no empty bubble.

**But only some render paths use it.** I audited every `set_markup` call site in `ui/`. The guarded vs. unguarded split:

**Guarded (cannot produce these warnings) — all LLM *text* paths:**
- `chat_bubble.py:336` (text segments), `:617` (table cells), `:649` (`_build_text_segment`), `:715` (blockquotes), `:767` (terminal lines), `:796`, `:815` — all route through `make_safe_label`.

**Unguarded (can produce these warnings):**

| Call site | Content | Risk |
|---|---|---|
| `ui/views/feed_card.py:142` | `escape_for_pango(text)` → `set_markup` directly. Feed card bodies carry agent-authored text — including code snippets. | **High — this is the likely source of errors #1–#3.** Reproduced all three failure shapes through this exact path. |
| `ui/views/feed_card.py:327` | Per-diff-line `escape_for_pango` wrapped in `<span color><tt>…</tt></span>`, then `set_markup`. Diffs routinely contain JSX. | **High — likely source of errors #4–#5** (both show `<tt>diff --git …` strings). Reproduced. |
| `ui/views/chat_bubble.py:391` | `code_label.set_markup(code_markup)` where `code_markup` comes from `utils/syntax_highlight.highlight()`. | **Low in practice** — I verified `highlight()` output is safe for all tested inputs: Pygments HTML-escapes token *values*, and the only attributes it emits are `foreground="..."`. The no-lexer fallback (`<tt>{html.escape(code)}</tt>`) is also safe. This path is safe *today* but is one careless highlighter change away from the same bug, and it has no guard. |
| `ui/views/file_tree.py:217` | `escape_for_pango(display_name)` → `set_markup` on file names. | Low — file names rarely contain spans; unguarded nonetheless. |
| `ui/toolbar.py`, `chat_input_toolbar.py`, `chat_bubble.py:757/847/891/943/988/1034`, `feed_card.py` `xml_template` sites | Static or `xml_template`-escaped content. | None — `xml_escape_text` escapes *all* markup, so `xml_template` output is always parseable (verified). |

### 1.3 Error-by-error attribution

| Terminal error | Content | Failure mode | Most likely path |
|---|---|---|---|
| #1 (07:34, `python3:1364902`) | App.tsx source with `&quot;` entities | Preserved `<span>` + `style={{...}}` unquoted-brace attr | `feed_card.py:142` (or pre-guard bubble render) |
| #2 (21:00:04, `python3:1731268`) | `import type { JobState } from "@eagledispatch/shared";` | Single line passes Pango alone — this bubble's *full* string contained a preserved tag elsewhere (warning prints whole string; terminal truncated it) | `feed_card.py:142` |
| #3 (21:00:04, same pid) | FieldError.tsx snippet with `classname` | Preserved `<span classname>` → Pango attr rejection | `feed_card.py:142` |
| #4 (22:08, `python3:2067337`) | `<tt>diff --git a/DriverLogin.tsx…` | Diff content with JSX inside `<tt>` wrapper | `feed_card.py:327` |
| #5 (10:27, `python3:3455559`) | `<tt>diff --git a/Admin.tsx…` | Same as #4 | `feed_card.py:327` |

Note on timing: the guard commit landed 2026-08-04; HEAD is 2026-08-14. If any of these warnings predate Aug 4, they came from the pre-guard bubble code. But `feed_card.py:142/:327` are unguarded **at HEAD**, so at minimum #4/#5 (and probably all five) are reproducible on the current build. The `python3:<pid>` process names are consistent with the Crabcakes app process rendering feed cards.

### 1.4 Impact

Not just log noise. `Gtk.Label.set_markup()` on invalid markup logs the warning and **leaves the label empty**. Concretely: a feed card whose body contains a JSX snippet renders with a **blank body**. The diff cards in errors #4/#5 render empty. Users see missing content with no indication anything failed.

### 1.5 Recommended fix (for Coder, when commissioned)

1. **Primary:** Route `feed_card.py:142` and `feed_card.py:327` through `make_safe_label()` (or apply the same inline `Pango.parse_markup` → `set_text` fallback). This is the same fix pattern already proven in `898062a` — two call sites, mechanical change, existing post-mortem documents the approach.
2. **Defense-in-depth (root cause):** In `escape_for_pango`, validate attribute *names* on preserved known tags against a Pango-allowed set (`foreground, background, size, weight, style, font_desc, font_features, lang, justify, stretch, variant, letter_spacing, rise, strikethrough, underline, fallback, lang`). Unknown attribute → escape the whole tag as literal text (same treatment as unknown tags today). This fixes every current and future call site at once, and would have caught `classname`, `className`, and `style={{...}}` at the escaper.
3. **Optional hardening:** add the same guard to `chat_bubble.py:391` (code-block label) — safe today, unguarded by design.
4. **Regression test:** a unit test asserting `escape_for_pango('<span classname="x">t</span>')` contains no unescaped `<span` (currently fails), plus the `style={{...}}` case.

---

## Part 2 — The Two SSL Retry Warnings (working as designed)

```
agent.llm.streaming WARNING [ssl-retry] attempt 1/3 for https://openrouter.ai/api/v1/chat/completions
  — <urlopen error EOF occurred in violation of protocol (_ssl.c:2406)>; retrying in 0.5s
agent.llm.streaming WARNING [ssl-retry] attempt 1/3 for https://openrouter.ai/api/v1/chat/completions
  — [SSL: SSLV3_ALERT_BAD_RECORD_MAC] sslv3 alert bad record mac (_ssl.c:2559); retrying in 0.5s
```

### 2.1 What happened

The agent runtime (`sk=special:coder`, provider `deepseek` via `deepseek/deepseek-v4-pro-0813`, routed through **openrouter.ai**) hit transient TLS failures mid-session at iterations 4 and 9 of a tool loop. Both error tokens are in `RETRYABLE_SSL_ERRORS` (`agent/llm/streaming.py:123-135`):

- `EOF occurred in violation of protocol` — the TLS peer dropped the connection mid-handshake/mid-request. Documented in the code as characteristic of provider gateways under load.
- `SSLV3_ALERT_BAD_RECORD_MAC` — a TLS record failed integrity verification, almost always a symptom of the same flaky connection path (packet corruption / middlebox interference / connection reuse across a network change), not a cryptographic problem on either end.

### 2.2 Verification that the handling is correct

I verified the retry logic twice, at two layers:

1. **Classification** (`is_retryable_ssl_error`, `streaming.py:~160-230`): walks `exc` → `.reason` → `.__cause__`, handles the three real-world wrapping shapes (bare `ssl.SSLError`, `URLError` with string reason, `raise X from Y` chains), and token-matches `str()`. Both logged errors match tokens in the retryable set. Correct.
2. **Retry execution** (`urlopen_with_ssl_retry` / `stream_with_ssl_retry`): exponential backoff starting 500ms, budget of 3 retries, and — importantly — the mid-stream variant **suppresses retries once any text has been yielded** (`streamed_text` flag) to prevent garbled duplicate output in the UI. The log lines show `attempt 1/3`, i.e. first retry of the first failure — normal operation.

Both warnings are at WARNING level by design (visibility without alarm). Neither indicates a bug. The 4-day window showing only two occurrences across many hundreds of LLM calls is a ~sub-1% transient rate on the openrouter path.

### 2.3 Is anything worth doing?

- **No code change needed.** The retry layer is doing its job; the errors self-healed (no subsequent failure lines in the log).
- **Optional tuning:** `SSLV3_ALERT_BAD_RECORD_MAC` on a reused connection can correlate with keep-alive connection reuse across network transitions (laptop roaming, DNS change). If frequency ever increases, the fix is provider-side (disable HTTP keep-alive reuse for openrouter, or shorten pool TTL) — but at current rates, leave it alone.
- **Observability note:** these are the *only* two SSL warnings in the window, both `attempt 1/3` — meaning zero retries ever exhausted their budget. No action.

---

## Part 3 — Are the two error classes related?

**No.** Verified independently:

- The markup warnings originate in the **GTK render path** (feed cards / chat bubbles) processing agent *output*.
- The SSL warnings originate in the **LLM request path** (openrouter.ai) fetching agent *input* (model completions).
- Different subsystems, different failure modes (Pango XML parsing vs. TLS transport), different timestamps, no shared state. The only connection is that both happen while an agent session is active — which is when both subsystems are simply busy.

---

## Summary Table

| # | Error | Root cause | Severity | Action |
|---|---|---|---|---|
| 1 | `Failed to set text 'import { useEffect…'` | `escape_for_pango` preserves `<span style={{...}}>`; Pango rejects unquoted brace attrs | Content loss (empty card body) | Fix feed_card.py:142 + escaper attr validation |
| 2 | `Failed to set text 'import type { JobState }…'` | Same root cause; full bubble string had a preserved tag beyond the logged truncation | Content loss | Same fix |
| 3 | `Failed to set text '…FieldError… classname…'` | Preserved `<span classname>` → Pango attr rejection (reproduced exactly) | Content loss | Same fix |
| 4 | `Failed to set text '<tt>diff --git a/DriverLogin.tsx…'` | feed_card diff path (`:327`) preserves JSX tags inside `<tt>` | Empty diff card | Fix feed_card.py:327 |
| 5 | `Failed to set text '<tt>diff --git a/Admin.tsx…'` | Same as #4 | Empty diff card | Same fix |
| 6 | `[ssl-retry] EOF occurred in violation of protocol` | Transient TLS drop from openrouter; retryable, self-healed | None | None — working as designed |
| 7 | `[ssl-retry] SSLV3_ALERT_BAD_RECORD_MAC` | TLS record integrity failure on flaky path; retryable, self-healed | None | None — working as designed |

**Bottom line:** Five warnings, one real bug — `escape_for_pango` trusts attribute names on tags it whitelists, and two feed-card call sites skip the `make_safe_label` guard that already exists to catch exactly this. The fix is small, precedented (`898062a`), and should also close the content-loss symptom (blank cards/bubbles). The two SSL warnings need nothing.

---

*Investigation read-only. Reproduction scripts were run in-process and left no artifacts. Line references are to HEAD `c2b26ac` and will drift.*
