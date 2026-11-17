# SPEC-65 Audit — Relay Truncation & Command-Injection Defects

**Date:** 2026-11-15
**Auditor:** Debugger
**Subject:** SPEC-64/65 implementation (uncommitted tree on `694431cf`)
**Method:** adversarial — real conversation data from `~/.config/develcakes/conversations/`, HEAD-vs-working-tree A/B, byte-exact reproduction

---

## Trigger

Coder delivered two `/ask` messages to the Debugger. Both arrived **truncated**:

```
[Coder asks]: unclosed payload
```

```
[Coder asks]: q2"'). I pinned the truthful invariant instead: each span locates its full `/cmd @Agent "payload
```

Neither was a real question. Both are **fragments of prose examples extracted from
Coder's own audit report** — the report was quoting the parser's own test fixtures
while describing them.

A third, identical symptom had already hit the Supervisor→Coder leg earlier in the
same session: Coder received a 1658-character order truncated to 522 characters,
ending at `...(2) the ` — cut precisely at the word `<think>`.

**The relay was silently eating messages. This is a live data-loss defect, not a
cosmetic one.**

---

## Root cause

`ui/handlers/agent_command_handler.py:818-823` — `_strip_thinking_blocks`:

```python
text = re.sub(r"<think\b[^>]*>.*?</think>", "\n", text, flags=re.DOTALL | re.IGNORECASE)
return re.sub(r"<think\b[^>]*>.*$", "", text, flags=re.DOTALL | re.IGNORECASE)
```

The second substitution strips from the **first unbalanced `<think>` to end-of-string**.
Any agent response that mentions `<think>` as a *word* (as prose, as a fixture name,
as a spec reference) has everything from that point onward deleted before command
scanning.

This regex is **mandated verbatim by the spec**: `docs/specs/SPEC-65-ASK-PARSER-REGISTRY-AND-RELAY-HARDENING.md:171-174`.

---

## BUG #1 — CRITICAL: unbalanced `<think>` truncates `/ask` payloads and silently drops later commands

**Severity:** CRITICAL · **Regression vs HEAD** (SPEC-65 introduced it)
**Location:** `ui/handlers/agent_command_handler.py:823`
**Spec:** `docs/specs/SPEC-65-...md:171-174` (mandates the regex), `:546` (AC)

### Evidence — real data, byte-exact

Supervisor→Coder order (from `agent:supervisor.json` idx=575):

| | length |
|---|---|
| original message | 1658 chars |
| payload parsed by **HEAD** | 1644 chars |
| payload parsed by **NEW** | **503 chars** |
| what Coder actually received | **522 chars** (`"[Supervisor asks]: "` + 503) |

Reconstruction is byte-exact — the probe asserted
`f"[Supervisor asks]: {payload}" == received` → **True**.

The cut lands exactly at the `<think>` in the phrase
`note the <think> fixtures at :48-49 are malformed`.

### Isolated reproduction

```python
t = '/ask @Coder "first half of the order; note the <think> fixtures are malformed; rewrite them."'
# HEAD -> payload len 138 (whole payload)
# NEW  -> payload len  34  'first half of the order; note the '
```

### Worse: commands after the opener are lost entirely

```python
t = '<think>reasoning\n/ask @Coder "real command after the think opener"'
# HEAD -> [('ask', '@Coder', 'real command after the think opener')]
# NEW  -> []            # the genuine command is SILENTLY LOST
```

### Why the test suite cannot catch it

`tests/test_spec65_ask_command_hardening.py:67` **pins the data loss as correct**:

```python
assert _cmds("<think>\n/ask @Coder \"never closed") == []
```

This asserts that an unclosed `<think>` kills everything after it. The behaviour is
now locked in by the acceptance suite.

### Fix direction

The sanitizer must not treat a `<think>` appearing **inside a quoted payload or
prose** as a block opener. Options:

1. Only strip `<think>` when it is the **last** thing in the string and the block
   is genuinely unterminated *and* no quoted payload is open.
2. Strip think-blocks **before** command scanning but **do not** let the strip
   rewrite text that a command payload spans — i.e. run the strip on a per-candidate
   basis, or mask the block regions instead of deleting them (preserve offsets).
3. At minimum: if the strip would delete text containing an open `/cmd @Agent "`
   span, refuse to strip and let the command be parsed.

Note the fix belongs in the **spec** (the regex is mandated there), so this is a
spec defect as well as an implementation defect.

---

## BUG #2 — CRITICAL: prose examples are extracted as real commands and relayed to live agents

**Severity:** CRITICAL · **Command injection**
**Location:** `ui/handlers/agent_command_handler.py:59-61` (`_CMD_PREFIX_RE`), `:167-280` (`_extract_quoted_commands`)
**Spec:** `docs/specs/SPEC-65-...md:487` (AC requires code-span examples to be inert)

### Evidence — the two fragments I received

Running the NEW scanner on Coder's real report (10109 chars, from `agent:supervisor.json` idx=576):

| | commands extracted |
|---|---|
| **HEAD** | **1** — `ask @Debugger "q2"` |
| **NEW** | **3** — `ask @Debugger` (97-char payload), `clear @Debugger\n/ask`, `ask @Debugger "unclosed payload"` |

The two extra extractions are precisely the two fragments delivered to me:

- `/ask @Debugger "unclosed payload"` — from Coder's prose `res.text unmodified (\`/ask @Debugger "unclosed payload"\`)`
- `/ask @Debugger "q2"\'`). I pinned the truthful invariant instead: ...` — from Coder's prose `(\`' /ask @Debugger "q2"\'\`)`

### The `/clear` one is worse

The third extraction is a **`/clear`** aimed at the agent named `@Debugger\n/ask`
(a literal backslash-n captured into the agent token):

```
clear -> @Debugger\n/ask    span = ' `/clear @Debugger\n/ask '
```

That is a conversation-clearing command aimed at a non-existent agent, extracted
from a code span inside a bullet list.

### The command cap is then fully consumed

`_MAX_COMMANDS_PER_RESPONSE = 3` (`ui/handlers/agent_command_handler.py:39`).
The three spurious commands consume the entire per-response budget. Dispatch-level proof:

```
dispatched 3 command(s) (cap = 3):
  [0] '/ask @Debugger "q2"\'`)...'
  [1] '/clear @Debugger\n/ask'
  [2] '/ask @Debugger "unclosed payload"'
GENUINE command dispatched: False
```

A **genuine** `/ask @Supervisor "do the real thing"` appended to the same response is
**silently dropped** — the cap is exhausted by the prose examples. This is exactly
the "draft commands inside `<think>` exhaust the 3-command cap" failure mode
SPEC-65 Finding 2B (`docs/specs/SPEC-65-...md:53`) set out to prevent; the fix
re-introduced it from a different direction.

### Regression mechanism

`_CMD_PREFIX_RE` (`ui/handlers/agent_command_handler.py:59-61`) now accepts an
inline Markdown wrapper before the slash:

```python
r"(?:^|\s|(?:(?<=^)|(?<=\n))[ \t]*[-+>][ \t]*)[`*_(\[]*/"
```

So a command inside a **code span**, **bold**, or **parentheses** is now matched:

| pattern | HEAD | NEW |
|---|---|---|
| `` see `/ask @Debugger "q"` `` | 0 | **1** |
| `see (/ask @Debugger "q")` | 0 | **1** |
| `**/ask @Debugger "q"**` | 0 | **1** |
| `see /ask @Debugger "q"` | 1 | 1 |

`_strip_fenced_blocks` only removes **fenced** (``` ``` ```) blocks — inline code
spans are untouched, so the new prefix rule reaches straight into them.

### Fix direction

Inline code spans (single-backtick) must be neutralised before scanning, exactly as
fenced blocks are. The spec's own AC (`:572`) states the intent for HTML cards
(`<code>/ask @Coder "example"</code>` → not executed) — the same rule must apply to
Markdown inline code.

---

## BUG #3 — HIGH: `raw_start` / `raw_end` index the SANITIZED text, not the original

**Severity:** HIGH · **Contract violation**
**Location:** `ui/handlers/agent_command_handler.py:167-280` (`_extract_quoted_commands`)

`_extract_quoted_commands` computes `clean = _sanitize_for_command_scan(text)` and
then records `raw_start` / `raw_end` as offsets **into `clean`**. The offsets are
returned to the caller as if they pointed into the original text.

Measured offsets:

| input | original len | sanitized len | shift |
|---|---|---|---|
| think block | 58 | 23 | 35 |
| code fence | 53 | 22 | 31 |
| html card | 66 | 25 | 41 |

Consequence — the span no longer locates the command:

```
span in ORIGINAL : 'think>\n/ask @Coder "dr'   -> locates cmd? False
span in SANITIZED: '\n/ask @Debugger "real"'  -> locates cmd? True
```

This directly contradicts the field's documented contract
(`ui/handlers/agent_command_handler.py:48`: *"Fields: command, agent, payload,
raw_start, raw_end"*) and the workorder item 3 invariant Coder was asked to pin
(`tests/test_spec65_ask_command_hardening.py:97-120`, *"raw_start points at ...
the original text"*).

The test only passes because its fixture (`/ask @Coder "q1" /ask @Debugger "q2"\n/ask @Supervisor "q3"`)
contains **no `<think>`, no fence, no HTML card** — i.e. sanitize is a no-op and
the two coordinate systems coincide. The invariant is pinned only on the one input
class where it cannot fail.

### Fix direction

Mask the stripped regions (replace with equal-length whitespace) instead of
deleting them, so offsets stay valid; or map offsets back to the original text.

---

## BUG #4 — MEDIUM: the new sanitizer regex deletes the payload's own `<think>` mention

**Severity:** MEDIUM · **Compounding factor for BUG #1**
**Location:** `ui/handlers/agent_command_handler.py:818-823`

Even for a *balanced* block, the strip rewrites the payload:

```python
'/ask @Coder "keep <think> x</think> this"'
-> sanitized: '/ask @Coder "keep \n this"'
```

The payload loses the literal text it was quoting. A payload that quotes the parser's
own fixtures cannot survive its own sanitizer.

---

## Findings NOT regressions (pre-existing, HEAD reproduces identically)

| # | Finding | Evidence |
|---|---|---|
| P1 | Bare `/ask @Agent "q"` in plain prose is executed (not in a code span) | HEAD=1, NEW=1 |
| P2 | Agent token captures a literal `\n` (`@Debugger\n/ask`) | HEAD and NEW both yield `clear @Debugger\n/ask` |
| P3 | `/ask` payload with no closing quote yields no command | HEAD=0, NEW=0 |

These are real weaknesses but are **not** introduced by SPEC-64/65 and should not
block this merge. P1 in particular is the root enabler for BUG #2 — a future fix
should address inline code spans (new) and prose-vs-intent disambiguation (P1).

---

## Summary

| # | Severity | Type | Introduced by SPEC-64/65? |
|---|---|---|---|
| BUG #1 | CRITICAL | Payload truncation + silent command loss | **Yes** (regression) |
| BUG #2 | CRITICAL | Command injection from prose examples + cap exhaustion | **Yes** (regression: 1→3 extractions) |
| BUG #3 | HIGH | `raw_start`/`raw_end` index the wrong string | **Yes** (new feature, broken contract) |
| BUG #4 | MEDIUM | Sanitizer eats the payload's own `<think>` | **Yes** (regression) |

**Verdict: DO NOT MERGE.** BUG #1 silently deletes agent orders and commands in
production traffic; BUG #2 turns any agent that documents the command syntax into
a command injector and silently starves genuine commands.

---

## Appendix — probes

All probes were read-only, lived under `.crabcakes/scratch/probe_*.py`, and were
deleted after use. Key results are reproduced inline above. No product code was
modified by this audit.
