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


# ── SPEC-53 §2: fallback routing (jev_room_fallback) ──────────────────────────

SUPERVISOR_MEMBERS = ["special:supervisor", "special:coder", "special:debugger"]
SUPERVISOR_NAMES = {
    "special:supervisor": "Supervisor",
    "special:coder": "Coder",
    "special:debugger": "Debugger",
}

FALLBACK_NOTE_SUPERVISOR = "[room] JEV offline — routed to Supervisor"
FALLBACK_NOTE_ALL = "[room] JEV offline — broadcast to all (fallback)"
FALLBACK_NOTE_DROP = "[room] JEV offline — message not routed"


def _set_fallback_setting(monkeypatch, value):
    """Patch jev_room_fallback. ``None`` simulates a missing setting (→ default)."""
    if value is None:
        monkeypatch.setattr(
            "agent.config.get_agent_setting",
            lambda key, default=None: default,
        )
    else:
        monkeypatch.setattr(
            "agent.config.get_agent_setting",
            lambda key, default=None: value if key == "jev_room_fallback" else default,
        )


def _make_fallback_handler(runtime_handler, members=MEMBERS, names=NAMES):
    mc = FakeMainContent(session_key="project:foo", input_text="hi debugger")
    handler = make_handler(mc, runtime_handler, projects_module=FakeProjectsModule())
    handler.set_project_handler(FakeProjectHandler(members))
    runtime_handler.names = dict(names)
    return mc, handler


def _force_resolver_empty(monkeypatch):
    monkeypatch.setattr(
        "agent.llm.jev_client.resolve_room_addressee",
        lambda text, roster: [],
    )


FOUR_MEMBERS = [
    "special:supervisor",
    "special:coder",
    "special:debugger",
    "special:alphacoder",
]
FOUR_NAMES = {
    "special:supervisor": "Supervisor",
    "special:coder": "Coder",
    "special:debugger": "Debugger",
    "special:alphacoder": "AlphaCoder",
}


def test_all_branch_resolver_multi_select_two_of_four(monkeypatch):
    """SPEC-53 §1 list path: a 2-of-4 selection wakes exactly the two chosen
    members with the raw text + project reply_target; the other two get the
    human line as a room PM (and not the raw text)."""
    monkeypatch.setattr(
        "agent.llm.jev_client.resolve_room_addressee",
        lambda text, roster: ["special:supervisor", "special:debugger"],
    )
    rt = FakeRuntimeHandler()
    _, handler = _make_fallback_handler(
        rt, members=FOUR_MEMBERS, names=FOUR_NAMES)

    handler.on_send()

    assert rt.sent == [
        ("special:supervisor", "hi debugger", "project:foo"),
        ("special:debugger", "hi debugger", "project:foo"),
    ]
    assert rt.room_marks == ["special:supervisor", "special:debugger"]
    assert rt.room_lines == [
        ("special:coder", "[room] PM: hi debugger"),
        ("special:alphacoder", "[room] PM: hi debugger"),
    ]


def test_all_branch_fallback_supervisor_default_routes_to_supervisor(monkeypatch):
    """SPEC-53 acceptance: JEV returns [] → fallback (default supervisor) wakes
    ONLY the Supervisor member and a visible [room] note appears on its session.
    The other members get the human PM line, not the raw text."""
    _force_resolver_empty(monkeypatch)
    _set_fallback_setting(monkeypatch, None)  # missing → supervisor
    rt = FakeRuntimeHandler()
    _, handler = _make_fallback_handler(
        rt, members=SUPERVISOR_MEMBERS, names=SUPERVISOR_NAMES)

    handler.on_send()

    assert rt.sent == [("special:supervisor", "hi debugger", "project:foo")]
    assert rt.room_marks == ["special:supervisor"]
    assert rt.room_lines == [
        ("special:coder", "[room] PM: hi debugger"),
        ("special:debugger", "[room] PM: hi debugger"),
        ("special:supervisor", FALLBACK_NOTE_SUPERVISOR),
    ]


def test_all_branch_fallback_supervisor_no_supervisor_falls_through_to_all(monkeypatch):
    """Supervisor fallback with no Supervisor member in the roster falls through
    to the historical 'all' broadcast (with the broadcast note line)."""
    _force_resolver_empty(monkeypatch)
    _set_fallback_setting(monkeypatch, "supervisor")
    rt = FakeRuntimeHandler()
    _, handler = _make_fallback_handler(rt)  # MEMBERS has no Supervisor

    handler.on_send()

    assert rt.sent == [
        ("special:coder", "hi debugger", "project:foo"),
        ("special:debugger", "hi debugger", "project:foo"),
    ]
    assert rt.room_marks == ["special:coder", "special:debugger"]
    assert rt.room_lines == [
        ("special:coder", FALLBACK_NOTE_ALL),
        ("special:debugger", FALLBACK_NOTE_ALL),
    ]


def test_all_branch_fallback_invalid_setting_behaves_as_supervisor(monkeypatch):
    """A corrupt/unknown setting value behaves as 'supervisor' (SPEC-53 §2)."""
    _force_resolver_empty(monkeypatch)
    _set_fallback_setting(monkeypatch, "sideways")
    rt = FakeRuntimeHandler()
    _, handler = _make_fallback_handler(
        rt, members=SUPERVISOR_MEMBERS, names=SUPERVISOR_NAMES)

    handler.on_send()

    assert rt.sent == [("special:supervisor", "hi debugger", "project:foo")]
    assert rt.room_marks == ["special:supervisor"]
    assert rt.room_lines[-1] == ("special:supervisor", FALLBACK_NOTE_SUPERVISOR)


def test_all_branch_fallback_setting_all_broadcasts(monkeypatch):
    """jev_room_fallback=all: keep the current broadcast-to-every-member
    behaviour, with the broadcast note line on each member."""
    _force_resolver_empty(monkeypatch)
    _set_fallback_setting(monkeypatch, "all")
    rt = FakeRuntimeHandler()
    _, handler = _make_fallback_handler(rt)

    handler.on_send()

    assert rt.sent == [
        ("special:coder", "hi debugger", "project:foo"),
        ("special:debugger", "hi debugger", "project:foo"),
    ]
    assert rt.room_marks == ["special:coder", "special:debugger"]
    assert rt.room_lines == [
        ("special:coder", FALLBACK_NOTE_ALL),
        ("special:debugger", FALLBACK_NOTE_ALL),
    ]


def test_all_branch_fallback_setting_drop_wakes_nobody(monkeypatch):
    """jev_room_fallback=drop: nobody is woken (no send_local / no marks), the
    human line is rendered as a room PM for every member, and the 'not routed'
    note is visible."""
    _force_resolver_empty(monkeypatch)
    _set_fallback_setting(monkeypatch, "drop")
    rt = FakeRuntimeHandler()
    _, handler = _make_fallback_handler(rt)

    handler.on_send()

    assert rt.sent == []
    assert rt.room_marks == []
    assert rt.room_lines == [
        ("special:coder", "[room] PM: hi debugger"),
        ("special:debugger", "[room] PM: hi debugger"),
        ("special:coder", FALLBACK_NOTE_DROP),
        ("special:debugger", FALLBACK_NOTE_DROP),
    ]


def test_all_branch_fallback_all_invalid_selection(monkeypatch):
    """A list selection with only hallucinated keys (already filtered to []
    by the resolver, but a stray legacy value must not broadcast) triggers the
    same fallback path as []."""
    monkeypatch.setattr(
        "agent.llm.jev_client.resolve_room_addressee",
        lambda text, roster: ["special:nope", "ghost"],
    )
    _set_fallback_setting(monkeypatch, "supervisor")
    rt = FakeRuntimeHandler()
    _, handler = _make_fallback_handler(
        rt, members=SUPERVISOR_MEMBERS, names=SUPERVISOR_NAMES)

    handler.on_send()

    assert rt.sent == [("special:supervisor", "hi debugger", "project:foo")]
    assert rt.room_marks == ["special:supervisor"]
    assert rt.room_lines[-1] == ("special:supervisor", FALLBACK_NOTE_SUPERVISOR)


def test_all_branch_fallback_resolver_exception_routes_same_path(monkeypatch):
    """The exception fallback in _resolve_and_deliver routes through the same
    fallback path (supervisor default), not a hard-coded broadcast."""
    def exploding_resolver(text, roster):
        raise RuntimeError("JEV down")

    monkeypatch.setattr("agent.llm.jev_client.resolve_room_addressee", exploding_resolver)
    _set_fallback_setting(monkeypatch, None)  # missing → supervisor
    rt = FakeRuntimeHandler()
    _, handler = _make_fallback_handler(
        rt, members=SUPERVISOR_MEMBERS, names=SUPERVISOR_NAMES)

    handler.on_send()

    assert rt.sent == [("special:supervisor", "hi debugger", "project:foo")]
    assert rt.room_marks == ["special:supervisor"]
    assert rt.room_lines[-1] == ("special:supervisor", FALLBACK_NOTE_SUPERVISOR)
