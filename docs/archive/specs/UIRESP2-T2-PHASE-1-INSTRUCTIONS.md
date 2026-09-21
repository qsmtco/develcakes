# UIRESP2-T2 Phase 1 — RENDERED body cap (Unit D) — Instructions (Coder)

**Origin:** external live profile (Qrusher 2026-09-12): pango text measurement
= 60.1% of main-thread burn; 4 approval cards at 16–39 KB bodies are the
worst offenders. Independent of P4/5; folds into the sprint as Unit D.

## Step 1 — VERIFIED (Supervisor, 2026-09-12, tree at d51b71a) — do not re-derive

1. **The 2,000 cap exists at producer side:** `ui/handlers/agent_runtime_handler.py:1583`
   in `_do_tool_call_result`: `display = output_text[:2000]` — applied ONLY to
   tool-result cards, and it truncates BEFORE storage (`card.body = display`),
   so tool-result storage is already lossy today.
2. **The bypass:** `_do_approval_needed` (:1745-1786) sets
   `body=f"$ {command}"` (:1773) with NO cap → 22,714-char heredoc card.
   Any other `body=` sites with unbounded strings have the same exposure —
   sweep all `body=` construction sites in ui/handlers/*.py as part of this
   phase and report them.
3. Existing tests: none pin the tool-result cap (the `2000` values in
   tests/test_feed_handler.py are scroll-adjustment values). The `crabcard`
   export path and `_make_copy_cb` (:2407) read `card_data.body` — both are
   storage-side and unaffected by a render cap.

## PHASE DELIVERABLE

**Cap the RENDERED body at 2,000 chars across ALL card types. Storage
unchanged.** feed.json + metadata keep full text.

### Edit A — render-side cap + reveal control (`ui/views/feed_card.py`)

- New module const `RENDERED_BODY_LIMIT = 2000`.
- In `_render_text_body` (and the file-event body if it can be long — check),
  when `len(text) > RENDERED_BODY_LIMIT`: render only the first 2,000 chars,
  then append a **reveal control**: a `Gtk.Button` labeled
  `… N more characters` (N = hidden count, exact). Clicking it replaces the
  truncated label with the full text (in place, same box, no scrollbar jump).
  "Reveal all" state must be sticky for the widget's lifetime.
  - Do NOT load-bear on `_text_label` alone — the reveal button is a sibling
    widget. `_text_label` must remain the text label (Part A of P4/5 depends
    on it) — the reveal control is additive.
- **Approval cards pending decision must NOT be truncation-blocked from the
  approver:** the cap still applies visually, but the reveal control is the
  reachability path — full text is one click away BEFORE deciding. Add a
  tooltip on the button for pending-approval cards: "Full command text — view
  before approving".

### Edit B — in-place update path composition (P4/5 Part A)

- `update_card`'s in-place path currently does `_apply_text_markup(_body_label,
  new_text)`. It must re-evaluate truncation: if new text exceeds the limit,
  the widget must show truncated + reveal control; if a previously revealed
  card gets a NEW oversized body, reset to truncated (sticky-reveal applies
  to the SAME text, not subsequent updates).
  If the in-place path can't cleanly host the reveal control, fall back to
  rebuild for that update (fallback exists and is correct).
- Keep F1 enqueue parity on both paths (committed tests ba75380 pin this).

### Edit C — storage-side cap removal (the lossy today-state)

- `_do_tool_call_result` :1583: REMOVE the `[:2000]` storage truncation —
  store the full output in `card.body` (bounded by a NEW large storage cap:
  `MAX_STORED_BODY = 200_000` chars to keep feed.json bounded; log a warning
  when the storage cap fires).
- This makes stored-vs-rendered separation real: store up to 200k, render 2k.
- `MAX_STORED_BODY` also applies in Edit A's sweep: any `body=` site storing
  more than 200k gets capped at storage (with the same warning), BEFORE
  render truncation ever sees it.

### Edit D — preserves (verify, don't break)

- `_make_copy_cb` (:2407) — copies STORED body (full text), unchanged code.
- Approve/reject affordance — the reveal control must not sit on top of the
  action row; place it inside the body box.
- Feed search/filters — grep for any body-search path; if none exists today,
  note it (nothing to preserve).

## TESTS (RED-FIRST, under xvfb for GTK suites)

1. 22,714-char body → rendered label text ≤ 2,000 chars + a visible "22,714 more characters"... wait, indicator shows HIDDEN count: `… {hidden} more characters` where hidden = len - 2000. Assert hidden count exact + full text retrievable via the reveal action (simulate click).
2. Stored body unchanged: build via feed_handler path with a 22,714-char body → `card_data.body` still 22,714 after widget build + after in-place update.
3. Approval card: oversized body + `needs_approval: True` → tooltip present on reveal button; full text reachable pre-decision (reveal works).
4. In-place update re-evaluates truncation: reveal a card, then update_card with new 5k body → back to truncated + fresh hidden count. And: update_card with a short body after reveal → shows short body (no stale expanded state).
5. `[:2000]` removal: `_do_tool_call_result` with 5,000-char output → `card.body` == full 5,000 chars (RED on current code — this is the red-first anchor).
6. Storage cap: 250k-char output → stored body == 200,000 + warning logged.
7. Copy: `_make_copy_cb` still returns the FULL stored body.

## GATES

- Red-first evidence for tests 1 and 5 (paste).
- Suites: test_feed_card, test_feed_handler, test_agent_runtime_handler
  (per-class if OOM), test_review_handler_feed_card — green under
  `PYTHONDONTWRITEBYTECODE=1 xvfb-run -a python3 -m pytest <suite> -q`.
- pyflakes /tmp/pf-venv3: 0 undefined on touched files.
- Hermetic: tmp_path everywhere; no real ~/.config/crabcakes.
- One commit: `perf(feed): render-side 2k body cap with reveal control + storage-side 200k cap (UIRESP2 T2)`.
- Report COMPLETENESS + evidence + the `body=` site sweep table (site, max observed, capped?).
