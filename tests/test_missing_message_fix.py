# tests/test_missing_message_fix.py — DELETED (SPEC-58 §2c / §2i).
#
# This file pinned ChatHandler's missing-message recovery cluster:
# _buffer_assistant_text / _assistant_text_buffer, the _chat_final_rendered
# guard, _clear_render_guard, and _handle_lifecycle_completed. The whole
# cluster is deleted — it was unreachable in production (the gateway event
# path it served was stripped in SPEC-05) and nothing could set
# _assistant_text_buffer. Local replies render through AgentRuntimeHandler
# and ChatRenderHandler.render_sync / render_async; those paths keep their
# own pins in tests/test_chat_render_handler.py and tests/test_agent_runtime.py.
