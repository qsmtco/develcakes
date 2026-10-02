# ui/styles.py
# Single source of truth for all application CSS.
#
# Security: No secrets, no file I/O. Pure CSS string + one apply function.
#
# Architecture rule (ARCHITECTURE.md Section 9):
#   - Views call add_css_class("name") to apply styles
#   - Views NEVER call Gtk.CssProvider().load_from_data() themselves
#   - All CSS definitions live here in APP_CSS
#   - apply_styles() is called once at startup from main.py

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk


# -- Feed bar --------------------------------------------------------------
# Applied to the thin strip between chat notebook and control bar.

# -- Button styles --------------------------------------------------------
# suggested-action = primary solid (Send button)
# btn-improve      = indigo tint (Improve button)
# flat             = ghost/transparent (Prompt button, toolbar mic)

# -- Input area -----------------------------------------------------------
# input-bubble = dark rounded input field

# -- Agent cards ----------------------------------------------------------
# agent-row, agent-name-label, agent-add-btn, agent-remove-btn

APP_CSS = """
/* -- Feed bar ----------------------------------------------------------- */
.project-feed-bar {
    /* UI-PILLBAR Phase 1: opaque — the bar is a chrome surface now (it
       hosts the activity pill), not a floating translucent overlay. Same
       hue as the old rgba(30, 30, 40, 0.75), alpha 1. */
    background: #1e1e28;
    border-radius: 4px;
}

/* -- Project settings bar interactive elements -------------------------- */
/* Applied to the agent / auto-accept / gear buttons inside the project
   settings bar (ui/views/main_content.py). These are Gtk.Button with
   set_has_frame(False), so :hover/:active give lightweight press feedback.
   GTK4-safe: no text-align (use padding/halign), border:none + box-shadow:none
   to neutralize the default button chrome. */

.project-bar-agent {
    color: #4ade80;
    font-weight: 600;
    padding: 0 6px;
    border-radius: 3px;
    min-height: 0;
    margin: 0;
    background: transparent;
    border: none;
    box-shadow: none;
}
.project-bar-agent:hover {
    background: rgba(74, 222, 128, 0.15);
}
.project-bar-agent:active {
    background: rgba(74, 222, 128, 0.25);
}

.project-bar-autoaccept {
    color: #facc15;
    padding: 0 6px;
    border-radius: 3px;
    min-height: 0;
    margin: 0;
    background: transparent;
    border: none;
    box-shadow: none;
}
.project-bar-autoaccept:hover {
    background: rgba(250, 204, 21, 0.15);
}
.project-bar-autoaccept:active {
    background: rgba(250, 204, 21, 0.25);
}

.project-bar-gear {
    font-size: 14px;
    padding: 0 8px;
    min-height: 0;
    border-radius: 3px;
    background: transparent;
    border: none;
    box-shadow: none;
}
.project-bar-gear:hover {
    background: rgba(255, 255, 255, 0.1);
}

/* -- Buttons ------------------------------------------------------------ */
button.suggested-action {
    background: #6366f1;
    color: #ffffff;
    border-radius: 6px;
}
button.suggested-action:hover {
    background: #4f46e5;
}

button.btn-improve {
    background: rgba(79, 70, 229, 0.2);
    color: #a5b4fc;
    border-radius: 6px;
    border: none;
    box-shadow: none;
}
button.btn-improve:hover {
    background: rgba(99, 102, 241, 0.35);
    color: #c7d2fe;
}

button.btn-improving {
    background: rgba(79, 70, 229, 0.45);
    color: #c7d2fe;
    border-radius: 6px;
    border: none;
    box-shadow: none;
    animation: improve-pulse 1.2s ease-in-out infinite;
}
@keyframes improve-pulse {
    0% { background: rgba(79, 70, 229, 0.35); }
    100% { background: rgba(79, 70, 229, 0.35); }
    50% { background: rgba(79, 70, 229, 0.55); }
}

button.btn-prompt {
    background: rgba(34, 197, 94, 0.15);
    color: #4ade80;
    border-radius: 6px;
    border: none;
    box-shadow: none;
}
button.btn-prompt:hover {
    background: rgba(34, 197, 94, 0.25);
    color: #86efac;
}
.recording-stop {
    color: #ef4444;
}

button.flat {
    background: transparent;
    color: #6b6b7a;
    border: 1px solid transparent;
    border-radius: 6px;
    box-shadow: none;
}
button.flat:hover {
    border: 1px solid #3f3f50;
    color: #9b9bab;
}

/* -- Input area --------------------------------------------------------- */
.input-bubble {
    background: #111114;
    border: none;
    border-radius: 14px 14px 14px 2px;
    color: #e8e8ec;
}
.input-bubble text {
    background-color: transparent;
    color: #e8e8ec;
}
.input-bubble selection {
    background-color: #6366f1;
    color: #ffffff;
}

/* -- Agent cards -------------------------------------------------------- */
.agent-row {
    background: rgba(255, 255, 255, 0.04);
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.08);
    margin: 2px 4px;
}
.agent-row:hover {
    background: rgba(99, 102, 241, 0.15);
    border-color: rgba(99, 102, 241, 0.4);
}
.agent-row:selected,
.agent-row:focus,
.agent-row:focus-visible {
    background: rgba(99, 102, 241, 0.2);
    outline: none;
    box-shadow: none;
}
.agent-name-label {
    color: #e8e8ec;
    font-size: 14px;
}
.agent-tag-label {
    color: #6b6b7a;
    font-size: 11px;
}
.agent-add-btn {
    background: rgba(16, 185, 129, 0.2);
    color: #6ee7b7;
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 12px;
}
.agent-add-btn:hover {
    background: rgba(16, 185, 129, 0.4);
    color: #a7f3d0;
}
.agent-remove-btn {
    background: rgba(244, 63, 94, 0.2);
    color: #fda4af;
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 12px;
}
.agent-remove-btn:hover {
    background: rgba(244, 63, 94, 0.4);
    color: #fecdd3;
}

/* Agent Builder — MCP server list */
.agent-builder-mcp-list {
    background: rgba(255, 255, 255, 0.04);
    border-radius: 6px;
}
.agent-builder-mcp-check {
    padding: 6px 8px;
}

/* Agent Builder — Tool category grid */
.agent-builder-tool-count {
    font-size: 0.8em;
    opacity: 0.6;
}
.agent-builder-tool-cat-label {
    font-size: 0.85em;
    font-weight: 600;
    opacity: 0.7;
    margin-top: 4px;
}
.agent-builder-tool-grid {
    background: rgba(255, 255, 255, 0.04);
    border-radius: 6px;
    padding: 4px;
}
.agent-builder-tool-check {
    padding: 6px 8px;
}

/* -- Prompt library ------------------------------------------------------ */
.lib-row {
    background: rgba(255, 255, 255, 0.04);
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.08);
    margin: 1px 4px;
    transition: background 0.15s ease, border-color 0.15s ease;
}
.lib-row:hover {
    background: rgba(99, 102, 241, 0.15);
    border-color: rgba(99, 102, 241, 0.4);
}
.lib-row.selected,
.agent-row.selected {
    background: rgba(99, 102, 241, 0.2);
    border-color: transparent;
    outline: none;
    box-shadow: none;
}
.lib-row:selected {
    background: rgba(99, 102, 241, 0.2);
}
button.lib-fav-star {
    color: #f59e0b;
    font-size: 16px;
}
.lib-tag {
    color: #6b6b7a;
    font-size: 11px;
}
/* Project cards */
.project-card {
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 6px;
    padding: 8px;
}
.project-card:hover {
    background: rgba(99, 102, 241, 0.15);
    border-color: rgba(102, 102, 241, 0.4);
}
.project-card:selected,
.project-card.selected {
    background: rgba(99, 102, 241, 0.2);
    border-color: transparent;
    outline: none;
    box-shadow: none;
}
.project-card-name {
    font-size: 13px;
    color: #e2e8f0;
    font-weight: 500;
}
.project-card-path {
    font-size: 11px;
    color: #6b6b7a;
}
.new-project-card {
    background: transparent;
    border: 1px dashed rgba(255, 255, 255, 0.15);
    border-radius: 6px;
    padding: 8px;
}
.new-project-card:hover {
    background: rgba(99, 102, 241, 0.08);
    border-color: rgba(102, 102, 241, 0.3);
}
.new-project-plus {
    font-size: 20px;
    font-weight: 300;
    color: #6b6b7a;
}
.new-prompt-row {
    background: transparent;
    border: 1px dashed rgba(255, 255, 255, 0.15);
    border-radius: 6px;
}
.new-prompt-row:hover {
    background: rgba(99, 102, 241, 0.08);
    border-color: rgba(102, 102, 241, 0.3);
}
.new-prompt-plus {
    font-size: 18px;
    font-weight: 300;
    color: #6b6b7a;
}

/* -- Chat bubbles (Phase 1) --------------------------------------------- */
.chat-bubble-agent {
    background: linear-gradient(135deg,
        rgba(34, 197, 94, 0.70) 0%,
        rgba(255, 255, 255, 0.20) 100%);
    border-radius: 12px 12px 12px 4px;
    padding: 6px 10px 8px 10px;
    margin: 2px 12px 2px 8px;
}

/* System activity bubbles — Phase 2 of SPEC-smarter-chat-ux */
.chat-bubble-System {
    background: rgba(255, 255, 255, 0.03);
    border-left: 2px solid rgba(255, 255, 255, 0.15);
    border-radius: 6px 6px 6px 2px;
    padding: 4px 10px 4px 10px;
    margin: 1px 12px 1px 8px;
}
.chat-bubble-System .chat-msg-label {
    font-family: monospace;
    font-size: 12px;
    color: #9090a8;
}
.chat-bubble-header {
    margin: 0 0 2px 0;
    padding: 0;
}
.chat-bubble-header-name {
    font-size: 11px;
    font-weight: 600;
    color: #c0c0d8;
}
.chat-bubble-header-dot {
    background: #22c55e;
    border-radius: 50%;
    min-width: 6px;
    min-height: 6px;
    margin: 0 2px;
}

/* Agent-colored bubble backgrounds — per-agent tint matching avatar color.
   Applied when build_role_bubble receives agent_color. Falls back to
   .chat-bubble-agent (generic green) when no color is provided. */
.agent-bg-6366f1 {
    background: linear-gradient(135deg,
        rgba(99, 102, 241, 0.70) 0%,
        rgba(255, 255, 255, 0.20) 100%);
    border-radius: 12px 12px 12px 4px;
    padding: 6px 10px 8px 10px;
    margin: 2px 12px 2px 8px;
}
.agent-dot-6366f1 { background: #6366f1; }
.agent-bg-10b981 {
    background: linear-gradient(135deg,
        rgba(16, 185, 129, 0.70) 0%,
        rgba(255, 255, 255, 0.20) 100%);
    border-radius: 12px 12px 12px 4px;
    padding: 6px 10px 8px 10px;
    margin: 2px 12px 2px 8px;
}
.agent-dot-10b981 { background: #10b981; }
.agent-bg-f59e0b {
    background: linear-gradient(135deg,
        rgba(245, 158, 11, 0.70) 0%,
        rgba(255, 255, 255, 0.20) 100%);
    border-radius: 12px 12px 12px 4px;
    padding: 6px 10px 8px 10px;
    margin: 2px 12px 2px 8px;
}
.agent-dot-f59e0b { background: #f59e0b; }
.agent-bg-f43f5e {
    background: linear-gradient(135deg,
        rgba(244, 63, 94, 0.70) 0%,
        rgba(255, 255, 255, 0.20) 100%);
    border-radius: 12px 12px 12px 4px;
    padding: 6px 10px 8px 10px;
    margin: 2px 12px 2px 8px;
}
.agent-dot-f43f5e { background: #f43f5e; }
.agent-bg-06b6d4 {
    background: linear-gradient(135deg,
        rgba(6, 182, 212, 0.70) 0%,
        rgba(255, 255, 255, 0.20) 100%);
    border-radius: 12px 12px 12px 4px;
    padding: 6px 10px 8px 10px;
    margin: 2px 12px 2px 8px;
}
.agent-dot-06b6d4 { background: #06b6d4; }
.agent-bg-8b5cf6 {
    background: linear-gradient(135deg,
        rgba(139, 92, 246, 0.70) 0%,
        rgba(255, 255, 255, 0.20) 100%);
    border-radius: 12px 12px 12px 4px;
    padding: 6px 10px 8px 10px;
    margin: 2px 12px 2px 8px;
}
.agent-dot-8b5cf6 { background: #8b5cf6; }
.agent-bg-ec4899 {
    background: linear-gradient(135deg,
        rgba(236, 72, 153, 0.70) 0%,
        rgba(255, 255, 255, 0.20) 100%);
    border-radius: 12px 12px 12px 4px;
    padding: 6px 10px 8px 10px;
    margin: 2px 12px 2px 8px;
}
.agent-dot-ec4899 { background: #ec4899; }
.agent-bg-14b8a6 {
    background: linear-gradient(135deg,
        rgba(20, 184, 166, 0.70) 0%,
        rgba(255, 255, 255, 0.20) 100%);
    border-radius: 12px 12px 12px 4px;
    padding: 6px 10px 8px 10px;
    margin: 2px 12px 2px 8px;
}
.agent-dot-14b8a6 { background: #14b8a6; }
.agent-bg-f97316 {
    background: linear-gradient(135deg,
        rgba(249, 115, 22, 0.70) 0%,
        rgba(255, 255, 255, 0.20) 100%);
    border-radius: 12px 12px 12px 4px;
    padding: 6px 10px 8px 10px;
    margin: 2px 12px 2px 8px;
}
.agent-dot-f97316 { background: #f97316; }
.agent-bg-84cc16 {
    background: linear-gradient(135deg,
        rgba(132, 204, 22, 0.70) 0%,
        rgba(255, 255, 255, 0.20) 100%);
    border-radius: 12px 12px 12px 4px;
    padding: 6px 10px 8px 10px;
    margin: 2px 12px 2px 8px;
}
.agent-dot-84cc16 { background: #84cc16; }

/* Tab dot — shows agent status in the chat tab label */
.tab-dot {
    min-width: 8px;
    min-height: 8px;
    border-radius: 50%;
    margin-right: 4px;
}
.tab-dot-idle {
    background: #4ade80;
}
/* Context meter — ProgressBar between chat tabs and input box.
   Zone classes are applied by set_context_meter() in main_content.py:
   low (<70%, green), medium (70-90%, yellow), high (>=90%, red). */
.context-meter {
    filter: none;
}
.context-meter-low trough {
    background: rgba(74, 222, 128, 0.15);
    border-color: rgba(74, 222, 128, 0.4);
    box-shadow: none;
}
.context-meter-low progress {
    background: #4ade80;
}
.context-meter-medium trough {
    background: rgba(250, 204, 21, 0.15);
    border-color: rgba(250, 204, 21, 0.4);
    box-shadow: none;
}
.context-meter-medium progress {
    background: #facc15;
}
.context-meter-high trough {
    background: rgba(239, 68, 68, 0.15);
    border-color: rgba(239, 68, 68, 0.4);
    box-shadow: none;
}
.context-meter-high progress {
    background: #ef4444;
}
.context-meter-label {
    font-size: 10px;
    color: #a0a0b0;
}
.context-meter-pct {
    font-size: 10px;
    color: #a0a0b0;
}
.context-meter-caption {
    font-size: 9px;
    color: #707088;
}
.agent-context-box {
    margin-right: 4px;
}
.context-agent-name {
    font-size: 13px;
    color: #a0a0b0;
}
.context-avatar {
    border-radius: 50%;
}
.tab-dot-unread {
    background: #facc15;
}
/* Tab label typography */
.tab-label-name {
    font-weight: bold;
}
.tab-label-separator {
    color: #707088;
    font-size: 11px;
}
.tab-label-session {
    color: #a0a0b0;
    font-size: 11px;
}
/* Compact tabs — shrink-to-fit content instead of expanding to fill width */
notebook > header > tabs > tab {
    min-width: 0;
    padding: 4px 8px;
}
.chat-bubble-header-time {
    font-size: 10px;
    color: #707088;
}
.chat-bubble-you {
    background: linear-gradient(135deg,
        rgba(99, 102, 241, 0.40) 0%,
        rgba(79, 85, 210, 0.12) 100%);
    border-radius: 12px 12px 4px 12px;
    padding: 6px 10px 8px 10px;
    margin: 2px 8px 2px 12px;
}
.chat-role-label {
    font-size: 11px;
    color: #8888a0;
    margin-bottom: 2px;
}
.chat-bubble-you .chat-role-label {
    color: #9090c0;
}
.chat-msg-label {
    font-size: 14px;
    color: #d8d8e8;
}
.chat-msg-label:selected {
    background-color: #6366f1;
    color: #ffffff;
}

/* -- Bubble pending state (optimistic UI) ------------------------------- */
.chat-bubble-pending {
    opacity: 0.6;
    transition: opacity 0.3s ease;
}
.chat-bubble-pending.chat-bubble-you {
    background: rgba(99, 102, 241, 0.15);
}
.chat-bubble-pending.chat-bubble-agent {
    background: rgba(255, 255, 255, 0.04);
}

/* -- Code blocks ------------------------------------------------------- */
.code-block {
    background: rgba(30, 30, 40, 0.95);
    border-radius: 8px;
    border-left: 3px solid #3d59a1;
    margin: 4px 0;
}
.code-block-content {
    font-family: monospace;
    font-size: 13px;
    color: #a9b1d6;
    padding: 8px 12px;
    background: transparent;
}
.code-block-header {
    background: rgba(61, 89, 161, 0.3);
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    padding: 4px 10px;
    font-size: 11px;
    color: #7aa2f7;
    font-family: monospace;
}
.code-lang-label {
    letter-spacing: 0;
    font-size: 11px;
    font-weight: bold;
}
.code-copy-btn {
    background: transparent;
    color: #565f89;
    border: none;
    border-radius: 4px;
    padding: 0;
    font-size: 11px;
}
.code-copy-btn:hover {
    background: rgba(255, 255, 255, 0.08);
    color: #a9b1d6;
}

/* -- Per-language code block accents ----------------------------------- */
.code-block.lang-python  { border-left-color: #61afef; }
.code-block.lang-javascript, .code-block.lang-js { border-left-color: #e5c07b; }
.code-block.lang-typescript, .code-block.lang-ts { border-left-color: #61afef; }
.code-block.lang-bash, .code-block.lang-sh, .code-block.lang-shell { border-left-color: #98c379; }
.code-block.lang-html { border-left-color: #e06c75; }
.code-block.lang-css { border-left-color: #61afef; }
.code-block.lang-rust { border-left-color: #e06c75; }
.code-block.lang-go { border-left-color: #56b6c2; }
.code-block.lang-java { border-left-color: #e5c07b; }
.code-block.lang-c, .code-block.lang-cpp { border-left-color: #61afef; }
.code-block.lang-ruby { border-left-color: #e06c75; }
.code-block.lang-php { border-left-color: #c678dd; }
.code-block.lang-swift { border-left-color: #e5c07b; }
.code-block.lang-kotlin { border-left-color: #c678dd; }
.code-block.lang-sql { border-left-color: #98c379; }
.code-block.lang-json { border-left-color: #98c379; }
.code-block.lang-yaml { border-left-color: #98c379; }
.code-block.lang-markdown { border-left-color: #61afef; }
.code-block.lang-r-lang { border-left-color: #61afef; }

/* -- Blockquotes -------------------------------------------------------- */
.blockquote {
    border-left: 3px solid rgba(168, 85, 247, 0.6);
    padding: 4px 10px;
    margin: 4px 0;
    background: rgba(168, 85, 247, 0.06);
    border-radius: 0 4px 4px 0;
}
.blockquote-text {
    font-size: 13px;
    color: #9b9bab;
    font-style: italic;
}


/* -- Event cards (Phase 4) ------------------------------------------------ */
.bubble-file-read       { border-left: 3px solid #22c55e; }
.bubble-edit-proposal   { border-left: 3px solid #f59e0b; }
.bubble-tool-call       { border-left: 3px solid #94a3b8; }
.bubble-error           { border-left: 3px solid #ef4444; background: rgba(239,68,68,0.08); }
.bubble-thinking        { border-left: 3px solid #f59e0b; }
.bubble-streaming       { border-left: 3px solid #6366f1; }

/* -- Terminal blocks ---------------------------------------------------- */
.terminal-block {
    background: rgba(20, 20, 30, 0.95);
    border-radius: 6px;
    border-left: 3px solid #e5c07b;
    margin: 4px 0;
}
.terminal-header {
    background: rgba(229, 192, 123, 0.12);
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    padding: 4px 10px;
    font-size: 11px;
    color: #e5c07b;
    font-family: monospace;
}
.terminal-content {
    font-family: monospace;
    font-size: 13px;
    color: #a9b1d6;
    padding: 8px 12px;
    background: transparent;
}
.terminal-line {
    font-family: monospace;
}
.terminal-prompt {
    color: #e5c07b;
    font-family: monospace;
    margin-right: 4px;
}

.chat-bubble-actions {
    margin-top: 2px;
}
.chat-action-btn {
    background: transparent;
    border: none;
    border-radius: 4px;
    padding: 2px;
    font-size: 11px;
    color: #9b9bab;
}
.chat-action-btn:hover {
    color: #a9b1d6;
}
/* -- Scroll-to-bottom floating button ------------------------------- */
.scroll-to-bottom-btn {
    background: rgba(30, 30, 50, 0.9);
    border: 1px solid rgba(99, 102, 241, 0.4);
    border-radius: 20px;
    padding: 4px 12px;
    color: #a9b1d6;
    font-size: 13px;
    opacity: 0;
    transition: opacity 0.2s ease;
}
.scroll-to-bottom-btn.visible {
    opacity: 1;
}
.scroll-to-bottom-btn:hover {
    background: rgba(60, 60, 90, 0.95);
    color: #d8d8e8;
}

.chat-action-btn:hover {
    background: rgba(255, 255, 255, 0.08);
    color: #a9b1d6;
}

/* -- Headings inside bubbles ------------------------------------------- */
.chat-heading { font-weight: 700; color: #e8e8f0; }
.chat-heading-1 { font-size: 20px; }
.chat-heading-2 { font-size: 17px; }
.chat-heading-3 { font-size: 15px; }
.chat-heading-4 { font-size: 14px; }

/* -- Task list items --------------------------------------------------- */
.task-item { font-size: 13px; color: #c0caf5; padding: 1px 0; }
.task-checked { color: #9ece6a; text-decoration: line-through; }
.task-unchecked { color: #f0f0f0; }

/* ── Table rendering ─────────────────────────────────── */
.table-block {
    margin: 6px 0;
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 6px;
}
.table-grid {
    background: transparent;
}
.table-cell-box {
    padding: 6px 10px;
    border-right: 1px solid rgba(255, 255, 255, 0.04);
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
}
.table-cell-header {
    font-size: 13px;
    font-weight: bold;
    color: #c0caf5;
}
.table-cell-header .table-cell-box {
    background: rgba(67, 27, 125, 0.3);
}
.table-cell {
    font-size: 13px;
    color: #e0e0f0;
}
.table-cell .table-cell-box {
    background: rgba(255, 255, 255, 0.03);
}
.table-cell-alt .table-cell-box {
    background: rgba(255, 255, 255, 0.06);
}

/* -- Response Status progress bar ------------------------------------- */
.response-progress {
    margin-top: 2px;
    margin-bottom: 0;
    min-height: 2px;
    border-radius: 1px;
}
.response-progress trough {
    background: rgba(99, 102, 241, 0.1);
    border-radius: 1px;
    min-height: 2px;
}
.response-progress progress {
    background: linear-gradient(90deg, #6366f1, #3b82f6, #6366f1);
    background-size: 200% 100%;
    border-radius: 1px;
    min-height: 2px;
    animation: progress-stripe 1.5s linear infinite;
}
@keyframes progress-stripe {
    from { background-position: 0 0; }
    to { background-position: 40px 0; }
}

/* -- Review Bar -------------------------------------------------------- */
.review-bar {
    background: rgba(0, 0, 0, 0.05);
    border-radius: 8px;
    padding: 6px 12px;
    margin: 4px 8px;
}
.review-bar-status {
    color: alpha(@theme_fg_color, 0.6);
    font-size: 0.9em;
}
.review-bar-btn-start {
    background: #6366f1;
    color: white;
    border-radius: 6px;
    padding: 4px 12px;
}
.review-bar-btn-start:hover {
    background: #4f46e5;
}
.review-bar-btn-check {
    background: alpha(@theme_fg_color, 0.1);
    border-radius: 6px;
    padding: 4px 12px;
}
.review-bar-btn-check:hover {
    background: alpha(@theme_fg_color, 0.15);
}
.review-bar-btn-accept {
    background: #10b981;
    color: white;
    border-radius: 6px;
    padding: 4px 12px;
}
.review-bar-btn-accept:hover {
    background: #059669;
}
.review-bar-btn-reject {
    background: #f43f5e;
    color: white;
    border-radius: 6px;
    padding: 4px 12px;
}
.review-bar-btn-reject:hover {
    background: #e11d48;
}
.review-bar-loading {
    opacity: 0.6;
}

/* -- Diff Cards -------------------------------------------------------- */
.diff-card {
    background: rgba(0, 0, 0, 0.03);
    border: 1px solid alpha(@theme_fg_color, 0.1);
    border-radius: 8px;
    margin: 4px 0;
}
.diff-card-header {
    padding: 8px 12px;
    border-bottom: 1px solid alpha(@theme_fg_color, 0.08);
    font-family: monospace;
    font-size: 0.9em;
}
.diff-card-header:hover {
    background: alpha(@theme_fg_color, 0.03);
}
.diff-card-body {
    padding: 4px 0;
}
.diff-line-add {
    background: rgba(16, 185, 129, 0.15);
    padding: 1px 12px;
    font-family: monospace;
    font-size: 0.85em;
}
.diff-line-remove {
    background: rgba(244, 63, 94, 0.15);
    padding: 1px 12px;
    font-family: monospace;
    font-size: 0.85em;
}
.diff-line-context {
    color: alpha(@theme_fg_color, 0.5);
    padding: 1px 12px;
    font-family: monospace;
    font-size: 0.85em;
}
.diff-line-number {
    font-family: monospace;
    min-width: 3em;
}
.diff-hunk-header {
    background: rgba(6, 182, 212, 0.1);
    color: alpha(@theme_fg_color, 0.5);
    padding: 2px 12px;
    font-family: monospace;
    font-size: 0.85em;
}
.diff-badge-add {
    background: rgba(16, 185, 129, 0.2);
    color: #10b981;
    border-radius: 4px;
    padding: 1px 6px;
    font-size: 0.8em;
}
.diff-badge-remove {
    background: rgba(244, 63, 94, 0.2);
    color: #f43f5e;
    border-radius: 4px;
    padding: 1px 6px;
    font-size: 0.8em;
}
.diff-badge-new {
    background: rgba(6, 182, 212, 0.2);
    color: #06b6d4;
    border-radius: 4px;
    padding: 1px 6px;
    font-size: 0.8em;
}
.diff-badge-deleted {
    background: alpha(@theme_fg_color, 0.1);
    color: alpha(@theme_fg_color, 0.5);
    border-radius: 4px;
    padding: 1px 6px;
    font-size: 0.8em;
}
.diff-collapsed .diff-card-body {
    opacity: 0;
}
.diff-btn-accept-file {
    background: rgba(16, 185, 129, 0.2);
    color: #10b981;
    border-radius: 4px;
    padding: 2px 8px;
    font-size: 0.8em;
}
.diff-btn-accept-file:hover {
    background: rgba(16, 185, 129, 0.3);
}
.diff-btn-reject-file {
    background: rgba(244, 63, 94, 0.2);
    color: #f43f5e;
    border-radius: 4px;
    padding: 2px 8px;
    font-size: 0.8em;
}
.diff-btn-reject-file:hover {
    background: rgba(244, 63, 94, 0.3);
}
.diff-btn-accept-all {
    background: #10b981;
    color: white;
    border-radius: 6px;
    padding: 6px 16px;
    font-size: 0.9em;
}
.diff-btn-accept-all:hover {
    background: #059669;
}
.diff-btn-reject-all {
    background: #f43f5e;
    color: white;
    border-radius: 6px;
    padding: 6px 16px;
    font-size: 0.9em;
}
.diff-btn-reject-all:hover {
    background: #e11d48;
}

/* -- Diff Viewer Widget ------------------------------------------------- */
.diff-viewer {
    background: rgba(0, 0, 0, 0.02);
}
.diff-viewer-header {
    padding: 8px 12px;
    border-bottom: 1px solid alpha(@theme_fg_color, 0.08);
    background: rgba(0, 0, 0, 0.05);
}
.diff-viewer-title {
    font-size: 1.1em;
    font-weight: bold;
    font-family: monospace;
    margin-left: 8px;
}
.diff-viewer-subtitle {
    font-size: 0.85em;
    color: alpha(@theme_fg_color, 0.5);
    margin-left: 8px;
}
.diff-viewer-action-bar {
    padding: 6px 12px;
    border-top: 1px solid alpha(@theme_fg_color, 0.08);
    background: rgba(0, 0, 0, 0.03);
}
.diff-viewer-revert-btn {
    background: rgba(244, 63, 94, 0.2);
    color: #f43f5e;
    border-radius: 6px;
    padding: 6px 16px;
    font-size: 0.9em;
}
.diff-viewer-revert-btn:hover {
    background: rgba(244, 63, 94, 0.3);
}
.diff-viewer-copy-btn {
    background: rgba(6, 182, 212, 0.2);
    color: #06b6d4;
    border-radius: 6px;
    padding: 6px 16px;
    font-size: 0.9em;
}
.diff-viewer-copy-btn:hover {
    background: rgba(6, 182, 212, 0.3);
}
.diff-history-row {
    padding: 6px 12px;
    border-bottom: 1px solid alpha(@theme_fg_color, 0.06);
}
.diff-history-row:hover {
    background: alpha(@theme_fg_color, 0.03);
}
.diff-history-row-sha {
    font-family: monospace;
    font-size: 0.85em;
    color: #06b6d4;
    min-width: 6em;
}
.diff-history-row-date {
    font-size: 0.85em;
    color: alpha(@theme_fg_color, 0.5);
    min-width: 8em;
}
.diff-history-row-msg {
    font-size: 0.9em;
}
/* -- Feed Cards -------------------------------------------------------- */
.feed-card {
    border-radius: 6px;
    border: 1px solid rgba(255, 255, 255, 0.08);
    margin-bottom: 8px;
}
.feed-card-header {
    padding: 2px 8px;
    font-weight: bold;
    font-size: 12px;
    border-radius: 6px 6px 0 0;
}
.feed-card-body {
    padding: 8px 12px;
    font-family: monospace;
    font-size: 12px;
    min-height: 24px;
}
.feed-card-footer {
    padding: 6px 12px;
    font-size: 11px;
    color: alpha(@theme_fg_color, 0.5);
    background: rgba(0, 0, 0, 0.25);
}
.feed-card-actions {
    padding: 4px 12px 8px;
    background: rgba(0, 0, 0, 0.25);
    border-radius: 0 0 6px 6px;
}

/* Card type colors */
.feed-card-git .feed-card-header { background: #2d5a3d; color: #a8e6c1; }
.feed-card-git .feed-card-body { background: #1a3d2a; }
.feed-card-diff .feed-card-header { background: #5a4a2d; color: #e6c1a8; }
.feed-card-diff .feed-card-body { background: #3d321a; }
.feed-card-file-new .feed-card-header { background: #2d4a5a; color: #a8c1e6; }
.feed-card-file-new .feed-card-body { background: #1a323d; }
.feed-card-file-mod .feed-card-header { background: #5a4a2d; color: #e6e6a8; }
.feed-card-file-mod .feed-card-body { background: #3d321a; }
.feed-card-file-del .feed-card-header { background: #5a2d2d; color: #e6a8a8; }
.feed-card-file-del .feed-card-body { background: #3d1a1a; }
.feed-card-dir-new .feed-card-header { background: #2d5a5a; color: #a8e6e6; }
.feed-card-dir-new .feed-card-body { background: #1a3d3d; }
.feed-card-dir-del .feed-card-header { background: #5a3d3d; color: #e6b8b8; }
.feed-card-dir-del .feed-card-body { background: #3d1a1a; }
.feed-card-agent .feed-card-header { background: #4a2d5a; color: #c1a8e6; }
.feed-card-agent .feed-card-body { background: #321a3d; }
.feed-card-task .feed-card-header { background: #5a5a2d; color: #e6e6a8; }
.feed-card-task .feed-card-body { background: #3d3d1a; }
.feed-card-system .feed-card-header { background: #3a3a3a; color: #b0b0b0; }
.feed-card-system .feed-card-body { background: #2a2a2a; }
.feed-card-audit .feed-card-header { background: #2d5a5a; color: #a8e6e6; }
.feed-card-audit .feed-card-body { background: #1a3d3d; }
/* Agent action sub-states */
.feed-card-agent.feed-card-approval .feed-card-header {
    background: #5a3d2d; color: #ffb085;
}
.feed-card-agent.feed-card-approval .feed-card-body {
    background: #3d2a1a;
}
.feed-card-agent.feed-card-running .feed-card-header {
    background: #2d3a5a; color: #a8c1e6;
}
.feed-card-agent.feed-card-running .feed-card-body {
    background: #1a273d;
}
.feed-card-agent.feed-card-complete .feed-card-header {
    background: #2d4a3d; color: #a8e6c1;
}
.feed-card-agent.feed-card-complete .feed-card-body {
    background: #1a3d2a;
}
.feed-card-agent.feed-card-error .feed-card-header {
    background: #5a2d2d; color: #e6a8a8;
}
.feed-card-agent.feed-card-error .feed-card-body {
    background: #3d1a1a;
}

/* Sequence number badge */
.feed-card-seq {
    background: rgba(99, 102, 241, 0.3);
    color: #c7d2fe;
    border-radius: 4px;
    padding: 1px 6px;
    font-size: 10px;
    font-weight: bold;
    min-width: 20px;
}

/* Batch accept bar */
.feed-batch-bar {
    background: rgba(30, 30, 40, 0.9);
    border: 1px solid rgba(99, 102, 241, 0.3);
    border-radius: 6px;
    padding: 6px 12px;
    margin-bottom: 8px;
}
.feed-batch-bar-info {
    color: #a5b4fc;
    font-size: 12px;
}
.feed-btn-batch-accept {
    background: rgba(16, 185, 129, 0.3);
    color: #6ee7b7;
    border-radius: 4px;
    padding: 4px 12px;
    font-size: 12px;
    border: none;
}
.feed-btn-batch-accept:hover {
    background: rgba(16, 185, 129, 0.5);
}

/* Persistent feed toolbar (Phase 5 — auto-accept toggle + batch button) */
.feed-toolbar {
    background: rgba(30, 30, 40, 0.9);
    border: 1px solid rgba(99, 102, 241, 0.3);
    border-radius: 6px;
    padding: 4px 8px;
    margin-top: 8px;
}
.feed-toolbar-toggle {
    background: rgba(99, 102, 241, 0.2);
    color: #a5b4fc;
    border-radius: 4px;
    padding: 2px 8px;
    font-size: 11px;
    min-height: 22px;
    min-width: 0;
    border: none;
}
.feed-toolbar-toggle:checked {
    background: rgba(16, 185, 129, 0.3);
    color: #6ee7b7;
}
.feed-toolbar-batch {
    border-radius: 4px;
    padding: 2px 8px;
    font-size: 11px;
    min-height: 22px;
    border: none;
}
.feed-toolbar-snooze {
    border-radius: 4px;
    padding: 2px 8px;
    font-size: 11px;
    min-height: 22px;
    border: none;
}
.feed-toolbar-agent-dropdown {
    padding: 0 6px;
    min-height: 22px;
    font-size: 11px;
}
.feed-toolbar > Gtk.Button,
.feed-toolbar > Gtk.MenuButton {
    margin: 0 2px;
}

/* Load More card */
.feed-card-load-more .feed-card-body {
    background: rgba(255, 255, 255, 0.02);
}
.feed-btn-load-more {
    border-radius: 4px;
    padding: 4px 12px;
    font-size: 12px;
}

/* Feed action buttons */
.feed-btn-review,
.feed-btn-accept,
.feed-btn-reject {
    border-radius: 4px;
    padding: 3px 10px;
    font-size: 11px;
    border: none;
}
.feed-btn-review {
    background: rgba(99, 102, 241, 0.2);
    color: #a5b4fc;
}
.feed-btn-review:hover {
    background: rgba(99, 102, 241, 0.35);
}
.feed-btn-accept {
    background: rgba(16, 185, 129, 0.2);
    color: #10b981;
}
.feed-btn-accept:hover {
    background: rgba(16, 185, 129, 0.35);
}
.feed-btn-reject {
    background: rgba(244, 63, 94, 0.2);
    color: #f43f5e;
}
.feed-btn-reject:hover {
    background: rgba(244, 63, 94, 0.35);
}

/* Feed tab container */
.feed-tab-bar { background: rgba(30, 30, 40, 0.5); border-radius: 6px; margin: 4px 8px; }
.feed-tab-bar button { padding: 6px 16px; }
.feed-tab-bar button:checked { background: rgba(99, 102, 241, 0.3); color: #c7d2fe; border-radius: 4px; }
.feed-scroll { background: transparent; }
.feed-card-list { padding: 8px; }
.feed-empty { padding: 48px; color: alpha(@theme_fg_color, 0.4); }

/* Feed reference in chat bubbles */
.feed-reference {
    background: rgba(99, 102, 241, 0.1);
    border: 1px solid rgba(99, 102, 241, 0.2);
    border-radius: 4px;
    padding: 4px 10px;
    font-size: 12px;
    color: #a5b4fc;
}
.feed-reference:hover {
    background: rgba(99, 102, 241, 0.2);
    color: #c7d2fe;
}
.feed-ref-icon { margin-right: 4px; }
.feed-ref-title { font-weight: 500; }

/* Feed card accepted/rejected overlays */
.feed-card-accepted { opacity: 0.6; }
.feed-card-rejected { opacity: 0.4; }
.feed-accepted-badge {
    background: rgba(16, 185, 129, 0.8);
    color: white;
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 10px;
    font-weight: bold;
}
.feed-rejected-badge {
    background: rgba(244, 63, 94, 0.8);
    color: white;
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 10px;
    font-weight: bold;
}
/* ── Feed Card Context Panel ────────────────────────────────────── */
.feed-context-panel {
    background: alpha(@theme_bg_color, 0.5);
    border-top: 1px solid alpha(@theme_fg_color, 0.1);
    border-radius: 0 0 8px 8px;
    padding: 8px;
    margin: 4px 6px 6px 6px;
}
.feed-context-header {
    font-weight: bold;
    font-size: 0.9em;
    color: alpha(@theme_fg_color, 0.7);
}
.feed-context-mini-bubble {
    padding: 4px 8px;
    border-radius: 6px;
    margin: 2px 0;
    font-size: 0.85em;
}
.feed-context-mini-bubble-user {
    background: alpha(@theme_selected_bg_color, 0.15);
    margin-left: 24px;
}
.feed-context-mini-bubble-agent {
    background: alpha(@theme_fg_color, 0.08);
    margin-right: 24px;
}
.feed-context-diff {
    font-family: monospace;
    font-size: 0.82em;
    padding: 8px;
    background: alpha(#1e1e1e, 0.9);
    border-radius: 4px;
    color: #d4d4d4;
}
.feed-context-diff-line-add {
    color: #6a9955;
}
.feed-context-diff-line-del {
    color: #f44747;
}
.feed-context-empty {
    color: alpha(@theme_fg_color, 0.4);
    font-style: italic;
    padding: 8px;
}

/* -- Inline chat images ----------------------------------------------- */
.chat-image {
    border-radius: 8px;
    margin: 4px 0;
}
.chat-image:hover {
    filter: brightness(1.1);
}

/* -- Welcome bubble -------------------------------------------------- */
.welcome-bubble {
    border-radius: 12px;
    background: linear-gradient(135deg, rgba(20, 74, 127, 0.5), rgba(70, 25, 80, 0.4));
    border: 1px solid rgba(255, 255, 255, 0.1);
    padding: 14px 20px;
    margin: 6px 0;
    min-width: 180px;
}
.welcome-bubble-title {
    font-size: 1.0em;
    font-weight: bold;
    color: rgba(255, 255, 255, 0.88);
}
.welcome-tagline {
    font-size: 0.82em;
    color: rgba(255, 255, 255, 0.5);
    margin-top: 4px;
    letter-spacing: 0.03em;
}
.welcome-logo {
}

/* -- Activity Drawer (SPEC-activity-drawer Phase 1) --------------------- */
.activity-drawer {
    background-color: rgba(255, 255, 255, 0.03);
    border-top: 1px solid rgba(255, 255, 255, 0.12);
    padding: 0;
}

.activity-drawer-header {
    background-color: rgba(20, 20, 25, 0.95);
    padding: 3px 6px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    min-height: 0;
}

.activity-drawer-header button {
    min-height: 20px;
    padding: 1px 4px;
}

.activity-drawer-row {
    padding: 3px 8px;
    font-family: monospace;
    font-size: 0.9em;
    border-bottom: 1px solid rgba(255, 255, 255, 0.04);
}

.activity-drawer-row-lifecycle_start,
.activity-drawer-row-lifecycle_end {
    background-color: rgba(255, 255, 255, 0.05);
    font-style: italic;
}

.activity-drawer-row-tool_start,
.activity-drawer-row-tool_end {
    background-color: rgba(99, 102, 241, 0.03);
}

.activity-drawer-row-tool_error {
    background-color: rgba(239, 68, 68, 0.06);
}

.activity-drawer-row-command_output {
    background-color: rgba(34, 197, 94, 0.04);
}

.activity-drawer-row-patch {
    background-color: rgba(168, 85, 247, 0.04);
}

.activity-drawer-row-plan {
    background-color: rgba(99, 102, 241, 0.02);
}

.activity-drawer-output {
    font-family: monospace;
    font-size: 0.85em;
    padding: 4px 8px 4px 32px;
    background-color: rgba(20, 20, 25, 0.5);
    color: rgba(255, 255, 255, 0.8);
}

.activity-drawer-separator {
    padding: 4px 8px;
    background-color: rgba(255, 255, 255, 0.02);
    color: rgba(255, 255, 255, 0.5);
    font-size: 0.85em;
    border-top: 1px solid rgba(255, 255, 255, 0.06);
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
}

/* -- Toolbar status dot -------------------------------------------- */
.toolbar-status-dot {
    color: #ef4444;
    font-size: 14px;
    font-weight: 700;
}

/* -- Settings dialog ------------------------------------------------ */
.settings-dialog {
    min-width: 560px;
    min-height: 480px;
}

.settings-provider-card {
    background: rgba(40, 40, 55, 0.45);
    border-radius: 8px;
    padding: 12px;
    margin-bottom: 8px;
}

button.settings-test-btn {
    background: rgba(99, 102, 241, 0.25);
    color: #c7d2fe;
    border-radius: 6px;
    border: none;
}
button.settings-test-btn:hover {
    background: rgba(99, 102, 241, 0.45);
    color: #e0e7ff;
}

button.settings-remove-btn {
    background: rgba(244, 63, 94, 0.18);
    color: #fda4af;
    border-radius: 6px;
    border: none;
}
button.settings-remove-btn:hover {
    background: rgba(244, 63, 94, 0.35);
    color: #fecdd3;
}

.settings-status-ok {
    color: #22c55e;
    font-weight: 600;
}
.settings-status-fail {
    color: #f87171;
    font-weight: 600;
}
.settings-status-untested {
    color: #6b6b7a;
}

.settings-empty-state {
    color: #6b6b7a;
    font-size: 14px;
    padding: 32px;
}

/* -- Input toolbar ------------------------------------------------------ */
.input-toolbar {
    background: rgba(17, 17, 20, 0.6);
    border-radius: 6px;
    min-height: 0;
    padding: 0px 4px;
}

.input-toolbar button,
.input-toolbar .flat {
    min-width: 24px;
    min-height: 20px;
    padding: 1px 4px;
    font-size: 11px;
}

.input-toolbar .toolbar-separator {
    margin: 0px 4px;
    opacity: 0.3;
}

/* Find bar */
.find-bar {
    background: rgba(17, 17, 20, 0.8);
    border-radius: 4px;
    padding: 4px 8px;
}

.find-bar entry {
    min-width: 200px;
    font-size: 11px;
}

.find-bar .char-count {
    color: #6b6b7a;
    font-size: 10px;
}

/* Spell check toggle active state */
.spell-active {
    background: rgba(99, 102, 241, 0.2);
    color: #a5b4fc;
    border-radius: 4px;
}

/* ── File Tree Diff Drawer (Phase A) ──────────────────────────────────── */
.file-tree-drawer {
    padding: 0;
    border-left: 2px solid alpha(@theme_selected_bg_color, 0.3);
    background: alpha(@theme_bg_color, 0.3);
}

.file-tree-drawer-tab-btn {
    padding: 1px;
    margin: 0;
    min-width: 0;
    min-height: 0;
}

/* ── FileTree ColumnView (Phase 9) ──────────────────────────────────────── */
.file-tree-column-view {
    background: transparent;
}

/* Override GTK4 default selection highlight for the file tree */
.file-tree-column-view row:selected,
.file-tree-column-view row:selected:focus,
.file-tree-column-view listview row:selected {
    background: transparent;
    color: inherit;
}

.file-tree-row {
    padding: 2px 8px;
    min-height: 24px;
    border-radius: 4px;
}

.file-tree-row:hover {
    background: alpha(@theme_selected_bg_color, 0.1);
}

.file-tree-row:selected {
    background: alpha(@theme_selected_bg_color, 0.03);
}

.file-tree-row-expander {
    margin-right: 4px;
    min-width: 16px;
    min-height: 16px;
    padding: 0;
    border: none;
    background: transparent;
    color: #4ade80;
    font-size: 10px;
}

.file-tree-row-expander:hover {
    background: alpha(@theme_selected_bg_color, 0.1);
}

.file-tree-row-icon {
    margin-right: 6px;
    min-width: 16px;
    min-height: 16px;
}

.file-tree-row-label {
    font-size: 13px;
    color: @theme_fg_color;
}

.file-tree-drawer-row {
    padding: 0;
    margin: 0;
    background: alpha(@theme_bg_color, 0.3);
}

/* ── File tree status badges (Phase 1) ─────────────────────────────────── */
.file-tree-status-badge { padding: 1px 6px; border-radius: 9999px; font-size: 10px; font-weight: 600; min-width: 18px; }
.file-tree-status-modified { background: #f59e0b; color: #1e1e1e; }
.file-tree-status-staged { background: #22c55e; color: #1e1e1e; }
.file-tree-status-untracked { background: #6366f1; color: #fff; }
.file-tree-status-deleted { background: #ef4444; color: #fff; }
.file-tree-status-renamed { background: #a855f7; color: #fff; }
.file-tree-status-ignored { background: #6b7280; color: #fff; }
.file-tree-size-column { padding-right: 8px; font-size: 12px; color: #a0a0b0; }
.file-tree-modified-column { padding-right: 8px; font-size: 12px; color: #a0a0b0; }
.file-icon-python { color: #f0c674; }
.file-icon-js { color: #e5c07b; }
.file-icon-ts { color: #61afef; }
.file-icon-json { color: #e5c07b; }
.file-icon-yaml { color: #e06c75; }
.file-icon-md { color: #98c379; }
.file-icon-rust { color: #e06c75; }
.file-icon-go { color: #61afef; }
.file-icon-cpp { color: #61afef; }
.file-icon-c { color: #61afef; }
.file-icon-sh { color: #98c379; }
.file-icon-png, .file-icon-jpg, .file-icon-gif { color: #c678dd; }
.file-icon-pdf { color: #e06c75; }
.file-icon-zip, .file-icon-tar, .file-icon-gz { color: #e5c07b; }
.file-icon-folder { color: #f0c674; }
.file-icon-default { color: #6b6b7a; }
.file-tree-sort-dropdown { min-width: 140px; margin-left: 8px; }
/* ── Chat surface activity pill (SPEC-07 SP1 — the pill Gtk.Label lives in
   the GTK widget tree, NOT the webview document, so its rules belong here) ── */
.pill-idle { color: #6b6b7a; }
.pill-thinking { color: #e0af68; }
.pill-tool { color: #7aa2f7; }
.pill-error { color: #f7768e; }
.pill-streaming { color: #7dcfff; }
.pill-done { color: #9ece6a; }
"""


def apply_styles():
    """
    Register the global CSS provider for the application.
    Call once at startup, before any windows are created.
    """
    display = Gdk.Display.get_default()
    if display is None:
        return  # headless / test environment
    provider = Gtk.CssProvider()
    provider.load_from_data(APP_CSS.encode())
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
