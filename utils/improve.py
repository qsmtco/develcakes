# utils/improve.py
# Prompt improvement via MiniMax API.
#
# Security Manifest:
#   Reads: ~/.config/develcakes/config.json (apiKey, baseUrl, model)
#   Reads: <crabcakes_root>/prompts/system/improve.md (system prompt template)
#   External: POST to baseUrl (MiniMax API), HTTPS
#   No files written; no secrets stored

import json
import os
import threading
import urllib.request


# MED-5: Custom redirect handler that strips Authorization on cross-host redirects
class _NoAuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Strips Authorization header when following redirects to a different host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        from urllib.parse import urlparse
        result = urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl
        )
        if result is not None:
            old_host = urlparse(req.full_url).hostname
            new_host = urlparse(newurl).hostname
            if old_host and new_host and old_host != new_host:
                if "Authorization" in result.headers:
                    del result.headers["Authorization"]
                if "authorization" in result.unredirected_hdrs:
                    del result.unredirected_hdrs["authorization"]
        return result

from utils.config import get_config_file

DEFAULT_BASE_URL = "https://api.minimax.io/v1/text/chatcompletion_v2"
DEFAULT_MODEL = "MiniMax-M2.5-Lightning"

# Template marker — replaced with user input text before sending to the API.
# Placed in improve-system-prompt.md to control where the user's text lands
# in the assembled prompt. Code searches for this marker; if found, the
# entire template is sent as a single user message. If not found, falls
# back to legacy two-message split (system + user).
USER_INPUT_MARKER = "{{USER_INPUT}}"

DEFAULT_SYSTEM_PROMPT = """You are an expert technical editor. Rewrite all input text to be maximally clear, detailed, and precise.

Specifically:
• Replace vague or ambiguous phrasing with specific, concrete language
• Expand every abbreviation and acronym on first use
• Define unexplained jargon and technical terms inline
• Add precision where statements are general or hand-wavy
• Correct all spelling, grammar, and punctuation errors
• Structure output to mirror input order — do not reorder, summarize, or omit content

Output format:
• Return ONLY the improved text — no preamble, no explanation, no quotes, no labels, no commentary
• Preserve all original meaning and intent
• Be maximally verbose — completeness and precision always outweigh brevity

**Input:**
{{USER_INPUT}}

**Output:** Return the improved version with all linguistic issues resolved, maintaining the original meaning and intent."""

_config = None
_config_lock = threading.Lock()


def _load_config():
    """Load config from ~/.config/develcakes/config.json (cached)."""
    global _config
    with _config_lock:
        if _config is not None:
            return _config
        path = get_config_file()
        try:
            with open(path) as f:
                _config = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _config = {}
        return _config


def improve_prompt(raw_text, callback, GLib=None):
    """
    Improve a prompt by sending it to the MiniMax API.

    Runs in a background thread. Calls callback(improved_text, error) when done.
    If GLib is provided, callback is dispatched via GLib.idle_add (thread-safe).

    Args:
        raw_text:   The raw prompt text to improve.
        callback:   Function(improved_text, error). error is None on success.
        GLib:       Optional GLib module for GTK main thread dispatch.
    """
    cfg = _load_config()

    api_key = cfg.get("apiKey", "").strip()
    if not api_key:
        _dispatch(callback, None, "MINIMAX_API_KEY not set in ~/.config/develcakes/config.json", GLib)
        return

    base_url = cfg.get("baseUrl", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL

    # MED-5: Validate non-loopback provider URL must use https://
    try:
        from utils.provider_url import validate_provider_url
        validate_provider_url(base_url)
    except ImportError:
        pass  # Skip validation if provider_url module is unavailable
    except ValueError as e:
        _dispatch(callback, None, str(e), GLib)
        return

    model = cfg.get("model", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    # MED-6: Validate config file permissions
    try:
        path = get_config_file()
        if os.path.isfile(path):
            from utils.file_security import assert_secure_file
            assert_secure_file(path)
    except (ImportError, FileNotFoundError):
        pass  # Skip if security module is unavailable
    except PermissionError as e:
        # Warn but proceed; config.json API keys are used at runtime
        import logging
        logging.getLogger(__name__).warning("MED-6: %s", e)

    # Load system prompt from file
    prompts_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts", "system")
    system_prompt = DEFAULT_SYSTEM_PROMPT
    prompt_path = os.path.join(prompts_dir, "improve.md")
    if os.path.isfile(prompt_path):
        try:
            with open(prompt_path) as f:
                loaded = f.read().strip()
            if loaded:
                system_prompt = loaded
        except (IOError, OSError):
            pass

    # Build messages — template mode or legacy split
    if USER_INPUT_MARKER in system_prompt:
        # Template mode: inject user text at the marker, send as single user message.
        # The prompt file controls structure and placement of the user's text.
        assembled = system_prompt.replace(USER_INPUT_MARKER, raw_text)
        messages = [{"role": "user", "content": assembled}]
    else:
        # Legacy mode: system prompt + user text as separate messages.
        # Backward compatible with prompt files that don't use the marker.
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": raw_text},
        ]

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.3,
    }).encode()

    def _thread():
        try:
            req = urllib.request.Request(
                base_url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
            )
            # MED-5: Use custom redirect handler that strips Authorization on cross-host redirect
            _opener = urllib.request.build_opener(_NoAuthRedirectHandler)
            with _opener.open(req, timeout=30) as resp:
                body = json.loads(resp.read())
            choices = body.get("choices")
            if not choices:
                raise ValueError(f"API error: {body.get('error', 'no choices in response')}")
            msg = choices[0].get("message")
            if not isinstance(msg, dict) or "content" not in msg:
                raise ValueError(f"API error: choices[0].message missing 'content': {msg}")
            improved = msg.get("content")
            if not isinstance(improved, str):
                improved = str(improved) if improved else ""
            _dispatch(callback, improved, None, GLib)
        except Exception as e:
            _dispatch(callback, None, str(e), GLib)

    t = threading.Thread(target=_thread, daemon=True)
    t.start()


def _dispatch(callback, text, error, GLib):
    """Thread-safe callback dispatch.

    If GLib is provided, uses GLib.idle_add to schedule the callback on the GTK
    main thread. This is required whenever the caller is a background thread and the
    callback makes GTK calls (which are not thread-safe).

    GLib.idle_add returns False from _safe to remove the source after one execution."""
    def _safe():
        try:
            callback(text, error)
        except Exception:
            pass
        return False
    if GLib is not None:
        GLib.idle_add(_safe)
    else:
        _safe()
