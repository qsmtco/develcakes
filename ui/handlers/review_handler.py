# ui/handlers/review_handler.py
# Review session logic — checkpoint, check changes, accept, reject.
# Coordinates git_ops, diff_parser, and GTK views.
# All GTK via GLib.idle_add(). No git calls on the main thread.

import logging
import re
import threading
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable

from models.command import Command, CommandResult
from models.feed_card import FeedCardData

from models.review_state import ReviewState
from utils import git_ops
from utils.diff_parser import parse_diff


# MED-11: Validate git commit SHA to prevent argument injection
_VALID_SHA_RE = re.compile(r"^(HEAD|[0-9a-fA-F]{4,40})$")

_logger = logging.getLogger(__name__)


def _validate_sha(sha: str, context: str = "") -> None:
    """Raise ValueError if sha does not match a valid git ref pattern."""
    if not _VALID_SHA_RE.match(sha):
        raise ValueError(f"Invalid git ref in {context}: {sha!r}")


class ReviewHandler:
    """
    Handles review session lifecycle for project tabs.

    Args:
        GLib: gi.repository.GLib — for idle_add dispatch to main thread
        main_content: MainContent instance — for ReviewBar insertion
        project_handler: ProjectHandler — for getting project paths
        on_review_started: Callable[[str, Gtk.Widget], None] — (project_name, review_bar)
        on_review_ended: Callable[[str], None] — project_name
        on_display_card: Callable[[dict], None] — display a card in project tab chat
        on_display_text: Callable[[str], None] — display text in project tab chat
    """

    def __init__(
        self,
        *,
        GLib,
        main_content,
        project_handler,
        on_review_started: Callable,
        on_review_ended: Callable,
        on_display_card: Callable,
        on_display_text: Callable,
        on_feed_card=None,  # callback(FeedCardData) — add git_commit card to project feed
    ):
        self._GLib = GLib
        self._mc = main_content
        self._ph = project_handler
        self._on_review_started = on_review_started
        self._on_review_ended = on_review_ended
        self._on_display_card = on_display_card
        self._on_display_text = on_display_text
        self._on_feed_card = on_feed_card
        self._agent_runtime_handler = None  # injected via set_agent_runtime_handler()

        # Per-project review states: project_name -> ReviewState
        self._states: dict[str, ReviewState] = {}

        # Chat handler reference for sending rejection messages (set via set_chat_handler)
        self._chat_handler = None

        # Feed handler reference for persisting review resolutions (set via
        # set_feed_handler; REVIEW-PERSIST-1 Edit B)
        self._feed_handler = None


    def set_chat_handler(self, chat_handler):
        """Set ChatHandler reference for rejection message sending."""
        self._chat_handler = chat_handler

    def set_feed_handler(self, feed_handler):
        """Set FeedHandler reference for persisting review resolutions.

        Called by window.py during _build (after both handlers exist — the
        same callback-injection pattern as on_feed_card/set_chat_handler; no
        handler→handler import). The stored reference is invoked from the
        accept_changes/reject_changes success paths, which write the decision
        back to the original needs_review cards (REVIEW-PERSIST-1 Edit B).
        """
        self._feed_handler = feed_handler

    def _persist_review_resolution(self, project_name: str, project_path: str,
                                   accepted: bool) -> None:
        """Write the review-bar resolution back to the ORIGINAL review-flagged cards.

        When a review session resolves, the tool-result cards flagged
        metadata["needs_review"] (set by AgentRuntimeHandler._do_tool_call_result)
        must record the decision — otherwise it is lost on reload. Mirrors
        approve_exec's durable-record write: mutate card.accepted +
        metadata["status"], clear the needs_review flag, then feed the card
        through FeedHandler.update_card, which refreshes the widget and
        enqueues the coalesced persist (F1: accepted is included because a
        decision exists — never None).

        Called from accept_changes/reject_changes via GLib.idle_add (main
        thread). Missing feed handler → warning, never a silent drop.
        """
        fh = getattr(self, "_feed_handler", None)
        if fh is None:
            _logger.warning(
                "_persist_review_resolution: no feed handler wired — review "
                "resolution for %s will not be persisted on its cards",
                project_name,
            )
            return
        for card in fh.get_cards_for_project(project_name):
            if not (card.metadata or {}).get("needs_review"):
                continue
            # Build the RESOLVED copy and let update_card perform the in-memory
            # replacement (it assigns self._cards[card_id] = card_data itself).
            # We deliberately do NOT mutate the live card here: update_card
            # returns early (warning only) when the card is no longer in
            # _cards, and skips the enqueue when no project path is registered.
            # Mutating first would leave the in-memory card looking resolved
            # while disk kept needs_review=True — worse than the original
            # lost-decision bug, because the UI would claim success.
            # (Audit BUG #1: mutate-before-persist.)
            new_metadata = dict(card.metadata or {})
            new_metadata.pop("needs_review", None)
            new_metadata["status"] = "approved" if accepted else "denied"
            resolved = replace(card, metadata=new_metadata, accepted=accepted)
            try:
                fh.update_card(card.card_id, resolved)
            except Exception:
                _logger.exception(
                    "_persist_review_resolution: failed to persist resolution "
                    "for card %s in %s",
                    card.card_id, project_name,
                )


    def _emit_feed_card(self, card_dict: dict) -> None:
        """Convert git_commit card dict to FeedCardData and fire feed callback.

        Only fires if _on_feed_card is set. Mirrors work_handler._emit_feed_card.
        """
        if not self._on_feed_card:
            return
        feed_card = FeedCardData(
            card_type="git_commit",
            source="git",
            title=card_dict.get("title", ""),
            body=card_dict.get("body", ""),
            author="PM",
            timestamp=datetime.now(timezone.utc),
            project_name=card_dict.get("project_name", ""),
            commit_sha=card_dict.get("commit_sha"),
        )
        self._on_feed_card(feed_card)

    # ── Mode management ─────────────────────────────────────────────────

    def set_review_mode(self, project_name: str, mode: str) -> None:
        """Set review mode for a project. 'off' or 'review'."""
        state = self._states.get(project_name)
        if state is None:
            return
        state.review_mode = mode

        if mode == "off":
            # Remove review bar if present
            self._GLib.idle_add(lambda: self._mc.set_review_bar(None))
            self._on_review_ended(project_name)
        elif mode == "review":
            # Create and show review bar
            self._GLib.idle_add(lambda: self._show_review_bar(project_name, state))

    def get_review_mode(self, project_name: str) -> str:
        """Current review mode for a project."""
        state = self._states.get(project_name)
        return state.review_mode if state else "off"

    def _show_review_bar(self, project_name: str, state: ReviewState) -> None:
        """Create and insert the ReviewBar into MainContent."""
        from ui.views.review_bar import ReviewBar

        bar = ReviewBar(
            on_mode_changed=lambda m: self.set_review_mode(project_name, m),
            on_start_clicked=lambda: self.start_review(project_name),
            on_check_clicked=lambda: self.check_changes(project_name),
        )
        bar.set_review_mode(state.review_mode)

        # Set accept/reject callbacks
        bar.set_accept_callback(lambda: self.accept_changes(project_name, "approved"))
        bar.set_reject_callback(lambda: self.reject_changes(project_name, "rejected"))

        if state.is_active():
            bar.set_state_reviewing(state.checkpoint_sha or "")
        else:
            bar.set_state_idle()

        self._mc.set_review_bar(bar)
        self._on_review_started(project_name, bar)

    # ── Review session lifecycle ────────────────────────────────────────

    def start_review(self, project_name: str, session_key: str | None = None) -> None:
        """Start a review session: git add -A && git commit → checkpoint SHA."""
        state = self._states.get(project_name)
        if state is None:
            return
        sk = session_key or f"project:{project_name}"

        if not state.can_checkpoint():
            self._GLib.idle_add(lambda sk=sk: self._on_display_text(sk, f"Cannot start review: mode={state.review_mode}, checkpoint={'exists' if state.checkpoint_sha else 'none'}"))
            return

        project_path = state.project_path

        def _do():
            # Ensure it's a git repo
            if not git_ops.is_repo(project_path):
                init_result = git_ops.init_repo(project_path)
                if not init_result.success:
                    self._GLib.idle_add(lambda sk=sk: self._on_display_text(sk, f"Failed to init git repo: {init_result.error}"))
                    return

            # Stage all
            stage_result = git_ops.stage_all(project_path)
            if not stage_result.success:
                self._GLib.idle_add(lambda sk=sk: self._on_display_text(sk, f"Failed to stage files: {stage_result.error}"))
                return

            # Commit checkpoint. allow_empty=True because a checkpoint is a
            # valid SHA marker even on a clean tree (user can start a review
            # without any changes to capture the current state).
            commit_result = git_ops.commit(project_path, "[review] checkpoint", allow_empty=True)
            if not commit_result.success:
                self._GLib.idle_add(lambda sk=sk: self._on_display_text(sk, f"Failed to create checkpoint: {commit_result.error}"))
                return

            sha = commit_result.sha

            def _update_state(sk=sk):
                state.checkpoint_sha = sha
                state.is_dirty = False
                state.last_check_files = []
                bar = self._mc.get_review_bar()
                if bar is not None:
                    bar.set_state_reviewing(sha)
                    bar.set_loading(False)
                self._on_display_text(sk, f"🔍 Review session started — checkpoint {sha[:7]}")

            self._GLib.idle_add(_update_state)

        threading.Thread(target=_do, daemon=True).start()

    def check_changes(self, project_name: str, session_key: str | None = None) -> None:
        """Check what changed since checkpoint: git diff <sha>."""
        state = self._states.get(project_name)
        if state is None:
            return
        sk = session_key or f"project:{project_name}"

        if not state.is_active():
            self._GLib.idle_add(lambda sk=sk: self._on_display_text(sk, "No active review session. Use /review to start one."))
            return

        project_path = state.project_path
        sha = state.checkpoint_sha

        def _do():
            diff_result = git_ops.diff_against(project_path, sha)
            if not diff_result.success:
                self._GLib.idle_add(lambda sk=sk: self._on_display_text(sk, f"Failed to diff: {diff_result.error}"))
                return

            parsed = parse_diff(diff_result.stdout)

            if not parsed.files:
                self._GLib.idle_add(lambda sk=sk: self._on_display_text(sk, "No changes detected since checkpoint."))
                return

            def _update_ui(sk=sk):
                state.last_check_files = [f.display_path for f in parsed.files]
                state.is_dirty = True
                bar = self._mc.get_review_bar()
                if bar:
                    bar.set_state_has_changes(len(parsed.files), parsed.total_additions, parsed.total_deletions)
                    bar.set_loading(False)

                # Display summary card with accept/reject-all callbacks
                def _make_accept_all(pn=project_name, sk=sk):
                    return lambda _: self.accept_changes(pn, "accepted", sk)
                def _make_reject_all(pn=project_name, sk=sk):
                    return lambda _: self.reject_changes(pn, "rejected", sk)
                self._on_display_card({
                    "type": "diff_summary",
                    "parsed_diff": parsed,
                    "session_key": sk,
                    "on_accept_all": _make_accept_all(),
                    "on_reject_all": _make_reject_all(),
                })

                # Display per-file diff cards with per-file reject callback
                def _make_reject_file(pn=project_name):
                    return lambda fp: self.reject_file(pn, fp)
                reject_file_cb = _make_reject_file()
                for file_diff in parsed.files:
                    self._on_display_card({
                        "type": "diff_file",
                        "file_diff": file_diff,
                        "session_key": sk,
                        "on_reject_file": reject_file_cb,
                    })

            self._GLib.idle_add(_update_ui)

        threading.Thread(target=_do, daemon=True).start()

    def accept_changes(self, project_name: str, message: str, session_key: str | None = None) -> None:
        """Accept all changes: git add -A && git commit -m <message>."""
        state = self._states.get(project_name)
        if state is None:
            return
        sk = session_key or f"project:{project_name}"

        project_path = state.project_path
        checkpoint_sha = state.checkpoint_sha

        def _do():
            # Stage all
            stage_result = git_ops.stage_all(project_path)
            if not stage_result.success:
                self._GLib.idle_add(lambda sk=sk: self._on_display_text(sk, f"Failed to stage: {stage_result.error}"))
                return

            # Generate the commit message from the ACTUAL staged files, not
            # from the input message parameter. The input message is just
            # user intent ("Accept: Modified X") but the real diff is in
            # repo.index.diff("HEAD").
            #
            # Only catch ImportError (gitpython not installed). Other exceptions
            # (InvalidGitRepositoryError, BadName, etc.) are reported to the
            # user as real errors — NOT silently treated as "nothing to commit",
            # which would mask a broken git repo or other real failure.
            try:
                import git as gitpython
            except ImportError:
                # gitpython not installed — fall through to empty case
                staged = []
            else:
                try:
                    repo = gitpython.Repo(project_path)
                    staged = repo.index.diff("HEAD")
                except Exception as e:
                    # Real error reading the diff — report to user, reset state.
                    self._GLib.idle_add(lambda sk=sk, err=e: self._on_display_text(
                        sk, f"❌ Failed to read diff: {type(err).__name__}: {err}"
                    ))
                    def _reset_state(sk=sk):
                        state.checkpoint_sha = None
                        state.is_dirty = False
                        state.last_check_files = []
                        bar = self._mc.get_review_bar()
                        if bar:
                            bar.set_state_idle()
                            bar.set_loading(False)
                        self._on_review_ended(project_name)
                    self._GLib.idle_add(_reset_state)
                    return

            if not staged:
                # Working tree is clean — nothing to commit. Show a friendly
                # message instead of the misleading "Failed to commit" error.
                self._GLib.idle_add(lambda sk=sk: self._on_display_text(
                    sk, "ℹ️ Nothing to commit — working tree clean. No changes were accepted."
                ))
                # Still update the state so the review bar resets.
                def _reset_state(sk=sk):
                    state.checkpoint_sha = None
                    state.is_dirty = False
                    state.last_check_files = []
                    bar = self._mc.get_review_bar()
                    if bar:
                        bar.set_state_idle()
                        bar.set_loading(False)
                    self._on_review_ended(project_name)
                self._GLib.idle_add(_reset_state)
                return

            # Build a descriptive message from the actual files
            file_list = sorted({d.a_path or d.b_path for d in staged if d.a_path or d.b_path})
            if len(file_list) == 1:
                full_message = f"[review] accepted: Accept: Modified {file_list[0]}"
            elif len(file_list) <= 3:
                full_message = f"[review] accepted: Accept: Modified {len(file_list)} files ({', '.join(file_list)})"
            else:
                full_message = f"[review] accepted: Accept: Modified {len(file_list)} files ({', '.join(file_list[:3])}...)"

            # Commit
            commit_result = git_ops.commit(project_path, full_message)
            if not commit_result.success:
                self._GLib.idle_add(lambda sk=sk: self._on_display_text(sk, f"Failed to commit: {commit_result.error}"))
                return

            def _update_state(sk=sk):
                state.checkpoint_sha = None
                state.is_dirty = False
                state.last_check_files = []
                bar = self._mc.get_review_bar()
                if bar:
                    bar.set_state_idle()
                    bar.set_loading(False)
                self._on_review_ended(project_name)
                # Use the actual generated message for the user-facing display
                self._on_display_text(sk, f"✅ Changes accepted and committed: {full_message.replace('[review] accepted: ', '')}")
                self._emit_feed_card({
                    "title": f"Accepted: {full_message.replace('[review] accepted: ', '')}",
                    "body": commit_result.stdout.strip() if commit_result.stdout else "",
                    "project_name": project_name,
                    "commit_sha": getattr(commit_result, "sha", None),
                })
                # REVIEW-PERSIST-1 Edit B: the decision must also land on the
                # review-flagged tool-result cards, or it is lost on reload.
                self._persist_review_resolution(project_name, project_path, True)

            self._GLib.idle_add(_update_state)

        threading.Thread(target=_do, daemon=True).start()

    def reject_changes(self, project_name: str, reason: str, session_key: str | None = None) -> None:
        """Reject all changes: git checkout <sha> -- ."""
        state = self._states.get(project_name)
        if state is None:
            return
        sk = session_key or f"project:{project_name}"

        if not state.is_active():
            self._GLib.idle_add(lambda sk=sk: self._on_display_text(sk, "No active review session to reject."))
            return

        project_path = state.project_path
        sha = state.checkpoint_sha

        def _do():
            # MED-4: Scope reject to last_check_files (agent-modified files), not all tracked files
            files_to_revert = state.last_check_files if state.last_check_files else ["."]

            # Show confirmation with file list
            file_list_display = ", ".join(files_to_revert) if len(files_to_revert) <= 5 else \
                ", ".join(files_to_revert[:5]) + f"... (+{len(files_to_revert)-5} more)"
            self._GLib.idle_add(lambda sk=sk: self._on_display_text(
                sk, f"Reverting {len(files_to_revert)} file(s): {file_list_display}"
            ))

            # MED-11: Validate commit_sha before passing to git
            _validate_sha(sha)

            result = git_ops.checkout_paths(project_path, sha, files_to_revert)
            if not result.success:
                self._GLib.idle_add(lambda sk=sk: self._on_display_text(sk, f"Failed to revert: {result.error}"))
                return

            # Send rejection message to all project members
            self._send_rejection_messages(project_name, reason, sha)

            def _update_state(sk=sk):
                state.checkpoint_sha = None
                state.is_dirty = False
                state.last_check_files = []
                bar = self._mc.get_review_bar()
                if bar:
                    bar.set_state_idle()
                    bar.set_loading(False)
                self._on_review_ended(project_name)
                self._on_display_text(sk, f"❌ Changes rejected — files reverted to checkpoint {sha[:7]}")
                self._emit_feed_card({
                    "title": f"Rejected: {reason}",
                    "body": result.stdout.strip() if result.stdout else "",
                    "project_name": project_name,
                    "commit_sha": sha,
                })
                # REVIEW-PERSIST-1 Edit B: persist the resolution on the
                # review-flagged tool-result cards (accept-path parity).
                self._persist_review_resolution(project_name, project_path, False)

            self._GLib.idle_add(_update_state)

        threading.Thread(target=_do, daemon=True).start()

    def reject_file(self, project_name: str, file_path: str) -> None:
        """Reject a single file: git checkout <sha> -- <file_path>."""
        state = self._states.get(project_name)
        if state is None or not state.is_active():
            return

        project_path = state.project_path
        sha = state.checkpoint_sha

        session_key = f"project:{project_name}"
        def _do():
            result = git_ops.checkout_paths(project_path, sha, [file_path])
            if not result.success:
                self._GLib.idle_add(lambda sk=session_key: self._on_display_text(sk, f"Failed to revert {file_path}: {result.error}"))
                return

            self._GLib.idle_add(lambda sk=session_key: self._on_display_text(sk, f"↩ {file_path} reverted to checkpoint"))

        threading.Thread(target=_do, daemon=True).start()

    def revert_file_to_sha(self, project_name: str, file_path: str, target_sha: str,
                           on_complete: Callable[[], None] | None = None) -> None:
        """Revert a single file to its state at an arbitrary commit SHA.

        Unlike reject_file() (which requires an active review session and reverts
        to checkpoint_sha), this method works on any commit.

        M17 fix: Requires an active review session for safety — reverting outside
        a review session risks losing untracked work without audit trail.

        H6 fix: Uses state.project_path (per-project lookup), not
        get_active_project_path() which could return a different project's path.

        SHA validation is handled by git_ops.checkout_paths() via _VALID_SHA_RE.

        on_complete: Optional callback (zero args) called on main thread after revert succeeds.
        """
        # M17 fix: require active review session
        state = self._states.get(project_name)
        if state is None or not state.is_active():
            session_key = f"project:{project_name}"
            self._GLib.idle_add(lambda sk=session_key: self._on_display_text(
                sk, "⚠ Revert requires an active review session. Run /review first."))
            return

        # H6 fix: use state.project_path, not get_active_project_path()
        project_path = state.project_path

        session_key = f"project:{project_name}"

        def _do():
            result = git_ops.checkout_paths(project_path, target_sha, [file_path])
            if not result.success:
                self._GLib.idle_add(lambda sk=session_key: self._on_display_text(
                    sk, f"⚠ Failed to revert {file_path}: {result.error}"))
                return

            self._GLib.idle_add(lambda sk=session_key: self._on_display_text(
                sk, f"↩ {file_path} reverted to {target_sha[:7]}"))
            # BUG #1: fire on_complete callback on main thread after success
            if on_complete is not None:
                self._GLib.idle_add(lambda cb=on_complete: cb())

        threading.Thread(target=_do, daemon=True).start()

    def _send_rejection_messages(self, project_name: str, reason: str, sha: str) -> None:
        """Send rejection reason to all project members via the local runtime path.

        Thread contract (FIX 7.2): this runs on a background thread but
        AgentRuntimeHandler is documented main-thread — so each member send is
        marshalled via GLib.idle_add when GLib is available, mirroring how the
        rest of ReviewHandler dispatches. Without GLib (tests) we call directly.
        Per-member error isolation (FIX 6.1) is preserved: one failure logs a
        WARNING and continues, it never aborts the loop.
        """
        if self._agent_runtime_handler is None:
            return
        members = self._ph.get_project_members(project_name)
        message = f"Changes rejected: {reason}. Files reverted to checkpoint {sha[:7]}."

        def _send_to(member: str, msg: str) -> None:
            # Local path only (SPEC-05 R1); receiver no-ops for
            # unregistered/remote keys.
            self._agent_runtime_handler.send_to_special_agent(member, msg)

        def _send_isolated(member: str = "", msg: str = "") -> None:
            try:
                _send_to(member, msg)
            except Exception as exc:
                _logger.warning(
                    "review: rejection message to %s failed: %s", member, exc
                )

        for member in members:
            if self._GLib is not None:
                # Marshal to the GTK main thread — ARH is main-thread-documented.
                self._GLib.idle_add(lambda m=member: _send_isolated(m, message))
            else:
                _send_isolated(member, message)

    def set_agent_runtime_handler(self, handler) -> None:
        """Inject AgentRuntimeHandler — the local send path for rejection
        messages (SPEC-05 R1). Called by window.py after ARH is built."""
        self._agent_runtime_handler = handler

    def get_state(self, project_name: str) -> ReviewState | None:
        """Get current review state for a project."""
        return self._states.get(project_name)

    # ── Project lifecycle hooks ─────────────────────────────────────────

    def cmd_review(self, cmd: Command, session_key: str | None = None) -> CommandResult:
        """/review → start a review session"""
        sk = cmd.source_session_key
        if not sk.startswith("project:"):
            return CommandResult(handled=True, response_text="Open a project tab first.")
        project_name = sk.split(":", 1)[1]
        # Enable review mode first — can_checkpoint() requires review_mode == "review"
        self.set_review_mode(project_name, "review")
        self.start_review(project_name, sk)
        return CommandResult(handled=True, response_text="Starting review...")

    def cmd_check(self, cmd: Command, session_key: str | None = None) -> CommandResult:
        """/check → check changes since checkpoint"""
        sk = cmd.source_session_key
        if not sk.startswith("project:"):
            return CommandResult(handled=True, response_text="Open a project tab first.")
        project_name = sk.split(":", 1)[1]
        self.check_changes(project_name, sk)
        return CommandResult(handled=True, response_text="Checking changes...")

    def cmd_accept(self, cmd: Command, session_key: str | None = None) -> CommandResult:
        """/accept → accept all changes"""
        sk = cmd.source_session_key
        if not sk.startswith("project:"):
            return CommandResult(handled=True, response_text="Open a project tab first.")
        project_name = sk.split(":", 1)[1]
        body = " ".join(cmd.args) or "approved"
        self.accept_changes(project_name, body, sk)
        return CommandResult(handled=True, response_text="Accepting changes...")


    def cmd_reject(self, cmd: Command, session_key: str | None = None) -> CommandResult:
        """/reject → reject all changes"""
        sk = cmd.source_session_key
        if not sk.startswith("project:"):
            return CommandResult(handled=True, response_text="Open a project tab first.")
        project_name = sk.split(":", 1)[1]
        reason = cmd.body or "rejected"
        self.reject_changes(project_name, reason, sk)
        return CommandResult(handled=True, response_text="Rejecting changes...")

    def on_project_opened(self, project_name: str, project_path: str) -> None:
        """Called when a project tab opens. Initializes ReviewState."""
        if project_name not in self._states:
            self._states[project_name] = ReviewState(
                project_path=project_path,
                review_mode="off",
            )
        else:
            # Update project path in case it changed
            self._states[project_name].project_path = project_path

    def on_project_closed(self, project_name: str) -> None:
        """Called when a project tab closes. Cleans up ReviewState."""
        self._states.pop(project_name, None)

    def on_project_members_changed(self, project_name: str, members: list[str]) -> None:
        """Called when project membership changes. Updates rejection message targets."""
        # State is already keyed by project_name; no action needed here
        pass
