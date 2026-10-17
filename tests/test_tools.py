# tests/test_tools.py
# Unit tests for agent/tools.py
#
# Principles:
#   - Test the contract: each tool's documented behavior
#   - Sandbox: paths outside project must be rejected
#   - Truncation: output > limit must be truncated
#   - Approval: exec_command requires approval callback

import json
import os
import tempfile
from typing import ClassVar

import pytest
import yaml

from agent.tools import (
    execute_tool,
    get_all_tools,
    get_tool_definitions_for_api,
    set_approval_callback,
)

# ═══════════════════════════════════════════════════════════════════
#  Tool registry
# ═══════════════════════════════════════════════════════════════════

class TestToolRegistry:
    def test_all_tools_returns_list(self):
        tools = get_all_tools()
        assert isinstance(tools, list)
        assert len(tools) > 0

    def test_all_tools_have_names(self):
        for tool in get_all_tools():
            assert tool.name
            assert tool.description
            assert tool.parameters

    def test_get_api_definitions_returns_dict_list(self):
        api_defs = get_tool_definitions_for_api()
        assert isinstance(api_defs, list)
        for item in api_defs:
            assert item["type"] == "function"
            assert "function" in item
            assert "name" in item["function"]
            assert "description" in item["function"]
            assert "parameters" in item["function"]

    def test_exec_command_requires_approval(self):
        for tool in get_all_tools():
            if tool.name == "exec_command":
                assert tool.requires_approval is True
                return
        pytest.fail("exec_command not found in tool registry")


# ═══════════════════════════════════════════════════════════════════
#  Sandbox: path escaping
# ═══════════════════════════════════════════════════════════════════

class TestSandbox:
    def test_read_file_absolute_outside_sandbox_rejected(self):
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("read_file", {"path": "/etc/passwd"}, proj)
            assert r.success is False
            assert "escapes" in r.error or "outside" in r.error

    def test_read_file_absolute_inside_sandbox_allowed(self):
        with tempfile.TemporaryDirectory() as proj:
            testfile = os.path.join(proj, "test.txt")
            with open(testfile, "w") as f:
                f.write("hello")
            r = execute_tool("read_file", {"path": testfile}, proj)
            assert r.success is True
            assert r.output == "hello"

    def test_read_file_relative_inside_sandbox(self):
        with tempfile.TemporaryDirectory() as proj:
            subdir = os.path.join(proj, "src")
            os.makedirs(subdir)
            with open(os.path.join(subdir, "a.py"), "w") as f:
                f.write("print('hi')")
            r = execute_tool("read_file", {"path": "src/a.py"}, proj)
            assert r.success is True
            assert r.output == "print('hi')"

    def test_write_file_absolute_outside_sandbox_rejected(self):
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("write_file", {"path": "/tmp/evil.txt", "content": "bad"}, proj)
            assert r.success is False
            assert "escapes" in r.error or "outside" in r.error

    def test_write_file_inside_sandbox(self):
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("write_file", {"path": "new.txt", "content": "world"}, proj)
            assert r.success is True
            with open(os.path.join(proj, "new.txt")) as f:
                assert f.read() == "world"

    def test_list_files_outside_sandbox_rejected(self):
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("list_files", {"path": "/etc"}, proj)
            assert r.success is False

    def test_list_files_inside_sandbox(self):
        with tempfile.TemporaryDirectory() as proj:
            open(os.path.join(proj, "a.txt"), "w").close()
            r = execute_tool("list_files", {"path": "."}, proj)
            assert r.success is True
            assert "a.txt" in r.output

    def test_list_files_recursive_caps(self):
        from agent.tools import MAX_LIST_ENTRIES
        with tempfile.TemporaryDirectory() as proj:
            for i in range(MAX_LIST_ENTRIES + 50):
                open(os.path.join(proj, f"f{i:04d}.txt"), "w").close()
            r = execute_tool("list_files", {"path": ".", "recursive": True}, proj)
            assert r.success is True
            lines = r.output.splitlines()
            assert len(lines) <= MAX_LIST_ENTRIES + 1
            assert lines[-1] == f"[... truncated at {MAX_LIST_ENTRIES} entries ...]"
            assert r.output.count("f") >= MAX_LIST_ENTRIES  # the fill names are present

    def test_list_files_exact_cap_has_no_notice(self):
        from agent.tools import MAX_LIST_ENTRIES
        with tempfile.TemporaryDirectory() as proj:
            sub = os.path.join(proj, "exact")
            os.makedirs(sub)
            for i in range(MAX_LIST_ENTRIES):
                open(os.path.join(sub, f"e{i:04d}.txt"), "w").close()
            r = execute_tool("list_files", {"path": "exact", "recursive": True}, proj)
            assert r.success is True
            assert "truncated" not in r.output
            assert len(r.output.splitlines()) == MAX_LIST_ENTRIES

    def test_list_files_stops_across_sibling_dirs(self):
        from agent.tools import MAX_LIST_ENTRIES
        with tempfile.TemporaryDirectory() as proj:
            for dirname in ("a", "b"):
                os.makedirs(os.path.join(proj, dirname))
                for i in range(MAX_LIST_ENTRIES):
                    open(os.path.join(proj, dirname, f"{dirname}{i:04d}.txt"), "w").close()
            r = execute_tool("list_files", {"path": ".", "recursive": True}, proj)
            assert r.success is True
            lines = [ln for ln in r.output.splitlines() if not ln.startswith("[")]
            assert len(lines) == MAX_LIST_ENTRIES
            assert r.output.splitlines()[-1].startswith("[... truncated at")

    def test_list_files_empty_directory(self):
        with tempfile.TemporaryDirectory() as proj:
            os.makedirs(os.path.join(proj, "empty"))
            r = execute_tool("list_files", {"path": "empty"}, proj)
            assert r.success is True
            assert r.output == "(empty)"

    def test_exec_output_bounded_for_model(self):
        from agent.tools import CONV_OUTPUT_HEAD, CONV_OUTPUT_TAIL, MAX_EXEC_OUTPUT
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool(
                "exec_command",
                {"command": f"python3 -c \"print('x' * {MAX_EXEC_OUTPUT + 5000})\""},
                proj,
                "special:coder",
                approval_callback=lambda sk, tn, args: True,
            )
            assert r.success is True
            assert len(r.output) <= CONV_OUTPUT_HEAD + CONV_OUTPUT_TAIL + 80
            assert "[... " in r.output and "chars omitted" in r.output
            assert "[... truncated at" in r.stdout
            assert len(r.stdout) > CONV_OUTPUT_HEAD + CONV_OUTPUT_TAIL

    def test_search_files_outside_sandbox_rejected(self):
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("search_files", {"pattern": "hello", "path": "/etc"}, proj)
            assert r.success is False

    def test_search_files_inside_sandbox(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "a.py"), "w") as f:
                f.write("def hello(): pass")
            r = execute_tool("search_files", {"pattern": "hello", "path": "."}, proj)
            assert r.success is True
            assert "hello" in r.output


# ═══════════════════════════════════════════════════════════════════
#  Tool behaviors
# ═══════════════════════════════════════════════════════════════════

class TestReadFile:
    def test_read_nonexistent_file(self):
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("read_file", {"path": "noexist.txt"}, proj)
            assert r.success is False
            assert "Not a file" in r.error or "not" in r.error.lower()

    def test_read_truncates_at_cap(self):
        from agent.tools import MAX_READ_SIZE
        with tempfile.TemporaryDirectory() as proj:
            large = os.path.join(proj, "large.txt")
            with open(large, "w") as f:
                f.write("x" * 100_000)  # 100 KB
            r = execute_tool("read_file", {"path": "large.txt"}, proj)
            assert r.success is True
            assert "truncated" in r.output
            assert len(r.output) <= MAX_READ_SIZE + 200
            assert r.output.count("x") == MAX_READ_SIZE
            # Window fills the read cap exactly; continuation points at the cap.
            assert f"offset={MAX_READ_SIZE}" in r.output
            assert "chars omitted" not in r.output  # not the conversation head/tail cutter

    def test_read_explicit_limit_cannot_exceed_cap(self):
        from agent.tools import MAX_READ_SIZE
        with tempfile.TemporaryDirectory() as proj:
            large = os.path.join(proj, "large.txt")
            with open(large, "wb") as f:
                f.write(b"q" * 500_000)
            r = execute_tool("read_file", {"path": "large.txt", "limit": 200_000}, proj)
            assert r.success is True
            assert "truncated" in r.output
            assert len(r.output) <= MAX_READ_SIZE + 200
            # "q" is not in the notice ("bytes" contains "y").
            assert r.output.count("q") == MAX_READ_SIZE

    def test_read_small_limit_has_no_notice(self):
        with tempfile.TemporaryDirectory() as proj:
            f = os.path.join(proj, "test.txt")
            with open(f, "w") as fh:
                fh.write("0123456789abcdefghij")
            r = execute_tool("read_file", {"path": "test.txt", "offset": 5, "limit": 5}, proj)
            assert r.success is True
            assert r.output == "56789"
            assert "truncated" not in r.output

    def test_read_negative_limit_rejected(self):
        with tempfile.TemporaryDirectory() as proj:
            f = os.path.join(proj, "test.txt")
            with open(f, "w") as fh:
                fh.write("hello")
            r = execute_tool("read_file", {"path": "test.txt", "limit": -1}, proj)
            assert r.success is False
            assert r.error == "limit must be >= 0"
            r0 = execute_tool("read_file", {"path": "test.txt", "limit": 0}, proj)
            assert r0.success is True
            assert r0.output == ""

    def test_read_negative_offset_is_oserror(self):
        with tempfile.TemporaryDirectory() as proj:
            f = os.path.join(proj, "test.txt")
            with open(f, "w") as fh:
                fh.write("hello")
            r = execute_tool("read_file", {"path": "test.txt", "offset": -1}, proj)
            assert r.success is False
            assert r.error.startswith("Cannot read test.txt:")

    def test_read_exact_cap_has_no_notice(self):
        from agent.tools import MAX_READ_SIZE
        with tempfile.TemporaryDirectory() as proj:
            f = os.path.join(proj, "exact.txt")
            with open(f, "wb") as fh:
                fh.write(b"z" * MAX_READ_SIZE)
            r = execute_tool("read_file", {"path": "exact.txt"}, proj)
            assert r.success is True
            assert r.output == "z" * MAX_READ_SIZE
            assert "truncated" not in r.output

    def test_read_bad_limit_type(self):
        with tempfile.TemporaryDirectory() as proj:
            f = os.path.join(proj, "test.txt")
            with open(f, "w") as fh:
                fh.write("hello")
            r = execute_tool("read_file", {"path": "test.txt", "limit": "5"}, proj)
            assert r.success is False
            assert "limit must be an integer" in r.error

    def test_read_binary_file_rejected(self):
        with tempfile.TemporaryDirectory() as proj:
            binary = os.path.join(proj, "bin.dat")
            with open(binary, "wb") as f:
                f.write(b"\x00\x01\x02\x03")
            r = execute_tool("read_file", {"path": "bin.dat"}, proj)
            assert r.success is False
            assert "Binary" in r.error or "binary" in r.error

    def test_read_with_offset_and_limit(self):
        with tempfile.TemporaryDirectory() as proj:
            f = os.path.join(proj, "test.txt")
            with open(f, "w") as fh:
                fh.write("0123456789abcdefghij")
            r = execute_tool("read_file", {"path": "test.txt", "offset": 5, "limit": 5}, proj)
            assert r.success is True
            assert r.output == "56789"


class TestWriteFile:
    def test_write_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("write_file", {"path": "a/b/c.txt", "content": "deep"}, proj)
            assert r.success is True
            assert os.path.isfile(os.path.join(proj, "a", "b", "c.txt"))

    def test_write_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as proj:
            f = os.path.join(proj, "f.txt")
            with open(f, "w") as fh:
                fh.write("original")
            r = execute_tool("write_file", {"path": "f.txt", "content": "updated"}, proj)
            assert r.success is True
            with open(f) as fh:
                assert fh.read() == "updated"

    def test_write_reports_bytes_written(self):
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("write_file", {"path": "f.txt", "content": "hello"}, proj)
            assert r.success is True
            assert "5 bytes" in r.output

    def test_write_file_description_does_not_ask_for_full_round_trip(self):
        desc = next(t.description for t in get_all_tools() if t.name == "write_file")
        assert "Always read_file first" not in desc
        assert "Always read a file before overwriting it" not in desc
        assert "use edit_file" in desc
        assert "read_file returns a window" in desc

    def test_edit_file_description_does_not_ask_to_read_the_full_file(self):
        desc = next(t.description for t in get_all_tools() if t.name == "edit_file")
        assert "reading the full file" not in desc
        assert "read the full file" not in desc
        assert "read_file returns a window" in desc
        assert "already have in hand" in desc


class TestExecCommand:
    def test_exec_without_callback_denied(self):
        set_approval_callback(None)
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("exec_command", {"command": "echo hi"}, proj, "special:coder")
            assert r.success is False
            assert "approval" in r.error.lower()

    def test_exec_with_callback_allow(self):
        set_approval_callback(lambda sk, tn, args: True)
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("exec_command", {"command": "echo hello"}, proj, "special:coder")
            assert r.success is True
            assert "hello" in r.output

    def test_exec_with_callback_deny(self):
        set_approval_callback(lambda sk, tn, args: False)
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("exec_command", {"command": "echo hi"}, proj, "special:coder")
            assert r.success is False
            assert "approval" in r.error.lower()

    def test_exec_blocklist_rm_rf(self):
        set_approval_callback(lambda sk, tn, args: True)  # would approve
        with tempfile.TemporaryDirectory() as proj:
            # These MUST be blocked regardless of approval
            for cmd in ["rm -rf /", "rm -rf /*", "mkfs", "wipefs"]:
                r = execute_tool("exec_command", {"command": cmd}, proj, "special:coder")
                assert r.success is False, f"{cmd!r} should be blocked"
                assert "blocked" in r.error.lower()

    def test_exec_safe_commands_allowed(self):
        """Commands that touch /dev/null or /tmp are not blocked."""
        set_approval_callback(lambda sk, tn, args: True)
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("exec_command", {"command": "dd if=/dev/zero of=/dev/null count=1"}, proj, "special:coder")
            assert r.success is True, f"Safe dd should succeed, got: {r.error}"

    def test_exec_timeout(self):
        set_approval_callback(lambda sk, tn, args: True)
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("exec_command", {"command": "sleep 10", "timeout": 1}, proj, "special:coder")
            assert r.success is False
            assert "timed out" in r.error.lower()

    # ── MED-2: env scrubbing ────────────────────────────────────────────
    def test_exec_command_scrubs_secrets_from_env(self, monkeypatch):
        """MED-2 (Phase 6): subprocess env must NOT inherit provider secrets.

        Sets a fake secret in the parent env, runs `env` via exec_command, and
        asserts the secret does NOT appear in the subprocess output.
        """
        set_approval_callback(lambda sk, tn, args: True)
        # Plant a fake secret that should be scrubbed
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-secret-DO-NOT-LEAK-12345")
        monkeypatch.setenv("CRABCAKES_GATEWAY_TOKEN", "fake-gateway-token-67890")
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("exec_command", {"command": "env"}, proj, "special:coder")
        assert r.success is True
        assert "sk-fake-secret-DO-NOT-LEAK-12345" not in r.output, (
            f"MED-2: secret leaked into subprocess env! Output: {r.output[:500]}"
        )
        assert "fake-gateway-token-67890" not in r.output, (
            f"MED-2: gateway token leaked into subprocess env! Output: {r.output[:500]}"
        )
        # Sanity: the safe allowlist vars ARE present
        assert "PATH=" in r.output or "PATH" in r.output

    def test_exec_command_allows_safe_vars(self, monkeypatch):
        """MED-2: safe vars (PATH, HOME, LANG) ARE forwarded to subprocess."""
        set_approval_callback(lambda sk, tn, args: True)
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/home/test")
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("exec_command", {"command": "env"}, proj, "special:coder")
        assert r.success is True
        # At least PATH should be in the output
        assert "/usr/bin:/bin" in r.output or "PATH" in r.output

    def test_get_scrubbed_env_allowlist(self):
        """MED-2: the allowlist in utils.env_security is the source of truth."""
        from utils.env_security import ALLOWED_SUBPROCESS_ENV_VARS, get_scrubbed_env
        # All allowed vars are documented shell/locale vars
        expected_safe = {"PATH", "HOME", "LANG", "LC_ALL", "LANGUAGES", "TZ", "TMPDIR", "PWD"}
        assert ALLOWED_SUBPROCESS_ENV_VARS == frozenset(expected_safe)
        # Function returns only those vars
        import os as _os
        _os.environ["MED2_TEST_SECRET"] = "should-not-appear"
        try:
            scrubbed = get_scrubbed_env()
            assert "MED2_TEST_SECRET" not in scrubbed
            assert "PATH" in scrubbed  # PATH is in env from test runner
        finally:
            del _os.environ["MED2_TEST_SECRET"]


class TestEnvSecurity:
    """MED-2: dedicated tests for utils.env_security."""

    def test_allowlist_excludes_api_keys(self):
        from utils.env_security import ALLOWED_SUBPROCESS_ENV_VARS
        # None of the common secret env var names should be in the allowlist
        for name in [
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_KEY",
            "CRABCAKES_GATEWAY_TOKEN", "OPENCLAW_DEVICE_AUTH",
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
            "GITHUB_TOKEN", "GH_TOKEN", "NPM_TOKEN",
        ]:
            assert name not in ALLOWED_SUBPROCESS_ENV_VARS, (
                f"MED-2: secret env var {name} is in the forward allowlist!"
            )

    def test_allowlist_includes_shell_basics(self):
        from utils.env_security import ALLOWED_SUBPROCESS_ENV_VARS
        for name in ["PATH", "HOME", "LANG", "LC_ALL", "TZ", "PWD"]:
            assert name in ALLOWED_SUBPROCESS_ENV_VARS


class TestListFiles:
    def test_list_empty_directory(self):
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("list_files", {"path": "."}, proj)
            assert r.success is True
            assert "(empty)" in r.output

    def test_list_recursive(self):
        with tempfile.TemporaryDirectory() as proj:
            os.makedirs(os.path.join(proj, "a", "b"))
            open(os.path.join(proj, "a", "f.txt"), "w").close()
            open(os.path.join(proj, "a", "b", "nested.txt"), "w").close()
            r = execute_tool("list_files", {"path": ".", "recursive": True}, proj)
            assert r.success is True
            assert "a/f.txt" in r.output
            assert "a/b/nested.txt" in r.output  # nested file shows the dir path


class TestSearchFiles:
    def test_search_finds_match(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "a.py"), "w") as f:
                f.write("def foo():\n    pass")
            r = execute_tool("search_files", {"pattern": "def foo"}, proj)
            assert r.success is True
            assert "foo" in r.output

    def test_search_no_match(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "a.py"), "w") as f:
                f.write("def foo():\n    pass")
            r = execute_tool("search_files", {"pattern": "NOTFOUND"}, proj)
            assert r.success is True
            assert "(no matches)" in r.output

    def test_search_file_type_filter(self):
        with tempfile.TemporaryDirectory() as proj:
            with open(os.path.join(proj, "a.py"), "w") as f:
                f.write("def foo(): pass")
            with open(os.path.join(proj, "b.txt"), "w") as f:
                f.write("def foo(): pass")
            r = execute_tool("search_files", {"pattern": "def foo", "file_type": "py"}, proj)
            assert r.success is True
            assert "a.py" in r.output
            assert "b.txt" not in r.output


class TestWebSearch:
    def test_web_search_without_api_key(self):
        # Remove any API key from environment for this test
        import os
        for key in ("BRAVE_API_KEY", "OPENCLAW_BRAVE_API_KEY"):
            os.environ.pop(key, None)
        r = execute_tool("web_search", {"query": "test"}, "/tmp")
        assert r.success is False
        assert "BRAVE_API_KEY" in r.error


class TestWebFetch:
    def test_web_fetch_success(self):
        r = execute_tool("web_fetch", {"url": "https://example.com"}, "/tmp")
        assert r.success is True
        assert len(r.output) > 0

    def test_web_fetch_truncates(self):
        # The response is small so truncation won't trigger in practice,
        # but verify the parameter is accepted
        r = execute_tool("web_fetch", {"url": "https://example.com", "max_chars": 50}, "/tmp")
        assert r.success is True
        assert len(r.output) <= 50 + 50  # allow for truncation message

    # ── MED-3: re-check-after-redirect ──────────────────────────────
    def test_web_fetch_rejects_loopback_initial(self, monkeypatch):
        """MED-3: loopback URL is rejected when restriction is enabled."""
        monkeypatch.setenv("CRABCAKES_WEB_FETCH_RESTRICT", "1")
        r = execute_tool("web_fetch", {"url": "http://127.0.0.1/"}, "/tmp")
        assert r.success is False
        assert "MED-3" in r.error
        assert "loopback" in r.error.lower()

    def test_web_fetch_rejects_redirect_to_loopback(self, monkeypatch):
        """MED-3 (Phase 6.1): a public URL that redirects to loopback is rejected.

        Verifies that _reject_restricted_url is called on the Location header
        BEFORE the TCP connection to the redirect target is made.
        """
        from agent import tools as tools_mod

        monkeypatch.setenv("CRABCAKES_WEB_FETCH_RESTRICT", "1")

        class FakeRedirectResponse:
            """Simulates a 302 redirect response."""
            status_code = 302
            headers: ClassVar[dict] = {"location": "http://127.0.0.1/private", "content-type": "text/html"}
            text = ""
            url = "http://initial.example.com/"

            def raise_for_status(self):
                pass

        # Patch _reject_restricted_url so the initial URL passes but loopback is blocked
        def fake_reject(url):
            from urllib.parse import urlparse as _up

            from agent.tools import ToolResult
            p = _up(url)
            host = (p.hostname or "").lower()
            if host in ("127.0.0.1", "localhost", "::1"):
                return ToolResult(success=False, error=f"MED-3: Refusing loopback request: {url}")
            return None  # allow

        def fake_get(url, **kwargs):
            assert kwargs.get("follow_redirects") is False, \
                f"Expected follow_redirects=False, got {kwargs.get('follow_redirects')}"
            return FakeRedirectResponse()

        monkeypatch.setattr(tools_mod, "_reject_restricted_url", fake_reject)
        monkeypatch.setattr(tools_mod.httpx, "get", fake_get)
        r = execute_tool("web_fetch", {"url": "https://initial.example.com/"}, "/tmp")
        assert r.success is False, f"Expected rejection, got: {r}"
        assert "MED-3" in r.error
        assert "Redirected to blocked" in r.error or "127.0.0.1" in r.error

    def test_web_fetch_rejects_redirect_to_private_ip(self, monkeypatch):
        """MED-3 (Phase 6.1): redirect to RFC1918 private IP is rejected before connecting."""
        from agent import tools as tools_mod

        monkeypatch.setenv("CRABCAKES_WEB_FETCH_RESTRICT", "1")

        class FakeRedirectResponse:
            status_code = 302
            headers: ClassVar[dict] = {"location": "http://10.0.0.5/internal", "content-type": "text/html"}
            text = ""
            url = "http://initial.example.com/"

            def raise_for_status(self):
                pass

        def fake_reject(url):
            import ipaddress as _ip
            from urllib.parse import urlparse as _up

            from agent.tools import ToolResult
            p = _up(url)
            host = p.hostname or ""
            try:
                ip = _ip.ip_address(host)
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    return ToolResult(success=False, error=f"MED-3: Refusing private IP request: {url}")
            except ValueError:
                pass
            return None  # allow

        def fake_get(url, **kwargs):
            assert kwargs.get("follow_redirects") is False
            return FakeRedirectResponse()

        monkeypatch.setattr(tools_mod, "_reject_restricted_url", fake_reject)
        monkeypatch.setattr(tools_mod.httpx, "get", fake_get)
        r = execute_tool("web_fetch", {"url": "https://initial.example.com/"}, "/tmp")
        assert r.success is False
        assert "MED-3" in r.error
        assert "Redirected to blocked" in r.error

    def test_web_fetch_no_redirect_check_when_unrestricted(self, monkeypatch):
        """MED-3: when restriction is OFF, redirect chain is not re-checked."""
        from agent import tools as tools_mod

        monkeypatch.delenv("CRABCAKES_WEB_FETCH_RESTRICT", raising=False)

        class FakeOkResponse:
            status_code = 200
            url = "http://example.com/"
            headers: ClassVar[dict] = {"content-type": "text/html"}
            text = "<html>ok</html>"

            def raise_for_status(self):
                pass

        def fake_get(url, **kwargs):
            return FakeOkResponse()

        monkeypatch.setattr(tools_mod.httpx, "get", fake_get)
        r = execute_tool("web_fetch", {"url": "https://example.com/"}, "/tmp")
        assert r.success is True

    def test_web_fetch_validates_location_before_following(self, monkeypatch):
        """MED-3 (Phase 6.1): httpx.get must be called with follow_redirects=False,
        and the private IP URL must NEVER be passed to httpx.get.

        QA-NEW-1 fix: use the REAL _reject_restricted_url with restricted mode ON,
        not a fake that only checks 127.0.0.1/localhost/::1. The previous fake
        would let the test pass even if the production check were broken for
        other private-IP forms (169.254.x, RFC1918, IPv6 link-local, etc.).
        """
        from agent import tools as tools_mod

        monkeypatch.setenv("CRABCAKES_WEB_FETCH_RESTRICT", "1")

        class FakeRedirectResponse:
            status_code = 302
            headers: ClassVar[dict] = {"location": "http://127.0.0.1/private", "content-type": "text/html"}
            text = ""
            url = "http://initial.example.com/"

            def raise_for_status(self):
                pass

        urls_called = []

        def fake_get(url, **kwargs):
            urls_called.append(url)
            return FakeRedirectResponse()

        # Patch getaddrinfo so the real _reject_restricted_url sees a public IP
        # for the initial hostname (otherwise the test would fail at the initial
        # check, never reaching the redirect path we want to exercise).
        import socket as _socket
        orig_getaddrinfo = _socket.getaddrinfo
        def fake_getaddrinfo(host, *args, **kwargs):
            if host == "initial.example.com":
                return [(2, 1, 6, "", ("93.184.216.34", 0))]  # example.com
            return orig_getaddrinfo(host, *args, **kwargs)
        monkeypatch.setattr(_socket, "getaddrinfo", fake_getaddrinfo)

        # Only mock httpx.get — let the real _reject_restricted_url validate.
        monkeypatch.setattr(tools_mod.httpx, "get", fake_get)
        r = execute_tool("web_fetch", {"url": "https://initial.example.com/"}, "/tmp")

        # Must return failure
        assert r.success is False
        assert "MED-3" in r.error
        assert "Redirected to blocked" in r.error

        # The private IP URL must NEVER have been passed to httpx.get
        for called_url in urls_called:
            assert "127.0.0.1" not in called_url, \
                f"MED-3 violation: httpx.get was called with blocked URL {called_url}"

    def test_web_fetch_blocks_private_ip_mid_redirect_chain(self, monkeypatch):
        """QA-NEW-2: a private IP appearing as redirect #5 (not #1) must be blocked.

        The previous exceeded_max_redirects test only verified the for/else
        fired with allowed redirects. It did not verify the URL was validated.
        This test exercises both code paths simultaneously: a chain where
        hops 1-4 are public, hop 5 is private. The MED-3 check must fire
        before the 6th httpx.get call.
        """
        from agent import tools as tools_mod

        monkeypatch.setenv("CRABCAKES_WEB_FETCH_RESTRICT", "1")

        locations = [
            "https://public{}.example.com/h{}",
            "https://public{}.example.com/h{}",
            "https://public{}.example.com/h{}",
            "https://public{}.example.com/h{}",
            "http://127.0.0.1/private",  # hop 5: blocked
            "https://should-never-be-fetched.example.com/",
        ]

        class FakeHopResponse:
            def __init__(self, status, location, url):
                self.status_code = status
                self.headers = {"location": location, "content-type": "text/html"} if location else {"content-type": "text/html"}
                self.text = ""
                self.url = url

            def raise_for_status(self):
                pass

        urls_called = []
        hop = [0]

        def fake_get(url, **kwargs):
            urls_called.append(url)
            i = hop[0]
            hop[0] += 1
            if i < len(locations) - 1:
                return FakeHopResponse(302, locations[i].format(i + 1, i + 2), url)
            return FakeHopResponse(302, locations[i], url)

        # Patch getaddrinfo so the real _reject_restricted_url sees public IPs
        # for the public hops (otherwise the test would fail before reaching
        # hop 5 where the private IP is meant to be caught).
        import socket as _socket
        orig_getaddrinfo = _socket.getaddrinfo
        def fake_getaddrinfo(host, *args, **kwargs):
            if host == "start.example.com" or host.startswith("public") and host.endswith(".example.com"):
                return [(2, 1, 6, "", ("93.184.216.34", 0))]  # example.com
            return orig_getaddrinfo(host, *args, **kwargs)
        monkeypatch.setattr(_socket, "getaddrinfo", fake_getaddrinfo)

        monkeypatch.setattr(tools_mod.httpx, "get", fake_get)
        r = execute_tool("web_fetch", {"url": "https://start.example.com/"}, "/tmp")

        assert r.success is False
        assert "MED-3" in r.error
        assert "127.0.0.1" in r.error
        # The 6th URL must NEVER have been fetched
        assert len(urls_called) == 5, (
            f"Expected exactly 5 httpx.get calls (hop 5 blocked before fetch), "
            f"got {len(urls_called)}: {urls_called}"
        )
        assert "127.0.0.1" not in urls_called[-1], (
            f"Last httpx.get call was the blocked private IP: {urls_called[-1]}"
        )

    # ── P6.1-1: raise_for_status error handling ────────────────────

    def test_web_fetch_http_error_caught(self, monkeypatch):
        """P6.1-1: HTTPStatusError from raise_for_status must be caught.

        A non-redirect 4xx/5xx response must return a ToolResult with a
        descriptive error, not raise an uncaught HTTPStatusError.
        """
        from agent import tools as tools_mod

        class FakeErrorResponse:
            status_code = 404
            url = "http://example.com/notfound"
            headers: ClassVar[dict] = {"content-type": "text/html"}
            text = "<html>not found</html>"

            def raise_for_status(self):
                raise tools_mod.httpx.HTTPStatusError(
                    "404 Not Found",
                    request=tools_mod.httpx.Request("GET", "http://example.com/notfound"),
                    response=tools_mod.httpx.Response(404),
                )

        monkeypatch.delenv("CRABCAKES_WEB_FETCH_RESTRICT", raising=False)
        monkeypatch.setattr(tools_mod.httpx, "get", lambda *a, **kw: FakeErrorResponse())
        r = execute_tool("web_fetch", {"url": "https://example.com/notfound"}, "/tmp")
        assert r.success is False
        assert "404" in r.error

    # ── P6.1-2: max redirects exceeded ─────────────────────────────

    def test_web_fetch_exceeded_max_redirects(self, monkeypatch):
        """P6.1-2: when all 10 iterations are redirects, the for/else fires.

        Previously the loop would exit and fall through to raise_for_status
        on a 3xx response (which doesn't raise), returning empty content.
        Now it returns a proper error.
        """
        from agent import tools as tools_mod

        monkeypatch.delenv("CRABCAKES_WEB_FETCH_RESTRICT", raising=False)

        class FakeAlwaysRedirect:
            status_code = 302
            headers: ClassVar[dict] = {"location": "http://example.com/next"}
            text = ""
            url = "http://example.com/"

            def raise_for_status(self):
                pass  # should never be called

        call_count = [0]

        def fake_get(url, **kwargs):
            call_count[0] += 1
            return FakeAlwaysRedirect()

        monkeypatch.setattr(tools_mod.httpx, "get", fake_get)
        r = execute_tool("web_fetch", {"url": "https://example.com/loop"}, "/tmp")
        assert r.success is False
        assert "max redirects" in r.error.lower()
        assert call_count[0] == 10



# ═══════════════════════════════════════════════════════════════════
#  Approval callback
# ═══════════════════════════════════════════════════════════════════

class TestApprovalCallback:
    def test_callback_receives_correct_args(self):
        received = {}
        def tracker(sk, tn, args):
            received["session_key"] = sk
            received["tool_name"] = tn
            received["args"] = args
            return True
        set_approval_callback(tracker)
        with tempfile.TemporaryDirectory() as proj:
            execute_tool("exec_command", {"command": "echo test"}, proj, "special:coder")
        assert received["session_key"] == "special:coder"
        assert received["tool_name"] == "exec_command"
        assert received["args"]["command"] == "echo test"
        assert received["args"]["cwd"] == proj
        set_approval_callback(None)  # clean up

    def test_callback_exception_returns_false(self):
        set_approval_callback(lambda sk, tn, args: 1 / 0)
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("exec_command", {"command": "echo test"}, proj, "special:coder")
        assert r.success is False
        assert "approval" in r.error.lower()
        set_approval_callback(None)


# ═══════════════════════════════════════════════════════════════════
#  Allowed-tools enforcement (§3.21n)
# ═══════════════════════════════════════════════════════════════════

class TestAllowedToolsGate:
    """The allowed_tools parameter on execute_tool() is the single
    authorization gate for non-default agents. Without it, the API-schema
    filter in get_tool_definitions_for_api is advisory only."""

    def test_disallowed_tool_returns_denied(self):
        """Agent without write_file in allowed_tools cannot invoke it."""
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool(
                "write_file",
                {"path": "x.txt", "content": "evil"},
                proj,
                allowed_tools=["read_file", "list_files"],
            )
        assert r.success is False
        assert "write_file" in r.error
        assert "allowed_tools" in r.error

    def test_disallowed_tool_creates_no_file(self):
        """Side-effect-free denial — no file written even on disallowed call."""
        with tempfile.TemporaryDirectory() as proj:
            execute_tool(
                "write_file",
                {"path": "should_not_exist.txt", "content": "leaked"},
                proj,
                allowed_tools=["read_file"],
            )
            full = os.path.join(proj, "should_not_exist.txt")
            assert not os.path.exists(full), "disallowed write_file leaked to disk"

    def test_allowed_tool_succeeds(self):
        """Tools in the allow-list still execute normally."""
        with tempfile.TemporaryDirectory() as proj:
            target = os.path.join(proj, "ok.txt")
            r = execute_tool(
                "write_file",
                {"path": "ok.txt", "content": "hello"},
                proj,
                allowed_tools=["write_file", "read_file"],
            )
            # Assertions inside the with-block: tempfile.TemporaryDirectory
            # cleans up on exit, so checking the file after the with-block
            # would fail (the temp dir no longer exists).
            assert r.success is True
            assert os.path.exists(target)

    def test_none_allowed_tools_permits_all(self):
        """Back-compat: allowed_tools=None permits any registered tool."""
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool(
                "write_file",
                {"path": "x.txt", "content": "hi"},
                proj,
                allowed_tools=None,
            )
        assert r.success is True

    def test_default_arg_permits_all(self):
        """Back-compat: omitting allowed_tools entirely permits any tool."""
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool("write_file", {"path": "x.txt", "content": "hi"}, proj)
        assert r.success is True

    def test_empty_allowed_list_denies_everything(self):
        """An explicit empty list denies everything (not None)."""
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool(
                "read_file",
                {"path": "x.txt"},
                proj,
                allowed_tools=[],
            )
        assert r.success is False
        assert "read_file" in r.error

    def test_unknown_tool_still_rejected_before_gate(self):
        """The allowed_tools gate fires first (covers both built-in and MCP tools).
        An unknown tool name with allowed_tools set is rejected by the gate,
        not by the 'Unknown tool' lookup. This is intentional: the gate is the
        single enforcement point for tool authorization."""
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool(
                "nonexistent_tool",
                {},
                proj,
                allowed_tools=["read_file"],
            )
        assert r.success is False
        assert "not in the agent's allowed_tools" in r.error

    def test_realistic_special_agent_config(self):
        """Mirror the actual failing scenario: special:coder-style tools
        without write/edit should deny those tools."""
        # Approximates a 'read-only investigator' agent.
        readonly_tools = ["read_file", "list_files", "search_files", "file_search"]
        with tempfile.TemporaryDirectory() as proj:
            r = execute_tool(
                "edit_file",
                {"path": "x.py", "old_text": "a", "new_text": "b"},
                proj,
                allowed_tools=readonly_tools,
            )
        assert r.success is False
        assert "edit_file" in r.error


# ═══════════════════════════════════════════════════════════════════
#  Tool-schema payload size budget
# ═══════════════════════════════════════════════════════════════════

DEFAULT_AGENTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "prompts", "default_agents"
)

# The provider gate rejects a tool payload >= this many characters. Each
# default agent's serialized tool schema must stay under it.
TOOL_PAYLOAD_BUDGET = 4_000


class TestToolSchemaSizeBudget:
    """Guard the per-agent tool-schema budgets in prompts/default_agents/*.yaml.

    The provider rejects a tool payload at/over 4,000 chars, so each default
    agent's serialized ``get_tool_definitions_for_api`` output must stay under
    that ceiling with margin to spare."""

    @pytest.mark.parametrize("agent", ["coder", "debugger", "supervisor"])
    def test_default_agent_tool_payload_under_budget(self, agent):
        cfg_path = os.path.join(DEFAULT_AGENTS_DIR, f"{agent}.yaml")
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        tools = cfg["tools"]
        defs = get_tool_definitions_for_api(tools)
        blob = json.dumps(defs, separators=(",", ":"))
        assert len(blob) < TOOL_PAYLOAD_BUDGET, (
            f"{agent} tool-schema JSON is {len(blob)} chars "
            f"(budget {TOOL_PAYLOAD_BUDGET}, over by "
            f"{len(blob) - TOOL_PAYLOAD_BUDGET})"
        )
