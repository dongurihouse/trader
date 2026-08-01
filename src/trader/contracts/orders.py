"""Order, fill, rejection, position, and portfolio records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .intents import Intent
from .types import Side


@dataclass(frozen=True)
class OrderTicket:
    ticket_id: str
    algo_id: str
    intent_ts: datetime
    instrument: str
    side: Side
    shares: int
    entry: Literal["market_next_open"]
    stop: float | None
    target: float | None
    risk: dict
    created_ts: datetime


@dataclass(frozen=True)
class Fill:
    ticket_id: str
    ts: datetime
    price: float
    shares: int
    kind: Literal["entry", "stop", "target", "reversal", "eod"]
    book: Literal["real", "shadow"]


@dataclass(frozen=True)
class Rejection:
    intent: Intent
    rule: str
    detail: str


@dataclass
class PositionState:
    instrument: str
    side: Side
    shares: int
    entry_price: float
    entry_ts: datetime
    stop: float | None
    target: float | None
    algo_id: str


@dataclass
class PortfolioState:
    cash: float
    equity: float
    positions: list[PositionState]
    entries_today: int
    realized_r_today: float
    muted_until: datetime | None


__all__ = [
    "Fill",
    "OrderTicket",
    "PortfolioState",
    "PositionState",
    "Rejection",
]
