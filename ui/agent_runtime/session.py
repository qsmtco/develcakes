"""Session clear, compact, send, and stop for AgentRuntimeHandler."""
from __future__ import annotations

from ui.agent_runtime._facade import _mod


class RuntimeSessionMixin:
    def clear_conversation(self, session_key: str) -> bool:
        """Reset a special agent's conversation in place.

        Resets messages=[], step_count=0, total_tokens=0, total_cost=0.0,
        and invalidates the token-estimate cache. Also deletes the persisted
        conversation JSON so the next session start loads a fresh state.

        In-place reset (vs remove + recreate) avoids races with in-flight
        tool loops: a background thread may be reading conv.messages via
        the runtime's _run_loop; resetting the list is safer than
        deleting the conversation object and recreating it, because the
        object identity stays stable for the running thread.

        Returns True on success, False if the session isn't a registered
        special agent or has no runtime/conversation.

        Spec: docs/specs/STEP-COUNT-RESET-FIX.md Edit 4.
        """
        # Guard: only special-agent sessions can be cleared this way.
        # `special:coder`, `special:debugger`, `special:crabcakes`, etc.
        if not isinstance(session_key, str) or not session_key.startswith("special:"):
            _mod().logger.warning(
                "clear_conversation: refusing non-special session_key=%r",
                session_key,
            )
            return False

        agent_def = self._agents.get(session_key)
        if agent_def is None:
            _mod().logger.warning(
                "clear_conversation: no registered special agent for %s",
                session_key,
            )
            return False

        # SPEC-10 SP2b: the /clear teardown drops any stale attribution
        # snapshot — a cleared session must not checkpoint a pre-clear turn.
        self._turn_attr.pop(session_key, None)

        # Resolve the runtime that owns this session. Display name is the
        # key in self._runtimes; _get_runtime will lazily create one if
        # the agent has never been used yet (clear-before-first-use is a
        # legitimate no-op case).
        try:
            rt = self._get_runtime(agent_def.display_name, agent_def=agent_def)
        except Exception as exc:
            _mod().logger.error(
                "clear_conversation: failed to acquire runtime for %s: %s",
                session_key, exc,
            )
            return False

        # In-place reset. Keep the Conversation object identity so any
        # in-flight _run_loop thread continues to see the same object.
        # FIX-CLEAR-ASK-RACE: refuse to wipe a conversation that an in-flight
        # _run_loop is actively reading. The /clear + /ask pairing rule can
        # fire /clear while the /ask thread is between add_user_message and
        # to_api_messages; wiping conv.messages at that instant produces a
        # system-only payload that MiniMax rejects (status_code=2013). Refuse
        # instead; the user can retry /clear once the loop finishes.
        if rt.is_loop_active(session_key):
            _mod().logger.warning(
                "clear_conversation: refusing reset for %s — tool loop is active; retry after it completes",
                session_key,
            )
            return False

        conv = rt.get_conversation(session_key)
        if conv is not None:
            try:
                conv.messages = []
                conv.step_count = 0
                conv.total_tokens = 0
                conv.total_cost = 0.0
                # _token_estimate_cache is keyed on (len(messages), hash(system_prompt))
                # — messages are now empty, so the cache MUST be invalidated
                # or the next trim pass will read a stale value.
                conv._token_estimate_cache = None
            except Exception as exc:
                _mod().logger.error(
                    "clear_conversation: in-place reset failed for %s: %s",
                    session_key, exc,
                )
                return False
            _mod().logger.info(
                "clear_conversation: reset in-memory conversation for %s",
                session_key,
            )

        # Delete the persisted JSON so a restart doesn't restore the old
        # state. Best-effort: a missing file is fine (nothing to delete),
        # other OSErrors are logged but don't fail the whole operation —
        # the in-memory state is already cleared.
        try:
            from utils.config import get_config_dir
            import os
            conv_dir = os.path.join(get_config_dir(), "conversations")
            conv_path = os.path.join(conv_dir, f"{session_key}.json")
            os.remove(conv_path)
            _mod().logger.info(
                "clear_conversation: deleted persisted conversation %s",
                conv_path,
            )
        except FileNotFoundError:
            pass  # No persisted file — that's fine.
        except OSError as exc:
            _mod().logger.warning(
                "clear_conversation: could not delete persisted file for %s: %s",
                session_key, exc,
            )

        # SPEC-08 store-mode load (SP4A): the JSON's absence makes the store
        # the sole arbiter of history — without this, the next load
        # resurrects the cleared conversation from transcript.db rows (the
        # D2 delete_session is the store's designed surface for this).
        # Best-effort, mirroring the JSON-delete tolerance: a store failure
        # logs but does not fail the whole clear (in-memory is already reset).
        try:
            from agent.persistence import delete_session_rows
            removed = delete_session_rows(session_key)
            _mod().logger.info(
                "clear_conversation: deleted %d transcript-store rows for %s",
                removed, session_key,
            )
        except Exception as exc:  # noqa: BLE001 — clear must not fail wholesale
            _mod().logger.warning(
                "clear_conversation: could not delete store rows for %s: %s",
                session_key, exc,
            )

        return True

    def compact_conversation(
        self, session_key: str, focus_text: str = ""
    ) -> dict:
        """Force compaction of a special agent's conversation.

        Spec: docs/specs/SPEC-CONTEXT-UI-COMPACT-LLM-2026-07-10.md §3.2.

        Args:
            session_key: "special:coder" etc.
            focus_text: Optional focus instructions for Phase C's LLM
                strategy (Phase B's textual strategy ignores this).

        Returns:
            dict with keys:
                messages_removed (int)
                tokens_freed (int)
                summary_chars (int)
                layer (int)

        Returns an empty dict {"messages_removed": 0, ...} on failure.
        """
        if not isinstance(session_key, str) or not session_key.startswith("special:"):
            _mod().logger.warning(
                "compact_conversation: refusing non-special session_key=%r",
                session_key,
            )
            return {"messages_removed": 0, "tokens_freed": 0, "summary_chars": 0, "layer": 0}

        agent_def = self._agents.get(session_key)
        if agent_def is None:
            _mod().logger.warning(
                "compact_conversation: no registered special agent for %s",
                session_key,
            )
            return {"messages_removed": 0, "tokens_freed": 0, "summary_chars": 0, "layer": 0}

        try:
            rt = self._get_runtime(agent_def.display_name, agent_def=agent_def)
        except Exception as exc:
            _mod().logger.error(
                "compact_conversation: failed to acquire runtime for %s: %s",
                session_key, exc,
            )
            return {"messages_removed": 0, "tokens_freed": 0, "summary_chars": 0, "layer": 0}

        conv = rt.get_conversation(session_key)
        if conv is None:
            return {"messages_removed": 0, "tokens_freed": 0, "summary_chars": 0, "layer": 0}

        try:
            _, hard_ceiling = rt._compute_compaction_threshold(conv)
        except Exception:
            _mod().logger.exception(
                "compact_conversation: failed to resolve hard_ceiling; using 128K"
            )
            hard_ceiling = 128_000

        target_budget = max(4_000, hard_ceiling // 2)
        messages_before = len(conv.messages)
        tokens_before = conv.get_token_estimate()

        strat_name = getattr(agent_def, "compaction_strategy", "textual")
        if strat_name == "llm" and hasattr(rt, "force_llm_compact"):
            # Phase C path — fires when force_llm_compact exists and
            # agent_def.compaction_strategy == "llm".
            try:
                return rt.force_llm_compact(conv, target_budget, focus_text, agent_def=agent_def)
            except Exception:
                _mod().logger.exception(
                    "compact_conversation: LLM strategy failed; "
                    "falling back to textual"
                )
                # Fall through to textual default.

        rt.force_compact(conv, target_budget)

        try:
            from agent.persistence import save_conversation_to_disk
            save_conversation_to_disk(conv, session_key)
        except Exception:
            _mod().logger.exception(
                "compact_conversation: persist failed; in-memory compact succeeded"
            )

        ev = rt._context_strategy.last_result
        tokens_after = conv.get_token_estimate()
        if ev is None:
            return {
                "messages_removed": 0,
                "tokens_freed": max(0, tokens_before - tokens_after),
                "summary_chars": 0,
                "layer": 0,
            }
        return {
            "messages_removed": ev.messages_removed,
            "tokens_freed": ev.tokens_freed,
            "summary_chars": ev.summary_tokens_injected,
            "layer": ev.layer,
        }

    def send_to_special_agent(self, session_key: str, text: str,
                              reply_target: str | None = None) -> None:
        """
        Send a user message to a special agent for processing.

        Called by ChatHandler.on_send() when the target tab is a special agent.

        Requires an active project — special agents are project-scoped.

        Args:
            session_key: the agent session key.
            text: the message text.
            reply_target: SPEC-12 (R5 REV 3) — the DISPLAY key this turn's
                reply renders under. `/ask`+`/delegate`/bubble-forward pass
                the agent's own key (private view); group fan-out passes
                "project:<name>"; None → the routing key (the project).
                Never a persistent mark — cleared at turn end (SP3b).
        """
        agent_def = self._agents.get(session_key)
        if agent_def is None:
            _mod().logger.warning(
                "send_to_special_agent: %s is not a registered special agent",
                session_key,
            )
            return

        # Special agents require an active project.
        # (The KB-helper carve-out died with the KB stack, SPEC-04.)
        if self._active_project is None:
            # SPEC-12 SP3a-audit BUG#1: this guard DOES render (an error
            # bubble via _do_error, whose _resolve_chat_box is slot-first) —
            # a stale slot from a prior /ask would shadow the session's own
            # tab and drop/misroute that error. Clear it so the error
            # resolves to the session's own tab (pre-SP3a behavior).
            self._turn_reply_target.pop(session_key, None)
            if self._GLib is not None:
                self._GLib.idle_add(self._do_error, session_key,
                                    "Open a project first. Special agents work within projects.")
            else:
                self._do_error(session_key,
                               "Open a project first. Special agents work within projects.")
            return

        # SPEC-12 (BUG#18/#26): ALWAYS normalize the slot for a send that will
        # actually run — a non-targeting caller defaults to the routing
        # (project) key, so no stale private target survives. Placed AFTER
        # both early-return guards so the slot is set only for sends that run
        # a turn. (The no-project guard above instead CLEARS the slot — its
        # error render is slot-consuming; SP3a-audit BUG#1.)
        self._turn_reply_target[session_key] = (
            reply_target if reply_target is not None
            else self._resolve_mount_key(session_key)
        )

        if self._active_project is not None:
            project_name, project_path = self._active_project
        else:
            project_name, project_path = "(none)", None
        rt = self._get_runtime(agent_def.display_name, agent_def=agent_def)

        _mod().logger.debug("[handler] send_to_special_agent: sk=%s agent=%s project=%s text_len=%d",
                     session_key, agent_def.display_name, project_name, len(text))

        # Resolve per-agent model override (Step 4 — user-defined agents)
        agent_model = self._resolve_agent_model(agent_def)

        # Resolve per-agent SI enforcement flag
        si_enforcement = None
        if hasattr(agent_def, 'get_self_improvement_config'):
            si_cfg = agent_def.get_self_improvement_config()
            si_enforcement = si_cfg.get('enforcement')

        # Create conversation if it doesn't exist yet, with project context and filtered tools.
        # First try to load the persisted conversation from disk (preserves message history,
        # token/cost data, and other state across app restarts). Only create fresh if no
        # persisted conversation exists.
        #
        # Phase 4 Part B (SPEC-UI-RESPONSIVENESS-2 §2.4): this whole block —
        # plus the per-agent state sync and step-count reset below — now runs
        # on the RUNTIME LOOP thread via the `prepare` callback handed to
        # send_message(), not on the GTK main thread (see
        # _prepare_turn_conversation).
        #
        # RACE-FIX v4: Clear the ended/completed flags and assign a NEW turn
        # token for this session. This is the ONLY place these should be
        # cleared. The new token ensures stale deltas from the previous turn
        # (which captured the OLD token) are rejected by the token mismatch
        # check in _do_text_delta. It is assigned HERE, on the main thread,
        # BEFORE the loop thread starts — the loop captures it via
        # send_message() and _prepare_turn_conversation checks it.
        self._ended_sessions.discard(session_key)
        self._session_completed.discard(session_key)
        # RACE-FIX v4b: Assign a new turn token ON THE RUNTIME object.
        # _dispatch captures this token at call time (background thread, stable).
        # The handler's _on_* callbacks receive it as a kwarg — they don't
        # read from a mutable dict that could change between dispatch and execution.
        new_token = object()
        self._turn_tokens[session_key] = new_token
        rt._turn_token = new_token

        # Phase 4 Part B: the main thread keeps only the turn-token setup and
        # the thread start. Everything that used to block here (conversation
        # disk load + deserialize, project reconciliation, per-agent state
        # sync, step-count reset) runs on the loop thread.
        def _prepare_turn() -> None:
            self._prepare_turn_conversation(
                rt=rt,
                session_key=session_key,
                agent_def=agent_def,
                project_path=project_path,
                project_name=project_name,
                agent_model=agent_model,
                si_enforcement=si_enforcement,
                turn_token=new_token,
            )

        rt.send_message(session_key, text, prepare=_prepare_turn)

    def stop_all(self) -> None:
        """Stop all agent runtimes. Called on window shutdown."""
        # BUG #31: Clean up MCP connections before stopping runtimes
        try:
            from utils.mcp_client import disconnect_all as mcp_disconnect_all
            mcp_disconnect_all()  # Clean up all MCP connections across all conversations
        except Exception:
            pass  # Best effort — daemon threads die on exit anyway

        for name, rt in list(self._runtimes.items()):
            rt.stop()
        self._runtimes.clear()

