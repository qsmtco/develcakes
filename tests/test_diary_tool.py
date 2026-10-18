"""diary remember/recall goes through the append-only context.md path."""

from agent.tools import execute_tool
from utils.project_awareness import load_project_context


def test_remember_persists_heading(tmp_path):
    result = execute_tool(
        "diary",
        {"action": "remember", "heading": "## 2026-10-18 — Auth", "body": "Use argon2."},
        str(tmp_path),
        session_key="special:coder",
    )
    assert result.success is True
    assert result.output == "OK — appended an entry to .crabcakes/context.md"
    loaded = load_project_context(str(tmp_path))
    assert "## 2026-10-18 — Auth" in loaded
    assert "Use argon2." in loaded


def test_remember_without_heading_does_not_write(tmp_path):
    diary = tmp_path / ".crabcakes"
    diary.mkdir()
    path = diary / "context.md"
    path.write_text("## keep\n\nuntouched\n", encoding="utf-8")
    result = execute_tool(
        "diary",
        {"action": "remember", "heading": "Auth decision", "body": "nope"},
        str(tmp_path),
        session_key="special:coder",
    )
    assert result.success is False
    assert "must start with ##" in result.error
    assert path.read_text(encoding="utf-8") == "## keep\n\nuntouched\n"


def test_recall_match_and_miss(tmp_path):
    execute_tool(
        "diary",
        {"action": "remember", "heading": "## Note", "body": "argon2 not bcrypt"},
        str(tmp_path),
        session_key="special:coder",
    )
    hit = execute_tool(
        "diary",
        {"action": "recall", "query": "argon2"},
        str(tmp_path),
        session_key="special:coder",
    )
    assert hit.success is True
    assert "argon2 not bcrypt" in hit.output
    miss = execute_tool(
        "diary",
        {"action": "recall", "query": "sqlite"},
        str(tmp_path),
        session_key="special:coder",
    )
    assert miss.success is True
    assert miss.output == "No diary entries match."


def test_recall_empty_query(tmp_path):
    result = execute_tool(
        "diary",
        {"action": "recall", "query": "  "},
        str(tmp_path),
        session_key="special:coder",
    )
    assert result.success is False
    assert result.error == "diary recall needs a query"
