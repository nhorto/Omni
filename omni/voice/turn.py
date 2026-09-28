"""End-of-turn detection: when has the speaker finished?

`VoiceLoop._capture` calls `reset()` when a capture starts and `update()` for every 32 ms
chunk once speech has been heard, and ends the utterance when it returns True. The hotkey,
the 30 s cap, and the no-speech timeout are handled by the loop, not here.
`SmartTurn` (smart_turn.py) asks a model at each short pause instead of waiting out a fixed
silence; it exposes `ready` once the model has loaded.
"""

from __future__ import annotations


class SilenceTurn:
    """The turn ends after a fixed stretch of trailing silence (`end_silence`)."""

    ready = True

    def __init__(self, settings):
        self.end_silence = settings.end_silence

    def reset(self, purpose: str = "request") -> None:
        """`purpose` is "request" or "answer" (a reply to a question or approval)."""

    def update(self, audio: bytearray, prob: float, silence: float) -> bool:
        """`audio` is the whole capture so far (16 kHz mono s16, pre-roll included, this chunk last);
        `prob` is Silero's speech probability for this chunk; `silence` is seconds since the last voiced chunk."""
        return silence >= self.end_silence


def make_turn_detector(settings):
    if settings.turn_detector == "smart":
        from .smart_turn import SmartTurn  # noqa: PLC0415 (only loads the model when chosen)
        return SmartTurn(settings)
    return SilenceTurn(settings)
