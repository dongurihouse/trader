"""Clock boundary for live and simulated execution."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    @property
    def live(self) -> bool: ...

    def now(self) -> datetime: ...

    def sleep_until(self, when: datetime) -> None: ...


__all__ = ["Clock"]
