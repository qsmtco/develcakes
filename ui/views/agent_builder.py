# ui/views/agent_builder.py
# GTK4 dialog for creating and editing user-defined agents.
#
# Pure view — receives data from AgentBuilderHandler, emits user actions
# back through callbacks (on_save, on_cancel).
#
# Architecture rule (ARCHITECTURE.md Section 9):
#   - Uses add_css_class() only, no inline CssProvider
#   - No business logic — delegates to handler for validation/persistence
#   - CSS classes: agent-builder-*

from __future__ import annotations

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gio, GLib

from models.providers import ProviderConfig
from utils.agent_defs import VALID_COMPACTION_STRATEGIES
from utils.mcp_config import load_mcp_servers, MCPConfigError


class AgentBuilderDialog:
    """GTK4 dialog for creating/editing agent definitions.

    The mode (create vs. edit) is set explicitly via the `is_edit` parameter,
    NOT inferred from the presence of `agent_def`. The handler's `create_new()`
    returns a non-None template dict for new agents, so a truthiness check on
    `agent_def` would incorrectly classify new agents as edits.

    Args:
        parent: Parent Gtk.Window for transient setting.
        handler: AgentBuilderHandler — provides tool/prompt/provider options.
        agent_def: Optional existing agent dict to pre-fill the form.
            Pass None for new agents, the loaded dict for edits.
        is_edit: True for edit mode (title="Edit Agent", button="Save");
            False for create mode (title="Create Agent", button="Create").
        on_save: Callback with the form dict when user clicks Save.
        on_cancel: Callback when user clicks Cancel or closes window.
    """

    def __init__(
        self,
        parent: Gtk.Window,
        *,
        handler,
        agent_def: dict | None = None,
        is_edit: bool = False,
        on_save=None,
        on_cancel=None,
    ):
        self._handler = handler
        self._on_save = on_save
        self._on_cancel = on_cancel
        self._is_edit = is_edit
        self._original_si = {}  # preserved SI overrides from edit source
        self._tool_checks: dict[str, Gtk.CheckButton] = {}
        self._tool_count_label: Gtk.Label | None = None
        self._mcp_checks: dict[str, Gtk.CheckButton] = {}
        self._providers: list = []  # list[ProviderConfig] — populated via set_provider_options()

        # ── Window setup ──────────────────────────────────────────────
        title = "Edit Agent" if self._is_edit else "Create Agent"
        self._window = Gtk.Window(title=title)
        self._window.set_transient_for(parent)
        self._window.set_modal(True)
        self._window.set_default_size(480, 620)
        self._window.add_css_class("agent-builder-window")

        self._window.connect("close-request", self._on_close_request)

        # ── Build form ────────────────────────────────────────────────
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Header bar with title + buttons
        header = self._build_header(title)
        content.append(header)

        # Scrollable form body
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        form_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        form_box.set_margin_start(20)
        form_box.set_margin_end(20)
        form_box.set_margin_top(16)
        form_box.set_margin_bottom(16)

        # Form fields
        # Name + Role on same row
        name_role_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        name_role_row.set_hexpand(True)
        self._name_entry = Gtk.Entry()
        self._name_entry.set_placeholder_text("Agent name")
        self._name_entry.set_hexpand(True)
        self._name_entry.connect("changed", lambda *_: self._update_save_button())
        self._add_labeled(name_role_row, "Name", self._name_entry)
        self._role_entry = Gtk.Entry()
        self._role_entry.set_placeholder_text("e.g. coder, debugger")
        self._role_entry.set_hexpand(True)
        self._add_labeled(name_role_row, "Role", self._role_entry)
        form_box.append(name_role_row)

        # Hidden emoji — not shown in form, defaults to 🤖
        self._emoji_entry = Gtk.Entry()
        self._emoji_entry.set_text("🤖")

        # Provider row
        provider_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        provider_row.set_hexpand(True)

        # Provider dropdown
        self._provider_dropdown = self._build_provider_dropdown()
        self._provider_labeled = self._labeled_box("Provider", self._provider_dropdown)
        provider_row.append(self._provider_labeled)

        form_box.append(provider_row)

        # Fallback provider row (always visible — every agent needs a fallback)
        self._fallback_row = self._build_fallback_provider_row()
        form_box.append(self._fallback_row)

        # Prompts multi-select
        self._prompts_list = self._build_prompts_list()
        self._add_labeled(form_box, "System Prompts", self._prompts_list, expand=True)

        # Tool presets + checkboxes
        tools_section = self._build_tools_section()
        self._add_labeled(form_box, "Tools", tools_section, expand=True)

        # MCP server checkboxes
        mcp_section = self._build_mcp_section()
        self._add_labeled(form_box, "MCP Servers", mcp_section, expand=False)

        # Phase C — Compaction strategy dropdown
        self._compaction_strategy_combo = Gtk.DropDown.new_from_strings(list(VALID_COMPACTION_STRATEGIES))
        self._compaction_strategy_combo.set_selected(0)
        strat_row = self._labeled_box("Compaction strategy", self._compaction_strategy_combo)
        form_box.append(strat_row)

        scroll.set_child(form_box)
        content.append(scroll)

        # Error label (hidden by default)
        self._error_label = Gtk.Label()
        self._error_label.add_css_class("agent-builder-error")
        self._error_label.set_visible(False)
        self._error_label.set_wrap(True)
        self._error_label.set_margin_start(20)
        self._error_label.set_margin_end(20)
        content.append(self._error_label)

        self._window.set_child(content)

        # Populate provider dropdown from handler first (reads providers.yaml),
        # so that _fill_form can match the saved llm_name against self._providers.
        self.set_provider_options(handler.get_provider_options())

        # Pre-fill if editing
        if agent_def:
            self._fill_form(agent_def)
        else:
            # New agent — ensure save button starts disabled
            self._update_save_button()

    # ── Public API ────────────────────────────────────────────────────

    def get_values(self) -> dict:
        """Extract current form values into an agent_def dict.
        The model field is left empty — the runtime resolves the model
        from providers.yaml using the provider name.
        """
        name = self._name_entry.get_text().strip()
        emoji = self._emoji_entry.get_text().strip() or "🤖"
        role = self._role_entry.get_text().strip() or name.lower().replace(" ", "-")
        llm_name = self._get_selected_llm_name()

        prompts = self._get_selected_prompts()
        tools = self._get_selected_tools()

        return {
            "name": name,
            "emoji": emoji,
            "role": role,
            "prompts": prompts,
            "tools": tools,
            "llm_name": llm_name,
            "mcp_servers": self._get_selected_mcp_servers(),
            "self_improvement": self._get_si_config(tools),
            "fallback_provider": self._get_selected_fallback_provider() or None,
            "compaction_strategy":
                list(VALID_COMPACTION_STRATEGIES)[self._compaction_strategy_combo.get_selected()],
        }

    def show(self) -> None:
        """Present the dialog window."""
        self._window.present()
        # Focus name entry after window appears
        GLib.idle_add(lambda: self._name_entry.grab_focus() and False)

    def close(self) -> None:
        """Close the dialog window."""
        self._window.close()

    def show_errors(self, errors: list[str]) -> None:
        """Display validation errors."""
        self._error_label.set_text("\n".join(errors))
        self._error_label.set_visible(True)

    def _clear_errors(self) -> None:
        self._error_label.set_visible(False)

    # ── Header ────────────────────────────────────────────────────────

    def _build_header(self, title: str) -> Gtk.Box:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_start(16)
        header.set_margin_end(16)
        header.set_margin_top(12)
        header.set_margin_bottom(8)

        title_label = Gtk.Label(label=title)
        title_label.add_css_class("title-2")
        title_label.set_hexpand(True)
        title_label.set_xalign(0.0)
        header.append(title_label)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.add_css_class("flat")
        cancel_btn.connect("clicked", lambda *_: self._do_cancel())
        header.append(cancel_btn)

        save_label = "Save" if self._is_edit else "Create"
        self._save_btn = Gtk.Button(label=save_label)
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.connect("clicked", lambda *_: self._do_save())
        self._save_btn.set_sensitive(False)
        header.append(self._save_btn)

        return header

    # ── Form helpers ──────────────────────────────────────────────────

    def _add_field(
        self,
        parent: Gtk.Box,
        label_text: str,
        entry: Gtk.Entry,
        placeholder: str = "",
        max_width: int = 0,
    ) -> Gtk.Entry:
        """Add a labeled entry field to the form."""
        entry.set_placeholder_text(placeholder)
        entry.set_hexpand(True)
        if max_width:
            entry.set_max_width_chars(6)
        self._add_labeled(parent, label_text, entry)
        return entry

    def _labeled_box(self, label_text: str, widget: Gtk.Widget, expand: bool = False) -> Gtk.Box:
        """Create a labeled vertical box (label above widget). Does NOT append to parent."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        label = Gtk.Label(label=label_text)
        label.add_css_class("caption")
        label.set_xalign(0.0)
        box.append(label)
        if expand:
            widget.set_vexpand(True)
        box.append(widget)
        return box

    def _add_labeled(
        self,
        parent: Gtk.Box,
        label_text: str,
        widget: Gtk.Widget,
        expand: bool = False,
    ) -> None:
        """Add a labeled widget to the form."""
        box = self._labeled_box(label_text, widget, expand)
        parent.append(box)

    # ── Provider + Model dropdowns ────────────────────────────────────

    # Providers matching openclaw.json format: (display_name, provider_id)
    def set_provider_options(self, providers) -> None:
        """Replace the provider list with the given providers.
        Called by the window when the Settings dialog fires on_providers_changed,
        and by __init__ with handler.get_provider_options().
        Accepts list[ProviderConfig] (from _on_providers_changed) or list[dict]
        (from handler.get_provider_options()) and normalizes to ProviderConfig.
        Each provider's default_model becomes the only entry in its model dropdown.

        Raises:
            TypeError: if providers is not a list or contains non-dict/non-ProviderConfig elements.
        """
        if not providers:
            self._providers = []
        else:
            if not isinstance(providers, list):
                raise TypeError(f"providers must be a list, got {type(providers).__name__}")
            # Normalize: accept both list[ProviderConfig] and list[dict]
            normalized = []
            for p in providers:
                if isinstance(p, dict):
                    name = p.get("name", "").strip() if isinstance(p.get("name"), str) else ""
                    if not name:
                        continue  # skip providers without a name
                    normalized.append(ProviderConfig(
                        name=name,
                        base_url=p.get("base_url", ""),
                        api_key=p.get("api_key", ""),
                        default_model=p.get("default_model", ""),
                    ))
                elif isinstance(p, ProviderConfig):
                    normalized.append(p)
                else:
                    raise TypeError(
                        f"Each provider must be dict or ProviderConfig, got {type(p).__name__}"
                    )
            self._providers = normalized

        self._rebuild_provider_dropdown()
        # Rebuild fallback provider dropdown too (in case providers changed)
        if hasattr(self, "_fallback_dropdown"):
            self._populate_fallback_provider_dropdown()
            self._update_fallback_visibility()

    def _rebuild_provider_dropdown(self) -> None:
        """Rebuild the provider dropdown from self._providers."""
        if not self._providers:
            names = Gtk.StringList.new(["(no providers — open Settings)"])
        else:
            names = Gtk.StringList.new([p.name for p in self._providers])
        self._provider_dropdown.set_model(names)
        # Select first provider by default
        if self._providers:
            self._on_provider_changed(self._provider_dropdown, None)

    def _build_provider_dropdown(self) -> Gtk.DropDown:
        dropdown = Gtk.DropDown(model=Gtk.StringList.new(["(loading...)"]))
        dropdown.connect("notify::selected", self._on_provider_changed)
        return dropdown

    def _get_selected_llm_name(self) -> str:
        idx = self._provider_dropdown.get_selected()
        if idx < len(self._providers):
            return self._providers[idx].name
        if self._providers:
            return self._providers[0].name
        return ""

    def _on_provider_changed(self, dropdown, _param) -> None:
        """When provider changes, refresh save button state and toggle fallback row."""
        self._update_save_button()
        self._update_fallback_visibility()

    # ── Fallback provider dropdown ─────────────────────────────────────

    def _build_fallback_provider_row(self) -> Gtk.Box:
        """Build the fallback provider row.

        Always visible — every agent must have a fallback provider configured.
        Includes a 'None' option (which is invalid; the save button stays
        disabled until a real provider is selected). Model is resolved at
        runtime from the selected provider's default_model — no sibling
        model dropdown.
        """
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.set_hexpand(True)

        # Fallback provider dropdown
        self._fallback_dropdown = Gtk.DropDown(model=Gtk.StringList.new(["None"]))
        self._fallback_dropdown.connect("notify::selected", self._on_fallback_provider_changed)
        self._fallback_labeled = self._labeled_box("Fallback Provider", self._fallback_dropdown)
        row.append(self._fallback_labeled)

        # Always visible — every agent must configure a fallback
        row.set_visible(True)

        return row

    def _populate_fallback_provider_dropdown(self) -> None:
        """Populate the fallback provider dropdown from self._providers.

        Includes a 'None' option at index 0 (invalid for save — the save button
        stays disabled until a real provider is selected). Excludes the
        currently-selected primary provider.
        """
        primary = self._get_selected_llm_name()
        names = ["None"]
        self._fallback_providers: list = []  # parallel list of ProviderConfig (index 0 = None sentinel)
        for p in self._providers:
            if p.name == primary:
                continue  # exclude the current primary — can't fall back to itself
            names.append(p.name)
            self._fallback_providers.append(p)

        sl = Gtk.StringList.new(names)
        self._fallback_dropdown.set_model(sl)
        # Try to preserve the previously-selected fallback if still in the list;
        # otherwise default to "None" (which keeps the save button disabled).
        prev = self._get_selected_fallback_provider()
        if prev and prev in names:
            idx = names.index(prev)
            self._fallback_dropdown.set_selected(idx)
        else:
            self._fallback_dropdown.set_selected(0)  # default to None

    def _on_fallback_provider_changed(self, dropdown, _param) -> None:
        """When fallback provider changes, refresh save button state.

        Model is resolved at runtime from the selected provider's default_model,
        matching the primary provider dropdown's contract. No sibling model
        dropdown is shown — one provider card = one vetted model.
        """
        self._update_save_button()

    def _update_fallback_visibility(self) -> None:
        """Repopulate the fallback provider dropdown whenever the primary changes.

        The row is always visible; this just rebuilds the options to exclude
        the new primary.
        """
        self._populate_fallback_provider_dropdown()

    def _get_selected_fallback_provider(self) -> str:
        """Return the selected fallback provider name, or '' for None."""
        idx = self._fallback_dropdown.get_selected()
        if idx == 0:
            return ""
        prov_idx = idx - 1
        if prov_idx < len(getattr(self, "_fallback_providers", [])):
            return self._fallback_providers[prov_idx].name
        return ""

    def _build_prompts_list(self) -> Gtk.ScrolledWindow:
        prompts = self._handler.get_prompt_options()
        scroll = Gtk.ScrolledWindow()
        scroll.set_min_content_height(100)
        scroll.set_max_content_height(160)
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        list_box = Gtk.ListBox()
        list_box.add_css_class("agent-builder-prompt-list")
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)

        self._prompt_checks: dict[str, Gtk.CheckButton] = {}
        for p in prompts:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)

            check = Gtk.CheckButton(label=p["name"])
            check.add_css_class("agent-builder-prompt-check")
            self._prompt_checks[p["filepath"]] = check
            check.connect("toggled", lambda *_: self._update_save_button())

            row.set_child(check)
            list_box.append(row)

        scroll.set_child(list_box)
        return scroll

    def _get_selected_prompts(self) -> list[str]:
        return [
            filepath for filepath, check in self._prompt_checks.items()
            if check.get_active()
        ]

    # ── Tools section with presets ────────────────────────────────────

    def _build_tools_section(self) -> Gtk.Box:
        """Build a categorized FlowBox grid of tool checkboxes.

        Tools are grouped into categories (Read, Write, Execute, Web). Unclassified
        tools fall into an "Other" category. Each tool displays as a simple checkbox
        with tooltip showing its description. Category headers are visually distinct.
        """
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        # Header row: presets on left, count badge on right
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        presets = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        presets.add_css_class("agent-builder-presets")

        full_btn = Gtk.Button(label="Full Access")
        full_btn.add_css_class("flat")
        full_btn.connect("clicked", lambda *_: self._apply_preset("full"))

        readonly_btn = Gtk.Button(label="Read Only")
        readonly_btn.add_css_class("flat")
        readonly_btn.connect("clicked", lambda *_: self._apply_preset("readonly"))

        custom_btn = Gtk.Button(label="Custom")
        custom_btn.add_css_class("flat")
        custom_btn.connect("clicked", lambda *_: self._apply_preset("custom"))

        presets.append(full_btn)
        presets.append(readonly_btn)
        presets.append(custom_btn)
        header.append(presets)

        # Count badge (right side)
        self._tool_count_label = Gtk.Label(label="0/0 tools")
        self._tool_count_label.add_css_class("agent-builder-tool-count")
        self._tool_count_label.set_halign(Gtk.Align.END)
        self._tool_count_label.set_hexpand(True)
        header.append(self._tool_count_label)

        outer.append(header)

        # Category definitions
        CATEGORIES = [
            ("Read", {"read_file", "list_files", "search_files"}),
            ("Write", {"write_file", "edit_file"}),
            ("Execute", {"exec_command"}),
            ("Web", {"web_search", "web_fetch"}),
        ]

        tools = self._handler.get_tool_options()

        # Build a map from tool name → tool dict
        tool_map = {t["name"]: t for t in tools}

        # Track which tools we've placed
        placed = set()

        # Render each category
        for cat_name, cat_tools in CATEGORIES:
            # Get tools in this category that actually exist
            cat_matches = [(name, tool_map[name]) for name in sorted(cat_tools) if name in tool_map]
            if not cat_matches:
                continue

            placed.update(name for name, _ in cat_matches)

            # Category header
            cat_label = Gtk.Label(label=cat_name)
            cat_label.add_css_class("agent-builder-tool-cat-label")
            cat_label.set_halign(Gtk.Align.START)
            outer.append(cat_label)

            # FlowBox for this category
            flow = Gtk.FlowBox()
            flow.add_css_class("agent-builder-tool-grid")
            flow.set_min_children_per_line(2)
            flow.set_max_children_per_line(3)
            flow.set_selection_mode(Gtk.SelectionMode.NONE)
            flow.set_homogeneous(True)
            outer.append(flow)

            # Checkboxes in this category
            for name, tool in cat_matches:
                check = Gtk.CheckButton(label=name)
                check.add_css_class("agent-builder-tool-check")
                # First line only — avoids "WHEN TO USE:" dev docs noise
                desc_first = tool["description"].split("\n")[0].strip()
                check.set_tooltip_text(desc_first)
                check.connect("toggled", lambda *_: self._update_tool_count())
                check.connect("toggled", lambda *_: self._update_save_button())
                self._tool_checks[name] = check

                child = Gtk.FlowBoxChild()
                child.set_can_focus(False)
                child.set_child(check)
                flow.append(child)

        # "Other" category for any unclassified tools
        other_tools = [(name, tool_map[name]) for name in sorted(tool_map.keys()) if name not in placed]
        if other_tools:
            cat_label = Gtk.Label(label="Other")
            cat_label.add_css_class("agent-builder-tool-cat-label")
            cat_label.set_halign(Gtk.Align.START)
            outer.append(cat_label)

            flow = Gtk.FlowBox()
            flow.add_css_class("agent-builder-tool-grid")
            flow.set_min_children_per_line(2)
            flow.set_max_children_per_line(3)
            flow.set_selection_mode(Gtk.SelectionMode.NONE)
            flow.set_homogeneous(True)
            outer.append(flow)

            for name, tool in other_tools:
                check = Gtk.CheckButton(label=name)
                check.add_css_class("agent-builder-tool-check")
                desc_first = tool["description"].split("\n")[0].strip()
                check.set_tooltip_text(desc_first)
                check.connect("toggled", lambda *_: self._update_tool_count())
                check.connect("toggled", lambda *_: self._update_save_button())
                self._tool_checks[name] = check

                child = Gtk.FlowBoxChild()
                child.set_can_focus(False)
                child.set_child(check)
                flow.append(child)

        # Initialize count badge
        self._update_tool_count()

        return outer

    def _apply_preset(self, preset: str) -> None:
        """Apply a tool preset to all checkboxes."""
        read_only = {"read_file", "list_files", "search_files", "web_search", "web_fetch"}

        if preset == "full":
            for name, check in self._tool_checks.items():
                check.set_active(True)
        elif preset == "readonly":
            for name, check in self._tool_checks.items():
                check.set_active(name in read_only)
        # "custom" — leave as-is

        self._update_tool_count()

    def _update_tool_count(self) -> None:
        """Update the tool count badge label."""
        if self._tool_count_label is None:
            return
        total = len(self._tool_checks)
        selected = sum(1 for check in self._tool_checks.values() if check.get_active())
        self._tool_count_label.set_label(f"{selected}/{total} tools")

    def _get_selected_tools(self) -> list[str]:
        return [name for name, check in self._tool_checks.items() if check.get_active()]

    # ── MCP Servers section ────────────────────────────────────────────

    def _build_mcp_section(self) -> Gtk.Box:
        """Build MCP server checkbox list for the agent builder form."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        try:
            all_servers = load_mcp_servers()
        except (FileNotFoundError, OSError, MCPConfigError) as exc:
            dim = Gtk.Label(label="No MCP servers configured.\nAdd servers to ~/.config/crabcakes/mcp-servers.json")
            dim.add_css_class("dim-label")
            dim.set_justify(Gtk.Justification.LEFT)
            dim.set_xalign(0.0)
            dim.set_wrap(True)
            outer.append(dim)
            return outer

        # Only show enabled servers
        enabled_servers = {name: cfg for name, cfg in all_servers.items() if cfg.enabled}

        if not enabled_servers:
            dim = Gtk.Label(label="No MCP servers configured.\nAdd servers to ~/.config/crabcakes/mcp-servers.json")
            dim.add_css_class("dim-label")
            dim.set_justify(Gtk.Justification.LEFT)
            dim.set_xalign(0.0)
            dim.set_wrap(True)
            outer.append(dim)
            return outer

        list_box = Gtk.ListBox()
        list_box.add_css_class("agent-builder-mcp-list")
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)

        for name, cfg in enabled_servers.items():
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)

            check = Gtk.CheckButton(label=f"{name}  — {cfg.description[:60]}")
            check.add_css_class("agent-builder-mcp-check")
            self._mcp_checks[name] = check

            row.set_child(check)
            list_box.append(row)

        outer.append(list_box)
        return outer

    def _get_selected_mcp_servers(self) -> list[str]:
        """Return list of MCP server names whose checkboxes are active."""
        return [name for name, check in self._mcp_checks.items() if check.get_active()]

    # ── SI config ─────────────────────────────────────────────────────

    def _get_si_config(self, tools: list[str]) -> dict:
        """Build SI config based on selected tools + preserved overrides."""
        from utils.agent_defs import get_default_si_config
        can_write = "write_file" in tools or "edit_file" in tools
        defaults = get_default_si_config(can_write=can_write)
        # Merge preserved overrides on top of fresh defaults
        if self._original_si:
            return {**defaults, **self._original_si}
        return defaults

    # ── Pre-fill for editing ──────────────────────────────────────────

    def _fill_form(self, agent_def: dict) -> None:
        """Pre-fill form fields from an existing agent definition."""
        # Preserve original SI config for round-trip
        self._original_si = dict(agent_def.get("self_improvement", {}))

        self._name_entry.set_text(agent_def.get("name", ""))
        self._emoji_entry.set_text(agent_def.get("emoji", ""))
        self._role_entry.set_text(agent_def.get("role", ""))

        # Select provider dropdown
        provider_id = agent_def.get("llm_name", "")
        for i, p in enumerate(self._providers):
            if p.name == provider_id:
                self._provider_dropdown.set_selected(i)
                break

        # Check prompts
        selected_prompts = set(agent_def.get("prompts", []))
        for filepath, check in self._prompt_checks.items():
            check.set_active(filepath in selected_prompts)

        # Check tools
        selected_tools = set(agent_def.get("tools", []))
        for name, check in self._tool_checks.items():
            check.set_active(name in selected_tools)

        self._update_tool_count()

        # Check MCP servers
        selected_mcp = set(agent_def.get("mcp_servers", []))
        for name, check in self._mcp_checks.items():
            check.set_active(name in selected_mcp)

        # Restore fallback provider if present
        fb_provider = agent_def.get("fallback_provider")
        if fb_provider:
            self._update_fallback_visibility()  # populates dropdown
            for i, p in enumerate(getattr(self, "_fallback_providers", [])):
                if p.name == fb_provider:
                    self._fallback_dropdown.set_selected(i + 1)  # +1 for None offset
                    break

        # Phase C — Restore compaction strategy
        cs = agent_def.get("compaction_strategy", VALID_COMPACTION_STRATEGIES[0])
        if cs in VALID_COMPACTION_STRATEGIES:
            self._compaction_strategy_combo.set_selected(VALID_COMPACTION_STRATEGIES.index(cs))
        else:
            self._compaction_strategy_combo.set_selected(0)

        self._update_save_button()

    # ── Save button state ────────────────────────────────────────────

    def _update_save_button(self) -> None:
        """Enable Save only when: name, prompts, tools, primary, AND fallback are set.

        This is widget state management (is the form complete?), NOT validation.
        Actual validation lives in validate_agent_def().
        """
        has_name = bool(self._name_entry.get_text().strip())
        has_prompts = any(c.get_active() for c in self._prompt_checks.values())
        has_tools = any(c.get_active() for c in self._tool_checks.values())
        has_provider = bool(self._get_selected_llm_name())
        has_fallback = bool(self._get_selected_fallback_provider())

        self._save_btn.set_sensitive(
            has_name and has_prompts and has_tools and has_provider and has_fallback
        )

    # ── Actions ───────────────────────────────────────────────────────

    def _do_save(self) -> None:
        """User clicked Save/Create."""
        self._clear_errors()
        values = self.get_values()
        if self._on_save:
            self._on_save(values)

    def _do_cancel(self) -> None:
        """User clicked Cancel."""
        if self._on_cancel:
            self._on_cancel()
        self._window.destroy()

    def _on_close_request(self, _widget=None) -> bool:
        """Window close button clicked."""
        if self._on_cancel:
            self._on_cancel()
        return False  # allow close
