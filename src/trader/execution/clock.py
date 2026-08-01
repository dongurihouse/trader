"""Production clocks for backtest and live execution."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import time


class BacktestClock:
    """A synthetic clock that advances instantly during backtests.

    ``sleep_until`` jumps ``now`` forward without a real delay because a
    backtest replays stored bars as fast as they can be read.
    """

    def __init__(self, start: datetime) -> None:
        self._current = start

    @property
    def live(self) -> bool:
        return False

    def now(self) -> datetime:
        return self._current

    def sleep_until(self, when: datetime) -> None:
        if when < self._current:
            raise ValueError("BacktestClock cannot move backwards")
        self._current = when

    def advance(self, delta: timedelta) -> None:
        self.sleep_until(self.now() + delta)


class LiveClock:
    """A wall clock with injectable time and sleep functions for live sessions."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._sleep = sleep
        self._now_fn = now_fn

    @property
    def live(self) -> bool:
        return True

    def now(self) -> datetime:
        return self._now_fn()

    def sleep_until(self, when: datetime) -> None:
        delta = (when - self.now()).total_seconds()
        if delta > 0:
            self._sleep(delta)


__all__ = ["BacktestClock", "LiveClock"]
