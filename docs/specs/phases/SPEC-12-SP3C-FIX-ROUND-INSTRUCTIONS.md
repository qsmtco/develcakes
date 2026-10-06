# SPEC-12 SP3c — FIX ROUND (Debugger test-quality findings)

**Spec:** `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md` §2c (BUG#18)
**Base:** `docs/specs/phases/SPEC-12-SP3C-INSTRUCTIONS.md`
**Scope:** ONE test file — `tests/test_agent_runtime.py`. No source edits.

Auditor (Debugger) confirmed the rewrite is faithful (244 green, no test loss,
SPEC-09 SP2 section byte-identical) but found the spec's **BUG#18 "always normalize
the slot on send" rule is unpinned**: T4 hand-sets the slot instead of driving the
real send path, and a source that stops normalizing still passes.

---

## BUG#1 (issue) — pin the real send-path normalization

Add a test that drives `send_to_special_agent` itself (stub the runtime):

```python
    def test_send_to_special_agent_normalizes_slot_every_send(self):
        """SPEC-12 BUG#18/#26: the slot is set on ENTRY of EVERY send — an
        explicit reply_target (private /ask) wins; a bare send normalizes to
        the routing (project) key, so no stale private target survives.
        Drives the REAL send path (falsifier: a conditional set — only when
        reply_target given — leaves the bare-send slot stale → RED)."""
        handler, _crh, _mc = _make_handler()
        _register_coder(handler)
        handler._active_project = ["alpha", "/p"]
        handler._get_runtime = unittest.mock.MagicMock(
            return_value=unittest.mock.MagicMock())
        # Keep the test hermetic — do not read providers.yaml.
        handler._resolve_agent_model = unittest.mock.MagicMock(return_value=None)

        # Send 1: explicit private target → slot = the agent key.
        handler.send_to_special_agent(
            "special:coder", "private", reply_target="special:coder")
        assert handler._turn_reply_target["special:coder"] == "special:coder"

        # Send 2: bare send → slot NORMALIZED to the routing project key.
        handler.send_to_special_agent("special:coder", "group")
        assert handler._turn_reply_target["special:coder"] == "project:alpha"
```

**Verify (RED proof):** mutate the source so the slot is set ONLY when
`reply_target` is not None (the rejected conditional form):
```python
        if reply_target is not None:
            self._turn_reply_target[session_key] = reply_target
```
→ the Send-2 assert fails. Paste the RED, restore the source (sha-verified).

## BUG#2 (suggestion) — reword T4's docstring

T4 (`test_turn_scoped_slot_group_then_private_then_group`) is a valid UNIT pin of
`_reply_key` slot precedence, but its inline comment "Send 2: group send normalizes
the slot away (no reply_target)" describes a send it does not perform. Reword:
```python
        # Send 1: private /ask sets the slot to the agent key.
        handler._turn_reply_target["special:coder"] = "special:coder"
        assert handler._reply_key("special:coder") == "special:coder"  # private
        # Send 2: a NON-targeting send normalizes the slot to the routing key
        # (unit-level: the real send-path normalization is pinned by
        # test_send_to_special_agent_normalizes_slot_every_send).
        handler._turn_reply_target["special:coder"] = handler._resolve_mount_key(
            "special:coder")
        assert handler._reply_key("special:coder") == "project:alpha"  # group
```

## Do NOT
- Do not edit the source (frozen; SP3a/SP3b audited).
- Do not remove/rename other tests.

## Verification battery (paste all outputs)
- `cd /home/mushy/projects/develcakes && xvfb-run -a .venv/bin/python -m pytest tests/test_agent_runtime.py -q` → 245 passed (was 244)
- RED proof (conditional-slot mutant fails the new assert) — pasted
- `~/.local/bin/ruff check tests/test_agent_runtime.py` → ≤112 (HEAD baseline), no new

## Report back (mandatory COMPLETENESS)
```
COMPLETENESS:
- [x/not done] BUG#1: real send-path test added — evidence: diff hunk
- [x/not done] BUG#1 RED proof: conditional-slot mutant fails — pasted
- [x/not done] BUG#2: T4 docstring reworded — evidence: diff hunk
- [x/not done] pytest 245 + ruff — pasted
```

Invoke `prompts/steelFramedCodeWriter.md` before writing. Please write the fixes per
this brief and report when done.