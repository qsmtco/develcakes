# SPEC: Lean the UI down to what is still on screen

**Date:** 2026-10-23
**Author:** Micah
**Revision:** 2026-10-24 fix round (Supervisor) — revert inlined into ProjectHandler, review-gated checkpoint path deleted, `ui/feed/load.py` covered, grep gate widened; 2026-10-24 round-2 section-1 ledger applied (dead chat-handler cluster named, staging tail 425-447 deleted, `_evict_surplus_rows` rename mandatory, 9 test casualties + collection-sweep rule, stale-comment sweep step 7, knowledge/README surfaces)
**Status:** Draft — for implementation
**Implements:** the keep/remove pass on 2026-10-23 (chat after SPEC-57)
**Depends on:** SPEC-57 (web surface paint — already on main). Do not revert it.
**Target branch:** main

> Architecture: the feed stays one WebKit document (`FeedSurface`). The transcript stays one WebKit document (`ChatSurface`), including live sections. Canvas tabs, timers, both splashes, both auto-accept controls, the Prompts tab, the Agents tab, the agent dashboard, the file-tree drawer (diff, history, revert), and the input strip stay. This spec deletes the UI that is not on that list. It does not restyle what stays.

---

## DISCOVERY

- Read `ui/views/main_content.py` `create_chat_tab`: the notebook page is `Gtk.Overlay` → `chat_scroll` (`PolicyType.AUTOMATIC`, `AUTOMATIC`, class `web-surface-host`) → `chat_box`. The overlay also receives `_project_settings` and `_scroll_btn_box` on the first tab. `_top_box.prepend(self._project_settings)` already parents the project bar above the notebook (`__init__`, after `update_project_settings`). Switch-page unparents those two widgets and re-adds them as overlays. `_tab_scrolls` stores `chat_scroll`. `scroll_chat_to_bottom` calls `surface.scroll_to_latest()` and returns when a surface is mounted; the outer-adjustment fallback is only for a page with no surface. The ↓ button reads that outer adjustment. `_agent_context_box` (avatar `Gtk.Picture` + name label) is appended to `button_bar`. Tab labels append a dot (`tab-dot` + `tab-dot-idle`), a bullet, and `_extract_session_display_name(session_key)`. `increment_unread` / `clear_unread` swap `tab-dot-unread`. Nothing outside the dead gateway path calls `increment_unread`.
- Read `ui/views/chat_surface.py`: `_make_owned_scroll()` builds a `Gtk.ScrolledWindow` with policy `(NEVER, AUTOMATIC)` and class `web-surface-host`. `ChatSurface` appends that scroll and later `set_child`s the WebView. `_pending_restore` is the GTK adjustment restore. Document scroll is a separate slot, `_pending_scroll`, applied from `load-changed` / `scroll_to_latest`. `create_chat_surface` returns `TextViewFallback` when `WebKit is None`. The module aliases `ChatSurface = TextViewFallback` in that same case.
- Read `ui/handlers/chat_handler.py`: `on_chat_event` is the gateway entry. `ui/constants.py` says that path is dead after SPEC-05. Production has no caller. `_handle_streaming_delta` returns on its first line. `_handle_final_response` is reached from `on_chat_event` and from `_handle_lifecycle_completed` (chat_handler.py:139) — itself caller-less (dead); the whole cluster (`on_chat_event`, `_handle_special_event`, `_handle_streaming_delta`, `_handle_final_response`, `_handle_lifecycle_completed`, `_clear_chat_final_rendered`, `_chat_final_rendered`) is orphaned (see §2c). `render_async` / `render_sync` ignore `on_forward_click` and return `None`, so the `if bubble is not None: chat_box.append(bubble)` branches do not append.
- Read `ui/handlers/chat_render_handler.py` `render_event_card`: builds GTK widgets from `ui/views/event_cards.py` and `ui/views/diff_card.py` (`build_diff_summary_card`, `build_file_diff_card`) and the caller appends them to `chat_box`. Callers: `window._on_command_card`, `chat_handler._handle_special_event`. `response_card` is never assigned in production (only in `tests/test_command_models.py`).
- Read `ui/window.py`: `ReviewHandler(` is constructed once, around the command wiring, with `on_confirm_batch_accept=self._confirm_batch_accept`. `_on_project_selected` is the only `DiffViewer` constructor and is passed to the left panel as `on_project_selected`. `FileTree` stores `on_file_selected` and never calls it (`ui/views/file_tree.py` has the assignment and no call). File-tree double-click calls `_toggle_drawer`. Drawer revert calls `ProjectHandler.revert_file_to_sha`, not `ReviewHandler`. A status bar `Gtk.Label` `"Agent: —"` is updated by `update_agent_id_display`. `ActivityWiringHandler` is not constructed. `ForwardHandler` is constructed with `gateway_handler=None`. `show_forward_popover` has no production caller. `/ask` and `/stop` are `CollabHandler`, not `ForwardHandler`.
- Read `ui/views/feed_tab.py`: `schedule_scroll_to_bottom` arms a GTK adjustment settle. `schedule_smart_scroll_to_bottom` returns immediately when `_html_surface is not None`. The snooze control is a `Gtk.MenuButton` with no popover and no clicked handler. The agent dropdown, Diffs, Files, Exec, and Accept All are real controls.
- Read `ui/feed/auto_accept.py`: `snooze_card` / `unsnooze_card` have no callers outside that file. `_is_card_auto_acceptable` still skips ids in `_prefs.snoozed_card_ids` (the `in self._prefs.snoozed_card_ids` check). `set_auto_accept_level` and the granular toggles both write `file_changes`. Do not remove either control.
- Read `ui/views/left_panel.py` `_build_agent_row`: a subtitle label is `DevelCakes` or `Openclaw` plus a session count. Right-click on a non-local key calls `show_session_menu`. Right-click on `agent:` calls `_show_local_agent_menu` (Edit, Delete). `_agent_mgr` is left `None` (gateway strip, `window.py`). `get_sorted_agents` returns `[]` when `_agent_mgr is None`, so the visible list is the `_agent_names` fallback.
- Read `ui/views/diff_card.py`: `render_diff_hunks` and `get_lang_from_path` are imported by `ui/file_tree/drawer.py` and `ui/views/file_tree.py`. `build_file_diff_card` / `build_diff_summary_card` are only used by `render_event_card` and `tests/test_diff_card.py`.
- Read `models/activity.py` `to_drawer_row`: the only callers are `ActivityWiringHandler` and `tests/test_activity_drawer.py`. `ActivityHandler` and `ActivityPillLabel` are the live activity UI. Do not delete those.
- Architecture owner: `MainContent` owns the chat notebook, the project bar, and the button bar. `ChatSurface` owns the transcript WebView. `FeedTab` / `FeedSurface` own the feed. `LeftPanel` owns Prompts, Agents, and the file tree. `ReviewHandler` owns a feature this spec deletes.

---

## 1. Overview

**Problem.** The window still builds UI that nothing shows, and UI that shows a second copy of a job the WebKit page already does. Pango cards are appended beside the transcript. A second chat implementation exists for a missing WebKit. The feed and the transcript each carry a GTK scroll path that does not move the document. Review mode, the activity drawer, the forward popover, and the full-pane diff viewer are not reachable from the running window.

**Solution.** Delete those pieces. Where a control stays, leave its behavior alone. The decisions below were made one at a time. Do not reopen them.

**Remove**

| Piece | What goes |
|---|---|
| Pango cards in the chat column | `render_event_card`, `ui/views/event_cards.py`, the chat-column diff cards |
| No-WebKit chat | `TextViewFallback`. A missing WebKit does not get a second transcript |
| Feed GTK scroll-to-bottom | `schedule_scroll_to_bottom` and the settle loop |
| Chat GTK scroll restore | `_pending_restore` and the adjustment handlers that only serve it |
| ↓ button and the outer chat scroller | the page-level `Gtk.ScrolledWindow`, the overlay, `_scroll_btn` |
| Chat surface scrollbar | the surface's own `Gtk.ScrolledWindow`. The page scrolls |
| Review mode | `ReviewHandler`, `/review` `/check` `/accept` `/reject`, `ReviewBar`, `DiffViewer`, `_confirm_batch_accept` |
| Click an image to open it | `_open_in_viewer` dies with `event_cards.py` |
| Snooze | the toolbar button, `snooze_card` / `unsnooze_card`, and the auto-accept skip |
| Bottom status bar | the `Agent:` label under the paned |
| Tab-dot color changes | `increment_unread`, `clear_unread`, `tab-dot-unread`. The dot stays green |
| Agent-row subtitle and gateway session switcher | the `DevelCakes` / `Openclaw` line, `show_session_menu` |
| Button-bar avatar and name | `_agent_context_box` |
| Activity drawer | `activity_drawer.py`, `activity_wiring_handler.py`, `to_drawer_row` |
| Forward popover | `ForwardHandler` |
| Test-only project-bar writers | `set_project_settings_text`, `set_feed_bar_text` |
| GTK feed-card widgets | `build_context_panel`, the `_card_widgets` eviction tail (the HTML dispatch stays, renamed), `_build_load_more_widget` |
| Review-gated agent checkpoints | `_maybe_agent_checkpoint`, `_project_lock_for`, `_null_lock`, `_checkpoint_locks`, the `review_handler` ctor param and `set_review_handler` on ARH. `note_stop_all_aborted` and the abort counter STAY (§2d-note-1) |
| Review plumbing on ProjectHandler | `set_review_handler`, `get_review_state`, the `Review:` line in `cmd_status` |
| `models/review_state.py` | the file and its `models/__init__.py` export |

**Keep** (do not touch the behavior)

Input strip (find/replace, spellcheck, save/open file and prompt). Agents tab and agent dashboard. File-tree History and Revert — the button stays, and its behavior MOVES: `revert_file_to_sha` is re-implemented inside `ProjectHandler` (§2d) with no ReviewHandler and no active-session gate. Both auto-accept controls (project-bar `Files:` cycle and the feed Diffs / Files / Exec toggles) and the feed agent dropdown. Snooze dies with its button — it was a third auto-accept control (`auto_accept` level `snooze`) whose toolbar label read the snoozed count; the two controls named here stay and are unaffected. Project-bar `Chat:` solo pin, member count, and git branch. Both splashes (`assets/splash.html`, `assets/splash-min.html`). Live HTML sections in the transcript. Canvas tabs. The feed Review button and its in-card context panel. Timers. Slash commands scanned out of agent replies (`AgentCommandHandler`). Both connect controls (icon and pip) and Stop All. The green tab dot, with no unread state. Prompts tab, including the second line, the star, the `+`, and the right-click menu. Copy Path and Copy File. File-tree columns Name, Status, Size, Modified. Middle-click to close a tab. The bullet and suffix on a chat tab. The colored initial on an agent row. Feed card footer, emoji, and copy icon. Edit and Delete on a local agent. Exec approval cards. The project-trust dialog. `CollabHandler` `/ask` and `/stop`. Feed Accept / Accept All — self-contained in `ui/feed/review.py` and `ui/feed/auto_accept.py` (own git stage + commit); it never called ReviewHandler and does not change. The feed Load More bar and the HTML row-windowing eviction — the document path (§2f-2). The feed batch bar (`update_batch_bar`). Stop All's runtime aggregation, the ARH stop-all registry, exec approvals, and the rest of `AgentRuntimeHandler` — only the review-gated checkpoint path goes (§2d-note-1). `_pending_approvals` (ARH:149) is KEPT — it backs the kept exec approval cards and the live-bridge approver (window.py:248-261); no Remove row touches it. `_remote_status_summary` and `/status` minus their Review lines.

**Scope**

| In | Out |
|---|---|
| Deleting the rows in the Remove table, and the tests that only exist to cover them | Restyling anything in the Keep list |
| Pointing chat scroll at the document only | SPEC-57 canvas color, feed patches, `.feed-scroll` on the feed scroller (the feed never used `.web-surface-host`) |
| A missing WebKit shows the same kind of hard failure the feed already shows | A new fallback transcript |
| Re-implementing `revert_file_to_sha` inside `ProjectHandler` as a standalone git revert (no ReviewHandler, no active-session gate) | Rebuilding review on top of the drawer |
| Deleting the review-gated agent-checkpoint path in ARH and the `Review:` lines in the two status summaries | Reintroducing any deleted widget to "fix" a test |
| File-tree diff hunks stay (`render_diff_hunks`) | Touching `utils/review_log.py` / `feedback_processor.py` / `audit_parser.py` (non-UI orphans, out of scope) |

---

## 2. Changes by File

### 2a. Chat page — `ui/views/main_content.py`

`create_chat_tab` today builds Overlay → ScrolledWindow → Box. After this change the notebook page is the `chat_box` itself (`Gtk.Box`, vertical, `hexpand`, `vexpand`). No `Gtk.Overlay`. No page-level `Gtk.ScrolledWindow`. No `_scroll_btn`, `_scroll_btn_box`, `_on_scroll_to_bottom_clicked`, `_on_vadjustment_changed`. The already-dead fields `self._scroll_to_bottom_btn` / `self._scroll_to_bottom_overlay` (:110-111, never read) go too.

The overlay is built in two places and both go: the switch-page unparent, and the **first-tab block in `create_chat_tab` (:681-687)** that unpairs `_project_settings` and `_scroll_btn_box` into the overlay on the very first tab (the Welcome tab). Delete that block with the overlay; the bar stays parented to `_top_box`.

Delete `_tab_scrolls` and `_tab_overlays` and every read of them — including **`_reindex_tabs` (:1054-1093), which must be rewritten, not just stripped**: it keys the page-widget → old-index map on `saved_overlays` (:1067, :1072-74). Rewrite it to build the identity map from `saved_chat_boxes` and drop the `new_scrolls` / `new_overlays` branches; the write-back keeps only `_tab_sessions` and `_tab_chat_boxes`. Left as-is, the empty overlay map makes `get_chat_box_for_session` return `None` for every session and the transcript never mounts.

`scroll_chat_to_bottom` becomes: resolve the page's `chat_box`, ask the render handler for `surface_for_box`, and if the surface is not `None`, call `surface.scroll_to_latest()` and return; otherwise return. Delete the outer-adjustment fallback (`GLib.timeout_add(16, ...)`). Switch-page must not unparent `_project_settings`. The bar's parent is `_top_box` (the existing `prepend`). The last-tab-close path in `_close_tab` deletes BOTH lines — `self._detach(self._project_settings)` and `self._top_box.prepend(self._project_settings)` (:1048-1049) — the bar never leaves `_top_box`.

One side effect to land with this step: `_CANVAS_TOP_SPACER_HTML` (window.py:59, injected at :1307-1309) exists only because the project bar overlays the top of every notebook page. With the bar a permanent `_top_box` row, the 40px spacer is dead space on every canvas document — delete the spacer and its tests (`tests/test_canvas_top_spacer.py`).

Delete `_agent_context_box`, `_context_avatar`, `_context_name_label`, and `update_agent_context_display`. Delete the `button_bar.append(self._agent_context_box)` line. `window._update_agent_display` (the closure wired via `set_on_session_changed`, window.py:811-818 + :828) has no remaining work once §2e deletes `update_agent_id_display` — delete the closure, the wiring, and its only helper `_resolve_agent_info` (:792-809); both of its calls die with the two features.

`MainContent.set_agent_manager` / `_agent_mgr` have no remaining reader once the branch below goes; delete them. In this same file, drop the `show_session_menu` import (line 19) and the agent branch of `_on_tab_right_click` (lines 1190-1206) — it is gateway-only, guarded by `if self._agent_mgr is None: return`, and `_agent_mgr` is permanently `None`. Without this, §2g's "delete `show_session_menu` if no caller remains" leaves an `ImportError` at boot.

Keep the tab dot. Give it `tab-dot` and `tab-dot-idle` and never swap classes. Delete `increment_unread`, `clear_unread`, `_update_tab_dot`, `_unread_tabs`, and the switch-page call to `clear_unread`. Delete the `.tab-dot-unread` rule from `ui/styles.py`. Keep `.tab-dot` and `.tab-dot-idle`. The `_dot` attribute handle (:745) dies with its only two readers (`_update_tab_dot` deleted here; `_update_tab_session_label` deleted below) — the dot WIDGET stays in the label build. The gateway session-switch chain goes with it: `_on_tab_right_click`'s agent branch (:1190-1206) is the only caller of `show_session_menu`, whose callback is the only caller of `_switch_tab_session` (:1215), whose :1227 call is the only caller of `_update_tab_session_label` (:973-993) — delete all four links, after which §2g's "delete `show_session_menu` if no remaining caller" is true. Keep `tab_label_box._session_key` (:758) — `_find_page_by_session` (:995-1012) and `close_project_tab` depend on it. The sibling write `chat_scroll._session_key` (:759) dies with the scroller.

Keep the bullet and `_extract_session_display_name`. Keep middle-click close. Keep the `×`.

Delete `set_project_settings_text` and `set_feed_bar_text`, and the tests that only call them. `update_project_settings` stays. It is the live bar builder (name, member count, `Chat:`, `Files:`, git branch, connect, Stop All, activity pill).

`get_review_bar`, `set_review_bar` (main_content.py:1468), `show_diff_viewer`, `hide_diff_viewer`, and `get_diff_viewer` (:1552) exist to host review and `DiffViewer`. Delete them with those features. Grep first. A caller that is itself being deleted does not need a stub. **`.web-surface-host` loses BOTH appliers** — the page-level copy (main_content.py:676) here, the surface-scroll copy (chat_surface.py:933, inside `_make_owned_scroll`) in §2b — and the feed never used the class (it uses `.feed-scroll`, feed_tab.py:95). Delete the styles.py:1311 rule and drop the `.web-surface-host` phrase from §1's Out-scope row and §2h's keep-list; the chat background now depends solely on `_apply_canvas_color` (kept per §2b).

### 2b. Transcript — `ui/views/chat_surface.py`

Delete `TextViewFallback` and both `ChatSurface = TextViewFallback` assignments. `create_chat_surface` returns `ChatSurface(...)`. When `WebKit is None`, raise `RuntimeError("chat requires WebKit")` instead of building a text view. Do not invent a third UI. **Honest framing of the trade-off (PM sign-off required):** the feed does NOT hard-fail without WebKit — it degrades to the `_show_no_webkit` label and keeps running. Chat raising means, because the first chat tab mounts during window construction, a hard boot failure on WebKit-less systems. That is a deliberate product decision this spec makes; if the PM prefers degraded-over-dead, the correct shape is a `_show_no_webkit`-style label in the chat page, never a text-view transcript. Note the escape hatch: `DEVELCAKES_NO_WEBKIT=1` is the documented Ubuntu 24.04/apparmor workaround (chat_surface.py:60-66; README.md:550) — §2b turns it into a guaranteed boot failure on those systems, so the README env-table row must be updated to say so (§8).

Delete `_make_owned_scroll` and `self._scroll`. In `_ensure_webview`, after `WebKit.WebView()` and `_apply_canvas_color` (SPEC-57, keep that call), `set_vexpand(True)`, `set_hexpand(True)`, and `self.append(self._webview)`. Do not put a `Gtk.ScrolledWindow` around it. The document scrolls (`scroll_to_latest`, `_pending_scroll`, `_BOTTOM_SCRIPT`). That path stays.

Delete the GTK restore: `_pending_restore`, `_was_at_bottom` if nothing in the document-scroll path reads it, `_bottom_adj`, the `value-changed` and `changed` handlers on the surface adjustment, `_settle_restore`, `_RESTORE_STABLE_FRAMES`, `_on_scroll_value_changed`, `_on_adjustment_changed`. Before deleting `_was_at_bottom`, grep the file. The document path stores intent in `_pending_scroll`. If a read of `_was_at_bottom` remains on that path, leave the field and delete only the adjustment writes.

Delete `get_vadjustment` if the only caller was `scroll_chat_to_bottom` or the GTK restore. Grep. Do not leave a method that returns an adjustment for a scroll widget that no longer exists.

Do not change live sections, `append_live`, `LiveBridge`, canvas `load_unsanitized_full_document`, or `_BASE_CSS` beyond what SPEC-57 already did.

### 2c. Pango cards — `event_cards.py`, `chat_render_handler.py`, `chat_handler.py`, `window.py`

Delete `ui/views/event_cards.py`. That file holds `build_role_bubble`, the file/edit/tool/error cards, and `_open_in_viewer`. Images in the transcript stay inside the page. Do not delete `utils/image_paths.py`. The HTML render path uses it. Grep before touching it.

Delete `ChatRenderHandler.render_event_card` and `render_task_card` if the task card is only reached from `render_event_card`. Grep `render_task_card`. Delete the `diff_summary` / `diff_file` branches with it.

Delete `window._on_command_card`. `CommandHandler` may still call `on_display_card`. Leave the callback slot, and pass a function that ignores the dict. `response_card` is never set in production, so this is a no-op that keeps the constructor signature from growing a new required argument. Do not build a widget from the dict.

Delete `ChatHandler.on_chat_event`, `_handle_special_event`, `_handle_streaming_delta`, `_handle_final_response`, `_handle_lifecycle_completed`, `_clear_chat_final_rendered`, and `_chat_final_rendered` — the whole dead cluster. They are the gateway path; `_handle_lifecycle_completed` is itself caller-less, and `_handle_final_response` is reached from both `on_chat_event` and `_handle_lifecycle_completed`. Local replies already render through `AgentRuntimeHandler` and `ChatRenderHandler.render_sync` / `render_async`. Do not route local replies through the deleted methods.

`on_forward_click` is ignored. Remove the parameter from `render_sync`, `render`, and `render_async`, and remove the arguments at every call. `ForwardHandler` is deleted in 2e, so there is no popover left to open. The whole forward plumbing dies with it: `ChatHandler._on_forward_message` / `set_on_forward_message` (chat_handler.py:52, :73-78, and the `on_forward_click=self._on_forward_message` arguments at :255/:292/:329/:375/:401/:426/:678), and `ChatRenderHandler._on_forward_message` / `set_on_forward_message` (chat_render_handler.py:193, :748-753) — no production caller wires either setter.

In `ui/views/diff_card.py`, keep `get_lang_from_path` and `render_diff_hunks`. Delete `build_file_diff_card` and `build_diff_summary_card`. The file-tree drawer imports the two that stay.

### 2d. Review — `ui/handlers/review_handler.py`, `ui/views/review_bar.py`, `ui/views/diff_viewer.py`

Delete those three modules. Delete `models/review_state.py` and its import/export in `models/__init__.py` (the only remaining importers after this spec are the tests listed in §2i, which go in the same step; `grep -RIn review_state ui models` must come back empty).

**Revert moves, review dies (keep the button, change what it calls).** The drawer's Revert button currently calls `ProjectHandler.revert_file_to_sha`, which delegates to `ReviewHandler.revert_file_to_sha` — and that method **requires an active review session** (M17 gate: "Revert requires an active review session. Run /review first."). After `/review` is deleted, nothing can start a session, so the old delegation would make the button a permanent silent no-op. Replace the delegation with a standalone implementation inside `ProjectHandler`:

1. Keep the existing signature: `revert_file_to_sha(self, project_name, file_path, target_sha, on_complete=None)`.
2. Keep the existing input guards: `file_path` must be a `str` and must not start with `"-"` (BUG #12 argument-injection guard); resolve the project path via `self._get_project_path(project_name)` and return early if `None`.
3. Body: on a background daemon thread, call `git_ops.checkout_paths(project_path, target_sha, [file_path])` (import `checkout_paths` from `utils.git_ops`; `is_repo`/`init_repo`/`stage_all`/`commit` already imported in that file).
4. On success, dispatch via `self._GLib.idle_add` when `self._GLib` is set (tests may pass None — then call directly): the drawer needs no success toast (it re-renders its diff as it does today); fire `on_complete()` if it is not None.
5. On failure, log a WARNING with the module-level `_logger` AND surface it — the drawer's try/except only catches synchronous errors and must NOT be relied on: the old ReviewHandler surfaced git failures as a `⚠ Failed to revert …` toast fired from inside its worker thread, never as an exception to the drawer. Keep that toast: add `ProjectHandler.set_on_revert_status(cb)` (house setter pattern; `self._on_revert_status = None` initialized in `__init__`), wire it in `window._build` with `self._project_handler.set_on_revert_status(self._on_command_text)` (the same callback ReviewHandler got as `on_display_text`, window.py:716), and on failure dispatch `cb(f"project:{project_name}", "⚠ Failed to revert {file_path}: {error}")` via `self._GLib.idle_add` (direct call when `self._GLib` is None). Without this, checkout failures are silent.
6. No review session, no `ReviewState`, no project lock. The pre-existing per-project lock family lived on ReviewHandler and is deleted with it; a single checkout of one file path is not a concurrent-accept hazard.

The drawer call site (`ui/file_tree/drawer.py:679`) is unchanged — it already calls `self._project_handler.revert_file_to_sha(...)`. Do not edit `drawer.py`'s revert wiring. The `checkpoint_sha` blocks at drawer.py:347-350 and 734-737 read `get_review_state` — with the handler gone they always take the `except`/None path and fall back to HEAD. Delete the try/except blocks entirely and assign `checkpoint_sha = None` directly, so no `get_review_state` call survives (the diff shows against HEAD, exactly as it does today outside a review session). `_diff_view` display, `_toggle_drawer`, `_on_history_key_pressed`, revert error label — unchanged.

**§2d-note-1 — the review-gated paths in `AgentRuntimeHandler` and `turn_ui.py` go with review.** The gate at `agent_runtime_handler.py:1669-1672` (`rh.get_state(...)` / `review_mode == "review"`) requires a mode only `/review` ever set; removing `/review` already makes `_maybe_agent_checkpoint` dead by construction. Delete it rather than leave an unwired gate: the method `_maybe_agent_checkpoint` (lines 1646-1744), the call to it in `ui/agent_runtime/turn_ui.py:491` (the line `self._maybe_agent_checkpoint(session_key, complete_token)` — surrounding dispatch code unchanged), `_project_lock_for` (1746-1754), `_null_lock` (module level, line 43 area), `_checkpoint_locks` (ctor line 228), the `review_handler` ctor param and `self._review_handler` (78-88), and `set_review_handler` (427-429).

`FeedHandler._extract_messages_from_chat_box` (feed_handler.py:923-945) reads `_crabcakes_role` / `_crabcakes_text`, set only by `build_role_bubble` (event_cards.py:203-204). The producer dies, but the walker and `_maybe_create_snapshot`'s agent branch (snapshots.py:187-204) STAY untouched — the walker already finds nothing in production (GTK bubbles were never appended since `render_sync` returns None) and the snapshot data path is Keep-listed. Do not delete them; do not leave a note in the code either — this is spec-only clarification.

`turn_ui.py` has two more review reads, both feeding the `needs_review` badge on tool cards (write/exec) — a review-mode-only flag: the `self._review_handler is not None` guard + `get_state` block at turn_ui.py:339-345, and the `proj_path is None or self._review_handler is None` guard + `get_state` block at turn_ui.py:426-429. Both blocks die with review: delete them. The second delete takes the WHOLE block 425-447 — the staging-copy tail after the guard (`os.makedirs` `.crabcakes_review_staging/`, `shutil.copy2`, and the `review_staging_dirname` config read) is review staging code that would otherwise run UNCONDITIONALLY after every saved writer turn once the guard is gone. Note `turn_ui.py` never assigns `_review_handler` — it is a mixin (`RuntimeTurnUiMixin`) reading ARH's attribute through composition, so deleting ARH's attribute (above) plus these two read blocks removes every reference. `render/feed_html.py` may render a `needs_review` badge when the flag is set; nothing will set it anymore, which is fine — do not touch the renderer.

Two adjacent cleanups land with the same edit: **`_turn_attr`** (ARH:223, written :1167, popped :1171 and at `ui/agent_runtime/session.py:52`) has its sole functional read inside `_maybe_agent_checkpoint` — after this deletion it is write-only bookkeeping. KEEP the field, its writes, and the `/clear` pop as dormant state (tests/test_live_bridge.py:295 and the SPEC-10 comment blocks reference it) and rewrite the SPEC-10 comments at ARH:1159-1165 and session.py:50-51. And **`_pending_exec_commands`** (turn_ui.py:261 writes unconditionally; the pop at :353-354 is gated on `_on_command_output is not None`, which is never wired once `activity_wiring_handler.py` dies) — pop it unconditionally at :353 so the dict does not grow per session forever. ARH's drawer-side seams (`set_on_command_output` / `set_on_activity_bubble` / `set_on_drawer_lifecycle`, ARH:297-400) STAY as test-pinned no-op seams; rewrite the ARH:307 comment that names the deleted `connection_sync_handler.sync()`.

Keep `stop_all_agents`, `stop_all_in_progress`, `note_stop_all_aborted`, and the abort counter — they keep working because ReviewHandler was the only external reader of the flag (review_handler.py:329,356) and it is deleted. `_emit_stop_all_card` keeps the `needs_review`-free summary card text as it is; the `aborted_checkpoints` line stays (always 0, dormant) — do not rewrite it.

**§2d-note-2 — ProjectHandler review plumbing goes too.** Delete `set_review_handler` (939-941), `get_review_state` (943-951), and the `Review:` lines from `cmd_status` (637-638, 644: drop `review_state`/`review_status` and the `f"Review: {review_status}"` entry). `tests/test_project_handler.py:278` pins `lines[2] == "Review: not started"` — update that pin to the new three-line summary (`Project / Members / Solo DM`) in the same step. The old `set_on_project_opened` wiring lambdas at window.py:878-882 (`self._review_handler.on_project_opened/on_project_closed`) go with review.

**Delete the review UI wiring in `window.py`.** Every `self._review_handler` statement goes: the `ReviewHandler(` construction (window.py:708-719, incl. :709), `set_agent_runtime_handler(self._review_handler)` (:720), the `review_handler=self._review_handler` kwarg on `CommandHandler` (:732), `set_review_handler(self._review_handler)` (:741), `set_feed_handler(self._review_handler)` (:744), and the lifecycle lambdas (:879/:882). The references at :1048/:1055 die with `_on_project_selected` (deleted below). `AgentRuntimeHandler` gets no `review_handler` kwarg (window.py:231 — pass no kwarg; the param itself is deleted per §2d-note-1); the `CommandHandler` ctor param `review_handler=None` at command_handler.py:58 and the register block at 92-102 go with it; `/ask`, `/stop`, `/status`, `/agents`, `/cost`, `/clear`, `/compact`, `/session`, `/help` stay. Delete `_on_review_started`, `_on_review_ended`, `_confirm_batch_accept` (2067+), `_on_project_selected` (the only `DiffViewer` constructor, 1041-1060), and the `DiffViewer` import inside it. `LeftPanel` is constructed with `on_project_selected=...`. Pass `None`. Do not make `FileTree` call that callback. Double-click stays `_toggle_drawer`. Delete the lifecycle wiring lambdas (window.py:878-882). Drop the review sentence from the Stop All dialog text (window.py:1899, "and any pending review checkpoint aborted" — keep the process-group and cannot-be-undone sentences). The Accept All confirm dialog at window.py:2085-2091 is the ReviewHandler batch-accept callback's dialog and goes with `_confirm_batch_accept`; the feed's own Accept All (auto_accept level `all` + `ui/feed/review.py`) is self-contained and stays.

`ui/file_tree/drawer.py` revert calls `self._project_handler.revert_file_to_sha`. That method is now the standalone implementation. Do not point the drawer at `ReviewHandler` — there is no longer a ReviewHandler to point at.

In `ui/handlers/command_handler.py`, the `review_handler` ctor param, the `review` / `check` / `accept` / `reject` `register_command` calls, and the `review, check, accept, reject` entries in the payload_free list (command_handler.py:332) go together. `/status` stays, minus its Review line (§2d-note-2).

Stale comments naming the deleted machinery must go too, or the §5 grep gate fails on them: `ui/feed/review.py:56` ("T2-RL2 in review_handler"), `ui/feed/review.py:180` ("mirrors ReviewHandler's rejection"), and `ui/handlers/telegram_bridge_handler.py:253` ("see ARH.set_review_handler"). Rewrite those comments to describe what the surviving code does; do not delete the code they describe.

### 2e. Drawer, forward, status bar

Delete `ui/views/activity_drawer.py` and `ui/handlers/activity_wiring_handler.py`. Delete `ActivityBubble.to_drawer_row` and the tests that only call it. Keep `ActivityBubble`, `ActivityHandler`, and `ActivityPillLabel`. The pill is the activity UI.

Delete `ui/handlers/forward_handler.py` and the `ForwardHandler(` construction in `window.py`. `/ask` stays on `CollabHandler`.

Delete the status-bar box in `window._build` (`_status_bar`, `_agent_id_label`) and `update_agent_id_display`. The vertical box's child is the paned only.

### 2f. Feed scroll and snooze

In `ui/views/feed_tab.py`, delete `schedule_scroll_to_bottom`, `_settle_scroll`, `_arm_settle`, `_on_adj_changed`, `_disarm_scroll`, `_cancel_settle`, `_bottom_value`, `_distance_from_bottom`, `_scroll_handler_id`, `_scroll_timeout_id`, `_settle_source`, and the `value-changed` connection that only fed `_was_near_bottom` for that settle. `schedule_smart_scroll_to_bottom` can stay as an empty method if callers still call it (`feed_handler` does). Body: `return`. Do not scroll a GTK adjustment. Name the internal call sites so they die with the method: the append-card path at feed_tab.py:514, `_on_scroll_mapped` at :527, and the comment at :539.

`is_near_bottom` keeps the HTML branch (`_html_surface._doc_near_bottom`). Delete the adjustment fallback only if every constructed tab has a surface. A tab with no surface still hits `_show_no_webkit`. Returning `True` there is the current "nothing rendered" answer. Keep that.

In the same file: delete the snooze `MenuButton` and the block in `update_auto_accept_prefs` that sets its label — the toolbar-label block dies with the button. In `ui/feed/auto_accept.py`, delete `snooze_card`, `unsnooze_card`, and the `if card.card_id in self._prefs.snoozed_card_ids: return False` check inside `_is_card_auto_acceptable`. Leave `snoozed_card_ids` on the prefs dataclass and in `from_dict` so an old prefs file still loads. The list is ignored. The `feed_store.py` round-trip is untouched: `snoozed_card_ids` still loads and saves at feed_store.py:836/:910/:946-948 — AND `AutoAcceptPrefs.to_dict` (feed_card.py:326) keeps writing the key; both are pinned by `tests/test_feed_retention.py:187-200` (`test_preserves_sibling_keys`), so do not prune them. Add `.feed-toolbar-snooze` (styles.py:1248) to the CSS deletions in §2h. Do not remove the agent dropdown, Diffs, Files, Exec, or Accept All.

### 2f-2. Feed load mixin — `ui/feed/load.py`

This file was missing from the original draft and owns half the dead GTK feed path. `FeedLoadMixin` is mixed into `FeedHandler` (`ui/handlers/feed_handler.py:47-50`); the `_card_widgets` dict is never assigned, so the GTK eviction tail can never release anything — but the method's HTML dispatch head (`self._evict_surplus_html_rows(exclude)`, load.py:359) is the LIVE row-windowing path and must keep firing.

- Keep `_evict_surplus_card_widgets(exclude)` as the call target (four live call sites: feed_handler.py:436, 551; load.py:184, 286), but reduce its body to the two live branches: `if self._html_feed: self._evict_surplus_html_rows(exclude); return` and the `self._feed_tab is None` guard. Delete everything after — the widget-cap read, the victim selection, the `is_above_viewport` walk, the vadjustment compensation, and the GTK Load More rebuild. The rename is MANDATORY: `_evict_surplus_card_widgets` becomes `_evict_surplus_rows` everywhere — the method, all four call sites, and the tests in §2i that call it (the §5 grep pattern stays `_card_widgets`, which now matches nothing).
- Delete `_build_load_more_widget` (load.py:223) and the `__load_more__` block it fed (load.py:424-432). The HTML Load More bar is `FeedTab.set_html_load_more` (feed_tab.py:297), already called at load.py:171, 284 — that is the Load More that stays.
- Delete the widget bookkeeping reads: `self._load_more_widget = None` at load.py:34, 175, 216 can go with the field, and `feed_handler.py:162` `self._load_more_widget: Gtk.Widget | None = None` goes with them. `_card_widgets`, `_card_container_spacing` (load.py:487), `is_above_viewport`, `get_vadjustment`, and `remove_card`-the-widget-path references inside the deleted tail go. Keep `FeedTab.remove_card` itself — `feed_handler.py:600-606` and `785` still use it as the compat forwarder to `remove_card_html` on legacy/mock tabs.
- The kept `feed_handler.py` methods must never touch the deleted `_card_widgets` field — order their cleanup in the same step: `remove_card` (feed_handler.py:592), `clear_project` (:776), and `_update_card_visual`'s GTK tail (:1148). The GTK tails are unreachable post-HTML-feed; make `_update_card_visual` HTML-only and drop the widget lines from `remove_card`/`clear_project`.
- `on_project_opened`'s `_append_and_schedule_scroll` keeps its HTML body (`set_html_cards` + `set_html_load_more` + `_schedule_smart_scroll` + eviction) and loses only the `self._load_more_widget = None` line.
- `_schedule_smart_scroll` / `schedule_smart_scroll_to_bottom` callers in this file keep working per §2f.

### 2g. Agents list — `ui/views/left_panel.py`

In `_build_agent_row`, delete `tag_lbl` and the `Openclaw` / `DevelCakes` / session-count string. The row is avatar, name, and the `+`/`−` button.

`_on_agent_right_click`: for a key that starts with `agent:`, call `_show_local_agent_menu` (Edit, Delete). Delete the `show_session_menu` branch. If `show_session_menu` has no remaining caller, delete it from `ui/views/session_menu.py`. Keep `show_project_menu`. The project-tab right-click and the `Chat:` button both set the solo target.

### 2h. GTK feed widgets that are never filled

`FeedHandler._card_widgets` is never assigned in `ui/` (grep `_card_widgets[`). `_evict_surplus_card_widgets`, `_build_load_more_widget`, and `ui/feed/snapshots.py` `_rebuild_context_panel` operate on that dict. Delete the widget eviction TAIL per §2f-2 — keep the method as the live HTML dispatch (`_evict_surplus_html_rows`), never delete that call. Keep snapshot data on `FeedCardData`. The HTML Review button reads `conversation_snapshot` through `render/feed_html.py`. Do not delete `ui/feed/snapshots.py` `_maybe_create_snapshot` or `_apply_snapshot`'s write onto the card. Delete only the widget walk.

Delete `build_context_panel` from `ui/views/feed_card.py` if that was its last caller. If the module is then empty of functions, delete the module and fix the `RENDERED_BODY_LIMIT` import in `tests/test_feed_html.py` to use `render.feed_html.FEED_RENDERED_BODY_LIMIT` only. Grep `from ui.views.feed_card` before deleting the file.

Delete `FeedTab.append_card`, `prepend_card`, `replace_card`, `get_card_container`, and `is_above_viewport` if grep shows no production caller after the widget path is gone. Tests that call them on a fake tab are deleted with the widget tests, not kept by adding the methods back.

GTK CSS in `ui/styles.py` for `.feed-card`, `.feed-card-git`, and the other type rules is unused once those widgets are gone. The HTML feed has its own `FEED_CSS`. Delete the GTK rules. Keep `.feed-scroll`, `.feed-toolbar`, and the toolbar button classes. `.web-surface-host` (styles.py:1311) loses both its appliers (§2a page scroller, §2b surface scroll) — delete the rule; the feed uses `.feed-scroll`, and the chat background rides `_apply_canvas_color` alone. Grep `add_css_class("feed-card` before deleting a rule. `feed-card-load-more` is on the HTML load-more bar in `FeedTab.set_html_load_more`. Keep that rule. Also die in this step, with their owners: `.feed-toolbar-snooze` (:1248, §2f), `.scroll-to-bottom-btn` + `.visible` + `:hover` (:777-793, dies with the ↓ button), `.chat-bubble-actions` / `.chat-action-btn` (:762-798), the `.chat-bubble-*` / `.chat-heading*` / `.agent-bg-*` / `.agent-dot-*` families (:339-420+, event_cards widgets), `.review-bar*`, and `.feed-context-*` (:1351-1391, `build_context_panel` classes). One stale-comment pair outside styles: `set_active_project_path` comments (window.py:644-648, ui/wiring.py:17-21/:36-38) claim the wiring exists for `event_cards._open_in_viewer` — the wiring STAYS (render/html.py:84-85 and utils/live_reader.py:21/:60 consume it) but rewrite the comments.

### 2i. Tests

A test that only constructs a deleted widget is deleted. Do not rewrite it against a fake of the deleted UI.

Delete, or cut down to whatever still imports a surviving symbol:

- `tests/test_activity_drawer.py`
- `tests/test_activity_wiring_handler.py`
- `tests/test_diff_viewer.py` (keep cases that call `get_lang_from_path` or `render_diff_hunks` if they do not construct `DiffViewer`)
- `tests/test_review_bar_queues.py`
- `tests/test_review_queues.py` (the SPEC-10 queue/lock tests and the `revert_file_to_sha` lock tests go with ReviewHandler; if a case still applies to the inlined standalone revert, retarget it to `ProjectHandler.revert_file_to_sha`)
- `tests/test_review_handler_feed_card.py`
- `tests/test_forward_handler.py`
- `tests/test_project_handler.py` — update the `Review: not started` pin (line 278) to the new three-line `/status` summary; add a case for the inlined `revert_file_to_sha` (guards, path resolution, `checkout_paths` call, no review dependency)
- `tests/test_agent_runtime.py` — delete the SPEC-10 SP2b checkpoint classes (`_wired`, `_spec10_rh`, the `_maybe_agent_checkpoint` / `set_review_handler` cases around lines 6440-6510); keep the rest
- `tests/test_stop_all.py` — delete Test 7 (review checkpoint refuses to commit, lines 651-745) and the `note_stop_all_aborted` summary-card consumption test if it builds a ReviewHandler; keep `stop_all_in_progress` flag tests that exercise ARH alone
- `tests/test_mvp_integration.py` — delete the ReviewHandler double (`_review_handler`, lines 66-101) and the review-state wiring; keep the send-path steps
- `tests/test_no_gateway_residuals.py` — remove `ui/handlers/review_handler.py` and `ui/handlers/forward_handler.py` from `REPOINTED_FILES` (they will no longer exist; the loop reads the whole directory, so the list entries are what change)
- `tests/test_feed_retention.py` — the GTK-widget eviction classes (around lines 660-760) go; retarget the live-window tests that matter to `_evict_surplus_html_rows` (or the retained dispatch method from §2f-2); delete `_patch_load_more_builder`
- `tests/test_window_telegram_bridge.py` — note that line 375 DOES assert `"Review:" in out`; update that assertion in the same step when `/status` drops the Review line (§2d-note-2). The rest of the `_remote_status_summary` tests stay and keep passing after the Review line drops.
- The `build_file_diff_card` / `build_diff_summary_card` classes in `tests/test_diff_card.py`. Keep the `render_diff_hunks` tests.
- `tests/test_low7_image_viewer.py`, `tests/test_activity_bubbles.py`, `tests/test_streaming.py`, `tests/test_chat_terminal_segment.py`, `tests/test_chat_task_segment.py`, `tests/test_chat_heading.py`, `tests/test_presentation_injection.py`, `tests/test_gtk_safe_link.py` if they import `event_cards`. If a test is really about `utils/image_paths.py`, point it there. Do not keep an `event_cards` import alive.
- Feed handler tests that drive `schedule_scroll_to_bottom` on a real `FeedTab` (`TestSmartScroll` and the real-adjustment scroll classes). The HTML `is_near_bottom` tests stay.
- Chat surface tests that construct `TextViewFallback` or drive `_pending_restore`. Tests of document scroll and live sections stay. Where a test used `TextViewFallback` only as a stand-in with `append_message`, use a small test double defined in that test file. Do not put that double in `ui/`.

`tests/test_chat_surface.py` imports `TextViewFallback` at module level. That import is removed. Split the file if needed so the WebKit tests still collect.

`tests/test_welcome_html.py` subclasses `TextViewFallback` (`class SpySurface(TextViewFallback)`, module-level import at line 27) as its surface stand-in. Convert `SpySurface` to a local double in that file (per the rule above); the welcome-rows tests themselves are about splash content, not the fallback class, and stay.

`tests/test_chat_render_handler.py` — its `SpySurface` subclasses `TextViewFallback` (module-level import at line 25): convert to a local double per the rule above. Delete the `render_event_card` test class (lines ~451-484) and the WebKit=None → `TextViewFallback` mount test (lines ~657-666). The render_sync / render_async / surface-mount tests stay.

These present-day test files are also casualties and join the delete list — they import deleted machinery and die at collection: `tests/test_review_state.py` (imports `models.review_state`), `tests/test_chat_render_scroll.py` (imports `TextViewFallback`), `tests/test_pango_guard_sites.py` (source-inspects `event_cards`, runtime error), `tests/test_wiring_low7_project_path.py` (30 event-cards matches), `tests/test_render_error_callbacks.py`, `tests/test_window_agent_label.py`, `tests/test_main_content_tab_switch.py`, `tests/test_missing_message_fix.py` (pins the dead cluster, §2c), `tests/test_chat_handler.py` (pins the dead cluster), and `tests/test_canvas_top_spacer.py` (pins the §2a-obsolete canvas spacer). Two more fail only at RUN time — their imports are function-local and escape the collect-only sweep: `tests/test_html_guard_sites.py` (event_cards witness at :413 — drop the witness, keep the file) and `tests/test_env_divergence.py` (:97-109, `event_cards._get_allowed_roots()` — retarget that case to `utils.image_paths.get_allowed_roots`, which stays; its CRABCAKES env cases are unrelated and stay untouched). In `tests/test_main_content_settings_bar.py`, delete only the `TestPillPreservation` class (:254-281, calls only the deleted bar writers); the rest of the file survives and §5 step 1 still runs it.

General rule — collection-sweep casualties: run `python3 -m pytest tests/ --collect-only -q`; delete or retarget every casualty not already listed in this section. An orphaned import anywhere in the suite fails collection and must be handled.

### 2j. Line shape

This is a deletion. `chat_surface.py` loses the fallback class (about 200 lines) and the GTK scroll restore. `event_cards.py` (about 800 lines), `review_handler.py`, `review_bar.py`, `diff_viewer.py`, `activity_drawer.py`, and `forward_handler.py` go away. Expect the `ui/` tree to shrink by several thousand lines, plus the tests that covered them. Do not add a replacement widget for any of them.

**Files NOT changed**

- `render/feed_html.py` card chrome (emoji, footer, copy button, Review context, reveal). No edit except a test import if `feed_card.py` goes away.
- `assets/splash.html`, `assets/splash-min.html`
- `ui/views/chat_input_toolbar.py` and `ui/handlers/input_toolbar_handler.py`
- `ui/views/agent_dashboard.py`
- `ui/file_tree/drawer.py` — one exception to "not changed": the two `checkpoint_sha` blocks (lines 347-350, 734-737) simplify to `checkpoint_sha = None`. Everything else — history, `_toggle_drawer`, revert wiring, revert error label — untouched.
- `ui/handlers/agent_command_handler.py`
- `ui/handlers/collab_handler.py`
- `ui/views/activity_pill.py` and `ui/handlers/activity_handler.py`
- `ui/feed/review.py` and `ui/feed/auto_accept.py` (beyond the §2f snooze deletions) — feed Accept is self-contained
- Feed Diffs / Files / Exec, the agent dropdown, Accept All, the project-bar `Files:` cycle
- `ChatSurface` live sections, `append_live`, canvas loading
- SPEC-57: `_apply_canvas_color`, feed patch / epoch, `html`/`body` canvas CSS
- `agent/runtime.py`, `agent/enforcement.py` — the runtime's `review_mode="off"` awareness-dict fields are agent-side constants, not UI review; out of scope
- `utils/review_log.py`, `utils/feedback_processor.py`, `utils/audit_parser.py` — orphaned non-UI helpers, out of scope for this UI lean (see §8)

---

## 3. Data Flow

**Open a chat tab.** `create_chat_tab` appends `chat_box` to the notebook. The render handler mounts `ChatSurface` into that box. The WebView is a direct child of the surface. The project bar stays on `_top_box`. Switching tabs does not move the bar.

**A local reply.** `AgentRuntimeHandler` → `ChatRenderHandler.render_sync` or `render_async` → `ChatSurface.append_message` / `append_live`. No `on_chat_event`. No GTK bubble. No unread increment. The tab dot stays `tab-dot-idle`.

**Scroll to the latest line.** `scroll_chat_to_bottom` → `surface.scroll_to_latest()` → the document script. There is no GTK adjustment on that path.

**Send in a project.** Solo target still comes from the `Chat:` button and from `show_project_menu`. `ChatHandler.on_send` is unchanged. `/ask` still returns `forward_to` from `CollabHandler`.

**File double-click.** `_toggle_drawer`. Diff text is `render_diff_hunks`. History and Revert stay. Revert calls `ProjectHandler.revert_file_to_sha` — now a standalone git checkout with no review session and no ReviewHandler. It needs no `/review` first.

**Feed Accept.** Unchanged, except a card id that used to be in `snoozed_card_ids` is no longer skipped.

**Exec approval.** Still a feed card with `needs_approval`. `approve_exec` stays. The trust dialog in `_maybe_prompt_project_trust` stays.

**A completed writer turn.** `turn_ui.py` dispatches the completed turn as it does today, minus the checkpoint call — with `/review` gone, no review session can exist, so no checkpoint was ever going to fire. The turn renders, the feed cards render, nothing else changes.

---

## 4. File Change Summary

| File | Change | Risk |
|---|---|---|
| `ui/views/main_content.py` | Drop overlay, outer scroll, ↓, avatar box, unread colors, test-only bar writers | High |
| `ui/views/chat_surface.py` | Drop fallback and the surface `ScrolledWindow`; document scroll stays | High |
| `ui/handlers/chat_render_handler.py` | Drop `render_event_card` and `on_forward_click` | Medium |
| `ui/handlers/chat_handler.py` | Drop the gateway event methods | Medium |
| `ui/window.py` | Drop review construction + wiring, DiffViewer, forward, status bar, drawer wiring, `Review:` line in `_remote_status_summary` | High |
| `ui/handlers/review_handler.py` | Delete | High |
| `ui/views/review_bar.py`, `diff_viewer.py`, `event_cards.py`, `activity_drawer.py` | Delete | Medium |
| `ui/handlers/forward_handler.py`, `activity_wiring_handler.py` | Delete | Medium |
| `ui/handlers/project_handler.py` | `revert_file_to_sha` re-implemented standalone (no ReviewHandler, no session gate); drop `set_review_handler`, `get_review_state`, `Review:` line in `cmd_status` | High |
| `ui/handlers/agent_runtime_handler.py` | Drop the review-gated checkpoint path (`_maybe_agent_checkpoint`, `_project_lock_for`, `_null_lock`, `_checkpoint_locks`, `review_handler` param, `set_review_handler`); keep stop-all registry | Medium |
| `ui/agent_runtime/turn_ui.py` | Drop the one checkpoint call line | Low |
| `ui/handlers/command_handler.py` | Drop `review_handler` param + `review`/`check`/`accept`/`reject` registrations | Low |
| `models/review_state.py` | Delete (and its `models/__init__.py` export) | Low |
| `ui/views/diff_card.py` | Keep hunk render; drop the two card builders | Medium |
| `ui/views/feed_tab.py` | Drop GTK settle, snooze button, dead card-widget compat methods | Medium |
| `ui/feed/load.py` | Drop widget eviction tail, `_build_load_more_widget`, `__load_more__` block, `_load_more_widget` field; keep HTML dispatch + `set_html_load_more` calls | High |
| `ui/handlers/feed_handler.py` | Drop `_load_more_widget` field (line 162); eviction call sites unchanged | Low |
| `ui/feed/auto_accept.py` | Drop snooze skip and the two methods | Low |
| `ui/views/left_panel.py` | Drop the agent subtitle and the gateway session menu | Low |
| `ui/views/session_menu.py` | Drop `show_session_menu` if unused; keep `show_project_menu` | Low |
| `ui/styles.py` | Drop `.tab-dot-unread` and unused `.feed-card*` widget rules | Low |
| `models/activity.py` | Drop `to_drawer_row` only | Low |
| `ui/file_tree/drawer.py` | Simplify the two `checkpoint_sha` blocks to `None` (was: "not changed") | Low |
| The test files in §2i | Delete or retarget | High |

---

## 5. Implementation Order

Start from a clean checkpoint: commit or accept all pending work before step 0. This spec deletes thousands of lines across dozens of files; it must land as one reviewable, bisectable unit on top of a known state.

Do these in order. After each step, run the tests named there. A step that deletes a module will fail import until the tests that import it are deleted in the same step. Land the test delete with the code delete.

0. **The revert cutover, first and alone.** Re-implement `ProjectHandler.revert_file_to_sha` as the standalone checkout (§2d), drop ProjectHandler's review plumbing (§2d-note-2), simplify the two drawer `checkpoint_sha` blocks, delete the `Review:` lines from `cmd_status` and `_remote_status_summary`, and update `tests/test_project_handler.py` (revert case + status pin). The old ReviewHandler is still present but unwired from this path. Run `tests/test_project_handler.py`. This lands the behavior change on its own so a later step cannot entangle it with deletions.

1. **Status bar, avatar box, unread colors, agent subtitle, session switcher, snooze, test-only bar writers.** No widget-tree change yet. Run `tests/test_main_content_settings_bar.py`, `tests/test_left_panel.py`, `tests/test_feed_handler.py` (expect the snooze and legacy-toggle tests to be updated in this step).

2. **Feed GTK settle and widget tail.** Delete the scroll methods, `_build_load_more_widget`, the `__load_more__` block, and the `_card_widgets` eviction tail (keep the HTML dispatch per §2f-2), plus the tests that call `schedule_scroll_to_bottom` or `_patch_load_more_builder` on a real tab or handler. Run `tests/test_feed_surface_scroll.py`, `tests/test_feed_retention.py`, and what remains of `tests/test_feed_handler.py`.

3. **Pango cards and `event_cards.py`.** Delete the module, `render_event_card`, and the tests that import them. Keep `render_diff_hunks`. Run `tests/test_diff_card.py` and `tests/test_chat_render_handler.py`.

4. **Review, DiffViewer, forward, activity drawer, checkpoint path.** Delete `review_handler.py`, `review_bar.py`, `diff_viewer.py`, `forward_handler.py`, `activity_drawer.py`, `activity_wiring_handler.py`, `models/review_state.py`, the ARH checkpoint path (§2d-note-1), the window wiring, and the command registrations together. `tests/test_review_queues.py`, `tests/test_review_bar_queues.py`, `tests/test_review_handler_feed_card.py`, `tests/test_forward_handler.py`, `tests/test_diff_viewer.py`, `tests/test_activity_drawer.py`, `tests/test_activity_wiring_handler.py` go in this step; the review/checkpoint classes in `tests/test_agent_runtime.py`, `tests/test_stop_all.py` (Test 7), `tests/test_mvp_integration.py`, and the `REPOINTED_FILES` entries in `tests/test_no_gateway_residuals.py` are cut down in this step. Run a grep for `ReviewHandler`, `DiffViewer`, `ForwardHandler`, `ActivityDrawer`, `render_event_card`, `TextViewFallback` and expect zero hits under `ui/`. Tests may still be mid-delete. Finish those imports in this step.

5. **Chat page structure and the surface scrollbar, plus the GTK restore.** This is the layout change. Run `tests/test_chat_surface.py` and `tests/test_chat_render_handler.py`. `TextViewFallback` is gone in this step, not before, so step 4's grep is re-run here and must be clean. No pytest certifies layout: after the gate, the PM does a visual pass before step 6 — resize the window small and large, switch between tabs, scroll a long transcript to the top and back, open a canvas tab, confirm the project bar never moves.

6. **GTK feed widget leftovers** (`build_context_panel`, dead CSS, the compat card methods). Run `tests/test_feed_html.py`.

7. **Stale-comment sweep.** Comments naming deleted machinery must go with the deletions or the §5 grep gate fails on them. Sweep: `ui/handlers/chat_render_handler.py` :45/:180/:367 (`render_event_card` / `TextViewFallback` comments), `ui/views/chat_surface.py` :30/:60 (docstring), `ui/views/activity_pill.py` :5, `ui/feed/load.py` :158/:347, `ui/handlers/agent_runtime_handler.py` :43/:102/:106/:452/:466/:1748-51, and `ui/handlers/command_handler.py` :58/:73/:91 — the :73 assignment especially (§2d names the param but not the assignment). Run the §5 grep; expect zero hits under `ui/`.

Full gate at the end:

```bash
python3 -m pytest tests/test_chat_surface.py tests/test_chat_render_handler.py tests/test_feed_surface_scroll.py tests/test_feed_html.py tests/test_feed_handler.py tests/test_feed_retention.py tests/test_left_panel.py tests/test_main_content_settings_bar.py tests/test_project_handler.py tests/test_agent_runtime.py tests/test_stop_all.py tests/test_window_telegram_bridge.py -q --tb=line
```

Paste the pytest summary. Then:

```bash
grep -RIn --include='*.py' -E 'TextViewFallback|ReviewHandler|DiffViewer|ActivityDrawer|show_forward_popover|render_event_card|schedule_scroll_to_bottom|_pending_restore|snooze_card|_review_handler|set_review_handler|get_review_state|_maybe_agent_checkpoint|_build_load_more_widget|_card_widgets' ui tests
```

Zero hits under `ui/`. In `tests/`, the allowed survivors: `revert_file_to_sha` may appear in `tests/test_project_handler.py`, and — if §2i's `test_review_queues.py` entry retargeted a lock case to the inlined revert — also in the retargeted cases there; the retarget is permitted by §2i. The `set_review_handler` / `get_review_state` / `_review_handler` strings must be gone everywhere (they have no surviving owner; the comment cleanups that make this true are in §2d). `note_stop_all_aborted` is NOT in the pattern — it survives on ARH with its test. A hit in a deleted-file path means the file is still there. After the mandatory §2f-2 rename, `_card_widgets` matches nothing — that is expected; the dispatch is `_evict_surplus_rows`.

Also run `python3 -m py_compile` on every touched `ui/` and `models/` file, and `grep -RIn review_state ui models` must be empty.

Finally, a collection sweep over the whole suite — the named gate covers 12 files and cannot see an orphaned import in the other ~240: `python3 -m pytest tests/ --collect-only -q > /dev/null` must exit 0. Paste the exit code with the pytest summary.

Do not "fix" a failure by reintroducing a deleted widget.

---

## 6. Acceptance Criteria

- [ ] A chat tab's notebook child is the chat box. There is no page-level `ScrolledWindow` and no `Gtk.Overlay` on that page.
- [ ] `ChatSurface` has no `ScrolledWindow`. The WebView is its child and expands. Document `scroll_to_latest` still runs.
- [ ] `WebKit is None` raises from `create_chat_surface`. There is no `TextViewFallback`.
- [ ] No GTK card is appended into `chat_box`. Replies are HTML on the surface.
- [ ] `/review`, `/check`, `/accept`, `/reject` are not registered. Double-clicking a file still opens the drawer. **Revert performs the checkout** — on a real repo, clicking Revert rewrites the file to the target SHA with no review session required (no "run /review first" message) and the drawer re-renders the diff.
- [ ] No `DiffViewer`, `ReviewBar`, `ActivityDrawer`, or forward popover is constructed. No `models/review_state.py` file exists.
- [ ] The window has no bottom `Agent:` status bar and no avatar/name on the button bar.
- [ ] Tab dots are `tab-dot-idle` only. Nothing adds `tab-dot-unread`.
- [ ] Agent rows have no subtitle. Right-click on a local agent is Edit and Delete.
- [ ] The feed toolbar has no Snooze button. A card id listed in old prefs is not skipped.
- [ ] `schedule_scroll_to_bottom` does not exist. Feed follow is the document path from SPEC-57.
- [ ] Feed retention still works: `_evict_surplus_rows` (renamed per §2f-2) still calls `_evict_surplus_html_rows`, the live window stays bounded, and Load More still restores older rows.
- [ ] `/status` and `_remote_status_summary` have no `Review:` line.
- [ ] Everything in the Keep list in §1 still works the way it did before this spec.
- [ ] The grep in §5 is clean.

---

## 7. Edge Cases

| Case | Expected |
|---|---|
| WebKit import fails | `create_chat_surface` raises at the first chat-tab mount. Since that happens during window construction, the practical result is a hard boot failure — the feed's no-WebKit label is not reached in a normal boot. No text view, no third UI. |
| `on_display_card` is invoked | The callback returns without building a widget. |
| Old prefs contain `snoozed_card_ids` | `from_dict` still accepts the key. Auto-accept does not read it. |
| Last chat tab closes | The project bar stays on `_top_box`. It is not unparented onto a deleted overlay. |
| Solo target is set | `Chat:` and the project right-click menu still set it. Send still uses it. |
| Drawer revert, session never started | The checkout still runs — no session gate, no "run /review first" toast. |
| Drawer revert, checkout fails | A ⚠ "Failed to revert …" message lands in the project chat via the wired `on_revert_status` callback, and a WARNING is logged. The drawer's try/except remains only for synchronous errors (e.g. a `None` handler). |
| Drawer revert, file_path starts with `-` | Early return, no checkout, no raise (the BUG #12 guard stays). |
| Live section button | Still posts through `LiveBridge`. Exec still raises the feed approval card. |
| Canvas tool | Still `create_chat_tab("canvas:…")` and `append_live` / `load_unsanitized_full_document`. |
| Timer fires | Still the existing `_deliver_timer` path. |
| Middle-click on a tab | Still closes it. |
| Stop All during a writer turn | Cancels turns, kills process groups, summary card reads `Checkpoints aborted: 0` (the line stays, the count is always zero now). |
| Test imports `TextViewFallback` | That test is deleted or uses a local double. The class is not re-added under another name. |
| Old feed has more cards than the live window | HTML row eviction still bounds the document; Load More still restores older rows. |

---

## 8. ARCHITECTURE.md Updates Required

After the deletions, these statements are false and must be edited or removed. Do not rewrite the document.

- The tree blurb for `ui/views/activity_drawer.py` ("NO LONGER MOUNTED" is not enough — the file is gone).
- §3.22a `feed_card.py` as the feed widget factory. The feed is `render/feed_html.py` and `FeedSurface`.
- Any sentence that names `ReviewHandler`, `DiffViewer`, `TextViewFallback`, or `event_cards.py` as a live view.
- `ActivityWiringHandler` as constructed from `window.py`. It is not, and the file is deleted.
- Any sentence describing the review command set (`/review`, `/check`, `/accept`, `/reject`) or the review session lifecycle.
- Any sentence naming `models/review_state.py` or `ReviewState` as a live model.
- Any sentence describing the agent-checkpoint path (`_maybe_agent_checkpoint`, `_project_lock_for`, `_checkpoint_locks`, `enqueue_agent_checkpoint`).
- The chat-surface blurb: no `ScrolledWindow`, no `TextViewFallback`, no GTK scroll restore — the WebView is a direct child of `ChatSurface`, the notebook page is the chat box, and the page scrolls in the document.
- The ProjectHandler blurb: `revert_file_to_sha` is a standalone git checkout, not a ReviewHandler delegate; no `set_review_handler` / `get_review_state`.
- `ui/feed/load.py`: no `_build_load_more_widget`, no `_card_widgets` eviction tail; Load More is `set_html_load_more`, row windowing is `_evict_surplus_html_rows`.

- `knowledge/*.md` — six matches (projects.md and two more files) name deleted machinery; update or remove them. Concretely: `knowledge/features.md:36-60` (Code Review System, ReviewBar, ReviewState), `knowledge/commands.md:161-190` (review commands via ReviewHandler), `knowledge/projects.md:197` (activity drawer) and `:208-218`.
- README.md rows: the :550 env-table row for `DEVELCAKES_NO_WEBKIT` (after §2b chat requires WebKit unconditionally, so on Ubuntu 24.04/apparmor systems it is a guaranteed boot failure), the :589-612 handler-tree rows (`review_handler.py`, `forward_handler.py`, `activity_wiring_handler.py`), and :628 (`review_state.py`).
- ARCHITECTURE.md beyond the bullets above: §7 review-bar integration (:726-759), :1505 (ReviewHandler ctor), :2642 (`review_handler` param), :4510-4521 (line counts), :4600 (`test_activity_drawer.py` in the test tree), :4223.
- Older specs and reference docs describing the deleted overlay/scroller/↓ button become historical, not live, descriptions: `SPEC-SETTINGS-TAB-AND-PROJECT-BAR-TRANSPORT.md:365/:371`, `SPEC-SETTINGS-BAR-TERMINAL-INVESTIGATION.md:34/:37`, `docs/reference/CODEBASE_DEEP_DIVE.md:473`, `SPEC-17-CHAT-SCROLL.md:63/:189`, `SPEC-57:563/:702` (the latter two explicitly defer scroller removal to "a different spec" — this is that spec). Do not rewrite history docs; annotate or leave.

One new sentence under the chat-surface blurb: the transcript WebView is a direct child of `ChatSurface`. The notebook page is the chat box. The page scrolls in the document. There is no GTK scrollbar on that page.
