# ui/handlers/media_handler.py
# MediaHandler — STT (push-to-talk) + prompt improvement.
#
# Thread safety: stop_async() calls on_result from a background thread.
# GTK dispatch is handled inside stt.stop_async() via GLib.idle_add.
# improve_prompt() dispatches its callback via GLib.idle_add when GLib is provided.

from typing import Callable


class MediaHandler:
    """Handles STT (OpenRouter cloud push-to-talk) and prompt improvement."""

    def __init__(
        self,
        main_content,
        improve_module=None,
        GLib_module=None,
        stt_engine_class=None,
    ):
        from utils.stt import STTEngine

        self._mc = main_content
        self._improve = improve_module
        self._GLib = GLib_module
        self._stt_class = stt_engine_class or STTEngine
        self._sync_callback: Callable = None  # called after voice send, with text

        self._stt_engine = self._stt_class(on_result=self._on_result)

    # ── STT — Public API ───────────────────────────────────────────────────

    def on_stt_click(self, _btn=None):
        """Toggle STT recording — start or stop_async."""
        if self._stt_engine.is_recording:
            self._mc.update_stt_state("idle")
            self._stt_engine.stop_async(on_done=self._on_result)
        else:
            self._stt_engine.start()
            # BUG #2 (SPEC-36 round 2): start() may refuse to record (no API key) —
            # re-read is_recording so the button never sticks on "recording".
            if self._stt_engine.is_recording:
                self._mc.update_stt_state("recording")
            else:
                self._mc.update_stt_state("idle")

    def set_on_send_callback(self, cb: Callable):
        """Window calls this so MediaHandler can trigger ChatHandler.on_send() after voice input."""
        self._sync_callback = cb

    # ── STT — Internal ─────────────────────────────────────────────────────

    def _on_result(self, text):
        """
        Handle transcription result — append to input and trigger send.
        Fires from background thread (dispatched via GLib.idle_add by stt_engine).
        """
        if not text:
            return
        if self._GLib is not None:
            self._GLib.idle_add(self._append_and_send, text)
        else:
            self._append_and_send(text)

    def _append_and_send(self, text):
        self._mc.append_stt_text(text)
        if self._sync_callback:
            self._sync_callback(text)

    # ── Improve — Public API ────────────────────────────────────────────────

    def _set_improve_status(self, text: str) -> None:
        """Surface a transient Improve status line, guarded (SPEC-55 BUG #4).

        ``self._mc`` may be any object with the improve surface — not every
        MainContent-like surface exposes ``set_improve_status`` (e.g. fake
        hosts in tests). Never let a missing surface crash the improve flow,
        but DO surface a real failure loudly: a status call that raises is
        logged, not silently swallowed by the dispatch wrapper.
        """
        setter = getattr(self._mc, "set_improve_status", None)
        if not callable(setter):
            return
        try:
            setter(text)
        except Exception:  # the status line must never break the improve flow
            import logging
            logging.warning(
                "[improve] status surface failed for text %r", text, exc_info=True
            )

    def on_improve_click(self, _btn=None):
        """Get current input text, send to the configured Improve model."""
        # A new improve click clears the previous transient status (SPEC-55 §3d).
        self._set_improve_status("")
        buf = self._mc.user_input.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).strip()
        if not text:
            return
        # Visual feedback — swap to "Improving..." + pulsing CSS
        btn = self._mc._improve_button
        btn.set_label("Improving…")
        btn.remove_css_class("btn-improve")
        btn.add_css_class("btn-improving")
        btn.set_sensitive(False)
        improve_prompt = self._improve.improve_prompt if self._improve else None
        if improve_prompt is None:
            return
        improve_prompt(text, self._on_improve_result, GLib=self._GLib)

    # ── Improve — Internal ─────────────────────────────────────────────────

    def _on_improve_result(self, improved_text, error):
        """Handle improve API result — replace input with improved text."""
        btn = self._mc._improve_button
        # Restore original label + CSS
        btn.set_label("Improve ✦")
        btn.remove_css_class("btn-improving")
        btn.add_css_class("btn-improve")
        btn.set_sensitive(True)
        if error:
            import logging
            logging.warning("[improve] error: %s", error)
            # SPEC-55 §3d: surface unconfigured/API errors near the input so the
            # failure is visible, not a silent flash.
            self._set_improve_status(error)
            return
        self._set_improve_status("")
        if improved_text:
            self._mc.replace_input_text(improved_text)
