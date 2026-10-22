"""SPEC-39 §2.6 chat-handler bullets (Phase 4): the ALL-branch delivery.

The three bullets from `docs/specs/SPEC-39-ROOM-ADDRESSING.md` §2.6:

- Choice `special:debugger` → one send to that member with the raw text and
  `reply_target="project:foo"`; `record_room_line("special:coder", ...)` for
  every OTHER member; the addressee itself never gets the human line as a PM.
- Choice `all` → both members are sent; `record_room_line` is never called
  with the human text.
- `get_solo_target` returns `special:coder` → the resolver is not called and
  one send goes to `special:coder` (the existing solo branch).

Fakes mirror `tests/test_chat_handler.py` (`FakeMainContent`,
`FakeProjectsModule`, `make_handler`) plus the SPEC-39 runtime contracts
(`get_agent_name_for_session`, `mark_room_turn`, `record_room_line`).
"""

from tests.test_chat_handler import FakeMainContent, FakeProjectsModule, make_handler


class FakeRuntimeHandler:
    """AgentRuntimeHandler fake with the SPEC-39 §2.3/§2.5 contracts."""

    def __init__(self):
        self.sent = []        # (session_key, text, reply_target)
        self.room_marks = []  # session_keys marked as room-turn speakers
        self.room_lines = []  # (session_key, line) witness lines
        self.names = {}       # session_key -> display name

    def get_special_agents(self):
        return {}

    def send_to_special_agent(self, session_key, text, reply_target=None):
        self.sent.append((session_key, text, reply_target))

    def get_agent_name_for_session(self, session_key):
        return self.names.get(session_key, "")

    def mark_room_turn(self, session_key):
        self.room_marks.append(session_key)

    def record_room_line(self, session_key, line):
        self.room_lines.append((session_key, line))


class FakeProjectHandler:
    def __init__(self, members, solo=None):
        self._members = list(members)
        self._solo = solo

    def get_project_members(self, project_name):
        return list(self._members)

    def get_solo_target(self, project_name):
        return self._solo


MEMBERS = ["special:coder", "special:debugger"]
NAMES = {"special:coder": "Coder", "special:debugger": "Debugger"}


def _make_handler(runtime_handler, solo=None):
    mc = FakeMainContent(session_key="project:foo", input_text="hi debugger")
    handler = make_handler(mc, runtime_handler, projects_module=FakeProjectsModule())
    handler.set_project_handler(FakeProjectHandler(MEMBERS, solo=solo))
    return mc, handler


def test_all_branch_resolver_choice_pms_one_member(monkeypatch):
    """Bullet 1: choice=special:debugger. One send to the addressee with the
    raw text + project reply_target; every OTHER member gets the human line as
    a room PM; the addressee does not get that PM."""
    monkeypatch.setattr(
        "agent.llm.jev_client.resolve_room_addressee",
        lambda text, roster: "special:debugger",
    )
    rt = FakeRuntimeHandler()
    rt.names = dict(NAMES)
    _, handler = _make_handler(rt)

    handler.on_send()

    assert rt.sent == [("special:debugger", "hi debugger", "project:foo")]
    assert rt.room_marks == ["special:debugger"]
    assert rt.room_lines == [("special:coder", "[room] PM: hi debugger")]


def test_all_branch_resolver_choice_all_fans_out(monkeypatch):
    """Bullet 2: choice=all. Both members get the raw text; the human line is
    NOT recorded as a room PM for anyone."""
    monkeypatch.setattr(
        "agent.llm.jev_client.resolve_room_addressee",
        lambda text, roster: "all",
    )
    rt = FakeRuntimeHandler()
    rt.names = dict(NAMES)
    _, handler = _make_handler(rt)

    handler.on_send()

    assert rt.sent == [
        ("special:coder", "hi debugger", "project:foo"),
        ("special:debugger", "hi debugger", "project:foo"),
    ]
    assert rt.room_marks == ["special:coder", "special:debugger"]
    assert rt.room_lines == []


def test_solo_target_skips_resolver(monkeypatch):
    """Bullet 3: a non-None solo target keeps the existing solo branch — the
    resolver is never called, one send goes to the solo member, and no room
    marks / PM lines are produced."""
    calls = {"n": 0}

    def counting_resolver(text, roster):
        calls["n"] += 1
        return "special:debugger"

    monkeypatch.setattr("agent.llm.jev_client.resolve_room_addressee", counting_resolver)
    rt = FakeRuntimeHandler()
    rt.names = dict(NAMES)
    _, handler = _make_handler(rt, solo="special:coder")

    handler.on_send()

    assert calls["n"] == 0
    assert rt.sent == [("special:coder", "hi debugger", "project:foo")]
    assert rt.room_marks == []
    assert rt.room_lines == []
