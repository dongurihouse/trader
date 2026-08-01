"""Scriptable clock test fake."""

from __future__ import annotations

from datetime import datetime, timedelta


class FakeClock:
    """A non-live clock that advances immediately when scripted."""

    def __init__(self, start: datetime) -> None:
        self._current = start

    @property
    def live(self) -> bool:
        return False

    def now(self) -> datetime:
        return self._current

    def sleep_until(self, when: datetime) -> None:
        if when < self._current:
            raise ValueError("FakeClock cannot move backwards")
        self._current = when

    def advance(self, delta: timedelta) -> None:
        self.sleep_until(self.now() + delta)


__all__ = ["FakeClock"]
