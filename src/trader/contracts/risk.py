"""Risk checking and position-sizing boundary."""

from __future__ import annotations

from typing import Protocol

from .intents import Intent
from .market import MarketData
from .orders import OrderTicket, PortfolioState, Rejection


class RiskEngine(Protocol):
    def check_and_size(
        self, intent: Intent, portfolio: PortfolioState, data: MarketData
    ) -> OrderTicket | Rejection: ...


__all__ = ["RiskEngine"]
