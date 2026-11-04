# agent/__init__.py
# Agent runtime package.
#
# SPEC-61 Phase C: the re-exports here had zero consumers — every
# `from agent import ...` in the codebase targets a submodule
# (agent.runtime, agent.tools, agent.context, ...). Import from the
# submodule that owns the symbol instead.
#
# Files in this package:
#   __init__.py       — this file (package marker)
#   config.py         — LLM provider configuration
#   tools.py          — tool definitions and execution
#   context.py        — system prompt + file context builder
#   runtime.py        — AgentRuntime (Phase 1.3a)
#   special_agents.py — Coder + Debugger definitions
#   enforcement.py    — policy enforcement checks
#   callbacks.py      — runtime callback protocols
#   context_strategy.py / context_edit.py — context tiering
#   group_tools.py    — group-surface tools
#   llm/              — provider adapters, streaming, registry
#   repo_map.py       — repository map
#   tool_middleware.py — tool middleware chain
