# SPEC-12 SP3c — rewrite R7-precedence tests + add the new pins

**Spec:** `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md` §2c, §6
**Pre-flight:** `docs/specs/phases/SPEC-12-PREFLIGHT-DECISIONS.md`
**Base:** SP3a/SP3b (source done + audited)
**Supervisor:** Supervisor; **Builder:** Coder; **Auditor:** Debugger
**Scope:** ONE test file — `tests/test_agent_runtime.py`. No source edits.

RED-first: the 2 precedence tests already fail against the landed source — rewrite
them to the R7 contract, then add the new pins. Target: ALL GREEN.

Helpers available: `_make_handler()` → `(handler, crh, mc)`, `_register_coder(handler)`.

---

## T1 — rewrite `test_streaming_direct_tab_branch_takes_precedence`

**Why:** the old test pinned direct-tab precedence in `_resolve_mount_key`. SP3a
dropped it (R7): the display key is the project; a private view is expressed through
`reply_target`/`_reply_key`, not tab precedence. Rename to
`test_streaming_reply_key_uses_turn_target_over_routing` and rewrite:

```python
    def test_streaming_reply_key_uses_turn_target_over_routing(self):
        """SPEC-12 R5 REV 3: a turn's reply key is the PER-SEND slot when set
        (a private /ask), NOT the routing project. Here the slot = the agent
        key (private), routing = project:alpha; the crabcard tab_key must be
        the agent key. Falsifier: drop the slot read in _reply_key → tab_key
        becomes project:alpha."""
        handler, crh, mc = _make_handler()
        _register_coder(handler)
        handler._agent_to_project = unittest.mock.MagicMock()
        handler._agent_to_project.get_project = lambda sk: "alpha"
        handler._active_project = ["alpha"]
        handler._turn_reply_target["special:coder"] = "special:coder"  # private
        fh = unittest.mock.MagicMock()
        handler.set_feed_handler(fh)
        crh.is_streaming.return_value = True
        crh.get_streaming_text.return_value = (
            "before\n\n```crabcard\ntype: diff\n"
            "title: Private card\nfile: x.py\n---\n+body\n```\n"
        )
        handler._do_response_complete("special:coder", "unused when streaming")
        card = fh.add_cards_batch.call_args.args[0][0]
        assert card.metadata["tab_key"] == "special:coder", (
            "the turn-scoped slot (private) must win over routing"
        )
```

## T2 — rewrite `test_streaming_fallback_branch_no_tab_no_routing`

**Why:** old premise "`_resolve_mount_key` returns None" is dead (R7 returns the
session key, then falls back to the ACTIVE project). Rename to
`test_reply_key_falls_back_to_active_project`:

```python
    def test_reply_key_falls_back_to_active_project(self):
        """SPEC-12 R7: with no per-agent routing, the reply key is the ACTIVE
        project (the unrouted-agent host — never dropped). Falsifier: drop
        the active-project fallback → tab_key becomes the raw session key."""
        handler, crh, mc = _make_handler()
        _register_coder(handler)
        mc.get_chat_box_for_session = lambda sk: None
        handler._agent_to_project = None
        handler._active_project = ["alpha", "/p"]
        handler._turn_reply_target.clear()          # no per-send slot
        fh = unittest.mock.MagicMock()
        handler.set_feed_handler(fh)
        crh.is_streaming.return_value = True
        crh.get_streaming_text.return_value = (
            "before\n\n```crabcard\ntype: diff\n"
            "title: R7 card\nfile: x.py\n---\n+body\n```\n"
        )
        handler._do_response_complete("special:coder", "unused when streaming")
        card = fh.add_cards_batch.call_args.args[0][0]
        assert card.metadata["tab_key"] == "project:alpha"
```

## T3 (NEW) — R7 unrouted render (the BUG#27 core)

```python
    def test_unrouted_agent_renders_into_open_project(self):
        """SPEC-12 R7/BUG#27: an agent with NO routing entry + NO direct tab,
        but a project OPEN, resolves to the project (never dropped). Falsifier:
        drop the active-project fallback in _resolve_mount_key AND
        _resolve_chat_box → surface mount_key is the raw session key and the
        box is None."""
        handler, crh, mc = _make_handler()
        _register_coder(handler)
        handler._agent_to_project = None
        handler._active_project = ["alpha", "/p"]
        handler._turn_reply_target.clear()
        assert handler._reply_key("special:coder") == "project:alpha"
        # _resolve_chat_box also carries the fallback (BUG#27).
        project_box = object()
        mc.get_chat_box_for_session = lambda sk: project_box if sk == "project:alpha" else None
        assert handler._resolve_chat_box("special:coder") is project_box
```

## T4 (NEW) — turn-scoped slot: group vs private (BUG#11/#19)

```python
    def test_turn_scoped_slot_group_then_private_then_group(self):
        """SPEC-12 BUG#11/#19: the reply target is per-send, not a session
        mark. A private /ask sets the agent key for THAT send; the member's
        LATER group send (no reply_target) routes back to the project."""
        handler, crh, mc = _make_handler()
        _register_coder(handler)
        handler._agent_to_project = unittest.mock.MagicMock()
        handler._agent_to_project.get_project = lambda sk: "alpha"
        handler._active_project = ["alpha", "/p"]
        # Send 1: private /ask to coder (reply_target = agent key).
        handler.send_to_special_agent = unittest.mock.MagicMock()
        # (We test _reply_key directly against the slot to avoid the send machinery.)
        handler._turn_reply_target["special:coder"] = "special:coder"
        assert handler._reply_key("special:coder") == "special:coder"  # private
        # Send 2: group send normalizes the slot away (no reply_target).
        handler._turn_reply_target["special:coder"] = handler._resolve_mount_key("special:coder")
        assert handler._reply_key("special:coder") == "project:alpha"  # group
```

## T5 (NEW) — terminal clear + turn-guard (SP3b)

```python
    def test_response_complete_clears_own_slot(self):
        """SPEC-12 SP3b: _do_response_complete clears the turn's slot."""
        handler, crh, mc = _make_handler()
        _register_coder(handler)
        handler._active_project = ["alpha", "/p"]
        handler._turn_tokens["special:coder"] = tok = object()
        handler._turn_reply_target["special:coder"] = "project:alpha"
        crh.is_streaming.return_value = False
        handler._do_response_complete("special:coder", "hi", complete_token=tok)
        assert "special:coder" not in handler._turn_reply_target

    def test_stale_token_leaves_slot_intact(self):
        """SPEC-12 SP3b: a stale completion must NOT clear the current slot."""
        handler, crh, mc = _make_handler()
        _register_coder(handler)
        handler._active_project = ["alpha", "/p"]
        handler._turn_tokens["special:coder"] = object()          # current
        handler._turn_reply_target["special:coder"] = "special:coder"
        crh.is_streaming.return_value = False
        handler._do_response_complete("special:coder", "hi", complete_token=object())  # stale
        assert handler._turn_reply_target.get("special:coder") == "special:coder"

    def test_nested_send_slot_not_clobbered(self):
        """SPEC-12 SP3b-audit BUG#1: a nested same-key send inside the turn
        owns a NEWER token + slot; the outer finally must not pop it."""
        handler, crh, mc = _make_handler()
        _register_coder(handler)
        handler._active_project = ["alpha", "/p"]
        handler._turn_tokens["special:coder"] = outer = object()
        handler._turn_reply_target["special:coder"] = "project:alpha"
        crh.is_streaming.return_value = False

        def nested(sk, text, proj):
            handler._turn_tokens[sk] = object()      # newer turn
            handler._turn_reply_target[sk] = sk      # newer private slot
        handler._on_agent_response = nested
        handler._do_response_complete("special:coder", "outer", complete_token=outer)
        assert handler._turn_reply_target.get("special:coder") == "special:coder", (
            "outer finally clobbered the nested turn's slot"
        )
```

## Verification battery (paste all outputs)
- `cd /home/mushy/projects/develcakes && xvfb-run -a .venv/bin/python -m pytest tests/test_agent_runtime.py -q` → **ALL GREEN** (was 2 failed/237 passed)
- `~/.local/bin/ruff check tests/test_agent_runtime.py` → compare to HEAD baseline (measure first)
- `wc -l tests/test_agent_runtime.py`

## Report back (mandatory COMPLETENESS)
```
COMPLETENESS:
- [x/not done] T1/T2 rewritten — evidence: diff hunks
- [x/not done] T3/T4/T5 pins added — evidence: diff hunks
- [x/not done] pytest ALL GREEN — pasted
- [x/not done] ruff output — pasted
- [x/not done] Related issues found, NOT fixed
```

Invoke `prompts/steelFramedCodeWriter.md` before writing. Please write the tests per
this brief and report when done.