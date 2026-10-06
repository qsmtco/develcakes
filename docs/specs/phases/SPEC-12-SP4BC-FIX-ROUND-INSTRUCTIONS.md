# SPEC-12 SP4b+c — FIX ROUND (Debugger coverage findings)

**Spec:** `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md` §2e/§2b
**Base:** `docs/specs/phases/SPEC-12-SP4BC-INSTRUCTIONS.md`
**Scope:** ONE test file — `tests/test_chat_handler.py`. No source edits (audited clean).

Auditor (Debugger) confirmed the source is correct but found two coverage gaps: the
solo-DM reply_target is unpinned (M2 drops it → suite stays green), and only 1 of the
6 mount_key sites is asserted (5 removable silently).

---

## BUG#1 (issue) — pin the solo-DM reply_target

Add to `TestSpec12ReplyTargetsAndMountKey`:

```python
    def test_solo_dm_send_targets_project_surface(self):
        """B2/BUG#11: a solo-DM send (get_solo_target non-None) still renders
        the reply in the project's ONE group surface (reply_target=project).
        Falsifier: drop the solo branch's reply_target → RED."""
        mc = FakeMainContent(session_key="project:alpha", input_text="hello")
        mc._tab_sessions = {0: "project:alpha"}
        gw = FakeGatewayClient()
        handler = make_handler(mc, gw)
        handler._project_handler = MagicMock()
        handler._project_handler.get_solo_target.return_value = "agent:q1"
        handler._chat_render_handler = MagicMock()
        handler._mc = mc

        handler.on_send()

        assert gw.get_reply_targets() == ["project:alpha"]
```

**Verify (RED):** drop the solo branch's `reply_target=f"project:{project_name}"`
→ this test fails; restore (sha-verified).

## BUG#2 (suggestion) — pin all 6 mount_key sites

Extend `test_render_async_call_sites_pass_mount_key` into per-site assertions (or
add per-branch cases). Cover:
- **forward_to echo** (:241) → `mount_key == result.forward_to` (drive a
  `forward_to` CommandResult; assert the render_async kwarg).
- **broadcast-command echo** (:279) → `session_key`.
- **special-agent echo** (:317) → `session_key`.
- **inline-solo echo** (:367) → `session_key`.
- **inline-broadcast echo** (:396) → `session_key`.
- **normal-send echo** (:424) → `session_key` (already pinned).

Each must be independently falsifiable: removing that one site's `mount_key=` turns
its assertion RED. Paste one representative per-site RED (e.g. the forward_to site).

## BUG#3 (observation — no code change)
Echo `mount_key` (current tab) and send `reply_target` (R7 routing) use distinct
models for the un-targeted branches; documented as intentional, out of SP4 scope.
No test needed.

## Verification battery (paste outputs)
- `cd /home/mushy/projects/develcakes && xvfb-run -a .venv/bin/python -m pytest tests/test_chat_handler.py -q` → all green (38 → ~44)
- RED proof for BUG#1 (solo reply_target drop) — pasted
- One per-site mount_key RED proof — pasted
- `~/.local/bin/ruff check tests/test_chat_handler.py` → ≤23 (baseline)

## Report back (mandatory COMPLETENESS)
```
COMPLETENESS:
- [x/not done] BUG#1: solo-DM test added — evidence: diff hunk
- [x/not done] BUG#1 RED proof — pasted
- [x/not done] BUG#2: 6 mount_key sites pinned — evidence: tests + per-site RED
- [x/not done] pytest + ruff — pasted
```

Invoke `prompts/steelFramedCodeWriter.md` before writing. Please write the tests per
this brief and report when done.