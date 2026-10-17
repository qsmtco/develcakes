# tests/test_agent_builder_preserve_tools.py
# Tests for Bugfix C3 (audit BUG #3): the Agent Builder must preserve
# hidden-tool permits across save.
#
# Refs: docs/specs/CONTEXT-EFFICIENCY-C3-BUILDER-PRESERVE.md
#
# The Builder hides file_search (B4), so a loaded agent def can carry tools
# no checkbox represents. AgentBuilderDialog stashes the un-representable
# subset in _hidden_tools_preserved on load (_fill_form) and merges it back
# in _get_selected_tools via the pure helper tested here. All tests are
# GTK-free — the merge logic lives in utils/agent_defs.py.

from __future__ import annotations

from utils.agent_defs import merge_preserved_tools


class TestPreserveHiddenTools:
    def test_load_with_file_search_read_ticked_returns_both(self):
        """The spec's regression scenario: load a def with
        [read_file, file_search], tick read_file, save — file_search must
        survive. Before the fix, _get_selected_tools() returned only the
        ticked checkboxes and silently dropped the B1 replay permit.
        """
        loaded_tools = {"read_file", "file_search"}
        visible_checkboxes = {"read_file"}  # B4 hides file_search — no checkbox
        hidden_preserved = loaded_tools - visible_checkboxes  # _fill_form stash

        merged = merge_preserved_tools(["read_file"], hidden_preserved)
        assert merged == ["read_file", "file_search"]

    def test_hidden_survives_when_nothing_ticked(self):
        """Sad path: user unticks everything (or loads an all-hidden def).
        The hidden permit is still returned, not silently dropped.
        """
        merged = merge_preserved_tools([], {"file_search"})
        assert merged == ["file_search"]

    def test_stash_resets_when_different_agent_loaded(self):
        """_fill_form assigns (not extends) the stash, so loading a second
        agent whose tools are all visible replaces the first agent's stash —
        permits never leak across sessions.
        """
        second_agent_tools = {"read_file", "list_files"}  # all visible
        visible_checkboxes = {"read_file", "list_files"}

        new_stash = second_agent_tools - visible_checkboxes  # _fill_form: assign
        assert new_stash == set()

        merged = merge_preserved_tools(["read_file"], new_stash)
        assert merged == ["read_file"]  # first agent's file_search gone

    def test_no_agent_loaded_stash_starts_empty(self):
        """A new-agent dialog (no _fill_form call) starts with an empty
        stash — save emits only visible ticks.
        """
        merged = merge_preserved_tools(["read_file"], set())
        assert merged == ["read_file"]

    def test_overlap_deduplicates_visible_first(self):
        """Defensive: if a hidden tool somehow also gets ticked, it appears
        exactly once, with visible tools ordered before hidden ones.
        """
        merged = merge_preserved_tools(["read_file", "file_search"], {"file_search"})
        assert merged == ["read_file", "file_search"]

    def test_drops_non_string_junk(self):
        """A malformed stash can never corrupt the saved tools list."""
        merged = merge_preserved_tools(["read_file"], {None, 5, "", "file_search"})
        assert merged == ["read_file", "file_search"]
