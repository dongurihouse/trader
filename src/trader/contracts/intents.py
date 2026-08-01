"""Strategy intent records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .types import Side


@dataclass(frozen=True)
class Intent:
    algo_id: str
    ts: datetime
    action: Literal["open", "close"]
    side: Side | None
    signal_symbol: str
    instrument: str
    entry: Literal["market_next_open"]
    stop: float | None
    target: float | None
    confidence: float | None
    reason: str
    meta: dict


__all__ = ["Intent"]
