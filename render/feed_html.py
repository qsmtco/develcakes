# render/feed_html.py — feed card → HTML fragment (SPEC-FEED-HTML-SURFACE).
#
# Pure. No GTK. Card bodies are escaped text (tool output, diffs, commands).
# They do not go through render_message or sanitize_agent_html.
#
# The document script is a constant. It posts {action, card_id} and never
# reads a card body. Probe (2026-10-17, WebKit 6.0, xvfb + aa-exec -p
# develcakes-python): window.webkit.messageHandlers.feed.postMessage(obj)
# delivers a JSC Value whose to_json(0) is that object's JSON.

from __future__ import annotations

import html

from models.feed_card import FeedCardData, format_feed_timestamp

# Same number as ui.views.feed_card.RENDERED_BODY_LIMIT. This module must
# not import the view (that import pulls GTK). A test pins the two equal.
FEED_RENDERED_BODY_LIMIT = 2000

REVEAL_TITLE_APPROVAL = "Full command text — view before approving"
REVEAL_TITLE_DEFAULT = "Show the full text"

# Inline SVG icons (inlined as markup — no external files/urls). Strokes use
# currentColor so CSS can tint them (e.g. the copy button's low-contrast
# resting state brightening on hover).
_COPY_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" '
    'viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<rect x="4.5" y="3" width="7.5" height="10" rx="1"/>'
    '<rect x="2" y="1" width="7.5" height="10" rx="1"/></svg>'
)
_CHECK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" '
    'viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<polyline points="3 8.5 6.5 12 13 4.5"/></svg>'
)

FEED_CSS = """
html { background: #1a1b26; height: 100%; }
body { margin: 0; background: #1a1b26; color: #c0caf5;
       font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
       min-height: 100%; }
#feed { display: flex; flex-direction: column; gap: 8px; padding: 12px;
        max-width: 760px; margin: 0 auto; }

/* Card elevation. The 3px left border is the per-type accent below. */
.feed-card { background: #1f2335; border: 1px solid #3b4261;
             border-left: 3px solid #3b4261; border-radius: 8px;
             padding: 12px; box-shadow: 0 1px 3px rgba(0,0,0,.35);
             overflow-wrap: anywhere; position: relative;
             transition: transform .15s ease, box-shadow .15s ease; }
.feed-card-enter { animation: feed-card-in .25s ease-out; }
.feed-card:hover { transform: translateY(-1px);
                   box-shadow: 0 4px 14px rgba(0,0,0,.42); }
@keyframes feed-card-in {
  from { opacity: 0; }
  to { opacity: 1; }
}

/* Type accent palette (git=blue, diff=teal, agent=purple,
   system=gray, audit=amber). Unlisted types keep the neutral gray. */
.feed-card-git { border-left-color: #7aa2f7; }
.feed-card-diff { border-left-color: #2ac3de; }
.feed-card-agent { border-left-color: #bb9af7; }
.feed-card-system { border-left-color: #565f89; }
.feed-card-audit { border-left-color: #e0af68; }
.feed-card-file-new { border-left-color: #9ece6a; }
.feed-card-file-del { border-left-color: #f7768e; }

/* Approval-pending cards get a gradient top hairline to draw the eye. */
.feed-card-approval::before {
  content: ""; position: absolute; top: 0; left: 12px; right: 12px;
  height: 2px; border-radius: 0 0 2px 2px;
  background: linear-gradient(90deg, #e0af68, #bb9af7, #7aa2f7);
}

/* Header band — distinct treatment: a tinted band with rounded top corners
   matching the card. */
.feed-card-header {
  display: flex; gap: 8px; align-items: center; justify-content: space-between;
  margin: -12px -12px 8px; padding: 10px 12px;
  background: linear-gradient(180deg, #242945, #1f2335);
  border-radius: 7px 7px 0 0;
  border-bottom: 1px solid rgba(59, 66, 97, .55);
}
.feed-card-footer, .feed-card-actions {
  display: flex; gap: 8px; align-items: center; }
.feed-card-footer { justify-content: space-between; color: #9b9bab;
                    font-size: 12px; }
.feed-card-title { font-weight: 600; flex: 1 1 auto; }
.feed-card-seq { color: #9b9bab; }
.feed-card-type-icon { font-size: 13px; line-height: 1; }

.feed-card-body { margin: 6px 0; font-size: 12.5px; color: #a9b1d6; }
.feed-body-mono, .feed-diff { white-space: pre-wrap;
                              font-family: ui-monospace, monospace;
                              background: #16161e; padding: 8px;
                              border-radius: 6px; }
.feed-body-path { margin: 0 0 6px 0; }
.feed-diff-add { color: #6a9955; }
.feed-diff-del { color: #f44747; }
.feed-task-id { color: #9b9bab; }

/* Verdict badges — real pills: tinted background + border, uppercase, inline-flex. */
.feed-accepted-badge, .feed-rejected-badge {
  display: inline-flex; align-items: center;
  font-size: 10px; font-weight: 600; letter-spacing: .05em;
  text-transform: uppercase; padding: 2px 8px;
  border-radius: 999px; border: 1px solid transparent; }
.feed-accepted-badge {
  color: #9ece6a; background: rgba(158, 206, 106, .12);
  border-color: rgba(158, 206, 106, .4); }
.feed-rejected-badge {
  color: #f7768e; background: rgba(247, 118, 142, .12);
  border-color: rgba(247, 118, 142, .4); }

.feed-context-panel { margin-top: 8px; border-top: 1px solid #3b4261;
                      padding-top: 8px; }
.feed-empty { text-align: center; padding: 24px; color: #9b9bab; }

button { background: #2f334d; color: #c0caf5; border: 1px solid #3b4261;
         border-radius: 6px; padding: 4px 10px; cursor: pointer; }
button:hover { background: #3b4261; border-color: #565f89; color: #c0caf5; }

/* Action buttons — distinct tints: Review neutral/blue, Accept green, Reject red. */
button.feed-btn-review {
  background: rgba(122, 162, 247, .13); border-color: rgba(122, 162, 247, .42);
  color: #a3b8ef; }
button.feed-btn-review:hover {
  background: rgba(122, 162, 247, .24); border-color: rgba(122, 162, 247, .7);
  color: #c0caf5; }
button.feed-btn-accept {
  background: rgba(158, 206, 106, .13); border-color: rgba(158, 206, 106, .42);
  color: #9ece6a; }
button.feed-btn-accept:hover {
  background: rgba(158, 206, 106, .24); border-color: rgba(158, 206, 106, .7);
  color: #c0caf5; }
button.feed-btn-reject {
  background: rgba(247, 118, 142, .13); border-color: rgba(247, 118, 142, .42);
  color: #f7768e; }
button.feed-btn-reject:hover {
  background: rgba(247, 118, 142, .24); border-color: rgba(247, 118, 142, .7);
  color: #c0caf5; }

/* Copy button — icon-only, subtle until hover. Check icon swaps in on copy. */
button.feed-copy-btn {
  display: inline-flex; align-items: center; justify-content: center;
  background: transparent; border: 1px solid transparent;
  padding: 4px; border-radius: 6px; opacity: .45;
  transition: opacity .12s ease, background .12s ease, border-color .12s ease; }
button.feed-copy-btn:hover { opacity: 1; background: #2f334d; border-color: #3b4261; }
.feed-copy-btn svg { display: block; }
.feed-copy-check { display: none; }
.feed-copy-btn.copied { opacity: 1; color: #9ece6a; }
.feed-copy-btn.copied .feed-copy-icon { display: none; }
.feed-copy-btn.copied .feed-copy-check { display: block; }
"""

# Static. Not formatted with card data.
FEED_SCRIPT = """
(function () {
  var bridge = window.webkit && window.webkit.messageHandlers
    && window.webkit.messageHandlers.feed;

  function post(obj) {
    if (bridge) bridge.postMessage(obj);
  }

  document.addEventListener("click", function (ev) {
    var node = ev.target;
    if (!node || !node.closest) return;
    var btn = node.closest("button[data-feed-action]");
    if (!btn) return;
    post({
      action: btn.getAttribute("data-feed-action"),
      card_id: btn.getAttribute("data-feed-card")
    });
    // Copy feedback: brief 'copied ✓' state — only when a live bridge is
    // present (post() is a silent no-op otherwise, so a checkmark would
    // assert a copy that never happened). Upsert reloads replace the whole
    // document, so this state is best-effort and may be cut short.
    if (bridge && btn.classList.contains("feed-copy-btn")) {
      btn.classList.add("copied");
      clearTimeout(btn._copyTimer);
      btn._copyTimer = setTimeout(function () {
        btn.classList.remove("copied");
      }, 1200);
    }
  });

  var doc = document.documentElement;
  var pending = false;
  var lastAt = 0;

  function scrollTop() {
    return window.scrollY || doc.scrollTop || 0;
  }

  function nearBottom() {
    var viewport = window.innerHeight || doc.clientHeight || 0;
    return (doc.scrollHeight - viewport - scrollTop()) <= 80;
  }

  function postScroll() {
    pending = false;
    lastAt = Date.now();
    post({
      action: "scroll_state",
      near_bottom: nearBottom(),
      y: scrollTop(),
      h: doc.scrollHeight
    });
  }

  window.addEventListener("scroll", function () {
    if (pending) return;
    var wait = 150 - (Date.now() - lastAt);
    if (wait <= 0) { postScroll(); return; }
    pending = true;
    setTimeout(postScroll, wait);
  }, { passive: true });
})();
"""

_EMPTY = (
    '<div class="feed-empty">'
    '<p>No activity yet</p>'
    '<p>Agent actions will appear here</p>'
    '</div>'
)

_MONO_TYPES = frozenset({"diff", "git_commit", "system"})
_FILE_TYPES = frozenset({
    "file_created", "file_modified", "file_deleted", "dir_created", "dir_deleted",
})

# Per-type header icon (emoji — small, tasteful, no external assets).
_TYPE_ICONS = {
    "git_commit": "🌿",
    "diff": "📎",
    "agent_action": "🤖",
    "system": "⚙️",
    "audit_report": "🛡️",
    "file_created": "📄",
    "file_modified": "✏️",
    "file_deleted": "🗑️",
    "dir_created": "📁",
    "dir_deleted": "🗂️",
    "task": "📋",
}


def _esc(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _meta(card: FeedCardData) -> dict:
    return card.metadata if isinstance(card.metadata, dict) else {}


def _body_text(card: FeedCardData) -> str:
    return card.body if isinstance(card.body, str) else ""


def _button(
    action: str,
    label: str,
    card_id: str,
    title: str | None = None,
    css_class: str | None = None,
) -> str:
    extra = f' title="{_esc(title)}"' if title else ""
    cls = f' class="{css_class}"' if css_class else ""
    return (
        f'<button type="button"{cls} data-feed-action="{action}" '
        f'data-feed-card="{_esc(card_id)}"{extra}>{_esc(label)}</button>'
    )


def _copy_button(card_id: str) -> str:
    """Icon-only copy button: inline SVG, title tooltip, data-feed-action intact."""
    return (
        '<button type="button" class="feed-copy-btn" data-feed-action="copy" '
        f'data-feed-card="{_esc(card_id)}" title="Copy" aria-label="Copy">'
        f'<span class="feed-copy-icon">{_COPY_SVG}</span>'
        f'<span class="feed-copy-check">{_CHECK_SVG}</span>'
        "</button>"
    )


def _capped_body(text: str, card_id: str, *, revealed: bool, approval: bool) -> str:
    if not text:
        return ""
    if revealed or len(text) <= FEED_RENDERED_BODY_LIMIT:
        return _esc(text)
    hidden = len(text) - FEED_RENDERED_BODY_LIMIT
    title = REVEAL_TITLE_APPROVAL if approval else REVEAL_TITLE_DEFAULT
    shown = _esc(text[:FEED_RENDERED_BODY_LIMIT])
    reveal = _button("reveal", f"… {hidden} more characters", card_id, title)
    return shown + reveal


def _classes(card: FeedCardData, meta: dict) -> str:
    names = ["feed-card", FeedCardData.css_class_for_type(card.card_type)]
    if card.accepted is True:
        names.append("feed-card-accepted")
    elif card.accepted is False:
        names.append("feed-card-rejected")
    if card.card_type == "agent_action":
        if meta.get("needs_approval"):
            names.append("feed-card-approval")
        elif meta.get("status") == "running":
            names.append("feed-card-running")
        elif meta.get("status") == "complete":
            names.append("feed-card-complete")
        elif meta.get("status") == "error":
            names.append("feed-card-error")
    return " ".join(names)


def _actions(card: FeedCardData, meta: dict, card_id: str) -> str:
    if not FeedCardData.is_actionable(card.card_type, meta):
        return ""
    review = _button("review", "Review", card_id, css_class="feed-btn-review")
    if card.accepted is not None:
        return f'<div class="feed-card-actions">{review}</div>'
    if meta.get("needs_approval"):
        accept = _button("accept", "Approve", card_id, css_class="feed-btn-accept")
        reject = _button("reject", "Deny", card_id, css_class="feed-btn-reject")
    else:
        accept = _button("accept", "Accept", card_id, css_class="feed-btn-accept")
        reject = _button("reject", "Reject", card_id, css_class="feed-btn-reject")
    return f'<div class="feed-card-actions">{review}{accept}{reject}</div>'


def _context(card: FeedCardData, *, context_open: bool) -> str:
    snap = card.conversation_snapshot
    if not context_open or snap is None:
        return ""
    if snap.snapshot_type == "conversation":
        header = "▼ Review Context"
        if snap.total_messages > len(snap.messages):
            header += (
                f" (showing last {len(snap.messages)} of {snap.total_messages})"
            )
        rows = []
        for msg in snap.messages:
            rows.append(
                f'<p><b>{_esc(msg.role)}:</b> {_esc(msg.text)}</p>'
            )
        if not rows:
            rows.append("<p>No conversation context available.</p>")
        body = "".join(rows)
    else:
        header = "▼ Diff Context"
        if not snap.diff_text:
            body = "<p>No diff available.</p>"
        else:
            lines = []
            for line in snap.diff_text.split("\n"):
                escaped = _esc(line)
                if line.startswith("+") and not line.startswith("+++"):
                    lines.append(f'<span class="feed-diff-add">{escaped}</span>')
                elif line.startswith("-") and not line.startswith("---"):
                    lines.append(f'<span class="feed-diff-del">{escaped}</span>')
                else:
                    lines.append(escaped)
            body = '<pre class="feed-diff">' + "\n".join(lines) + "</pre>"
    return f'<div class="feed-context-panel"><p>{_esc(header)}</p>{body}</div>'


def _main_body(card: FeedCardData, meta: dict, card_id: str, *, revealed: bool) -> str:
    text = _body_text(card)
    approval = bool(meta.get("needs_approval"))
    rendered = _capped_body(text, card_id, revealed=revealed, approval=approval)
    mono = " feed-body-mono" if card.card_type in _MONO_TYPES else ""
    parts: list[str] = []
    if card.card_type in _FILE_TYPES:
        icon = "📁" if card.card_type == "dir_created" else "📄"
        parts.append(f'<p class="feed-body-path">{icon} <b>{_esc(card.file_path)}</b></p>')
    if card.card_type == "task":
        parts.append(f"<p><b>📋 {_esc(card.title)}</b></p>")
    if rendered:
        parts.append(f'<div class="feed-card-body{mono}">{rendered}</div>')
    elif not parts:
        parts.append('<div class="feed-card-body"></div>')
    if card.card_type == "task" and card.task_id:
        parts.append(f'<p class="feed-task-id">ID: {_esc(card.task_id)}</p>')
    return "".join(parts)


def render_feed_card(
    card: FeedCardData,
    *,
    revealed: bool = False,
    context_open: bool = False,
) -> str:
    """One card as an HTML fragment. Body text is escaped. No sanitizer."""
    meta = _meta(card)
    card_id = card.card_id or ""
    seq = f'<span class="feed-card-seq">#{card.seq_num}</span>' if card.seq_num is not None else ""
    icon = _TYPE_ICONS.get(card.card_type, "")
    icon_html = f'<span class="feed-card-type-icon">{icon}</span>' if icon else ""
    badge = ""
    if card.accepted is True:
        badge = '<span class="feed-accepted-badge">ACCEPTED</span>'
    elif card.accepted is False:
        badge = '<span class="feed-rejected-badge">REJECTED</span>'
    header = (
        '<div class="feed-card-header">'
        f'{seq}{icon_html}<span class="feed-card-title">{_esc(card.title)}</span>'
        f'{_copy_button(card_id)}'
        "</div>"
    )
    footer = (
        '<div class="feed-card-footer">'
        f'<span>{_esc(card.author)} • {_esc(format_feed_timestamp(card.timestamp))}</span>'
        f"{badge}</div>"
    )
    return (
        f'<article class="{_classes(card, meta)}" data-card-id="{_esc(card_id)}">'
        f"{header}{_main_body(card, meta, card_id, revealed=revealed)}"
        f"{footer}{_actions(card, meta, card_id)}{_context(card, context_open=context_open)}"
        "</article>"
    )


def render_feed_document(fragments: list[str]) -> str:
    """Full document. fragments are render_feed_card output, oldest first.

    An empty list is the empty state. The script is FEED_SCRIPT, not card data.
    """
    body = "".join(fragments) if fragments else _EMPTY
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{FEED_CSS}</style></head><body>"
        f'<div id="feed">{body}</div><script>{FEED_SCRIPT}</script>'
        "</body></html>"
    )
