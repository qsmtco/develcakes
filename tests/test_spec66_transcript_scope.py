"""SPEC-66 §2.5/§12.6 — conversation scoping (R-16).

One session key keeps one transcript PER PROJECT: the file is
``<session_key>__<sha16>.json`` where sha16 = first 16 hex of
sha256(realpath(project_path)), or ``__none`` when project_path is None.
A legacy ``<session_key>.json`` is adopted ONLY when its stored
``project_path`` realpaths to the requested project, and the adopted
conversation carries that project BEFORE the caller saves (§10.3), so the
scoped copy lands under the real slug — never ``__none``.

Runner: plain (§12.7). No ui.window / ui.views / agent_runtime_handler /
feed_handler imports. Isolation: the autouse
``_isolate_config_resolution`` fixture redirects HOME and XDG_CONFIG_HOME;
this file never monkeypatches ``utils.config.get_config_dir`` (§12.11).
"""
from __future__ import annotations

import hashlib
import json
import os

from agent.persistence import (
    conversations_dir,
    conversation_store_name,
    load_conversation_from_disk,
    save_conversation_to_disk,
)


def _sha16(project_path: str) -> str:
    return hashlib.sha256(
        os.path.realpath(project_path).encode("utf-8")
    ).hexdigest()[:16]


def _payload(session_key: str, project_path, content: str) -> dict:
    """A minimal on-disk conversation body (what save_conversation_to_disk writes)."""
    return {
        "session_key": session_key,
        "agent_name": "Coder",
        "project_path": project_path,
        "model": "openai/gpt-4o",
        "provider": "openai",
        "messages": [
            {
                "role": "user",
                "content": content,
                "tool_calls": [],
                "tool_call_id": None,
                "tokens_used": 0,
            }
        ],
        "system_prompt": "",
        "total_tokens": 0,
        "total_cost": 0.0,
        "step_count": 0,
    }


def _write(directory: str, name: str, data: dict) -> str:
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    return path


class TestScopedTranscripts:
    """R-16: the four §12.6 nodes."""

    def test_store_name_is_scoped(self, tmp_path):
        """The name carries the slug; None is the `none` slug."""
        project = tmp_path / "proj"
        project.mkdir()
        name = conversation_store_name("agent:coder", str(project))
        assert name == f"agent:coder__{_sha16(str(project))}.json"
        assert conversation_store_name("agent:coder", None) == "agent:coder__none.json"
        assert conversation_store_name("agent:coder", "") == "agent:coder__none.json"

    def test_other_project_is_not_loaded(self, tmp_path):
        """A transcript saved for project A is invisible from project B."""
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        _write(conversations_dir(), conversation_store_name("agent:coder", str(a)),
               _payload("agent:coder", str(a), "from project A"))

        assert load_conversation_from_disk("agent:coder", str(b)) is None
        hit = load_conversation_from_disk("agent:coder", str(a))
        assert hit is not None
        conv, _data, adopted = hit
        assert conv.messages[0].content == "from project A"
        assert adopted is False

    def test_legacy_file_is_rejected_on_realpath_mismatch(self, tmp_path):
        """Legacy `<sk>.json` for another project is NOT adopted."""
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        _write(conversations_dir(), "agent:coder.json",
               _payload("agent:coder", str(a), "legacy from A"))

        assert load_conversation_from_disk("agent:coder", str(b)) is None
        # A None project_path never reads the legacy file at all (§2.5 step 4).
        assert load_conversation_from_disk("agent:coder", None) is None

    def test_legacy_hit_writes_scoped_file_with_project_slug(self, tmp_path):
        """§10.3: the conversation carries the project BEFORE the save, so the
        scoped copy lands under the real sha16 slug and NOT under __none."""
        project = tmp_path / "proj"
        project.mkdir()
        _write(conversations_dir(), "agent:coder.json",
               _payload("agent:coder", str(project), "adopt me"))

        hit = load_conversation_from_disk("agent:coder", str(project))
        assert hit is not None
        conv, _data, adopted = hit
        assert adopted is True
        # The runtime's contract: project_path is set on the conversation
        # (from the VALIDATED raw string) before any save happens.
        assert conv.project_path == str(project)

        save_conversation_to_disk(conv, "agent:coder")

        scoped = os.path.join(
            conversations_dir(),
            f"agent:coder__{_sha16(str(project))}.json",
        )
        assert os.path.isfile(scoped), "scoped file was not written under the real slug"
        assert os.path.abspath(scoped).startswith(os.path.abspath(str(tmp_path)))
        assert not os.path.isfile(
            os.path.join(conversations_dir(), "agent:coder__none.json")
        ), "the legacy hit must NOT be saved under the empty-project slug"
        # The legacy file is left in place (accepted duplication debt, §11.7).
        assert os.path.isfile(os.path.join(conversations_dir(), "agent:coder.json"))

    def test_scoped_file_wins_over_legacy_with_different_content(self, tmp_path):
        """Both files present: the scoped one is authoritative."""
        project = tmp_path / "proj"
        project.mkdir()
        _write(conversations_dir(), "agent:coder.json",
               _payload("agent:coder", str(project), "stale legacy"))
        _write(conversations_dir(), conversation_store_name("agent:coder", str(project)),
               _payload("agent:coder", str(project), "fresh scoped"))

        hit = load_conversation_from_disk("agent:coder", str(project))
        assert hit is not None
        conv, _data, adopted = hit
        assert conv.messages[0].content == "fresh scoped"
        assert adopted is False

    def test_refuses_a_session_key_that_could_escape_the_dir(self):
        """conversation_store_name rejects path-ish keys (ValueError)."""
        import pytest

        for bad in ("../escape", "a/b", "..", ""):
            with pytest.raises(ValueError):
                conversation_store_name(bad, None)
