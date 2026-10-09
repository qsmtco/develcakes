"""Call-time lookup of names tests patch on AgentRuntimeHandler's module."""


def _mod():
    import ui.handlers.agent_runtime_handler as facade
    return facade
