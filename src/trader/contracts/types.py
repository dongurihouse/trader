"""Core value types used by trader contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Mode = Literal["backtest", "paper", "live"]
Side = Literal["long", "short"]
AlgoStatus = Literal["emitting", "probe", "disabled"]


@dataclass(frozen=True)
class Bar:
    """A complete one-minute market bar identified by its open time."""

    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


__all__ = ["AlgoStatus", "Bar", "Mode", "Side"]
