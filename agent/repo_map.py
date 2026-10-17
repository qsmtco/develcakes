"""Python reference graph for the wire prompt.

The block is attached by the runtime. It is not part of conv.system_prompt.
Dirty files rank first and the shown signature follows the current edit.
Clean files stay in graph order. The decision model is not called here: a
noul answer without confidence cannot pass the rerank gate, and the call
blocked every user message. A missing grammar returns "".
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading

_logger = logging.getLogger(__name__)

_MAX_FILE_BYTES = 1_000_000
_CACHE_CAP = 20_000
_TOP_N = 30
_TOKEN_BUDGET = 2_000
_LINE_CAP = 200
_RECENCY_COMMITS = 50        # commits considered "recent" for the recency bonus
_RECENCY_WEIGHT = 3.0        # personalization weight for recently-committed files
_STUB_PENALTY = 0.1          # multiplier for mock/stub defs in test files
# SPEC-§6: every tests/ path sorts behind product code. A test node keeps
# whatever score the graph gave it, multiplied down, so a goal that names a
# test file or a test function can still lift it into view. Demote, never drop.
_TEST_DIR_PENALTY = 0.25
_TEST_DIR = "tests/"
_DIRTY_WEIGHT = 50.0
_HUNK_LINE_CAP = 20_000
_DELETED_SIG = "(deleted)"
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
_HUNK_HEADER = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_STUB_NAMES = {
    "open", "set", "get", "emit", "wait", "join", "start", "stop",
    "set_markup", "set_child", "set_label", "get_next_sibling",
    "get_text", "show_all", "queue_draw", "Thread", "Label", "Box", "Mock",
}

_PARSE_CACHE: dict[str, tuple] = {}
# One Parser and one compiled Query per thread. tree-sitter parsers are not
# shared across threads; building them once per thread avoids 362 constructions
# on a cold walk.
_PARSE_LOCAL = threading.local()
_LANG = None
_LANG_FAILED = False

# SPEC-§6 walk cache. Keyed on (project_path, tree_fingerprint). Holds the
# goal-independent inputs to a build: the def/ref walk, the git metadata, and
# the derived graph (nodes, by_name, edges). A new goal re-enters with the same
# fingerprint and only re-ranks and re-renders. The fingerprint is a stat
# sweep, so it is far cheaper than the walk it guards and it changes on a
# commit, a checkout, or any on-disk edit.
_WALK_CACHE: dict[tuple, _WalkInputs] = {}
_WALK_CACHE_CAP = 4
# Guards _WALK_CACHE for reads, writes, and eviction. build_repo_map can be
# called from the worker thread and the GTK main thread at the same time; a
# torn or missing entry must never produce a wrong map, only a slower one.
_WALK_CACHE_LOCK = threading.Lock()


class _WalkInputs:
    """Goal-independent build inputs, computed once per tree fingerprint.

    Attributes are populated in ``_build_walk_inputs``; this class exists so
    the cache stores one object per (project, fingerprint) rather than a loose
    tuple that every caller has to index.
    """

    __slots__ = (
        "anchors",
        "by_name",
        "defs",
        "deleted",
        "dirty",
        "edge_index",
        "edges",
        "graph_nodes",
        "node_pos",
        "nodes",
        "out_degree",
        "present",
        "recent",
        "refs",
    )

    def __init__(self) -> None:
        self.defs: list = []
        self.refs: list = []
        self.dirty: set[str] = set()
        self.recent: set[str] = set()
        self.anchors: dict = {}
        self.present: set[str] = set()
        self.deleted: set[str] = set()
        self.nodes: dict[str, tuple] = {}
        self.by_name: dict[str, list[str]] = {}
        self.edges: list[tuple[str, str]] = []
        self.graph_nodes: list[str] = []
        self.out_degree: dict[str, int] = {}
        # SPEC-§6 warm-path index, derived once per tree fingerprint so
        # ``_rank_inputs`` never re-resolves node names inside the PageRank
        # loop. ``edge_index`` is (src_pos, dst_pos, out_degree) aligned with
        # ``edges``; ``node_pos`` maps a graph node to its vector slot.
        self.edge_index: list[tuple[int, int, int]] = []
        self.node_pos: dict[str, int] = {}


def _tree_fingerprint(project_path: str) -> str:
    """Cheap stat-only identity for the current tree state.

    Deliberately does not shell out to git: it must stay cheaper than the walk
    it guards, and a HEAD change alters every tracked file's stat, so commits
    and checkouts are caught without paying for a subprocess. When stat is
    unavailable or the sweep is empty the fingerprint still changes, so the
    worst case is a rebuild rather than a stale map.
    """
    parts: list[tuple] = []
    try:
        for root, dirs, files in os.walk(project_path):
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".") and d not in _PRUNE_DIRS
            ]
            rel_root = os.path.relpath(root, project_path)
            if rel_root == ".":
                rel_root = ""
            for name in files:
                if not name.endswith(".py"):
                    continue
                abs_path = os.path.join(root, name)
                try:
                    st = os.stat(abs_path)
                except OSError:
                    parts.append((abs_path, None))
                    continue
                parts.append((rel_root, name, st.st_mtime_ns, st.st_size))
    except OSError:
        return "unreadable"
    if not parts:
        return "empty"
    digest = hash(tuple(sorted(parts)))
    return f"{len(parts)}:{digest}"


def _build_walk_inputs(project_path: str) -> _WalkInputs:
    """Run the expensive goal-independent half of a build.

    This is the cold path: git metadata, diff anchors, the tree-sitter walk,
    and the graph derived from it. ``build_repo_map`` reaches this only on a
    fingerprint miss.
    """
    inputs = _WalkInputs()
    lang = _language()
    if lang is None:
        return inputs
    inputs.dirty, inputs.recent = _git_metadata(project_path)
    inputs.anchors = _diff_anchors(project_path, inputs.dirty)
    inputs.defs, inputs.refs = _walk(project_path, lang)
    inputs.present = {rel for rel, _name, _sig, _line_no in inputs.defs}
    for rel in inputs.dirty:
        if rel in inputs.present:
            continue
        abs_path = os.path.join(project_path, *rel.split("/"))
        if not os.path.isfile(abs_path):
            inputs.deleted.add(rel)
    nodes = inputs.nodes
    by_name = inputs.by_name
    for rel, name, line, line_no in inputs.defs:
        node = f"{rel}:{name}"
        nodes[node] = (rel, name, line, line_no)
        by_name.setdefault(name, []).append(node)
    for rel in inputs.deleted:
        if not isinstance(rel, str) or not rel:
            continue
        node = f"{rel}:{_DELETED_SIG}"
        nodes.setdefault(node, (rel, _DELETED_SIG, _DELETED_SIG, 0))
    edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rel, name in inputs.refs:
        targets = by_name.get(name) or []
        if len(targets) != 1:
            continue
        dst = targets[0]
        if rel == nodes[dst][0]:
            continue
        key = (rel, dst)
        if key in seen:
            continue
        seen.add(key)
        edges.append(key)
    inputs.edges = edges
    graph_nodes = set(nodes)
    graph_nodes.update(src for src, _dst in edges)
    inputs.graph_nodes = sorted(graph_nodes)
    out_degree = inputs.out_degree
    for src, _dst in edges:
        out_degree[src] = out_degree.get(src, 0) + 1
    return inputs


def _walk_inputs(project_path: str) -> _WalkInputs:
    """Cached goal-independent inputs for ``project_path``.

    The fingerprint is computed under the lock, then the cache is checked and
    possibly repopulated. The lock is held across the cold build so two threads
    arriving together do not both walk the tree; the second one then finds a
    warm entry and returns immediately.
    """
    fingerprint = _tree_fingerprint(project_path)
    key = (project_path, fingerprint)
    with _WALK_CACHE_LOCK:
        cached = _WALK_CACHE.get(key)
        if cached is not None:
            return cached
        inputs = _build_walk_inputs(project_path)
        if len(_WALK_CACHE) >= _WALK_CACHE_CAP:
            # Drop the oldest insertion; dicts preserve insertion order. The
            # previous fingerprint for the same project is now stale anyway.
            oldest = next(iter(_WALK_CACHE))
            _WALK_CACHE.pop(oldest, None)
        _WALK_CACHE[key] = inputs
        return inputs


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


def _looks_like_stub(name: str) -> bool:
    return name in _STUB_NAMES


def _git_argv(project_path: str, *args: str) -> list[str]:
    """git argv with quoting disabled so paths match the walk byte for byte.

    Default ``core.quotePath`` renders ``café.py`` as ``"caf\\303\\251.py"``.
    That string does not match the path ``os.walk`` yields, so the file
    would miss the dirty-file boost.
    """
    return ["git", "-C", project_path, "-c", "core.quotePath=false", *args]


def _git_prefix(project_path: str) -> str:
    """Path of project_path inside the work tree, with a trailing slash.

    ``git status`` and ``git diff`` print paths from the work-tree root even
    when ``-C`` is a subdirectory. The walk's paths are relative to
    project_path, so callers strip this prefix. Empty at the work-tree root
    and when git is unavailable.
    """
    try:
        proc = subprocess.run(
            _git_argv(project_path, "rev-parse", "--show-prefix"),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _logger.debug("repo map git prefix failed: %s", exc)
        return ""
    prefix = (proc.stdout or "").strip().replace("\\", "/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return prefix


def _normalize_git_path(raw: str, prefix: str) -> str | None:
    """Repo-relative git path → project-relative POSIX path, or None.

    Keeps ``.py`` files inside the project prefix. Rename rows keep the
    post-rename path. Surrounding quotes from porcelain are removed.
    """
    if not isinstance(raw, str):
        return None
    path = raw.strip().strip('"').replace("\\", "/")
    if not path or path == "/dev/null":
        return None
    if prefix:
        if not path.startswith(prefix):
            return None
        path = path[len(prefix):]
    if not path.endswith(".py"):
        return None
    return path


def _git_metadata(project_path: str) -> tuple[set[str], set[str]]:
    """(dirty_paths, recent_paths) relative POSIX paths. Read-only git.

    Never raises; returns (set(), set()) if git is absent or the tree is
    not a repository. A timeout is logged at debug and treated as empty.
    """
    dirty: set[str] = set()
    recent: set[str] = set()
    try:
        prefix = _git_prefix(project_path)
        status = subprocess.run(
            _git_argv(project_path, "status", "--porcelain", "--untracked-files=all"),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=False,
        )
        for row in (status.stdout or "").splitlines():
            if len(row) < 4:
                continue
            path = row[3:].strip().rsplit(" -> ", 1)[-1]
            norm = _normalize_git_path(path, prefix)
            if norm:
                dirty.add(norm)
        log = subprocess.run(
            _git_argv(project_path, "log", f"-{_RECENCY_COMMITS}",
                      "--name-only", "--pretty=format:"),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=False,
        )
        for path in (log.stdout or "").splitlines():
            norm = _normalize_git_path(path.strip(), prefix)
            if norm:
                recent.add(norm)
    except (OSError, subprocess.SubprocessError) as exc:
        _logger.debug("repo map git metadata failed: %s", exc)
    return dirty, recent


def _hunk_range(header: str) -> tuple[int, int] | None:
    """Inclusive new-file line range for one ``@@`` header, or None.

    ``@@ -12,0 +13,4 @@`` covers lines 13 through 16. A missing count means
    one line. A count of 0 is a deletion and anchors at that line (line 1
    when git reports ``+0``). The range is capped so a hostile count cannot
    allocate a giant span.
    """
    if not isinstance(header, str):
        return None
    match = _HUNK_HEADER.match(header)
    if not match:
        return None
    start = int(match.group(1))
    raw_count = match.group(2)
    count = 1 if raw_count is None else int(raw_count)
    start = max(start, 1)
    if count <= 0:
        return (start, start)
    count = min(count, _HUNK_LINE_CAP)
    return (start, start + count - 1)


def _diff_anchors(project_path: str, dirty_paths: set[str]) -> dict[str, list[tuple[int, int]]]:
    """Inclusive changed-line ranges per dirty file, from ``git diff --unified=0``.

    Untracked files get no hunks (absent from the dict). A ``+++ /dev/null``
    header clears the current file so a deletion's hunks are not attached to
    the previous path. Never raises.
    """
    anchors: dict[str, list[tuple[int, int]]] = {}
    if not dirty_paths:
        return anchors
    try:
        prefix = _git_prefix(project_path)
        diff = subprocess.run(
            _git_argv(project_path, "diff", "--unified=0", "HEAD"),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, check=False,
        )
        current: str | None = None
        for line in (diff.stdout or "").splitlines():
            if line.startswith("+++ /dev/null"):
                current = None
                continue
            if line.startswith("+++ b/"):
                current = _normalize_git_path(line[6:], prefix)
                if current is not None:
                    anchors.setdefault(current, [])
                continue
            if line.startswith("+++ "):
                current = _normalize_git_path(line[4:], prefix)
                if current is not None:
                    anchors.setdefault(current, [])
                continue
            if line.startswith("@@") and current is not None:
                span = _hunk_range(line)
                if span is not None:
                    anchors[current].append(span)
    except (OSError, subprocess.SubprocessError) as exc:
        _logger.debug("repo map diff anchors failed: %s", exc)
    return anchors


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
            # git diff hunks are 1-based; tree-sitter rows are 0-based.
            line_no = row + 1
            line = lines[row].strip() if 0 <= row < len(lines) else ident
            defs.append((rel_path, ident, line[:_LINE_CAP], line_no))
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


def _rank_inputs(inputs: _WalkInputs, goal: str) -> list:
    """Score every def in ``inputs``. Returns ``(rel, name, sig, line_no)`` best first.

    This is the warm half of a build: it touches only the cached graph, never
    the filesystem or a subprocess, so a new goal on an unchanged tree costs
    this function alone.

    Selection uses every def in the file, so capping happens in ``_render``
    rather than on this ranked list. ``_render`` caps only the clean tail at
    ``_TOP_N``. A deleted path is one synthetic node with the dirty weight, so
    it sorts with the other dirty files instead of after them.
    """
    nodes = inputs.nodes
    if not nodes:
        return []
    goal_names = {m.group(0) for m in _GOAL_NAME.finditer(goal or "") if len(m.group(0)) >= 3}
    graph_nodes = inputs.graph_nodes
    dirty_paths = inputs.dirty
    recent_paths = inputs.recent
    personal = {}
    for node in graph_nodes:
        weight = 1.0
        entry = nodes.get(node)
        if entry is not None:
            rel, name = entry[0], entry[1]
            if name in goal_names:
                weight = 10.0
            if rel in dirty_paths:
                weight = max(weight, _DIRTY_WEIGHT)
            elif rel in recent_paths:
                weight = max(weight, _RECENCY_WEIGHT)
        personal[node] = weight
    total = sum(personal.values()) or 1.0
    personal = {node: weight / total for node, weight in personal.items()}
    out_degree = inputs.out_degree
    score = dict(personal)
    for _ in range(_ITERATIONS):
        new = {node: (1 - _DAMPING) * personal[node] for node in graph_nodes}
        for src, dst in inputs.edges:
            degree = out_degree.get(src) or 1
            new[dst] = new.get(dst, 0.0) + _DAMPING * score.get(src, 0.0) / degree
        score = new
    for node, (rel, name, _line, _line_no) in nodes.items():
        if not rel.startswith(_TEST_DIR):
            continue
        if _looks_like_stub(name):
            score[node] = score.get(node, 0.0) * _STUB_PENALTY
        else:
            # SPEC-§6: demote, never drop. The node keeps its graph score
            # multiplied down, so a goal that names a test file or a test
            # function still lifts it into view, and _TOP_N can still reach it.
            score[node] = score.get(node, 0.0) * _TEST_DIR_PENALTY
    ordered = sorted(nodes, key=lambda node: (-score.get(node, 0.0), node))
    return [nodes[node] for node in ordered]


def _rank(defs: list, refs: list, goal: str, git_meta=None, anchors=None, deleted=None) -> list:
    """Score every def. Returns ``(rel, name, signature, line_no)`` best first.

    The direct-list entry point, kept for callers and tests that hand it raw
    ``defs``/``refs``. It assembles the same ``_WalkInputs`` the cached path
    uses and defers to ``_rank_inputs``, so both share one ranking.
    """
    del anchors
    inputs = _WalkInputs()
    inputs.defs = defs
    inputs.refs = refs
    if git_meta:
        inputs.dirty, inputs.recent = git_meta
    if deleted:
        inputs.deleted = set(deleted)
    _derive_graph(inputs)
    return _rank_inputs(inputs, goal)


def _derive_graph(inputs: _WalkInputs) -> None:
    """Populate nodes/by_name/edges/graph_nodes/out_degree from defs+refs+deleted.

    Split out of ``_build_walk_inputs`` so the direct ``_rank`` path builds the
    identical graph the cached path serves. An edge source is the *calling
    file* path and an edge target is a ``rel:name`` def node, so the
    self-reference test compares the caller's path against the target's path,
    not against the node string.
    """
    nodes = inputs.nodes
    by_name = inputs.by_name
    for rel, name, line, line_no in inputs.defs:
        node = f"{rel}:{name}"
        nodes[node] = (rel, name, line, line_no)
        by_name.setdefault(name, []).append(node)
    for rel in inputs.deleted:
        if not isinstance(rel, str) or not rel:
            continue
        node = f"{rel}:{_DELETED_SIG}"
        nodes.setdefault(node, (rel, _DELETED_SIG, _DELETED_SIG, 0))
    edges: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rel, name in inputs.refs:
        targets = by_name.get(name) or []
        if len(targets) != 1:
            continue
        dst = targets[0]
        if rel == nodes[dst][0]:
            continue
        key = (rel, dst)
        if key in seen:
            continue
        seen.add(key)
        edges.append(key)
    inputs.edges = edges
    graph_nodes = set(nodes)
    graph_nodes.update(src for src, _dst in edges)
    inputs.graph_nodes = sorted(graph_nodes)
    out_degree = inputs.out_degree
    for src, _dst in edges:
        out_degree[src] = out_degree.get(src, 0) + 1


def _overlap(line_no: int, end: int | None, start: int, stop: int) -> int:
    """How many hunk lines sit inside ``[line_no, end]``. ``end`` None runs to EOF."""
    hi = stop if end is None else min(end, stop)
    lo = max(line_no, start)
    if lo > hi:
        return 0
    return hi - lo + 1


def _dist_to_range(line_no: int, start: int, stop: int) -> int:
    if start <= line_no <= stop:
        return 0
    if line_no < start:
        return start - line_no
    return line_no - stop


def _anchored_def(file_defs: list, hunks: list):
    """Def covering the most hunk lines, else the nearest def.

    ``file_defs`` is ``(name, signature, line_no)`` sorted by line. ``hunks``
    is inclusive ``(start, stop)`` ranges. The last def runs to end of file.
    Returns that triple, or None when there is nothing to anchor.
    """
    if not file_defs or not hunks:
        return None
    spans = []
    last = len(file_defs) - 1
    for index, (name, sig, line_no) in enumerate(file_defs):
        if index < last:
            end = max(file_defs[index + 1][2], line_no)
        else:
            end = None
        spans.append((name, sig, line_no, end))
    containing = []
    for name, sig, line_no, end in spans:
        covered = 0
        dist = None
        for start, stop in hunks:
            lines = _overlap(line_no, end, start, stop)
            if lines <= 0:
                continue
            covered += lines
            piece = _dist_to_range(line_no, start, stop)
            if dist is None or piece < dist:
                dist = piece
        if covered:
            containing.append((-covered, dist if dist is not None else 0, name, sig, line_no))
    if containing:
        containing.sort()
        _count, _dist, name, sig, line_no = containing[0]
        return (name, sig, line_no)
    nearest = []
    for name, sig, line_no, _end in spans:
        dist = min(_dist_to_range(line_no, start, stop) for start, stop in hunks)
        nearest.append((dist, name, sig, line_no))
    nearest.sort()
    _dist, name, sig, line_no = nearest[0]
    return (name, sig, line_no)


def _signature_for(file_defs: list, hunks: list, fallback, goal_names: set[str]) -> str:
    """One signature line for a dirty file.

    Hunks pick the anchored def. No hunks (untracked) pick a goal-name match.
    """
    fallback_sig = ""
    fallback_name = ""
    if fallback is not None:
        fallback_name = fallback[1]
        fallback_sig = fallback[2]
    if hunks:
        anchored = _anchored_def(file_defs, hunks)
        if anchored is not None:
            return anchored[1]
        return fallback_sig or (file_defs[0][1] if file_defs else "")
    matches = [item for item in file_defs if item[0] in goal_names]
    if len(matches) == 1:
        return matches[0][1]
    if len(matches) > 1:
        if fallback_name in goal_names and fallback_sig:
            return fallback_sig
        matches.sort(key=lambda item: (item[2], item[0]))
        return matches[0][1]
    return fallback_sig or (file_defs[0][1] if file_defs else "")


def _display_entries(ranked: list, anchors, dirty, defs, goal: str) -> list:
    """Dirty files first (one signature each, no TOP_N cap), then clean top N."""
    anchors = anchors or {}
    dirty = set(dirty or ())
    goal_names = {m.group(0) for m in _GOAL_NAME.finditer(goal or "") if len(m.group(0)) >= 3}
    by_file: dict[str, list] = {}
    for rel, name, sig, line_no in defs or ():
        by_file.setdefault(rel, []).append((name, sig, line_no))
    for file_defs in by_file.values():
        file_defs.sort(key=lambda item: (item[2], item[0]))
    fallback: dict[str, tuple] = {}
    for rel, name, sig, line_no in ranked:
        fallback.setdefault(rel, (rel, name, sig, line_no))
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for rel, _name, _sig, _line_no in ranked:
        if rel not in dirty or rel in seen:
            continue
        seen.add(rel)
        sig = _signature_for(
            by_file.get(rel, []),
            anchors.get(rel) or [],
            fallback.get(rel),
            goal_names,
        )
        if sig:
            entries.append((rel, sig))
    for rel in sorted(dirty):
        if rel in seen or rel not in by_file:
            continue
        sig = _signature_for(
            by_file[rel],
            anchors.get(rel) or [],
            None,
            goal_names,
        )
        if sig:
            entries.append((rel, sig))
    clean = 0
    for rel, _name, sig, _line_no in ranked:
        if rel in dirty:
            continue
        entries.append((rel, sig))
        clean += 1
        if clean >= _TOP_N:
            break
    return entries


def _render(ranked: list, anchors=None, dirty=None, defs=None, goal: str = "") -> str:
    if anchors is None and dirty is None and defs is None:
        entries = [(rel, line) for rel, _name, line, *_rest in ranked]
    else:
        entries = _display_entries(ranked, anchors, dirty, defs, goal)
    if not entries:
        return ""
    parts: list[str] = []
    for rel, line in entries:
        piece = f"{rel}\n  {line}\n"
        if parts and (len("".join(parts)) + len(piece)) // 4 > _TOKEN_BUDGET:
            break
        parts.append(piece)
    return "".join(parts)


def build_repo_map(project_path: str, goal: str) -> str:
    """Signature block for ``goal``, or "" if there is nothing to show.

    Never raises. Dirty files rank first (no cap); the signature is the def
    covering the most lines of the diff hunks. The last def runs to end of
    file. Untracked files fall back to symbol match. A dirty ``.py`` path
    that is no longer on disk is shown as ``(deleted)``. A missing grammar,
    a clean empty tree, or absent git still returns the graph's own top
    signatures, or "" when the tree has no Python definitions.
    """
    if not isinstance(project_path, str) or not project_path or not os.path.isdir(project_path):
        return ""
    if not isinstance(goal, str):
        goal = ""
    try:
        if _language() is None:
            return ""
        # SPEC-§6: the git metadata, diff anchors, tree-sitter walk, and the
        # graph derived from them are all goal-independent, so they are cached
        # on (project, tree fingerprint). A new goal on an unchanged tree
        # re-enters warm and pays only the rank + render below.
        inputs = _walk_inputs(project_path)
        ranked = _rank_inputs(inputs, goal)
        if not ranked:
            return ""
        # Dirty files rank first. Their signature follows the hunk, or a
        # goal-name match when the file has no hunks. A deleted path is a
        # scored dirty node, not a tail entry. The decision model is not called.
        return _render(
            ranked, anchors=inputs.anchors, dirty=inputs.dirty,
            defs=inputs.defs, goal=goal,
        )
    except Exception:
        _logger.warning("build_repo_map failed", exc_info=True)
        return ""
