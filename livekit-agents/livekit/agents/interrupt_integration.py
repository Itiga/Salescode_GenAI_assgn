# livekit-agents/src/livekit/agents/interrupt_integration.py
from __future__ import annotations
from typing import Any
import os
import time

from .interrupt_controller import InterruptController

def wire_interrupt_to_session(session: Any, *, pending_timeout_ms: int | None = None, fuzzy_distance: int | None = None) -> None:
    """
    Attempt to wire InterruptController to a session object.
    The function is defensive: it checks for common plugin APIs and
    registers callbacks where possible.

    Usage:
        from livekit.agents.interrupt_integration import wire_interrupt_to_session
        wire_interrupt_to_session(session)
    """

    pending_timeout_ms = pending_timeout_ms or int(os.getenv("INTERRUPT_PENDING_MS", "250"))
    fuzzy_distance = fuzzy_distance or int(os.getenv("FUZZY_DISTANCE", "1"))

    controller = InterruptController(pending_timeout_ms=pending_timeout_ms, fuzzy_distance=fuzzy_distance)

    # helper: canonical stop function
    def stop_tts_immediate():
        # prefer session.stop or tts_player.stop
        try:
            if hasattr(session, "stop"):
                session.stop()
                return
            tts = getattr(session, "tts_player", None) or getattr(session, "tts", None)
            if tts is not None and hasattr(tts, "stop"):
                tts.stop()
                return
            # fallback: attempt to call a player attr
            if hasattr(session, "tts_player") and getattr(session.tts_player, "stop", None):
                session.tts_player.stop()
        except Exception:
            pass

    # helper: canonical handle user text function
    def handle_user_text(text: str):
        # try common intake methods
        try:
            if hasattr(session, "handle_user_text"):
                session.handle_user_text(text)
                return
            if hasattr(session, "_enqueue_user_text"):
                session._enqueue_user_text(text)
                return
            # some sessions have process_transcript / on_user_transcript
            if hasattr(session, "process_transcript"):
                session.process_transcript(text)
                return
            if hasattr(session, "on_user_transcript"):
                session.on_user_transcript(text)
                return
            # fallback: keep in logs
            print("[interrupt_integration] user text:", text, flush=True)
        except Exception:
            pass

    # Register tts hooks (start/end)
    try:
        tts = getattr(session, "tts_player", None) or getattr(session, "tts", None)
        if tts is not None:
            if hasattr(tts, "on_start"):
                try:
                    tts.on_start(controller.on_tts_start)
                except Exception:
                    pass
            elif hasattr(tts, "add_start_callback"):
                try:
                    tts.add_start_callback(controller.on_tts_start)
                except Exception:
                    pass

            if hasattr(tts, "on_end"):
                try:
                    tts.on_end(controller.on_tts_end)
                except Exception:
                    pass
            elif hasattr(tts, "add_end_callback"):
                try:
                    tts.add_end_callback(controller.on_tts_end)
                except Exception:
                    pass
    except Exception:
        pass

    # Hook STT partials
    try:
        stt = getattr(session, "stt", None)
        if stt is not None:
            # prefer an on_partial registration
            if hasattr(stt, "on_partial"):
                try:
                    stt.on_partial(lambda txt: controller.on_stt_partial(txt, stop_tts_immediate, handle_user_text))
                except Exception:
                    pass
            # try add_partial_handler
            elif hasattr(stt, "add_partial_handler"):
                try:
                    stt.add_partial_handler(lambda txt: controller.on_stt_partial(txt, stop_tts_immediate, handle_user_text))
                except Exception:
                    pass
            # if stt emits events via callbacks attribute
            elif hasattr(stt, "callbacks"):
                try:
                    stt.callbacks["partial"] = lambda txt: controller.on_stt_partial(txt, stop_tts_immediate, handle_user_text)
                except Exception:
                    pass
    except Exception:
        pass

    # Hook VAD start event(s)
    # Many VADs have .on_start / .on_speech_start or they call a session method like _on_vad_start
    try:
        vad = getattr(session, "vad", None) or getattr(session, "vad_engine", None)
        if vad is not None:
            if hasattr(vad, "on_start"):
                try:
                    # register wrapper to call controller but not stop TTS directly
                    vad.on_start(lambda ev=None: controller.on_vad_start(stop_tts_immediate))
                except Exception:
                    pass
            elif hasattr(vad, "on_speech_start"):
                try:
                    vad.on_speech_start(lambda ev=None: controller.on_vad_start(stop_tts_immediate))
                except Exception:
                    pass
    except Exception:
        pass

    # If session implements its own _on_vad_start method, wrap it
    try:
        if hasattr(session, "_on_vad_start"):
            orig = session._on_vad_start
            async def wrapped_vad_start(ev):
                # ask controller first
                try:
                    dec = controller.on_vad_start(stop_tts_immediate)
                except Exception:
                    dec = "passThrough"
                # ensure STT starts
                try:
                    if getattr(session, "stt", None) and hasattr(session.stt, "start_stream"):
                        await session.stt.start_stream()
                except Exception:
                    pass
                if dec == "passThrough":
                    await orig(ev)
                else:
                    # deferred: do not call original stop logic that pauses TTS
                    return
            session._on_vad_start = wrapped_vad_start
    except Exception:
        pass

    # If session has explicit tts playback, try to instrument start/stop there
    try:
        if hasattr(session, "play_tts") and callable(getattr(session, "play_tts")):
            orig_play = session.play_tts
            def wrapped_play(*args, **kwargs):
                try:
                    controller.on_tts_start()
                except Exception:
                    pass
                r = orig_play(*args, **kwargs)
                # Note: if orig_play is async, we can't await here; best-effort only
                return r
            session.play_tts = wrapped_play
        # hook end via stop
        if hasattr(session, "tts_player") and hasattr(session.tts_player, "stop"):
            # nothing more to do; stop_tts_immediate uses tts_player.stop
            pass
    except Exception:
        pass

    # persist controller for debugging
    setattr(session, "_interrupt_controller", controller)
    print("[interrupt_integration] wired controller to session (best-effort)", flush=True)
