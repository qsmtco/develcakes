# tests/test_spec66_think.py — SPEC-66 §2.8: the think scratchpad rule.
#
# R-19 row node: test_prose_think_keeps_later_ask
#
# PLAIN runner (§12.7): the closure is GTK-free. Import the scanner DIRECTLY
# from ui.handlers.agent_command_handler — never ui.window, which builds GTK
# widgets at import time and would turn this plain row into an xvfb row.
#
# The rule (Debugger-verified design, §2.8):
#   1. Balanced <think ...>...</think ...> pairs are removed ANYWHERE.
#   2. An UNCLOSED opener removes the rest of the message ONLY when it is the
#      first non-whitespace thing in it (the model opened with reasoning and
#      was truncated — the SPEC-65 pinned assertion).
#   3. An unclosed opener that appears AFTER other prose must NOT eat the
#      payload; a command following a later, separate think block is still
#      scanned. (SPEC-65 audit CRITICAL: truncation dropped a real /ask.)
#
# NOTE on the tag shape: the parser's pattern is `<think\b[^>]*>`, so an opener
# needs its closing `>` to be a tag at all. Every fixture below is composed
# from OPEN/CLOSE for that reason; test_bare_opener_without_gt_is_prose pins
# the no-`>` case deliberately.

from __future__ import annotations

from ui.handlers.agent_command_handler import (
    AgentCommandHandler,
    _extract_quoted_commands,
)

# This module imports ui.handlers.* only (no ui.window / ui.views.* /
# agent_runtime_handler / feed_handler), so it runs under the plain runner.
# The flag documents the contract §12.7(b) asks of every plain row.
GTK_HEADLESS_SAFE = True

OPEN = "<think"        # a complete opener is OPEN + ">"
CLOSE = "</think"      # a complete closer is CLOSE + ">"


def _cmds(text: str, names: set[str] | None = None):
    """The scanner under test — mirrors the SPEC-65 helper."""
    return _extract_quoted_commands(text, names)


def _strip(text: str) -> str:
    return AgentCommandHandler._strip_thinking_blocks(text)


def test_prose_think_keeps_later_ask():
    """R-19 NODE: prose, then a LATER think block, then a real command.

    Pre-fix the trailing `re.sub(r"<think\\b[^>]*>.*$")` ran on the whole
    message, so the later opener swallowed the /ask below it and the command
    was silently lost. The guard only fires now when the message STARTS with
    the opener, so this command survives.
    """
    text = (
        "Here is my plan.\n"
        + OPEN + ">\n"
        "let me reason about the parser\n"
        "\n"
        "/ask @Debugger \"verify the lexer\""
    )
    assert not text.startswith(OPEN) or text.startswith("Here"), text
    got = _cmds(text)
    assert [c.payload for c in got] == ["verify the lexer"], (
        "a later unclosed think block ate the command: "
        f"{[(c.agent, c.payload) for c in got]!r}"
    )
    assert got[0].agent == "@Debugger"


def test_command_after_a_balanced_think_block_is_scanned():
    """A CLOSED block followed by a command: the command must be seen (the
    balanced removal already handled this — pinned so the new guard does not
    regress it)."""
    text = (
        "Reasoning first.\n"
        + OPEN + ">\n"
        "draft\n"
        + CLOSE + ">\n"
        "\n"
        "/ask @Coder \"implement it\""
    )
    assert [c.payload for c in _cmds(text)] == ["implement it"]


def test_balanced_block_content_is_never_scanned():
    """Nothing INSIDE a think block is a command — balanced pairs are removed
    everywhere, including mid-message."""
    text = (
        "Prose.\n"
        + OPEN + ">\n"
        "/ask @Debugger \"should not run\"\n"
        + CLOSE + ">\n"
        "\n"
        "/ask @Coder \"real\""
    )
    got = _cmds(text)
    assert [c.payload for c in got] == ["real"], (
        "a command inside a balanced think block leaked into the scan"
    )


def test_unclosed_leading_think_still_swallows_everything():
    """PINNED SPEC-65 ASSERTION (§2.8, unchanged): a message that OPENS with an
    unclosed block is truncated reasoning — nothing after it is a command.
    Repeated here so R-19's mutation cannot satisfy one suite and break the
    other. Literal kept byte-identical to the SPEC-65 fixture."""
    assert _cmds("<think" + ">\n/ask @Coder \"never closed") == []


def test_unclosed_leading_think_after_leading_whitespace():
    """The same rule with leading whitespace/newlines (the model often emits a
    blank line first) — `lstrip()` is what makes that still count as leading."""
    assert _cmds("\n\n   " + OPEN + ">\n/ask @Coder \"never closed") == []


def test_truncation_mid_payload_preserves_the_prose():
    """SPEC-65-audit CRITICAL truncation case: prose that preceded the opener
    must survive. That prose is not reasoning, so it is not the model's to
    delete — only a message that IS reasoning from the first character loses
    its tail."""
    text = "Ship the fix.\n" + OPEN + ">\nstill thinking about edge cases"
    assert "Ship the fix." in _strip(text), (
        "the unclosed block ate prose that preceded it (SPEC-65-audit CRITICAL)"
    )
    # ...and with a command below it, the command is still scanned.
    with_cmd = (
        "Ship the fix.\n"
        + OPEN + ">\n"
        "still thinking\n"
        "/ask @Debugger \"confirm the truncation fix\""
    )
    assert [c.payload for c in _cmds(with_cmd)] == ["confirm the truncation fix"]


def test_balanced_pair_is_removed_even_when_unclosed_follows():
    """Both shapes at once: the balanced pair is stripped, and the following
    unclosed opener (no longer leading, once the pair is gone) is not treated
    as truncation."""
    text = (
        OPEN + ">\nscratch\n" + CLOSE + ">\n"
        "\n"
        "Answer: done.\n"
        + OPEN + ">\nmore reasoning"
    )
    stripped = _strip(text)
    assert "Answer: done." in stripped
    assert "scratch" not in stripped, "the balanced block survived"


def test_bare_opener_without_gt_is_prose():
    """Documents the tag shape the guard inherits: `<think\\b[^>]*>` needs the
    closing `>`, so a bare opener is not a tag. The text is untouched and its
    command is scanned. Pinned so the prefix guard cannot be "fixed" into a
    bare-substring test without that case being decided deliberately."""
    assert [c.payload for c in _cmds(OPEN + "\n/ask @Coder \"v\"")] == ["v"]
