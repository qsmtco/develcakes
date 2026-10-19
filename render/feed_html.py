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

FEED_CSS = """
body { margin: 0; background: #1a1b26; color: #c0caf5;
       font: 13px/1.4 sans-serif; }
#feed { display: flex; flex-direction: column; gap: 8px; padding: 8px; }
.feed-card { border: 1px solid #3b4261; border-radius: 6px; padding: 8px; }
.feed-card-header, .feed-card-footer, .feed-card-actions {
  display: flex; gap: 8px; align-items: center; }
.feed-card-body { margin: 6px 0; }
.feed-body-mono, .feed-diff { white-space: pre-wrap; font-family: monospace; }
.feed-diff-add { color: #6a9955; }
.feed-diff-del { color: #f44747; }
.feed-task-id { color: #9b9bab; }
.feed-accepted-badge { color: #9ece6a; }
.feed-rejected-badge { color: #f7768e; }
.feed-empty { text-align: center; padding: 24px; }
button { background: #2f334d; color: #c0caf5; border: 1px solid #3b4261;
         border-radius: 6px; padding: 4px 10px; }
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


def _esc(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _meta(card: FeedCardData) -> dict:
    return card.metadata if isinstance(card.metadata, dict) else {}


def _body_text(card: FeedCardData) -> str:
    return card.body if isinstance(card.body, str) else ""


def _button(action: str, label: str, card_id: str, title: str | None = None) -> str:
    extra = f' title="{_esc(title)}"' if title else ""
    return (
        f'<button type="button" data-feed-action="{action}" '
        f'data-feed-card="{_esc(card_id)}"{extra}>{_esc(label)}</button>'
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
    review = _button("review", "Review", card_id)
    if card.accepted is not None:
        return f'<div class="feed-card-actions">{review}</div>'
    if meta.get("needs_approval"):
        accept = _button("accept", "Approve", card_id)
        reject = _button("reject", "Deny", card_id)
    else:
        accept = _button("accept", "Accept", card_id)
        reject = _button("reject", "Reject", card_id)
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
    badge = ""
    if card.accepted is True:
        badge = '<span class="feed-accepted-badge">ACCEPTED</span>'
    elif card.accepted is False:
        badge = '<span class="feed-rejected-badge">REJECTED</span>'
    header = (
        '<div class="feed-card-header">'
        f'{seq}<span class="feed-card-title">{_esc(card.title)}</span>'
        f'{_button("copy", "Copy", card_id)}'
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
