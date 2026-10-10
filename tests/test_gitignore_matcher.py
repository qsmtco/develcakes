"""SPEC-21 SP2: compiled .gitignore matcher matches the previous fnmatch loop."""
from __future__ import annotations

import os
import random
from fnmatch import fnmatch
from pathlib import Path

from agent.context import (
    _compile_gitignore,
    _load_gitignore_patterns,
    build_file_index,
)


def _reference_match_gitignore(name: str, patterns: list[str], anchored: bool = False) -> bool:
    """Verbatim copy of the pre-SPEC-21 _match_gitignore loop."""
    for pattern in patterns:
        negated = pattern.startswith("!")
        active = pattern[1:] if negated else pattern
        dir_only = active.endswith("/")
        if dir_only:
            active = active[:-1]
            if "/" not in active and fnmatch(name, active):
                return not negated
            continue
        if fnmatch(name, active):
            return not negated
    return False


def test_compile_gitignore_matches_reference_randomized():
    atoms = [
        "*.py",
        "*.py[cod]",
        "build/",
        "src/build/",
        "!keep.py",
        "dist",
        ".env",
        ".env.*",
        "a?c",
        "[!x]*.md",
        "*~",
        "docs/",
        "!docs",
        "*$py.class",
        "x/y",
        "**/z",
        "",
        "!",
    ]
    names = [
        "a.py",
        "b.pyc",
        "build",
        "dist",
        "keep.py",
        ".env",
        ".env.local",
        "abc",
        "x.md",
        "y.md",
        "foo~",
        "docs",
        "z",
        "x",
        "y",
        "zz.class",
        "a$py.class",
        "README.md",
        "src",
        "build/",
        "",
    ]
    rng = random.Random(7)
    for _ in range(4000):
        pats = rng.sample(atoms, rng.randint(0, 8))
        compiled = _compile_gitignore(pats)
        for nm in names:
            assert compiled(nm) == _reference_match_gitignore(nm, pats), (
                f"mismatch name={nm!r} patterns={pats!r}"
            )


def test_compile_gitignore_matches_real_repo_walk():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    patterns = _load_gitignore_patterns(root)
    compiled = _compile_gitignore(patterns)
    skip = {".venv", ".git", "node_modules", "__pycache__"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        rel = os.path.relpath(dirpath, root)
        parts = [] if rel == "." else rel.replace("\\", "/").split("/")
        for part in parts + list(dirnames) + list(filenames):
            assert compiled(part) == _reference_match_gitignore(part, patterns), part


def test_build_file_index_matches_reference_matcher(tmp_path, monkeypatch):
    (tmp_path / "keep.py").write_text("a\nb\n")
    (tmp_path / "skip.pyc").write_text("x")
    (tmp_path / ".gitignore").write_text("*.pyc\n")
    (tmp_path / "empty.txt").write_bytes(b"")
    (tmp_path / "no_nl.txt").write_bytes(b"one\ntwo")

    from agent import context as ctx

    monkeypatch.setattr(ctx, "_GITIGNORE_MATCHERS", {})
    new_on = build_file_index(str(tmp_path), include_line_counts=True)
    new_off = build_file_index(str(tmp_path), include_line_counts=False)

    monkeypatch.setattr(ctx, "_match_gitignore", _reference_match_gitignore)
    monkeypatch.setattr(ctx, "_GITIGNORE_MATCHERS", {})
    ref_on = build_file_index(str(tmp_path), include_line_counts=True)
    ref_off = build_file_index(str(tmp_path), include_line_counts=False)
    assert new_on == ref_on
    assert new_off == ref_off
    assert "1 lines /" in new_on or "2 lines /" in new_on


def test_line_counts_empty_and_no_trailing_newline(tmp_path):
    (tmp_path / "empty.txt").write_bytes(b"")
    (tmp_path / "nonewline.txt").write_bytes(b"one\ntwo")
    (tmp_path / "withnl.txt").write_bytes(b"one\ntwo\n")
    idx = build_file_index(str(tmp_path), include_line_counts=True)
    assert "empty.txt" in idx
    # empty file: 0 lines (omitted or 0). nonewline: 2. withnl: 2.
    assert "nonewline.txt" in idx
    assert "2 lines /" in idx


def test_fnmatch_function_is_not_imported():
    src = Path(__file__).resolve().parents[1] / "agent" / "context.py"
    text = src.read_text()
    assert "from fnmatch import fnmatch" not in text
    assert "from fnmatch import translate" in text
