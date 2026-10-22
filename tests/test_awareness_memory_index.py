"""MEMORY_INDEX is the index file. PROJECT_MEMORY stays the fact board."""

from utils.project_awareness import build_awareness_dict
from utils.project_memory import dispatch


def test_index_is_separate_from_facts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "utils.project_awareness.load_live_facts",
        lambda _path: [{"text": "fact line", "files": ["a.py"], "sha": "abc"}],
    )
    root = tmp_path / ".crabcakes"
    root.mkdir()
    (root / "facts.json").write_text("[]", encoding="utf-8")
    result = dispatch(
        str(tmp_path), "agent:supervisor", "supervisor", "promote",
        name="auth", body="argon2 decision",
    )
    assert result.success is True
    data = build_awareness_dict(str(tmp_path))
    assert "auth" in data["MEMORY_INDEX"]
    assert "- auth — argon2 decision" in data["MEMORY_INDEX"]
    assert "fact line" in data["PROJECT_MEMORY"]
    assert "argon2 decision" not in data["PROJECT_MEMORY"]
