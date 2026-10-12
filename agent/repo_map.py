"""Python reference graph for the wire prompt.

The block is attached by the runtime. It is not part of conv.system_prompt.
Order is the graph's own rank. The decision model is not called here: a noul
answer without confidence cannot pass the rerank gate, and the call blocked
every user message. A missing grammar returns "".
"""
from __future__ import annotations

import logging
import os
import re
import threading

_logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 1_000_000
_CACHE_CAP = 20_000
_TOP_N = 30
_TOKEN_BUDGET = 2_000
_LINE_CAP = 200
_DAMPING = 0.85
_ITERATIONS = 20
_PRUNE_DIRS = frozenset({
    "__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".tox",
})
_QUERY = """
(function_definition name: (identifier) @def)
(class_definition name: (identifier) @def)
(call function: (identifier) @ref)
(call function: (attribute attribute: (identifier) @ref))
"""
_GOAL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_PARSE_CACHE: dict[str, tuple] = {}
# One Parser and one compiled Query per thread. tree-sitter parsers are not
# shared across threads; building them once per thread avoids 362 constructions
# on a cold walk.
_PARSE_LOCAL = threading.local()
_LANG = None
_LANG_FAILED = False


def _language():
    """Load the Python grammar once. None after the first failure."""
    global _LANG, _LANG_FAILED
    if _LANG_FAILED:
        return None
    if _LANG is not None:
        return _LANG
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language
        _LANG = Language(tspython.language())
    except Exception:
        _logger.warning("tree-sitter python grammar unavailable", exc_info=True)
        _LANG_FAILED = True
        return None
    return _LANG


def _captures(raw):
    if isinstance(raw, dict):
        pairs = []
        for name, nodes in raw.items():
            for node in nodes:
                pairs.append((node, name))
        return pairs
    return list(raw)


def _parse_tools(lang):
    """Return (parser, compiled query) for this thread, building them once."""
    cached = getattr(_PARSE_LOCAL, "tools", None)
    if cached is not None and cached[0] is lang:
        return cached[1], cached[2]
    from tree_sitter import Parser
    parser = Parser(lang)
    if hasattr(lang, "query"):
        compiled = lang.query(_QUERY)
    else:
        from tree_sitter import Query
        compiled = Query(lang, _QUERY)
    _PARSE_LOCAL.tools = (lang, parser, compiled)
    return parser, compiled


def _parse_file(abs_path: str, rel_path: str, lang) -> tuple[list, list]:
    try:
        st = os.stat(abs_path)
    except OSError:
        return [], []
    if st.st_size > _MAX_FILE_BYTES:
        return [], []
    cached = _PARSE_CACHE.get(abs_path)
    if cached is not None and cached[0] == st.st_mtime:
        return cached[1], cached[2]
    try:
        with open(abs_path, "rb") as fh:
            source = fh.read()
    except OSError:
        return [], []
    try:
        parser, compiled = _parse_tools(lang)
        tree = parser.parse(source)
        if hasattr(lang, "query"):
            raw = compiled.captures(tree.root_node)
        else:
            from tree_sitter import QueryCursor
            raw = QueryCursor(compiled).captures(tree.root_node)
        pairs = _captures(raw)
    except Exception:
        _logger.debug("repo map parse failed for %s", rel_path, exc_info=True)
        return [], []
    text = source.decode("utf-8", errors="replace")
    lines = text.splitlines()
    defs = []
    refs = []
    for node, name in pairs:
        ident = node.text.decode("utf-8", errors="replace") if node.text else ""
        if not ident:
            continue
        if name == "def":
            row = node.start_point[0]
            line = lines[row].strip() if 0 <= row < len(lines) else ident
            defs.append((rel_path, ident, line[:_LINE_CAP]))
        elif name == "ref":
            refs.append((rel_path, ident))
    if len(_PARSE_CACHE) >= _CACHE_CAP:
        _PARSE_CACHE.clear()
    _PARSE_CACHE[abs_path] = (st.st_mtime, defs, refs)
    return defs, refs


def _walk(project_path: str, lang) -> tuple[list, list]:
    from agent.context import _is_ignored, _load_gitignore_patterns
    patterns = _load_gitignore_patterns(project_path)
    defs: list = []
    refs: list = []
    for root, dirs, files in os.walk(project_path):
        rel_root = os.path.relpath(root, project_path)
        if rel_root == ".":
            rel_root = ""
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".") and d not in _PRUNE_DIRS
        ]
        if rel_root and _is_ignored(rel_root, project_path, patterns):
            dirs[:] = []
            continue
        kept_dirs = []
        for d in dirs:
            rel_dir = f"{rel_root}/{d}" if rel_root else d
            if not _is_ignored(rel_dir, project_path, patterns):
                kept_dirs.append(d)
        dirs[:] = kept_dirs
        for name in files:
            if not name.endswith(".py"):
                continue
            rel = f"{rel_root}/{name}" if rel_root else name
            rel = rel.replace("\\", "/")
            if _is_ignored(rel, project_path, patterns):
                continue
            file_defs, file_refs = _parse_file(os.path.join(root, name), rel, lang)
            defs.extend(file_defs)
            refs.extend(file_refs)
    return defs, refs


def _rank(defs: list, refs: list, goal: str) -> list:
    by_name: dict[str, list[str]] = {}
    nodes: dict[str, tuple] = {}
    for rel, name, line in defs:
        node = f"{rel}:{name}"
        nodes[node] = (rel, name, line)
        by_name.setdefault(name, []).append(node)
    edges: set[tuple[str, str]] = set()
    for rel, name in refs:
        targets = by_name.get(name) or []
        if len(targets) != 1:
            continue
        src = rel
        dst = targets[0]
        if src == dst:
            continue
        edges.add((src, dst))
    goal_names = {m.group(0) for m in _GOAL_NAME.finditer(goal or "") if len(m.group(0)) >= 3}
    graph_nodes = set(nodes)
    graph_nodes.update(src for src, _dst in edges)
    if not nodes:
        return []
    personal = {}
    for node in graph_nodes:
        if node in nodes and nodes[node][1] in goal_names:
            personal[node] = 10.0
        else:
            personal[node] = 1.0
    total = sum(personal.values()) or 1.0
    personal = {node: weight / total for node, weight in personal.items()}
    out_degree: dict[str, int] = {}
    for src, _dst in edges:
        out_degree[src] = out_degree.get(src, 0) + 1
    score = dict(personal)
    for _ in range(_ITERATIONS):
        new = {node: (1 - _DAMPING) * personal[node] for node in graph_nodes}
        for src, dst in edges:
            degree = out_degree.get(src) or 1
            new[dst] = new.get(dst, 0.0) + _DAMPING * score.get(src, 0.0) / degree
        score = new
    ordered = sorted(nodes, key=lambda node: (-score.get(node, 0.0), node))
    return [(nodes[node][0], nodes[node][1], nodes[node][2]) for node in ordered[:_TOP_N]]


def _render(ranked: list) -> str:
    if not ranked:
        return ""
    parts: list[str] = []
    for rel, _name, line in ranked:
        piece = f"{rel}\n  {line}\n"
        if parts and (len("".join(parts)) + len(piece)) // 4 > _TOKEN_BUDGET:
            break
        parts.append(piece)
    return "".join(parts)


def build_repo_map(project_path: str, goal: str) -> str:
    """Signature block for ``goal``, or "" if there is nothing to show.

    Never raises. A missing grammar, an empty tree, or a decision-model
    failure still returns the graph's own top signatures, or "" when
    the tree has no Python definitions.
    """
    if not isinstance(project_path, str) or not project_path or not os.path.isdir(project_path):
        return ""
    if not isinstance(goal, str):
        goal = ""
    try:
        lang = _language()
        if lang is None:
            return ""
        defs, refs = _walk(project_path, lang)
        ranked = _rank(defs, refs, goal)
        if not ranked:
            return ""
        # Graph order only. The decision-model noul answer does not carry
        # confidence, so the 0.6 gate cannot pass and the POST was a blocking
        # no-op on every user message. classify_unknown still owns that client.
        return _render(ranked)
    except Exception:
        _logger.warning("build_repo_map failed", exc_info=True)
        return ""
