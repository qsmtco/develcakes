# models/team.py
# Team data models for project awareness.
#
# Manifest: reads nothing, writes nothing, no network
# Architecture: pure data — no GTK, no network, no file I/O.
#
# Files that import these:
#   - utils/project_awareness.py (load/save team.json)
#   - ui/handlers/project_handler.py (membership management)
#   - tests/test_team.py (unit tests)

from dataclasses import dataclass, field


@dataclass
class TeamMember:
    """A single agent or user on a project team."""
    session_key: str           # unique session identifier (gateway key or agent: prefix)
    name: str                  # display name
    role: str = ""             # free-form: "implementation", "diagnostics", "assistant"
    can_write: bool = False    # whether this member can write to project files

    def to_dict(self) -> dict:
        return {
            "session_key": self.session_key,
            "name": self.name,
            "role": self.role,
            "can_write": self.can_write,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TeamMember":
        return cls(
            session_key=data.get("session_key", ""),
            name=data.get("name", ""),
            role=data.get("role", ""),
            can_write=data.get("can_write", False),
        )


@dataclass
class ProjectTeam:
    """Team roster for a project. Stored in .crabcakes/team.json."""
    members: list[TeamMember] = field(default_factory=list)
    pm_name: str = ""          # project manager display name
    pm_id: str = ""            # project manager identifier (e.g. "cli")

    def to_dict(self) -> dict:
        return {
            "members": [m.to_dict() for m in self.members],
            "pm": {
                "name": self.pm_name,
                "id": self.pm_id,
            },
        }

    @classmethod
    def from_dict(cls, data) -> "ProjectTeam":
        # Handle legacy list format (raw session key list) or current dict format.
        # If data is a list, treat it as a list of session keys (legacy members.json).
        # If data is a dict, treat it as {"members": [...], "pm": {...}}.
        if isinstance(data, list):
            members = []
            for sk in data:
                if isinstance(sk, str) and sk:
                    members.append(TeamMember(session_key=sk, name="", role="", can_write=False))
            return cls(members=members)
        if not isinstance(data, dict):
            return cls()
        members_data = data.get("members", [])
        members = [TeamMember.from_dict(m) for m in members_data if isinstance(m, dict)]
        pm = data.get("pm", {})
        return cls(
            members=members,
            pm_name=pm.get("name", ""),
            pm_id=pm.get("id", ""),
        )

    def get_member(self, session_key: str) -> TeamMember | None:
        """Find a member by session key, or None."""
        for m in self.members:
            if m.session_key == session_key:
                return m
        return None

    def get_session_keys(self) -> list[str]:
        """Return all member session keys."""
        return [m.session_key for m in self.members]

    def add_member(self, member: TeamMember) -> None:
        """Add a member. No-op if already present."""
        if not self.get_member(member.session_key):
            self.members.append(member)

    def remove_member(self, session_key: str) -> bool:
        """Remove a member by session key. Returns True if removed."""
        before = len(self.members)
        self.members = [m for m in self.members if m.session_key != session_key]
        return len(self.members) < before

    def has_member(self, session_key: str) -> bool:
        """True if session_key is in the team."""
        return self.get_member(session_key) is not None
