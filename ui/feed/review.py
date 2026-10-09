"""Review, accept, reject, and batch-accept actions."""
from __future__ import annotations

import time

from ui.feed._facade import _mod


class FeedReviewMixin:
    def handle_review(self, card_id: str, card_widget=None) -> None:
        """Review button clicked — toggle context panel visibility."""
        card = self._cards.get(card_id)
        if card is None:
            return

        card.reviewed = True

        if card_widget is not None and hasattr(card_widget, '_context_panel'):
            panel = card_widget._context_panel
            panel.set_visible(not panel.get_visible())

    def handle_accept(self, card_id: str) -> None:
        """
        Accept button clicked.

        For git-backed cards (diff, file_created, file_deleted):
          1. Stage + commit in background thread
          2. Mark card.accepted = True on main thread
          3. Visual feedback via CSS class

        For other card types:
          1. Mark card.accepted = True
        """
        card = self._cards.get(card_id)
        if card is None:
            return

        if card.card_type in ("diff", "file_created", "file_modified", "file_deleted"):
            project_path = card.metadata.get("project_path", "") or self._project_paths.get(card.project_name, "")
            if not project_path:
                return

            def _git_accept():
                result_stage = _mod().git_ops.stage_all(project_path)
                if not result_stage.success:
                    _mod()._logger.warning("handle_accept: git stage failed for %s", project_path)
                    return

                # Generate the commit message from the ACTUAL staged files,
                # not from card.title. card.title is user-facing text (not a
                # file path) and may not match the real diff. Same fix as
                # T2-RL2 in review_handler.
                #
                # Only catch ImportError (gitpython not installed). Other
                # exceptions are logged as warnings — the user clicked Accept
                # on a card and the card remains visible, so we don't need
                # to surface a chat message like T2-RL2 does.
                try:
                    import git as gitpython
                except ImportError:
                    staged = []
                else:
                    try:
                        repo = gitpython.Repo(project_path)
                        staged = repo.index.diff("HEAD")
                    except Exception as e:
                        _mod()._logger.warning(
                            "handle_accept: failed to read diff for %s: %s: %s",
                            project_path, type(e).__name__, e,
                        )
                        return

                if not staged:
                    # Working tree is clean — nothing to commit. Silent no-op:
                    # the user clicked Accept on a card but the underlying
                    # changes have already been accepted (or never existed).
                    # Log a warning for observability, but don't create an
                    # empty commit and don't mark the card as accepted.
                    _mod()._logger.info(
                        "handle_accept: nothing to commit for card %s (working tree clean)",
                        card_id,
                    )
                    return

                # Build a descriptive message from the actual files
                file_list = sorted({d.a_path or d.b_path for d in staged if d.a_path or d.b_path})
                if len(file_list) == 1:
                    commit_msg = f"Accept: {file_list[0]}"
                elif len(file_list) <= 3:
                    commit_msg = f"Accept: {len(file_list)} files ({', '.join(file_list)})"
                else:
                    commit_msg = f"Accept: {len(file_list)} files ({', '.join(file_list[:3])}...)"

                result_commit = _mod().git_ops.commit(project_path, commit_msg)
                if result_commit.success:
                    card.accepted = True
                    card.metadata["project_path"] = project_path
                    # Persist to feed.json (Phase 1: via the background writer)
                    self._enqueue_card_update(project_path, card_id, {"accepted": True})
                    # Update visual on main thread
                    def _mark():
                        self._update_card_visual(card_id, accepted=True)
                    self._GLib.idle_add(_mark)
                    self._GLib.idle_add(lambda: self._add_git_card(card, result_commit))

            # Record path for echo suppression BEFORE starting git thread.
            # CrabWatch will fire events when git modifies the filesystem;
            # on_filesystem_event() checks _recent_git_paths to suppress echoes.
            if card.file_path:
                self._recent_git_paths[card.file_path] = time.monotonic()

            t = _mod().threading.Thread(target=_git_accept, daemon=True)
            t.start()
        else:
            # REVIEW-PERSIST-1: a non-git decision is durable state too — the
            # git path above enqueues {"accepted": True}; this branch used to
            # stop at the in-memory mutation, so the decision was lost on
            # reload. update_card records the decision, refreshes the widget
            # (in-place seam or rebuild) and enqueues the persist — the same
            # approve_exec mechanism the approval cards use.
            project_path = card.metadata.get("project_path", "") or self._project_paths.get(card.project_name, "")
            if not project_path:
                _mod()._logger.warning(
                    "handle_accept: non-git card %s accepted with no project "
                    "path — decision cannot be persisted to feed.json",
                    card_id,
                )
            card.accepted = True
            self.update_card(card_id, card)
            # Refresh batch accept bar (Phase 5)
            self._update_batch_bar_for_active_project()

    def handle_reject(self, card_id: str) -> None:
        """
        Reject button clicked.

        For git-backed cards:
          1. Revert changes in background thread
          2. Mark card.accepted = False
          3. Notify agent via on_send_to_agent

        For other cards:
          1. Mark card.accepted = False
        """
        card = self._cards.get(card_id)
        if card is None:
            return

        if card.card_type in ("diff", "file_created", "file_modified", "file_deleted"):
            project_path = card.metadata.get("project_path", "") or self._project_paths.get(card.project_name, "")
            if not project_path:
                return

            def _git_reject():
                fp = card.file_path
                sha = card.commit_sha or "HEAD"

                # MED-11: Validate commit_sha before git call
                if not _mod()._VALID_SHA_RE.match(sha):
                    _mod()._logger.warning(
                        "MED-11: Invalid commit SHA %r for card %s — skipping reject",
                        sha, card_id,
                    )
                    return

                result_reject = _mod().git_ops.checkout_paths(project_path, sha, [fp]) if fp else None

                def _mark():
                    card.accepted = False
                    card.metadata["project_path"] = project_path
                    self._update_card_visual(card_id, accepted=False)
                    # Persist to feed.json (Phase 1: via the background writer)
                    self._enqueue_card_update(project_path, card_id, {"accepted": False})
                    # Notify agents — FIX 6.2 (SP2 audit): the former
                    # f"project:{name}" key was a guaranteed no-op receiver.
                    # Per-member routing mirrors ReviewHandler's rejection
                    # fan-out; special:supervisor is the fallback when the
                    # project has no registered members (honest minimal
                    # option — the FeedHandler callback shape is a single
                    # (session_key, text) pair, so member iteration happens
                    # here rather than in the callback).
                    msg = f"[PM] Rejected change: {card.title}"
                    members = []
                    if self._project_handler is not None:
                        try:
                            members = list(
                                self._project_handler.get_project_members(
                                    card.project_name
                                )
                            )
                        except Exception as exc:
                            _mod()._logger.warning(
                                "feed: member lookup for %s failed: %s",
                                card.project_name, exc,
                            )
                    targets = members or ["special:supervisor"]
                    for target in targets:
                        self._on_send_to_agent(target, msg)
                    # Add git card
                    self._add_git_card(card, result_reject)
                self._GLib.idle_add(_mark)

            # Record path for echo suppression BEFORE starting git thread.
            if card.file_path:
                self._recent_git_paths[card.file_path] = time.monotonic()

            t = _mod().threading.Thread(target=_git_reject, daemon=True)
            t.start()
        else:
            # REVIEW-PERSIST-1: same persist parity as handle_accept's non-git
            # branch — record the durable decision via update_card.
            project_path = card.metadata.get("project_path", "") or self._project_paths.get(card.project_name, "")
            if not project_path:
                _mod()._logger.warning(
                    "handle_reject: non-git card %s rejected with no project "
                    "path — decision cannot be persisted to feed.json",
                    card_id,
                )
            card.accepted = False
            self.update_card(card_id, card)
            # Refresh batch accept bar (Phase 5)
            self._update_batch_bar_for_active_project()

    def handle_batch_accept(self, card_ids: list[str]) -> None:
        """
        Accept a batch of consecutive file-change cards in one click.
        Iterates in order (top-to-bottom in the feed); each accept creates a
        git_commit card via _add_git_card() with the same flow as the singular
        handle_accept(). (Phase 5)

        Used by the batch accept bar when ≥2 file-change cards are pending.
        """
        for card_id in card_ids:
            self.handle_accept(card_id)

    def _on_batch_accept_clicked(self) -> None:
        """
        Called when user clicks the batch accept bar's "Accept All" button.
        Computes the list of consecutive pending file-change cards at the
        bottom of the feed and accepts them all. (Phase 5)
        """
        if self._feed_tab is None:
            return
        project_name = self._active_project_name
        if project_name is None:
            return
        all_cards = self.get_cards_for_project(project_name)
        if not all_cards:
            return
        actionable_types = ("diff", "file_created", "file_modified", "file_deleted")
        batch_ids: list[str] = []
        for card in all_cards:  # newest first
            if (card.card_type in actionable_types
                    and card.accepted is None
                    and card.card_id is not None):
                batch_ids.append(card.card_id)
            else:
                break
        # batch_ids is newest-first; reverse to top-to-bottom for handle_accept order
        batch_ids.reverse()
        self.handle_batch_accept(batch_ids)
        # Refresh the batch bar (count may now be 0 or 1)
        self._update_batch_bar_for_active_project()

