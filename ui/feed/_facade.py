"""Call-time lookup of names tests patch on the FeedHandler module."""


def _mod():
    import ui.handlers.feed_handler as facade
    return facade
