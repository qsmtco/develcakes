# SPEC-12 SP1 — FIX ROUND (Debugger audit findings)

**Spec:** `docs/specs/SPEC-12-PROJECT-FIRST-GROUP-CHAT.md` §2a — **AMENDED** this
round (BUG#1: grouping key + box-class derivation; BUG#3: CSS comment).
**Base:** `docs/specs/phases/SPEC-12-SP1-INSTRUCTIONS.md`
**Scope:** the SAME two files — `ui/views/chat_surface.py` + `tests/test_chat_surface.py`.
No other file. RED-first.

Auditor (Debugger) probed SP1 and reported 3 findings. All are being fixed here.

---

## BUG#1 (MEDIUM) — "You" collision: group on `(agent, role)`, class from `role`

**Defect:** `_document` groups rows by display name only, and derives
`box_class = "role-user" if agent == "You" else "role-agent"`. An agent whose
display name is literally `"You"` (agent-builder does not reserve the name) merges
with the user's adjacent rows into one `role-user` box — mis-attributing the
agent's reply to the user.

**Fix:** the row already carries an authoritative normalized role (`append_message`
stores only `user`/`agent`/`system`, chat_surface.py:241). Update the GROUPPING KEY
and the CLASS DERIVATION to use it. Spec §2a now shows the target body — implement
it exactly:

```python
        # collect the run of same-agent rows — key on (agent, role) so an
        # agent displaying the literal name "You" (role "agent") can never
        # merge with the user's own rows (role "user") (SP1-audit BUG#1).
        role = row.get("role") or "system"
        j = i
        while (j < n and (rows[j].get("agent") or "") == agent
               and (rows[j].get("role") or "system") == role):
            j += 1
        ...
        # SP1-audit BUG#1: derive the box class from the row's ROLE, not the
        # display-name string — the name is not a user/agent discriminator.
        box_class = "role-user" if role == "user" else "role-agent"
```

Everything else in `_document` is unchanged.

## BUG#2 (LOW) — `role-agent` is unpinned (mutant escapes)

**Defect:** no test asserts `role-agent`; mutating `box_class` to always
`role-user` passes all 6 tests. Asymmetric coverage.

**Fix:** in `TestGroupedAgentBoxes`, assert BOTH classes:
- agent run → `'class="agent-box role-agent"' in doc`
- user run → `'class="agent-box role-user"' in doc` AND
  `'class="agent-box role-agent"' not in doc`

## BUG#3 (suggestion) — misleading CSS comment

**Defect:** the added comment claims `.role-user .agent-name` "stops matching"
under the new structure (sibling reasoning). Wrong: the box element itself carries
`role-user`, so the header span is still a DESCENDANT and the old rule still
matches. The new rule is redundant (harmless).

**Fix:** correct the comment in `_BASE_CSS` to the corrected text in spec §2a
(keep the rule; it is explicit/intent). No behavior change.

## Tests (append/extend in `tests/test_chat_surface.py`, RED-first)

1. `test_document_you_named_agent_does_not_merge_with_user` — rows:
   `{"role":"user","html":"<p>q</p>","agent":"You"}` immediately followed by
   `{"role":"agent","html":"<p>r</p>","agent":"You"}` → `doc.count('class="agent-box') == 2`,
   the user box is `role-user`, the agent box is `role-agent` (assert each class
   present), both bodies present. (RED against current code: count == 1.)
2. `test_document_agent_run_pins_role_agent` — a Coder agent run →
   `'class="agent-box role-agent"' in doc`.
3. `test_document_user_run_pins_role_user_only` — a `You` run →
   `role-user` present AND `role-agent` ABSENT.

## What must NOT change
- Bare (empty-agent) branch unchanged.
- `_cap_row_html`, windowing, streaming, destroy semantics unchanged.
- All pre-existing tests stay green unmodified.

## Verification battery (paste all outputs)
- `cd /home/mushy/projects/develcakes && xvfb-run -a .venv/bin/python -m pytest tests/test_chat_surface.py -q`
- `~/.local/bin/ruff check ui/views/chat_surface.py tests/test_chat_surface.py`
- `.venv/bin/pyright ui/views/chat_surface.py`
- Mutation proof (BUG#2): flip `box_class` to always `"role-user"` → the NEW
  role-agent assertion must FAIL; paste that failure; revert.

## Report back (mandatory COMPLETENESS)
```
COMPLETENESS:
- [x/not done] BUG#1: grouping key (agent,role) + class from role — evidence: diff hunk
- [x/not done] BUG#2: role-agent/role-user assertions — evidence: pytest output
- [x/not done] BUG#3: CSS comment corrected — evidence: diff hunk
- [x/not done] RED proofs: new tests fail pre-fix — pasted
- [x/not done] Mutation proof (always-role-user) fails the new assert — pasted
- [x/not done] ruff / pyright / wc outputs
```

Invoke `prompts/steelFramedCodeWriter.md` before writing. Please write the fixes
per this brief and report when done.