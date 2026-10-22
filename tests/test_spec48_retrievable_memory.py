# tests/test_spec48_retrievable_memory.py
# SPEC-48 Phase 1: the offloaded tool-output store and the read_tool_output
# tool. Phase 2 (the offload hook in the compaction path) and Phase 3 (the
# typed digest) are covered by their own tests later; the acceptance rows
# R-8 / R-9 / R-10 land with those phases.
#
# The path-sanitization invariant asserted here is: "the resolved file's
# parent is the store dir", NOT "the name contains no ..". A dot is a legal
# filename character and a flat name cannot traverse.

import os

import pytest

from agent.tools import (
    execute_tool,
    get_all_tools,
    get_tool_definitions_for_api,
)
from utils.tool_output_store import (
    PREVIEW_CHARS,
    offload_tool_output,
    read_tool_output,
    tool_outputs_dir,
)

# ═══════════════════════════════════════════════════════════════════
#  The store: offload_tool_output
# ═══════════════════════════════════════════════════════════════════

class TestToolOutputStore:
    def test_tool_outputs_dir_absolute_and_not_created(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        d = tool_outputs_dir(str(proj))
        assert os.path.isabs(d)
        assert d == os.path.join(str(proj), ".crabcakes", "tool-outputs")
        assert not os.path.exists(d)  # not created by the accessor

    def test_offload_writes_file_and_returns_metadata(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        content = "line one\nline two\nline three"
        meta = offload_tool_output(str(proj), "exec_command", content)
        assert meta is not None
        assert meta["path"].startswith(".crabcakes/tool-outputs/exec_command-")
        assert meta["path"].endswith(".txt")
        assert not meta["path"].startswith("/")
        assert meta["bytes"] == len(content.encode("utf-8"))
        assert meta["lines"] == 3
        assert meta["preview"] == content[:PREVIEW_CHARS]
        full = os.path.join(str(proj), meta["path"])
        assert os.path.isfile(full)
        with open(full, encoding="utf-8") as fh:
            assert fh.read() == content

    def test_offload_is_idempotent_for_identical_content(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        m1 = offload_tool_output(str(proj), "exec_command", "same body")
        m2 = offload_tool_output(str(proj), "exec_command", "same body")
        assert m1 is not None and m2 is not None
        assert m1["path"] == m2["path"]
        store = proj / ".crabcakes" / "tool-outputs"
        assert len(list(store.glob("*.txt"))) == 1

    def test_offload_distinct_content_distinct_hash(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        m1 = offload_tool_output(str(proj), "exec_command", "aaa")
        m2 = offload_tool_output(str(proj), "exec_command", "bbb")
        assert m1 is not None and m2 is not None
        assert m1["path"] != m2["path"]

    def test_offload_sanitizes_tool_name(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        meta = offload_tool_output(str(proj), "weird/../na me!@#", "x")
        assert meta is not None
        filename = meta["path"].rsplit("/", 1)[1]
        assert filename.startswith("weird_.._na_me___-")
        assert "/" not in filename

    def test_offload_empty_content(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        meta = offload_tool_output(str(proj), "exec_command", "")
        assert meta is not None
        assert meta["bytes"] == 0
        assert meta["lines"] == 0
        assert meta["preview"] == ""

    def test_offload_preview_capped_at_preview_chars(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        content = "y" * (PREVIEW_CHARS + 500)
        meta = offload_tool_output(str(proj), "exec_command", content)
        assert meta is not None
        assert len(meta["preview"]) == PREVIEW_CHARS

    def test_offload_unwritable_store_returns_none(self, tmp_path):
        # The store path is blocked by a regular file, so makedirs raises
        # FileExistsError (an OSError) → None, never a raise.
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".crabcakes").mkdir()
        (proj / ".crabcakes" / "tool-outputs").write_text("blocker")
        assert offload_tool_output(str(proj), "exec_command", "body") is None

    def test_offload_leaves_no_tmp_files(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        offload_tool_output(str(proj), "exec_command", "body")
        store = proj / ".crabcakes" / "tool-outputs"
        leftovers = [p.name for p in store.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_offload_refuses_symlinked_tmp_path(self, tmp_path):
        # B1: a pre-planted symlink at the .tmp path must not redirect the
        # write outside the project — open(tmp) follows it before any
        # replace. The offload returns None and the outside target never
        # receives the payload.
        outside = tmp_path / "outside.txt"
        outside.write_text("planted")
        proj = tmp_path / "proj"
        proj.mkdir()
        meta = offload_tool_output(str(proj), "exec_command", "SECRET-BYTES")
        assert meta is not None  # learn the deterministic store name
        filename = meta["path"].rsplit("/", 1)[1]
        store = proj / ".crabcakes" / "tool-outputs"
        tmp_link = store / f".{filename}.tmp"
        try:
            tmp_link.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks not supported on this filesystem")
        meta2 = offload_tool_output(str(proj), "exec_command", "SECRET-BYTES")
        assert meta2 is None
        assert outside.read_text() == "planted"  # payload never landed outside

    def test_offload_replace_failure_leaves_no_tmp(self, tmp_path):
        # C2: when os.replace(tmp, final) fails (a directory occupies the
        # final name), the offload returns None AND the leftover .tmp file
        # is cleaned up (best-effort unlink in the except branch).
        proj = tmp_path / "proj"
        proj.mkdir()
        meta = offload_tool_output(str(proj), "exec_command", "body")
        assert meta is not None
        full = os.path.join(str(proj), meta["path"])
        os.unlink(full)
        os.mkdir(full)  # occupy the final name with a directory
        assert offload_tool_output(str(proj), "exec_command", "body") is None
        store = proj / ".crabcakes" / "tool-outputs"
        leftovers = [p.name for p in store.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_offload_name_with_dots_stays_flat_and_inside_store(self, tmp_path):
        # The REAL sanitization invariant: a flat name may contain ".."
        # (a dot is a legal filename char) as long as the resolved file's
        # parent is the store dir. This is what the spec audit verified.
        proj = tmp_path / "proj"
        proj.mkdir()
        meta = offload_tool_output(str(proj), "weird/../name", "body")
        assert meta is not None
        filename = meta["path"].rsplit("/", 1)[1]
        assert "/" not in filename  # flat, never a path
        store_dir = os.path.realpath(tool_outputs_dir(str(proj)))
        resolved = os.path.realpath(os.path.join(str(proj), meta["path"]))
        assert os.path.dirname(resolved) == store_dir  # parent IS the store dir
        assert os.path.isfile(resolved)


# ═══════════════════════════════════════════════════════════════════
#  The store: read_tool_output
# ═══════════════════════════════════════════════════════════════════

class TestReadToolOutput:
    def test_read_returns_body_and_flags(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        content = "a\nbb\nccc"
        meta = offload_tool_output(str(proj), "exec_command", content)
        assert meta is not None
        r = read_tool_output(str(proj), meta["path"])
        assert r["found"] is True
        assert r["output"] == content
        assert r["bytes"] == len(content.encode("utf-8"))
        assert r["truncated"] is False
        assert r["error"] == ""

    def test_read_is_line_windowed_not_byte_truncated(self, tmp_path):
        # max_bytes is tolerated but the read is line-based: the first 200
        # lines come back whole, total_lines is exact, and the continuation
        # flag comes from the real line count (BUG #1 fix).
        proj = tmp_path / "proj"
        proj.mkdir()
        content = "\n".join(f"line {i}" for i in range(1, 301))
        meta = offload_tool_output(str(proj), "exec_command", content)
        assert meta is not None
        r = read_tool_output(str(proj), meta["path"], max_bytes=64)
        assert r["found"] is True
        assert r["total_lines"] == 300
        assert r["lines"] == 200
        assert r["truncated"] is True
        assert r["output"].startswith("line 1")
        assert "line 200" in r["output"]

    def test_read_big_body_lines_past_16kb_reachable_by_paging(self, tmp_path):
        # 500 lines x ~45 chars = well past 16 KB; every byte must be
        # reachable by paging with an exact total_lines (BUG #1 fix).
        proj = tmp_path / "proj"
        proj.mkdir()
        content = "\n".join(f"line{i:04d} " + "z" * 40 for i in range(1, 501))
        assert len(content.encode("utf-8")) > 16 * 1024
        meta = offload_tool_output(str(proj), "exec_command", content)
        assert meta is not None
        r = read_tool_output(str(proj), meta["path"])
        assert r["found"] is True
        assert r["total_lines"] == 500
        assert r["lines"] == 200
        assert r["truncated"] is True
        assert r["output"].startswith("line0001")
        r2 = read_tool_output(str(proj), meta["path"], line=400, max_lines=200)
        assert r2["found"] is True
        assert r2["lines"] == 101
        assert r2["truncated"] is False
        assert r2["output"].startswith("line0400")
        assert r2["output"].splitlines()[-1] == "line0500 " + "z" * 40

    def test_read_start_line_past_end_errors(self, tmp_path):
        # A start line past the end is an error naming the end — never an
        # empty success indistinguishable from an empty file (BUG #2 fix).
        proj = tmp_path / "proj"
        proj.mkdir()
        meta = offload_tool_output(str(proj), "exec_command", "a\nb\nc")
        assert meta is not None
        r = read_tool_output(str(proj), meta["path"], line=4)
        assert r["found"] is False
        assert r["output"] == ""
        assert "past the end" in r["error"]

    def test_read_empty_file_is_success_not_error(self, tmp_path):
        # A genuinely empty file is total_lines == 0 and still returns
        # success (empty body, not an error).
        proj = tmp_path / "proj"
        proj.mkdir()
        meta = offload_tool_output(str(proj), "exec_command", "")
        assert meta is not None
        r = read_tool_output(str(proj), meta["path"], line=1)
        assert r["found"] is True
        assert r["total_lines"] == 0
        assert r["output"] == ""
        assert r["truncated"] is False
        assert r["error"] == ""

    @pytest.mark.parametrize(
        "bad",
        [
            "tool-outputs/exec_command-ab12cd34.txt",
            ".crabcakes/feed.json",
            ".crabcakes/tool-outputs",
            "../foo.txt",
            "/abs/path.txt",
            "README.md",
        ],
    )
    def test_read_rejects_paths_outside_store(self, tmp_path, bad):
        r = read_tool_output(str(tmp_path), bad)
        assert r["found"] is False
        assert r["error"]

    def test_read_rejects_traversal_via_name_slash(self, tmp_path):
        assert read_tool_output(str(tmp_path), ".crabcakes/tool-outputs/../x.txt")["found"] is False
        assert read_tool_output(str(tmp_path), ".crabcakes/tool-outputs/sub/x.txt")["found"] is False
        assert read_tool_output(str(tmp_path), ".crabcakes/tool-outputs/.")["found"] is False
        assert read_tool_output(str(tmp_path), ".crabcakes/tool-outputs/..")["found"] is False

    @pytest.mark.parametrize("rel_path", [None, 123, "", "plain.txt", 3.14])
    def test_read_never_raises_on_garbage_input(self, tmp_path, rel_path):
        r = read_tool_output(str(tmp_path), rel_path)
        assert isinstance(r, dict)
        assert r["found"] is False
        assert isinstance(r["error"], str) and r["error"]

    def test_read_nul_byte_rel_path_never_raises(self, tmp_path):
        # G2: a NUL byte inside rel_path must be rejected by the store-path
        # check so the read returns found=False with an error, never raising
        # ValueError (embedded null byte) — the documented never-raises
        # contract holds.
        r = read_tool_output(str(tmp_path), ".crabcakes/tool-outputs/ok\x00.txt")
        assert isinstance(r, dict)
        assert r["found"] is False
        assert isinstance(r["error"], str) and r["error"]

    def test_read_missing_file_returns_found_false(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        r = read_tool_output(str(proj), ".crabcakes/tool-outputs/exec_command-deadbeef.txt")
        assert r["found"] is False
        assert "deadbeef" in r["error"]

    def test_read_missing_store_dir_returns_found_false(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()  # no .crabcakes at all
        r = read_tool_output(str(proj), ".crabcakes/tool-outputs/exec_command-deadbeef.txt")
        assert r["found"] is False
        assert r["error"]

    def test_read_rejects_symlink_escaping_store(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        store = proj / ".crabcakes" / "tool-outputs"
        store.mkdir(parents=True)
        outside = tmp_path / "outside.txt"
        outside.write_text("other")
        link = store / "link.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks not supported on this filesystem")
        r = read_tool_output(str(proj), ".crabcakes/tool-outputs/link.txt")
        assert r["found"] is False
        assert "escapes" in r["error"]

    def test_offload_refuses_symlinked_store_dir(self, tmp_path):
        # A symlinked tool-outputs/ pointing outside the project is refused:
        # the write returns None and NOTHING lands outside (BUG #3 fix).
        outside = tmp_path / "outside"
        outside.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".crabcakes").mkdir()
        link = proj / ".crabcakes" / "tool-outputs"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks not supported on this filesystem")
        meta = offload_tool_output(str(proj), "exec_command", "SECRET-BYTES")
        assert meta is None
        assert list(outside.iterdir()) == []  # nothing written outside

    def test_read_refuses_symlinked_store_dir(self, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".crabcakes").mkdir()
        link = proj / ".crabcakes" / "tool-outputs"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks not supported on this filesystem")
        r = read_tool_output(str(proj), ".crabcakes/tool-outputs/whatever-00000000.txt")
        assert r["found"] is False
        assert "escapes" in r["error"]

    def test_read_refuses_symlinked_file_inside_store(self, tmp_path):
        # A symlinked FILE inside the store cannot redirect the read: the
        # resolved parent is not the store dir, so it is refused.
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "target.txt").write_text("ESCAPED")
        proj = tmp_path / "proj"
        proj.mkdir()
        meta = offload_tool_output(str(proj), "exec_command", "real body")
        assert meta is not None
        real = os.path.join(str(proj), meta["path"])
        os.unlink(real)
        try:
            os.symlink(os.path.join(str(outside), "target.txt"), real)
        except OSError:
            pytest.skip("symlinks not supported on this filesystem")
        r = read_tool_output(str(proj), meta["path"])
        assert r["found"] is False
        assert "escapes" in r["error"]


# ═══════════════════════════════════════════════════════════════════
#  The read_tool_output tool
# ═══════════════════════════════════════════════════════════════════

class TestReadToolOutputTool:
    def test_tool_registered_and_visible_in_api(self):
        defn = next(t for t in get_all_tools() if t.name == "read_tool_output")
        assert defn.parameters["required"] == ["path"]
        assert set(defn.parameters["properties"]) == {"path", "line", "max_lines"}
        assert defn.requires_approval is False
        names = [d["function"]["name"] for d in get_tool_definitions_for_api()]
        assert "read_tool_output" in names

    def test_tool_reads_offloaded_output(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        meta = offload_tool_output(str(proj), "exec_command", "one\ntwo\nthree")
        assert meta is not None
        result = execute_tool("read_tool_output", {"path": meta["path"]}, str(proj))
        assert result.success
        assert result.output.startswith("[line 1 of .crabcakes/tool-outputs/")
        assert "1|one" in result.output
        assert "2|two" in result.output
        assert "3|three" in result.output

    def test_tool_windows_by_lines(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        content = "\n".join(f"line {i}" for i in range(1, 60))
        meta = offload_tool_output(str(proj), "exec_command", content)
        assert meta is not None
        result = execute_tool(
            "read_tool_output",
            {"path": meta["path"], "line": 5, "max_lines": 3},
            str(proj),
        )
        assert result.success
        assert "5|line 5" in result.output
        assert "6|line 6" in result.output
        assert "7|line 7" in result.output
        assert "8|line 8" not in result.output

    def test_tool_clamps_max_lines_to_line_window(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        content = "\n".join(f"line {i}" for i in range(1, 300))
        meta = offload_tool_output(str(proj), "exec_command", content)
        assert meta is not None
        result = execute_tool(
            "read_tool_output",
            {"path": meta["path"], "max_lines": 5000},
            str(proj),
        )
        assert result.success
        shown = [ln for ln in result.output.splitlines() if "|line " in ln]
        assert len(shown) == 200

    def test_tool_emits_line_continuation_notice(self, tmp_path):
        # >200 lines, so the 200-line window engages: the first page shows
        # 200 lines and the continuation notice names the next line.
        proj = tmp_path / "proj"
        proj.mkdir()
        content = "\n".join(f"{i:03d} " + "z" * 71 for i in range(1, 221))
        meta = offload_tool_output(str(proj), "exec_command", content)
        assert meta is not None
        result = execute_tool("read_tool_output", {"path": meta["path"]}, str(proj))
        assert result.success
        assert "continue with line=201" in result.output

    def test_tool_large_body_truncated_to_16kb(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        content = "\n".join(f"line {i}" for i in range(1, 4000))
        meta = offload_tool_output(str(proj), "exec_command", content)
        assert meta is not None
        result = execute_tool("read_tool_output", {"path": meta["path"]}, str(proj))
        assert result.success
        assert len(result.output) < 16384  # windowed — never the whole blob
        assert "continue with line=201" in result.output

    def test_tool_long_single_line_is_fully_readable(self, tmp_path):
        # The store read is line-based, not byte-truncated: a single giant
        # line (one line <= the 200-line window) comes back whole.
        proj = tmp_path / "proj"
        proj.mkdir()
        content = "A" * 20000
        meta = offload_tool_output(str(proj), "exec_command", content)
        assert meta is not None
        result = execute_tool("read_tool_output", {"path": meta["path"]}, str(proj))
        assert result.success
        assert "1|" in result.output
        assert result.output.count("A") == 20000
        assert "continue with line=" not in result.output

    def test_tool_pages_beyond_16kb_and_continuation_names_next_line(self, tmp_path):
        # BUG #1: bytes past 16 KB must be reachable by paging, and the
        # continuation notice must name the next line from the real total.
        proj = tmp_path / "proj"
        proj.mkdir()
        content = "\n".join(f"line{i:04d} " + "z" * 40 for i in range(1, 501))
        meta = offload_tool_output(str(proj), "exec_command", content)
        assert meta is not None
        first = execute_tool("read_tool_output", {"path": meta["path"]}, str(proj))
        assert first.success
        assert "continue with line=201" in first.output
        paged = execute_tool(
            "read_tool_output",
            {"path": meta["path"], "line": 400, "max_lines": 200},
            str(proj),
        )
        assert paged.success
        assert "400|line0400" in paged.output
        assert "500|line0500" in paged.output
        assert "continue with line=" not in paged.output

    def test_tool_start_line_past_end_fails_with_empty_output(self, tmp_path):
        # BUG #2: a start line past the end errors (success=False) with an
        # empty output — never an empty success like an empty file.
        proj = tmp_path / "proj"
        proj.mkdir()
        meta = offload_tool_output(str(proj), "exec_command", "a\nb\nc")
        assert meta is not None
        result = execute_tool(
            "read_tool_output",
            {"path": meta["path"], "line": 99},
            str(proj),
        )
        assert not result.success
        assert result.output == ""
        assert "past the end" in result.error

    @pytest.mark.parametrize("bad_line", [0, -1, True, "1"])
    def test_tool_invalid_line_rejected(self, tmp_path, bad_line):
        proj = tmp_path / "proj"
        proj.mkdir()
        meta = offload_tool_output(str(proj), "exec_command", "a\nb\nc")
        assert meta is not None
        result = execute_tool(
            "read_tool_output",
            {"path": meta["path"], "line": bad_line},
            str(proj),
        )
        assert not result.success
        assert "line must be an integer >= 1" in result.error

    @pytest.mark.parametrize("bad_max_lines", [0, -1, True, "1"])
    def test_tool_invalid_max_lines_rejected(self, tmp_path, bad_max_lines):
        proj = tmp_path / "proj"
        proj.mkdir()
        meta = offload_tool_output(str(proj), "exec_command", "a\nb\nc")
        assert meta is not None
        result = execute_tool(
            "read_tool_output",
            {"path": meta["path"], "max_lines": bad_max_lines},
            str(proj),
        )
        assert not result.success
        assert "max_lines must be an integer >= 1" in result.error

    def test_tool_no_project_rejected(self, tmp_path):
        result = execute_tool(
            "read_tool_output",
            {"path": ".crabcakes/tool-outputs/x.txt"},
            "",
        )
        assert not result.success
        assert "No active project" in result.error

    def test_tool_path_outside_store_rejected(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "real.txt").write_text("legit project file")
        result = execute_tool("read_tool_output", {"path": "real.txt"}, str(proj))
        assert not result.success
        assert result.error
        result = execute_tool("read_tool_output", {"path": ".crabcakes/feed.json"}, str(proj))
        assert not result.success
        assert result.error

    def test_tool_missing_file_rejected(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        result = execute_tool(
            "read_tool_output",
            {"path": ".crabcakes/tool-outputs/exec_command-deadbeef.txt"},
            str(proj),
        )
        assert not result.success
        assert "deadbeef" in result.error

    def test_tool_empty_body(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        meta = offload_tool_output(str(proj), "exec_command", "")
        assert meta is not None
        result = execute_tool("read_tool_output", {"path": meta["path"]}, str(proj))
        assert result.success
        assert "(empty)" in result.output
