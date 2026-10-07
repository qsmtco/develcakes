# ui/constants.py
# Cross-cutting UI constants used by both views and handlers.
#
# Architecture rule (ARCHITECTURE.md §8.6 R7):
#   Views must not import from ui/handlers/. To share state between a view
#   and a handler, put the constant here. Both sides import from this neutral
#   module.
#
# Mutable state lives here, not on handler classes, when both the view and
#   the handler need to read AND write it. For one-way state (handler-only or
#   view-only), pass via constructor or setter from ui/window.py instead.

# Streaming: the local agent runtime paints one row at turn end (the
# AgentRuntimeHandler buffer path). The old STREAMING_ENABLED toggle + Stream
# toolbar button were removed 2026-10-07 — the flag's only reader was the
# dead gateway event path (on_chat_event, gateway stripped SPEC-05), so the
# toggle never affected local-agent rendering.
