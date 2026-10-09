"""Call-time lookup of names tests patch on ui.views.file_tree."""


def _mod():
    import ui.views.file_tree as facade
    return facade
