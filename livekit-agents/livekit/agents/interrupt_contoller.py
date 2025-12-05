# livekit-agents/src/livekit/agents/interrupt_controller.py
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations
from typing import Callable, Optional, Sequence
import time

class InterruptController:
    """
    Small FSM to decide whether to interrupt TTS when VAD fires.
    Designed to be dependency-free and platform-agnostic.
    """

    SILENT = "SILENT"
    SPEAKING = "SPEAKING"
    PENDING = "PENDING_INTERRUPT"

    def __init__(
        self,
        ignore_list: Optional[Sequence[str]] = None,
        interrupt_list: Optional[Sequence[str]] = None,
        pending_timeout_ms: int = 250,
        fuzzy_distance: int = 1,
    ) -> None:
        self.state = self.SILENT
        self._pending_started_at: Optional[float] = None
        self._pending_timeout_ms = max(50, int(pending_timeout_ms))
        self._fuzzy = max(0, int(fuzzy_distance))

        self._ignore = set(
            s.lower()
            for s in (ignore_list or ["yeah", "ok", "okay", "hmm", "uh-huh", "mm-hmm", "right", "yup"])
        )
        self._interrupt = [s.lower() for s in (interrupt_list or ["stop", "wait", "hold", "pause", "no", "hold on", "hang on"])]

    # lifecycle
    def on_tts_start(self) -> None:
        self.state = self.SPEAKING

    def on_tts_end(self) -> None:
        self.state = self.SILENT

    # vad event
    def on_vad_start(self, stop_tts_immediate: Callable[[], None]) -> str:
        if self.state == self.SILENT:
            return "passThrough"
        self.state = self.PENDING
        self._pending_started_at = time.monotonic()
        return "defer"

    # stt partial/final
    def on_stt_partial(
        self,
        text: str,
        stop_tts_immediate: Callable[[], None],
        handle_user_text: Callable[[str], None],
    ) -> str:
        txt = (text or "").strip().lower()

        if self.state != self.PENDING:
            if self.state == self.SILENT and txt:
                handle_user_text(text)
            return "pass"

        if not txt:
            if self._is_pending_expired():
                self._clear_pending()
                return "ignore"
            return "pass"

        if self._contains_interrupt(txt):
            self._clear_pending()
            stop_tts_immediate()
            self.state = self.SILENT
            handle_user_text(text)
            return "interrupt"

        if self._is_soft_backchannel(txt):
            self._clear_pending()
            return "ignore"

        if len(txt.split()) > 3:
            self._clear_pending()
            stop_tts_immediate()
            self.state = self.SILENT
            handle_user_text(text)
            return "interrupt"

        if self._is_pending_expired():
            self._clear_pending()
            return "ignore"
        return "pass"

    def _is_pending_expired(self) -> bool:
        if self.state != self.PENDING or self._pending_started_at is None:
            return False
        return (time.monotonic() - self._pending_started_at) * 1000.0 >= self._pending_timeout_ms

    def _clear_pending(self) -> None:
        if self.state == self.PENDING:
            self.state = self.SPEAKING
        self._pending_started_at = None

    def _contains_interrupt(self, txt: str) -> bool:
        if "stop" in txt and ("don't stop" in txt or "do not stop" in txt):
            return False
        return any(kw in txt for kw in self._interrupt)

    def _is_soft_backchannel(self, txt: str) -> bool:
        toks = [t for t in txt.split() if t]
        if not toks:
            return True
        if len(toks) > 3:
            return False
        return all(self._near_ignore(t) for t in toks)

    def _near_ignore(self, tok: str) -> bool:
        tok = tok.lower()
        if tok in self._ignore:
            return True
        if self._fuzzy <= 0:
            return False
        return any(self._levenshtein(tok, ig) <= self._fuzzy for ig in self._ignore)

    @staticmethod
    def _levenshtein(a: str, b: str) -> int:
        if a == b:
            return 0
        la, lb = len(a), len(b)
        dp = list(range(lb + 1))
        for i, ca in enumerate(a, 1):
            prev = dp[0]
            dp[0] = i
            for j, cb in enumerate(b, 1):
                cur = dp[j]
                dp[j] = min(
                    dp[j] + 1,
                    dp[j - 1] + 1,
                    prev + (0 if ca == cb else 1),
                )
                prev = cur
        return dp[lb]
