"""Broker execution boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .market import MarketData
from .orders import Fill, OrderTicket


class Broker(Protocol):
    def submit(self, ticket: OrderTicket) -> None: ...

    def on_bar(self, asof: datetime, data: MarketData) -> list[Fill]: ...

    def cancel_open(self, reason: str) -> None: ...


__all__ = ["Broker"]
