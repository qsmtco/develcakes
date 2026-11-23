# tests/test_spec66_package.py — SPEC-66 §2.12: packaging + stale strings.
#
# R-22 row node: test_render_and_prompts_are_packages
#
# PLAIN runner, and deliberately read-only: this node reads source files as
# TEXT and never imports them (importing ui.views.left_panel would build GTK
# widgets). The real non-editable `pip install .` check stays manual/CI — the
# row must not shell out to pip.
#
# Two distinct failures are covered:
#   * packages.find lacked "render*", so a wheel dropped the entire render
#     package (sanitizer + emitter + feed_html) and the installed app died on
#     first paint;
#   * the system prompts are DATA, so packages.find cannot ship them — they
#     need an explicit package-data mapping.
#
# §12.8 splits the stale-string half into its own row (R-23); the assertions
# live here anyway so one node proves all three sub-claims, as the work order
# requires.

from __future__ import annotations

import os

GTK_HEADLESS_SAFE = True

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel_path: str) -> str:
    with open(os.path.join(_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def test_render_and_prompts_are_packages():
    """R-22 NODE: all three sub-claims.

    (a) the include list declares render*
    (b) package-data maps the prompts markdown
    (c) neither stale string survives in its source file
    """
    pyproject = _read("pyproject.toml")

    # ── (a) the include list ──────────────────────────────────────────────
    assert "[tool.setuptools.packages.find]" in pyproject, (
        "pyproject.toml lost its packages.find section"
    )
    find_section = pyproject[pyproject.index("[tool.setuptools.packages.find]"):]
    # Cut at the next table header so the search stays inside the section.
    next_header = find_section.find("\n[", 1)
    find_section = find_section[: next_header] if next_header != -1 else find_section
    assert "include" in find_section, "packages.find has no include list"
    include_line = next(
        ln for ln in find_section.splitlines() if ln.strip().startswith("include")
    )
    assert "render*" in include_line, (
        f"packages.find does not declare render*: {include_line!r} — a "
        "non-editable install ships no render package"
    )
    for required in ("ui*", "agent*", "utils*", "models*", "prompts*", "transport*"):
        assert required in include_line, (
            f"packages.find dropped {required}: {include_line!r}"
        )

    # ── (b) the prompts data mapping ──────────────────────────────────────
    assert "[tool.setuptools.package-data]" in pyproject, (
        "no package-data section — the prompts markdown ships with nothing"
    )
    data_section = pyproject[pyproject.index("[tool.setuptools.package-data]"):]
    next_header = data_section.find("\n[", 1)
    data_section = data_section[: next_header] if next_header != -1 else data_section
    prompts_mapping = [
        ln for ln in data_section.splitlines()
        if ln.strip().startswith("prompts")
    ]
    assert prompts_mapping, (
        f"package-data has no prompts entry: {data_section!r}"
    )
    assert any("system/*.md" in ln for ln in prompts_mapping), (
        f"the prompts mapping does not carry system/*.md: {prompts_mapping!r}"
    )

    # ── (c) the two stale strings ─────────────────────────────────────────
    left_panel = _read("ui/views/left_panel.py")
    assert "Connect to discover agents" not in left_panel, (
        "the Agents placeholder still points at a Connect control that no "
        "longer exists"
    )
    assert "No agents yet" in left_panel, (
        "the Agents placeholder was not replaced with the spec'd copy"
    )

    setup = _read("knowledge/setup.md")
    assert "local-kb" not in setup, (
        "knowledge/setup.md still advertises the removed local-kb provider"
    )
    assert "CrabCakes" not in setup, (
        "knowledge/setup.md still uses the pre-rename product name"
    )


def test_prompts_markdown_actually_exists():
    """The mapping in (b) is worthless if the glob matches nothing — assert the
    data is present in the tree too."""
    system_dir = os.path.join(_ROOT, "prompts", "system")
    assert os.path.isdir(system_dir), f"missing prompts/system at {system_dir}"
    md_files = [f for f in os.listdir(system_dir) if f.endswith(".md")]
    assert md_files, "prompts/system contains no markdown — system/*.md is empty"


def test_render_package_is_importable_source():
    """The include list declares render*; make sure the package it names is a
    real importable package (has __init__.py) rather than a loose directory."""
    render_dir = os.path.join(_ROOT, "render")
    assert os.path.isdir(render_dir), "no render/ directory"
    assert os.path.isfile(os.path.join(render_dir, "__init__.py")), (
        "render/ has no __init__.py — packages.find cannot discover it even "
        "with render* in the include list"
    )
