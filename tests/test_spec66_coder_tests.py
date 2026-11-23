# tests/test_spec66_coder_tests.py — SPEC-66 §2 (finding 9): coders verify.
#
# R-20 row node: test_coder_does_not_disable_test_run
#
# PLAIN runner (§12.7): no GTK, no real pytest spawn. The runtime is built on a
# tmp project and driven through one fake LLM turn; the enforcement check itself
# is replaced by a recorder, so what is asserted is the CONFIG the runtime hands
# the verification tier — not the tier's internals.
#
# Pre-fix the coder branch did
#   dataclasses.replace(enf, lint_check=False, test_run=False)
# so a coder never re-ran the suite after its own writes: the verification tier
# the role exists for was silently off for exactly the role that writes code.

from __future__ import annotations

import uuid
from unittest.mock import patch

from agent.config import AgentConfig, LLMProviderConfig
from agent.runtime import AgentRuntime

GTK_HEADLESS_SAFE = True


def _make_cfg(*, test_run: bool = True) -> AgentConfig:
    return AgentConfig(
        providers={
            "openai": LLMProviderConfig(
                name="openai",
                base_url="https://api.openai.com/v1",
                api_key="test-key",
                default_model="gpt-4o",
            ),
        },
        default_provider="openai",
        default_model="openai/gpt-4o",
        max_tool_iterations=5,
        tool_timeout_seconds=30,
        auto_save_conversations=False,
    )


def _uniq() -> str:
    return f"spec66coder{uuid.uuid4().hex[:8]}"


def _write_then_done(target: str) -> list[dict]:
    """One write_file tool call, then a plain completion."""
    return [
        {
            "choices": [{"message": {
                "content": "",
                "tool_calls": [{
                    "id": "call_1",
                    "function": {
                        "name": "write_file",
                        "arguments": f'{{"path": "{target}", "content": "x = 1\\n"}}',
                    },
                }],
            }}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
        {
            "choices": [{"message": {"content": "Done.", "tool_calls": []}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
    ]


def _run_one_write(tmp_path, *, agent_role: str) -> dict:
    """Run one write-producing turn; return the config the tier received.

    `EnforcementMiddleware` calls `enforcement_check_fn(tool_name, args,
    result, project_path, enforcement_config)` — the recorder captures that
    last argument, which is the per-call enforcement config the coder branch
    builds.
    """
    rt = AgentRuntime(_make_cfg())
    rt.start()
    target = str(tmp_path / "written.py")
    sk = _uniq()
    rt.create_conversation(
        "Coder", sk, str(tmp_path), agent_role=agent_role,
        allowed_tools=["write_file"],
    )

    seen: list = []

    def _recorder(tool_name, args, result, project_path, enforcement_config):
        seen.append(enforcement_config)
        raise AssertionError("stop after recording the config")  # noqa: TRY003

    middleware = rt._tool_chain._middlewares[0]  # EnforcementMiddleware
    with patch.object(middleware, "_check", _recorder):
        responses = _write_then_done(target)
        with patch.object(
            rt, "_call_llm", lambda *a, **k: responses.pop(0),
        ):
            rt._run_loop(sk, "write the file")
    rt.stop()

    assert seen, (
        "enforcement ran zero times for a successful coder write — the "
        "verification tier never saw this call at all"
    )
    return seen[0]


def test_coder_does_not_disable_test_run(tmp_path):
    """R-20 NODE: a coder-role write passes test_run=True to the tier.

    Mutant (R-20.diff) restores `test_run=False`, and this assertion fails.
    """
    ctx = _run_one_write(tmp_path, agent_role="coder")
    assert ctx.test_run is True, (
        "the coder branch disabled test_run again — coders no longer re-run "
        "the suite after their own writes (SPEC-66 §2 finding 9)"
    )


def test_coder_still_disables_lint_check(tmp_path):
    """The OTHER half of the pre-fix replace stays: lint stays off for coders
    (spec: "lint_check stays False"). Removing the whole replace would be a
    different change than the one specified."""
    ctx = _run_one_write(tmp_path, agent_role="coder")
    assert ctx.lint_check is False, (
        "lint_check is no longer forced off for coders — the spec keeps it off"
    )


def test_non_coder_role_keeps_config_defaults(tmp_path):
    """Control: a non-coder role gets the config defaults untouched, so the
    node above is measuring the coder branch and not a global default."""
    ctx = _run_one_write(tmp_path, agent_role="debugger")
    assert ctx.test_run is True
    assert ctx.lint_check is True
