"""Session clear, compact, send, and stop for AgentRuntimeHandler."""
from __future__ import annotations

from ui.agent_runtime._facade import _mod


def _estimate(conv) -> int:
    try:
        return int(conv.get_token_estimate())
    except Exception:
        return 0


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
        # Guard: only local-agent sessions can be cleared this way.
        # `agent:coder`, `agent:debugger`, `agent:crabcakes`, etc.
        if not isinstance(session_key, str) or not session_key.startswith("agent:"):
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
                # _token_estimate_cache is keyed on
                # (len(messages), hash(system_prompt), hash(task_card))
                # — messages are now empty, so the cache MUST be invalidated
                # or the next trim pass will read a stale value.
                conv.task_card = ""
                conv.invalidate_token_estimate()
                rt.drop_read_cache(session_key)
                rt.drop_compile(session_key)
                rt.drop_repo_map(session_key)
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

        return True

    def compact_conversation(
        self, session_key: str, focus_text: str = "", *, protect_open_turn: bool = False,
    ) -> dict:
        """Force compaction of a special agent's conversation.

        Spec: docs/specs/SPEC-CONTEXT-UI-COMPACT-LLM-2026-07-10.md §3.2.

        Args:
            session_key: "agent:coder" etc.
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
        if not isinstance(session_key, str) or not session_key.startswith("agent:"):
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
                result = rt.force_llm_compact(
                    conv,
                    target_budget,
                    focus_text,
                    agent_def=agent_def,
                    preserve_current_turn=protect_open_turn,
                )
            except Exception:
                _mod().logger.exception(
                    "compact_conversation: LLM strategy failed; "
                    "falling back to textual"
                )
                # Fall through to textual default.
            else:
                result["persisted"] = self._persist_context(conv, session_key)
                return result

        rt.force_compact(
            conv, target_budget, preserve_current_turn=protect_open_turn,
        )

        ev = rt._context_strategy.last_result
        tokens_after = conv.get_token_estimate()
        if ev is None:
            result = {
                "messages_removed": 0,
                "tokens_freed": max(0, tokens_before - tokens_after),
                "summary_chars": 0,
                "layer": 0,
            }
        else:
            result = {
                "messages_removed": ev.messages_removed,
                "tokens_freed": ev.tokens_freed,
                "summary_chars": ev.summary_tokens_injected,
                "layer": ev.layer,
            }
        result["persisted"] = self._persist_context(conv, session_key)
        return result

    def _persist_context(self, conv, session_key: str) -> bool:
        """Write the live conversation back to its file. Returns False on failure."""
        try:
            from agent.context_edit import persist_conversation
            persist_conversation(conv, session_key)
            return True
        except Exception:
            _mod().logger.exception(
                "context persist failed for %s; in-memory edit stands",
                session_key,
            )
            return False

    def _owned_conversation(self, session_key: str):
        """Return (conversation, runtime, error). error is a string or None."""
        if not isinstance(session_key, str) or not session_key.startswith("agent:"):
            return None, None, "not a special agent"
        agent_def = self._agents.get(session_key)
        if agent_def is None:
            return None, None, "no registered agent"
        try:
            rt = self._get_runtime(agent_def.display_name, agent_def=agent_def)
        except Exception as exc:
            _mod().logger.error(
                "context edit: failed to acquire runtime for %s: %s",
                session_key, exc,
            )
            return None, None, "could not reach the conversation"
        conv = rt.get_conversation(session_key)
        if conv is None:
            return None, None, "no conversation yet"
        return conv, rt, None

    def inspect_conversation(
        self, session_key: str, *, protect_open_turn: bool = False,
    ) -> dict:
        """Numbered outline of a conversation. Does not mutate or stop the loop."""
        conv, _rt, err = self._owned_conversation(session_key)
        if err is not None:
            return {"ok": False, "error": err}
        from agent.context_edit import outline_messages
        return {
            "ok": True,
            "output": outline_messages(conv.messages, protect_open_turn=protect_open_turn),
        }

    def plan_conversation_edit(
        self,
        session_key: str,
        spec: dict | None,
        *,
        protect_open_turn: bool = False,
    ) -> dict:
        """Check an edit against the current messages. Does not mutate."""
        conv, _rt, err = self._owned_conversation(session_key)
        if err is not None:
            return {"ok": False, "error": err}
        remove, plan_err = self._plan_spec(conv, spec, protect_open_turn)
        if plan_err is not None:
            return {"ok": False, "error": plan_err}
        return {"ok": True, "remove": sorted(remove)}

    def edit_conversation(
        self,
        session_key: str,
        spec: dict | None,
        *,
        protect_open_turn: bool = False,
    ) -> dict:
        """Drop or keep messages, then write the conversation file.

        A self-edit runs inside the caller's tool loop, so it does not
        wait for that loop to go idle. ``protect_open_turn`` keeps the
        tool call that is still running. A peer edit refuses while that
        peer's loop is still active.
        """
        conv, rt, err = self._owned_conversation(session_key)
        if err is not None:
            return {"ok": False, "error": err, "messages_removed": 0}
        if not protect_open_turn and rt.is_loop_active(session_key):
            return {"ok": False, "error": "peer loop still active", "messages_removed": 0}
        remove, plan_err = self._plan_spec(conv, spec, protect_open_turn)
        if plan_err is not None:
            return {"ok": False, "error": plan_err, "messages_removed": 0}
        return self._commit_context_edit(rt, conv, session_key, remove)

    def trim_own_conversation(self, session_key: str) -> dict:
        """Drop finished messages and write the conversation file.

        Used when an agent clears itself from inside its own tool loop.
        The current request and the running tool call stay, so the loop
        can finish.
        """
        conv, rt, err = self._owned_conversation(session_key)
        if err is not None:
            return {"ok": False, "error": err, "messages_removed": 0}
        from agent.context_edit import plan_clear_before_open_turn
        return self._commit_context_edit(
            rt, conv, session_key, plan_clear_before_open_turn(conv.messages),
        )

    def _plan_spec(self, conv, spec: dict | None, protect_open_turn: bool):
        from agent.context_edit import plan_context_edit
        drop = None if not isinstance(spec, dict) else spec.get("drop")
        keep = None if not isinstance(spec, dict) else spec.get("keep")
        return plan_context_edit(
            conv.messages,
            drop=drop,
            keep=keep,
            protect_open_turn=protect_open_turn,
        )

    def _commit_context_edit(self, rt, conv, session_key: str, remove: set[int]) -> dict:
        lock = getattr(rt, "_compaction_lock", None)
        if lock is None:
            return self._commit_context_edit_unlocked(conv, session_key, remove)
        with lock:
            return self._commit_context_edit_unlocked(conv, session_key, remove)

    def _commit_context_edit_unlocked(self, conv, session_key: str, remove: set[int]) -> dict:
        from agent.context_edit import format_indexes, kept_messages
        kept_n = len(conv.messages) - len(remove)
        removed = sorted(remove)
        if not remove:
            return {
                "ok": True,
                "messages_removed": 0,
                "messages_kept": len(conv.messages),
                "removed": [],
                "removed_text": "none",
                "tokens_freed": 0,
                "persisted": False,
                "error": "",
            }
        before = _estimate(conv)
        conv.messages[:] = kept_messages(conv.messages, remove)
        conv.invalidate_token_estimate()
        after = _estimate(conv)
        persisted = self._persist_context(conv, session_key)
        conv.invalidate_token_estimate()
        error = "" if persisted else "in-memory edit applied; the conversation file was not written"
        return {
            "ok": persisted,
            "messages_removed": len(remove),
            "messages_kept": kept_n,
            "removed": removed,
            "removed_text": format_indexes(removed),
            "tokens_freed": max(0, before - after),
            "persisted": persisted,
            "error": error,
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
                reply renders under. `/ask`/bubble-forward pass
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

        # Obtain the runtime and claim the session BEFORE writing the reply
        # target: a queued send must not rotate the running turn's target or
        # token, and a failed _get_runtime (no provider / no API key) must
        # not leave a new reply target on a send that did not start
        # (SPEC-WIRE-CONTEXT-PRESSURE §2.3).
        rt = self._get_runtime(agent_def.display_name, agent_def=agent_def)
        if not rt.begin_send(session_key, text):
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

        # The session is already claimed by begin_send above; do not call
        # send_message (it would begin_send again). Start the loop directly
        # with the new token (SPEC-WIRE-CONTEXT-PRESSURE §2.3).
        rt.start_claimed_loop(session_key, text, new_token, _prepare_turn)

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

